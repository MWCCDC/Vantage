# tasks/quotient.py
import os
import json
import logging
import requests
from datetime import datetime, timezone, UTC
import uuid
from celery_app import celery
import valkey
from utils.valkey_client import get_valkey_client
from routes.quotient import update_last_run_status
from app import app
from models import db, Score, Team

logger = logging.getLogger('quotient-sync')

def get_engine_session_key(engine_id):
    """Generate the Valkey key for a specific engine's session."""
    return f"quotient:engine{engine_id}:auth_session"

def get_engine_config():
    """Retrieve engine list from Valkey."""
    vk = get_valkey_client(db=0)
    raw = vk.get("quotient:engines")

    if raw:
        try:
            engines = json.loads(raw)
            if isinstance(engines, list) and len(engines) > 0:
                return engines
        except json.JSONDecodeError:
            logger.error("Failed to parse quotient:engines from Valkey.")

    return []

def get_cookie_expiry_timestamp(session):
    """Extract the earliest expiry timestamp from all cookies in the session."""
    earliest_expiry = None
    for cookie in session.cookies:
        exp_val = cookie.expires
        if exp_val is not None:
            if isinstance(exp_val, (int, float)):
                exp_dt = datetime.fromtimestamp(exp_val, tz=timezone.utc)
            elif isinstance(exp_val, str):
                try:
                    exp_dt = datetime.strptime(exp_val, "%a, %d-%b-%Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            else:
                continue
            
            if earliest_expiry is None or exp_dt < earliest_expiry:
                earliest_expiry = exp_dt
    return earliest_expiry

def serialize_session_with_expiry(session):
    """Serializes session cookies and calculates Valkey TTL based on cookie expiry."""
    cookies_dict = session.cookies.get_dict()
    expiry_dt = get_cookie_expiry_timestamp(session)
    
    if expiry_dt:
        now = datetime.now(timezone.utc)
        ttl_seconds = int((expiry_dt - now).total_seconds())
        if ttl_seconds <= 60:
            logger.warning("Cookie expiry is imminent. Forcing re-login.")
            return None, 0
        logger.info(f"Detected cookie expiry: {expiry_dt}. Setting Valkey TTL to {ttl_seconds}s")
    else:
        ttl_seconds = 3600
        logger.warning("No cookie expiry detected. Using default TTL of 3600s.")

    return json.dumps(cookies_dict), ttl_seconds

def get_cached_engine_session(engine):
    """Retrieve pre-authenticated session for a specific engine from Valkey."""
    vk = get_valkey_client(db=0)
    session_key = get_engine_session_key(engine['id'])
    
    if not vk.exists(session_key):
        return None
        
    cookies_json = vk.get(session_key)
    if not cookies_json:
        return None
    
    try:
        cookies_dict = json.loads(cookies_json)
        session = requests.Session()
        for name, value in cookies_dict.items():
            session.cookies.set(name, value)
        return session
    except Exception as e:
        logger.error(f"Failed to restore session for Engine {engine['id']}: {e}")
        vk.delete(session_key)
        return None

def login_and_cache_engine_session(engine):
    """Perform login for a specific engine and cache cookies with dynamic TTL."""
    logger.info(f"Performing fresh login for Engine {engine['id']}: {engine['name']}...")
    
    session = requests.Session()
    login_payload = {"username": engine['user'], "password": engine['password']}
    login_headers = {"Content-Type": "application/json"}
    
    try:
        url = f"{engine['protocol']}://{engine['domain']}/api/login"
        response = session.post(url, headers=login_headers, json=login_payload, timeout=10)
        
        if response.status_code != 200:
            raise Exception(f"Login failed: {response.status_code} - {response.text[:200]}")
        
        cookies_json, ttl = serialize_session_with_expiry(session)
        if not cookies_json:
            raise Exception("Failed to serialize session cookies.")

        vk = get_valkey_client(db=0)
        session_key = get_engine_session_key(engine['id'])
        vk.setex(session_key, ttl, cookies_json)
        
        logger.info(f"Engine {engine['id']} logged in. Cached session with TTL: {ttl}s")
        return session
    except Exception as e:
        logger.error(f"Engine {engine['id']} login error: {str(e)}")
        raise

def ensure_valid_session(engine, session):
    """Verify session validity; if expired, re-login and return new session."""
    try:
        test_url = f"{engine['protocol']}://{engine['domain']}/api/engine"
        test_req = session.get(test_url, timeout=5)
        
        if test_req.status_code in [401, 403]:
            logger.warning(f"Engine {engine['id']} session expired. Re-authenticating.")
            vk = get_valkey_client(db=0)
            vk.delete(get_engine_session_key(engine['id']))
            return login_and_cache_engine_session(engine)
        return session
    except requests.exceptions.RequestException as e:
        logger.error(f"Engine {engine['id']} network error during ping: {e}")
        raise
    except Exception as e:
        logger.error(f"Engine {engine['id']} unexpected error during ping: {e}")
        raise

# --- SYNC TASKS ---

def sync_single_engine(engine):
    """
    Sync data for a single engine: Teams, Announcements, Injects, Credlists, PCR, Services Uptime, and Scores.
    Saves raw blobs to Valkey for the UI and inserts Scores into the DB.
    """
    engine_id = engine['id']
    scorer_name = f"engine_{engine_id}"
    protocol = engine['protocol']
    domain = engine['domain']
    
    logger.info(f"Starting sync for Engine {engine_id}: {engine['name']}")
    
    # 1. Get/Refresh Session
    session = get_cached_engine_session(engine)
    if not session:
        try:
            session = login_and_cache_engine_session(engine)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # 2. Verify Session (Ping)
    try:        
        test_url = f"{protocol}://{domain}/api/engine"
        test_req = session.get(test_url, timeout=5)
        if test_req.status_code in [401, 403]:
            logger.warning(f"Engine {engine_id} session expired. Re-authenticating.")
            vk = get_valkey_client(db=0)
            vk.delete(get_engine_session_key(engine_id))
            session = login_and_cache_engine_session(engine)
    except Exception as e:
        logger.error(f"Engine {engine_id} ping failed: {e}")
        return {'success': False, 'error': str(e)}
    
    try:
        status_url = f"{protocol}://{domain}/api/engine"
        status_resp = session.get(status_url, timeout=10)
        
        if status_resp.status_code == 200:
            engine_data = status_resp.json()
            
            logger.info(f"Engine {engine_id}: API reports competition_started = {engine_data.get('competition_started')}")
            logger.info(f"Engine {engine_id}: API reports running = {engine_data.get('running')}")
            
            blob_data = {
                "data": json.dumps(engine_data),
                "synced_at": datetime.now(timezone.utc).isoformat()
            }
            
            vk = get_valkey_client(db=0)
            status_key = f"quotient:engine{engine_id}:status"
            vk.setex(status_key, 3600, json.dumps(blob_data))
            
            logger.info(f"Engine {engine_id}: Updated status blob in Valkey.")
        else:
            logger.warning(f"Engine {engine_id}: Failed to fetch status blob (HTTP {status_resp.status_code}).")
    except Exception as e:
        logger.error(f"Engine {engine_id}: Error fetching/updating status blob: {e}")

    # --- HELPER: Save Blob to Valkey ---
    def save_blob_to_valkey(key_suffix, data):
        vk = get_valkey_client(db=0)
        blob = {
            "data": json.dumps(data),
            "synced_at": datetime.now(timezone.utc).isoformat()
        }
        key = f"quotient:engine{engine_id}:{key_suffix}"
        try:
            vk.setex(key, 3600, json.dumps(blob))
            logger.debug(f"Engine {engine_id}: Saved {key_suffix} to Valkey.")
        except Exception as e:
            logger.error(f"Engine {engine_id}: Failed to save {key_suffix} to Valkey: {e}")

    # --- 3. Fetch & Save TEAMS ---
    teams_data = []
    try:
        url = f"{protocol}://{domain}/api/teams"
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            teams_data = resp.json()
            save_blob_to_valkey("teams", teams_data)
            logger.info(f"Engine {engine_id}: Fetched {len(teams_data) if isinstance(teams_data, list) else 'unknown'} teams.")
        else:
            logger.warning(f"Engine {engine_id}: Failed to fetch teams (HTTP {resp.status_code}).")
    except Exception as e:
        logger.error(f"Engine {engine_id}: Error fetching teams: {e}")

    # --- 4. Fetch & Save ANNOUNCEMENTS ---
    try:
        url = f"{protocol}://{domain}/api/announcements"
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            save_blob_to_valkey("announcements", resp.json())
            logger.info(f"Engine {engine_id}: Fetched announcements.")
        else:
            logger.warning(f"Engine {engine_id}: Failed to fetch announcements (HTTP {resp.status_code}).")
    except Exception as e:
        logger.error(f"Engine {engine_id}: Error fetching announcements: {e}")

    # --- 5. Fetch & Save INJECTS ---
    try:
        url = f"{protocol}://{domain}/api/injects"
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            save_blob_to_valkey("injects", resp.json())
            logger.info(f"Engine {engine_id}: Fetched injects.")
        else:
            logger.warning(f"Engine {engine_id}: Failed to fetch injects (HTTP {resp.status_code}).")
    except Exception as e:
        logger.error(f"Engine {engine_id}: Error fetching injects: {e}")

    # --- 6. Fetch & Save CREDLISTS (Metadata) ---
    credlists_meta = []
    try:
        url = f"{protocol}://{domain}/api/credlists"
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            credlists_meta = resp.json()
            save_blob_to_valkey("credlists", credlists_meta)
            logger.info(f"Engine {engine_id}: Fetched {len(credlists_meta)} credlist definitions.")
        else:
            logger.warning(f"Engine {engine_id}: Failed to fetch credlists (HTTP {resp.status_code}).")
    except Exception as e:
        logger.error(f"Engine {engine_id}: Error fetching credlists: {e}")

    # --- 7. Fetch & Save LATEST PCR (Actual Credentials) ---
    latest_pcr_data = {}
    
    if teams_data and credlists_meta:
        logger.info(f"Engine {engine_id}: Fetching PCR details for {len(teams_data)} teams and {len(credlists_meta)} credlists...")
        
        if not isinstance(teams_data, list):
            teams_data = teams_data.get('teams', [])
            
        for team in teams_data:
            team_id = team.get('ID') or team.get('id')
            if not team_id:
                continue
            
            team_pcrs = []
            
            for credlist in credlists_meta:
                credlist_name = credlist.get('path')
                if not credlist_name:
                    continue
                
                try:
                    pcr_url = f"{protocol}://{domain}/api/pcrs?team_id={team_id}&credlist={credlist_name}"
                    pcr_resp = session.get(pcr_url, timeout=10)
                    
                    if pcr_resp.status_code == 200:
                        pcr_entries = pcr_resp.json()
                        for entry in pcr_entries:
                            entry['CredlistName'] = credlist_name
                            team_pcrs.append(entry)
                    else:
                        logger.debug(f"Engine {engine_id}: No PCR data for Team {team_id}, Credlist {credlist_name} (HTTP {pcr_resp.status_code})")
                except Exception as pcr_err:
                    logger.error(f"Engine {engine_id}: Error fetching PCR for Team {team_id}, Credlist {credlist_name}: {pcr_err}")
            
            if team_pcrs:
                latest_pcr_data[str(team_id)] = team_pcrs

    if latest_pcr_data:
        save_blob_to_valkey("latestPCR", latest_pcr_data)
        logger.info(f"Engine {engine_id}: Saved PCR data for {len(latest_pcr_data)} teams.")
    else:
        logger.warning(f"Engine {engine_id}: No PCR data collected.")

    # --- 8. Fetch & Save SERVICE UPTIME DATA ---
    logger.info(f"Engine {engine_id}: Fetching service uptime data for all teams...")
    services_uptime_data = {}
    
    if teams_data:
        if not isinstance(teams_data, list):
            teams_data = teams_data.get('teams', [])
        
        for team in teams_data:
            team_num = team.get('ID') or team.get('id')
            if not team_num:
                continue
            
            try:
                services_url = f"{protocol}://{domain}/api/services/{team_num}"
                services_resp = session.get(services_url, timeout=10)
                
                if services_resp.status_code == 200:
                    services_list = services_resp.json()
                    
                    team_services_uptime = {}
                    for service in services_list:
                        service_name = service.get('ServiceName')
                        uptime = service.get('Uptime')
                        if service_name and uptime is not None:
                            team_services_uptime[service_name] = {
                                'uptime': uptime,
                                'sla_count': service.get('SlaCount', 0),
                                'last_10_rounds': service.get('Last10Rounds', [])
                            }
                    
                    services_uptime_data[str(team_num)] = team_services_uptime
                    logger.debug(f"Engine {engine_id}: Fetched uptime for {len(team_services_uptime)} services for Team {team_num}")
                else:
                    logger.warning(f"Engine {engine_id}: Failed to fetch services uptime for Team {team_num} (HTTP {services_resp.status_code}).")
            except Exception as services_err:
                logger.error(f"Engine {engine_id}: Error fetching services uptime for Team {team_num}: {services_err}")
    
    if services_uptime_data:
        save_blob_to_valkey("servicesUptime", services_uptime_data)
        logger.info(f"Engine {engine_id}: Saved service uptime data for {len(services_uptime_data)} teams.")
    else:
        logger.warning(f"Engine {engine_id}: No service uptime data collected.")

    # --- 9. Fetch & Save SCORES (and DB Insert) ---
    try:
        url = f"{protocol}://{domain}/api/engine/export/scores"
        resp = session.get(url, timeout=10)
        
        if resp.status_code != 200:
            raise Exception(f"Score fetch failed: {resp.status_code}")
        
        scores_data = resp.json()
        if isinstance(scores_data, dict):
            scores_list = scores_data.get('scores', scores_data.get('Scores', []))
        else:
            scores_list = scores_data
            
        if not isinstance(scores_list, list):
            scores_list = []

        save_blob_to_valkey("scores", scores_data)

        deleted = Score.query.filter_by(category='blue', scorer_username=scorer_name).delete()
        
        synced_count = 0
        skipped_count = 0
        for entry in scores_list:
            team_num = entry.get('team_id')
            if not team_num: continue
            
            team = Team.query.filter_by(team_number=int(team_num)).first()
            if not team:
                skipped_count += 1
                continue
                
            new_score = Score(
                team_id=team.id,
                scorer_username=scorer_name,
                category='blue',
                points=entry.get('total_points', 0),
                description=f"QUOTIENT_SYNC_ENGINE_{engine_id}",
                reference_id=f"QUOTIENT_SYNC_ENGINE_{engine_id}",
                is_approved=True,
                score_metadata={'engine_id': engine_id}
            )
            db.session.add(new_score)
            synced_count += 1
            
        db.session.commit()
        logger.info(f"Engine {engine_id} sync complete. Synced {synced_count} scores, skipped {skipped_count}, deleted {deleted} old.")
        return {'success': True, 'synced': synced_count, 'skipped': skipped_count, 'deleted': deleted}

    except Exception as e:
        db.session.rollback()
        logger.error(f"Engine {engine_id} score sync failed: {e}")
        return {'success': False, 'error': str(e)}

@celery.task(name='tasks.quotient.sync_all_engines')
def sync_all_quotient_engines():
    """Sync data from ALL configured Quotient engines"""
    with app.app_context():
        engines = get_engine_config()
        if not engines:
            return {'status': 'no_engines'}
            
        results = {}
        overall_success = True
        
        for engine in engines:
            if not engine.get('is_active', True): continue
            res = sync_single_engine(engine)
            results[engine['id']] = res
            if not res['success']: overall_success = False
            
            vk = get_valkey_client(db=0)
            status_key = f"quotient:engine{engine['id']}:celery_status"
            vk.setex(status_key, 3600, json.dumps({
                'last_run_at': datetime.now(timezone.utc).isoformat(),
                'last_run_success': res['success'],
                'last_run_error': res.get('error'),
                'synced_count': res.get('synced', 0)
            }))
        
        update_last_run_status(overall_success, None if overall_success else "One or more engines failed")
        return results

@celery.task(name='tasks.quotient.sync_quotient_data_legacy')
def sync_quotient_data_legacy():
    return sync_all_quotient_engines.delay()

# --- ENGINE CONTROL TASKS ---

def _control_engine_action(engine, action_type, payload):
    """Generic handler for engine control actions (start/stop/pause)."""
    engine_id = engine['id']
    session = get_cached_engine_session(engine)
    
    if not session:
        try:
            session = login_and_cache_engine_session(engine)
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    try:
        session = ensure_valid_session(engine, session)
    except Exception as e:
        return {'success': False, 'error': str(e)}

    try:
        url = f"{engine['protocol']}://{engine['domain']}/api/{action_type}"
        headers = {'Content-Type': 'application/json'}
        resp = session.post(url, headers=headers, json=payload, timeout=10)
        
        if resp.status_code != 200:
            raise Exception(f"Action failed: {resp.status_code} - {resp.text[:200]}")
        
        logger.info(f"Engine {engine_id}: Successfully executed {action_type}")
        return {'success': True, 'action': action_type, 'status_code': resp.status_code}
    except Exception as e:
        logger.error(f"Engine {engine_id}: Failed to execute {action_type}: {e}")
        return {'success': False, 'error': str(e)}

@celery.task(name='tasks.quotient.start_engine')
def start_engine_task(engine_id):
    """Start the scoring engine for a specific engine ID."""
    with app.app_context():
        engines = get_engine_config()
        engine = next((e for e in engines if e['id'] == engine_id), None)
        if not engine:
            return {'success': False, 'error': f"Engine {engine_id} not found"}
        
        return _control_engine_action(engine, 'engine/pause', {"pause": False})

@celery.task(name='tasks.quotient.stop_engine')
def stop_engine_task(engine_id):
    """Stop (pause) the scoring engine for a specific engine ID."""
    with app.app_context():
        engines = get_engine_config()
        engine = next((e for e in engines if e['id'] == engine_id), None)
        if not engine:
            return {'success': False, 'error': f"Engine {engine_id} not found"}
        
        return _control_engine_action(engine, 'engine/pause', {"pause": True})

@celery.task(name='tasks.quotient.start_competition')
def start_competition_task(engine_id):
    """Start the competition for a specific engine ID."""
    with app.app_context():
        engines = get_engine_config()
        engine = next((e for e in engines if e['id'] == engine_id), None)
        if not engine:
            return {'success': False, 'error': f"Engine {engine_id} not found"}
        
        return _control_engine_action(engine, 'competition/start', {"started": True})

@celery.task(name='tasks.quotient.stop_competition')
def stop_competition_task(engine_id):
    """Stop the competition for a specific engine ID."""
    with app.app_context():
        engines = get_engine_config()
        engine = next((e for e in engines if e['id'] == engine_id), None)
        if not engine:
            return {'success': False, 'error': f"Engine {engine_id} not found"}
        
        return _control_engine_action(engine, 'competition/start', {"started": False})

# --- GLOBAL CONTROL TASKS ---

def _global_control(action_func, action_name):
    """Helper to run an action on all active engines."""
    with app.app_context():
        engines = get_engine_config()
        if not engines:
            return {'status': 'no_engines'}
        
        results = {}
        success_count = 0
        
        for engine in engines:
            if not engine.get('is_active', True):
                results[engine['id']] = {'success': False, 'error': 'Engine inactive'}
                continue
            
            res = action_func(engine['id'])
            results[engine['id']] = res
            if res.get('success'):
                success_count += 1
        
        return {
            'action': action_name,
            'total_engines': len(engines),
            'successful': success_count,
            'failed': len(engines) - success_count,
            'details': results
        }

@celery.task(name='tasks.quotient.start_all_engines')
def start_all_engines_task():
    """Start scoring engines for all active configured engines."""
    return _global_control(start_engine_task, 'start_all_engines')

@celery.task(name='tasks.quotient.stop_all_engines')
def stop_all_engines_task():
    """Stop scoring engines for all active configured engines."""
    return _global_control(stop_engine_task, 'stop_all_engines')

@celery.task(name='tasks.quotient.start_all_competitions')
def start_all_competitions_task():
    """Start competitions for all active configured engines."""
    return _global_control(start_competition_task, 'start_all_competitions')

@celery.task(name='tasks.quotient.stop_all_competitions')
def stop_all_competitions_task():
    """Stop competitions for all active configured engines."""
    return _global_control(stop_competition_task, 'stop_all_competitions')

@celery.task(name='tasks.quotient.reset_engine_session')
def reset_engine_session(engine_id):
    """Force a session reset for a specific engine by deleting its cached session."""
    vk = get_valkey_client(db=0)
    session_key = get_engine_session_key(engine_id)
    vk.delete(session_key)
    logger.info(f"Reset session cache for Engine {engine_id}")
    return {'success': True, 'engine_id': engine_id}

## Add announcement
# HTTP POST (multipart form)
# Form Data: title, descript, open-time (in ISO format, UTC time like 2026-04-11T22:59:00.000Z)
# Return HTTP 500, response in JSON, error: "failed to create directory" is okay because it still creates the announcement. submissions/announcements needs to exist first.

## Create inject
# HTTP POST (multipart form)
# Endpoint: /api/injects/create
# Form Data: title, description, open-time, due-time, close-time, files

## Reset scores
# HTTP GET
# https://quotient.ccdc.events/api/engine/reset

# --- ENGINE RESET TASK ---

def _reset_engine(engine):
    """
    Reset a single engine via GET /api/engine/reset.
    Note: This endpoint takes no payload and is invoked with GET.
    """
    engine_id = engine['id']
    session = get_cached_engine_session(engine)

    if not session:
        try:
            session = login_and_cache_engine_session(engine)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    try:
        session = ensure_valid_session(engine, session)
    except Exception as e:
        return {'success': False, 'error': str(e)}

    try:
        url = f"{engine['protocol']}://{engine['domain']}/api/engine/reset"
        resp = session.get(url, timeout=15)

        if resp.status_code != 200:
            raise Exception(f"Reset failed: {resp.status_code} - {resp.text[:200]}")
        
        logger.info(f"Engine {engine_id}: Successfully reset.")
        return {'success': True, 'action': 'engine/reset', 'status_code': resp.status_code}
    except Exception as e:
        logger.error(f"Engine {engine_id}: Failed to reset: {e}")
        return {'success': False, 'error': str(e)}


@celery.task(name='tasks.quotient.reset_engine')
def reset_engine_task(engine_id):
    """Reset a specific engine via /api/engine/reset."""
    with app.app_context():
        engines = get_engine_config()
        engine = next((e for e in engines if e['id'] == engine_id), None)
        if not engine:
            return {'success': False, 'error': f"Engine {engine_id} not found"}

        return _reset_engine(engine)


@celery.task(name='tasks.quotient.reset_all_engines')
def reset_all_engines_task():
    """Reset all active configured engines."""
    return _global_control(reset_engine_task, 'reset_all_engines')