# routes/quotient.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from routes.decorators import login_required, role_required
import valkey
import json
import os
from datetime import datetime, timezone, timedelta
from utils.valkey_client import get_valkey_client
from redbeat import RedBeatSchedulerEntry
from celery.schedules import schedule as celery_schedule
from celery_app import celery
from models import db, Team, Score, extAccounts, AuditLog
import requests

# Create the blueprint
quotient_bp = Blueprint('quotient', __name__, url_prefix='/quotient')

# RedBeat Scheduler Entry Key
REDBEAT_KEY = 'quotient-sync'
TASK_NAME = 'tasks.quotient.sync_all_engines'

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
            current_app.logger.error("Failed to parse quotient:engines from Valkey.")
    
    return []

def parse_valkey_blob(raw):
    """Parse a Valkey-stored blob."""
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and 'data' in parsed:
        inner = json.loads(parsed['data'])
        synced_at = parsed.get('synced_at', 'Unknown')
        return inner, synced_at
    if isinstance(parsed, dict):
        return parsed, 'Unknown'
    raise ValueError("Unexpected Valkey blob structure")

def fetch_quotient_data():
    """Fetch and parse Quotient data from Valkey (STRICTLY from Primary Engine)."""
    try:
        vk = get_valkey_client()
        vk.ping()
    except Exception as e:
        return None, f"Failed to connect to Valkey: {str(e)}"

    engines = get_engine_config()
    if not engines:
        return None, "No engines configured."

    # 1. Identify the Primary Engine
    primary_engine = next((e for e in engines if e.get('is_primary')), None)
    
    if not primary_engine:
        # Fallback: If no primary is marked, use the first one (legacy behavior)
        primary_engine = engines[0]
        current_app.logger.warning("No primary engine marked. Using first engine as fallback.")

    primary_id = primary_engine['id']
    current_app.logger.info(f"Fetching data for Primary Engine ID: {primary_id}")

    # 2. Fetch Primary Engine Data ONLY
    # We look for the engine-specific keys: quotient:engine{id}:status
    engine_raw = vk.get(f"quotient:engine{primary_id}:status")
    
    if not engine_raw:
        # Fallback to legacy key if the new namespaced key doesn't exist yet
        engine_raw = vk.get("quotient:status")
    
    if not engine_raw:
        return None, "No quotient status data found for Primary Engine. Has the sync run yet?"

    try:
        engine, engine_synced_at = parse_valkey_blob(engine_raw)
    except Exception as e:
        return None, f"Failed to parse quotient status: {str(e)}"

    # 3. Fetch Teams (Primary Only)
    teams_raw = vk.get(f"quotient:engine{primary_id}:teams")
    if not teams_raw:
        teams_raw = vk.get("quotient:teams")
        
    teams_list = []
    teams_synced_at = None
    if teams_raw:
        try:
            teams_data, teams_synced_at = parse_valkey_blob(teams_raw)
            if isinstance(teams_data, list):
                teams_list = teams_data
            elif isinstance(teams_data, dict):
                teams_list = teams_data.get('teams') or teams_data.get('Teams') or []
        except Exception as e:
            current_app.logger.warning(f"Failed to parse teams: {str(e)}")

    team_lookup = {}
    for team in teams_list:
        if not isinstance(team, dict): continue
        tid = team.get('ID') or team.get('id')
        tname = team.get('Name') or team.get('name') or f"Team {tid}"
        if tid is not None:
            team_lookup[tid] = tname

    # 4. Fetch Scores (Primary Only)
    scores_raw = vk.get(f"quotient:engine{primary_id}:scores")
    if not scores_raw:
        scores_raw = vk.get("quotient:scores")

    scores_list = []
    scores_synced_at = None
    if scores_raw:
        try:
            scores_data, scores_synced_at = parse_valkey_blob(scores_raw)
            if isinstance(scores_data, list):
                scores_list = scores_data
            elif isinstance(scores_data, dict):
                scores_list = scores_data.get('scores', scores_data.get('Scores', []))
        except Exception as e:
            current_app.logger.warning(f"Failed to parse scores: {str(e)}")

    # 5. Build Team Table (Primary Only)
    teams_table = []
    has_scores_data = bool(scores_list)

    if has_scores_data:
        for entry in scores_list:
            if not isinstance(entry, dict): continue
            tid = entry.get('team_id')
            tname = entry.get('team_name') or team_lookup.get(tid, f"Team {tid}")
            gross = entry.get('gross_points', 0)
            sla_viol = entry.get('total_sla_violations', 0)
            sla_pen = entry.get('total_sla_penalty', 0)
            total = entry.get('total_points', 0)
            if sla_pen > 0: sla_pen = -abs(sla_pen)

            services = []
            for svc in (entry.get('services') or []):
                if not isinstance(svc, dict): continue
                services.append({
                    'service_name': svc.get('service_name', 'Unknown'),
                    'service_points': svc.get('service_points', 0),
                    'sla_violations': svc.get('sla_violations', 0),
                    'sla_penalty': svc.get('sla_penalty', 0),
                })

            teams_table.append({
                'team_id': tid,
                'team_name': tname,
                'gross_points': gross,
                'sla_violations': sla_viol,
                'sla_penalty': sla_pen,
                'total_points': total,
                'services': services,
            })
    else:
        # Fallback to engine checks if no scores blob
        check_agg = {}
        last_round = engine.get('last_round') or {}
        checks = last_round.get('Checks') or []
        for check in checks:
            if not isinstance(check, dict): continue
            tid = check.get('TeamID')
            if tid is None: continue
            if tid not in check_agg:
                check_agg[tid] = {'checks_total': 0, 'checks_passed': 0, 'checks_failed': 0, 'points_earned': 0, 'points_possible': 0}
            agg = check_agg[tid]
            agg['checks_total'] += 1
            agg['points_possible'] += check.get('Points', 0)
            if check.get('Result', False):
                agg['checks_passed'] += 1
                agg['points_earned'] += check.get('Points', 0)
            else:
                agg['checks_failed'] += 1

        for tid in sorted(team_lookup.keys()):
            name = team_lookup[tid]
            ca = check_agg.get(tid, {})
            teams_table.append({
                'team_id': tid,
                'team_name': name,
                'gross_points': ca.get('points_earned', 0),
                'sla_violations': 0,
                'sla_penalty': 0,
                'total_points': ca.get('points_earned', 0),
                'checks_total': ca.get('checks_total', 0),
                'checks_passed': ca.get('checks_passed', 0),
                'checks_failed': ca.get('checks_failed', 0),
                'services': [],
            })

    teams_table.sort(key=lambda x: x['total_points'], reverse=True)

    synced_at = engine_synced_at
    for ts in [teams_synced_at, scores_synced_at]:
        if ts and ts != 'Unknown': synced_at = ts

    last_round = engine.get('last_round') or {}

    # Return data strictly for the Primary Engine
    return {
        'engine': engine,
        'synced_at': synced_at,
        'last_round_id': last_round.get('ID'),
        'last_round_time': last_round.get('StartTime'),
        'teams_table': teams_table,
        'has_scores_data': has_scores_data,
        'has_teams_api_data': bool(team_lookup),
        'engines': engines # Pass the full list for the Engine Management table
    }, None

def _perform_score_sync(username):
    """Core logic for syncing Quotient scores into the Vantage database."""
    engines = get_engine_config()
    if not engines:
        return {'success': False, 'error': 'No engines configured.'}

    total_synced = 0
    total_skipped = 0
    total_deleted = 0

    for engine in engines:
        engine_id = engine['id']
        scorer_name = f"engine_{engine_id}"
        
        # 1. Fetch scores from Valkey for this engine
        vk = get_valkey_client()
        scores_raw = vk.get(f"quotient:engine{engine_id}:scores")
            
        if not scores_raw:
            current_app.logger.warning(f"No score data found for Engine {engine_id}. Skipping.")
            continue

        try:
            scores_data, _ = parse_valkey_blob(scores_raw)
            if isinstance(scores_data, list):
                scores_list = scores_data
            elif isinstance(scores_data, dict):
                scores_list = scores_data.get('scores') or scores_data.get('Scores') or []
                if not isinstance(scores_list, list): scores_list = []
            else:
                return {'success': False, 'error': f'Unexpected score data format for Engine {engine_id}.'}
        except Exception as e:
            current_app.logger.error(f"Failed to fetch scores for Engine {engine_id}: {str(e)}")
            continue

        if not scores_list:
            continue

        # 2. Delete existing blue scores for THIS engine
        try:
            deleted_count = Score.query.filter_by(category='blue', scorer_username=scorer_name).delete()
            total_deleted += deleted_count
        except Exception as e:
            current_app.logger.error(f"Failed to delete old scores for Engine {engine_id}: {e}")
            continue

        # 3. Insert new scores
        synced_count = 0
        skipped_count = 0
        for entry in scores_list:
            quotient_team_id = entry.get('team_id')
            if quotient_team_id is None: continue

            team = Team.query.filter_by(team_number=int(quotient_team_id)).first()
            if not team:
                skipped_count += 1
                continue

            new_score = Score(
                team_id=team.id,
                scorer_username=username,
                category='blue',
                points=entry.get('total_points', 0),
                description=f"QUOTIENT_SYNC_ENGINE_{engine_id}",
                reference_id=f"QUOTIENT_SYNC_ENGINE_{engine_id}",
                is_approved=True,
                score_metadata={'engine_id': engine_id}
            )
            db.session.add(new_score)
            synced_count += 1

        total_synced += synced_count
        total_skipped += skipped_count

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {'success': False, 'error': f'Sync failed: {str(e)}'}

    # Audit Log
    audit_entry = AuditLog(
        username=username,
        action='sync_quotient_scores',
        entity_type='score',
        entity_id=None,
        old_value={'deleted_blue_scores': total_deleted},
        new_value={
            'synced_scores': total_synced,
            'skipped_teams': total_skipped,
            'engines_processed': len(engines)
        }
    )
    db.session.add(audit_entry)
    db.session.commit()

    return {
        'success': True,
        'synced_count': total_synced,
        'skipped_count': total_skipped,
        'deleted_count': total_deleted
    }

def get_celery_task_status():
    """Get global task status (aggregated or last run)."""
    try:
        entry = RedBeatSchedulerEntry.from_key(f'redbeat:{REDBEAT_KEY}', app=celery)
        enabled = getattr(entry, 'enabled', True)
        schedule_seconds = 300
        if hasattr(entry, 'schedule') and hasattr(entry.schedule, 'run_every'):
            schedule_seconds = int(entry.schedule.run_every.total_seconds())
    except (KeyError, TypeError, AttributeError):
        enabled = False
        schedule_seconds = 300
    
    # Get last run info from Valkey (Global)
    last_run_info = {}
    try:
        vk = get_valkey_client(db=0)
        run_data = vk.get("quotient:celery:status")
        if run_data:
            last_run_info = json.loads(run_data)
    except Exception as e:
        current_app.logger.warning(f"Failed to read last run info: {e}")
    
    return {
        'enabled': enabled,
        'last_run_at': last_run_info.get('last_run_at'),
        'last_run_success': last_run_info.get('last_run_success'),
        'last_run_error': last_run_info.get('last_run_error'),
        'schedule_seconds': schedule_seconds
    }

def update_celery_task_schedule(enabled, schedule_seconds):
    try:
        if enabled:
            entry = RedBeatSchedulerEntry(
                name=REDBEAT_KEY,
                task=TASK_NAME,
                schedule=celery_schedule(run_every=timedelta(seconds=schedule_seconds)),
                app=celery,
            )
            entry.save()
        else:
            try:
                entry = RedBeatSchedulerEntry.from_key(f'redbeat:{REDBEAT_KEY}', app=celery)
                entry.delete()
            except (KeyError, TypeError):
                pass
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to update RedBeat schedule: {str(e)}")
        return False

def update_last_run_status(success, error_msg=None):
    vk = get_valkey_client(db=0)
    status_data = {
        'last_run_at': datetime.now(timezone.utc).isoformat(),
        'last_run_success': success,
        'last_run_error': error_msg
    }
    try:
        vk.set("quotient:celery:status", json.dumps(status_data))
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to update last run status: {e}")
        return False

# --- ROUTES ---

@quotient_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def quotient_dashboard():
    """Quotient configuration and status page"""
    data, error = fetch_quotient_data()

    celery_config = get_celery_task_status()

    # --- ALWAYS fetch engine data, regardless of sync status ---
    engines = get_engine_config()

    engine_statuses = []
    try:
        vk = get_valkey_client(db=0)
        for eng in engines:
            status_key = f"quotient:engine{eng['id']}:celery_status"
            raw = vk.get(status_key)
            status = json.loads(raw) if raw else {
                'last_run_at': 'Never',
                'last_run_success': None,
                'last_run_error': None
            }
            eng_status_key = f"quotient:engine{eng['id']}:status"
            eng_status_raw = vk.get(eng_status_key)
            if eng_status_raw:
                status_check = json.loads(json.loads(eng_status_raw).get('data'))
                eng_status = {
                    'running': status_check.get('running'),
                    'competition_started': status_check.get('competition_started')
                }
            else:
                eng_status = {
                    'running': False,
                    'competition_started': False
                }
            engine_statuses.append({
                'id': eng['id'],
                'name': eng['name'],
                'domain': eng['domain'],
                'is_primary': eng.get('is_primary', False),
                'is_active': eng.get('is_active', True),
                'status': status,
                'eng_status': eng_status
            })
    except Exception as e:
        current_app.logger.error(f"Failed to fetch engine statuses: {e}")

    # --- SINGLE return, always passing engine data ---
    return render_template('quotient.html',
                           quotient_data=data,
                           error=error,
                           celery_config=celery_config,
                           engines=engines,
                           engine_statuses=engine_statuses,
                           is_config_page=True)

# --- ENGINE MANAGEMENT ROUTES ---

@quotient_bp.route('/engines', methods=['GET'])
@login_required
@role_required('admin')
def list_engines():
    """Get list of configured engines"""
    engines = get_engine_config()
    # Mask passwords in response
    safe_engines = []
    for e in engines:
        safe_e = e.copy()
        safe_e['password'] = '***' if e.get('password') else None
        safe_engines.append(safe_e)
    return jsonify(safe_engines)

@quotient_bp.route('/engines', methods=['POST'])
@login_required
@role_required('admin')
def add_engine():
    """Add a new engine configuration"""
    data = request.json
    name = data.get('name')
    protocol = data.get('protocol', 'https')
    domain = data.get('domain')
    user = data.get('user')
    password = data.get('password')
    
    if not all([name, domain, user, password]):
        return jsonify({'error': 'Missing required fields'}), 400

    vk = get_valkey_client()
    raw = vk.get("quotient:engines")
    engines = json.loads(raw) if raw else []
    
    new_id = max([e.get('id', 0) for e in engines], default=0) + 1
    
    # If this is the first engine, mark as primary
    is_primary = len(engines) == 0
    
    new_engine = {
        'id': new_id,
        'name': name,
        'protocol': protocol,
        'domain': domain,
        'user': user,
        'password': password,
        'is_primary': is_primary,
        'is_active': True
    }
    
    engines.append(new_engine)
    vk.set("quotient:engines", json.dumps(engines))
    
    return jsonify({'success': True, 'engine': new_engine})

@quotient_bp.route('/engines/<int:engine_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_engine(engine_id):
    """Update engine config"""
    data = request.json
    vk = get_valkey_client()
    raw = vk.get("quotient:engines")
    engines = json.loads(raw) if raw else []
    
    engine = next((e for e in engines if e['id'] == engine_id), None)
    if not engine:
        return jsonify({'error': 'Engine not found'}), 404
        
    engine['name'] = data.get('name', engine['name'])
    engine['protocol'] = data.get('protocol', engine['protocol'])
    engine['domain'] = data.get('domain', engine['domain'])
    engine['user'] = data.get('user', engine['user'])
    
    if 'password' in data and data['password']:
        engine['password'] = data['password']
        
    vk.set("quotient:engines", json.dumps(engines))
    return jsonify({'success': True})

@quotient_bp.route('/engines/<int:engine_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_engine(engine_id):
    """Delete engine and clean up orphaned scores"""
    vk = get_valkey_client()
    raw = vk.get("quotient:engines")
    engines = json.loads(raw) if raw else []
    
    engine = next((e for e in engines if e['id'] == engine_id), None)
    if not engine:
        return jsonify({'error': 'Engine not found'}), 404
        
    engines = [e for e in engines if e['id'] != engine_id]
    vk.set("quotient:engines", json.dumps(engines))
    
    # Cleanup orphaned scores
    scorer_name = f"engine_{engine_id}"
    deleted_count = Score.query.filter_by(category='blue', scorer_username=scorer_name).delete()
    db.session.commit()
    
    # Clear Valkey cache
    vk.delete(f"quotient:engine{engine_id}:status")
    vk.delete(f"quotient:engine{engine_id}:teams")
    vk.delete(f"quotient:engine{engine_id}:scores")
    vk.delete(f"quotient:engine{engine_id}:celery_status")
    vk.delete(f"quotient:engine{engine_id}:auth_session")
    
    return jsonify({'success': True, 'deleted_scores': deleted_count})

@quotient_bp.route('/engines/test', methods=['POST'])
@login_required
@role_required('admin')
def test_connection():
    """Test connection to a specific engine without saving"""
    data = request.json
    protocol = data.get('protocol', 'https')
    domain = data.get('domain')
    user = data.get('user')
    password = data.get('password')
    
    if not all([domain, user, password]):
        return jsonify({'error': 'Missing credentials'}), 400

    try:
        session = requests.Session()
        login_payload = {"username": user, "password": password}
        resp = session.post(f"{protocol}://{domain}/api/login", json=login_payload, timeout=5)
        
        if resp.status_code == 200:
            engine_resp = session.get(f"{protocol}://{domain}/api/engine", timeout=5)
            if engine_resp.status_code == 200:
                return jsonify({'success': True, 'message': 'Connection successful'})
            else:
                return jsonify({'success': False, 'message': f'Login OK, but engine check failed: {engine_resp.status_code}'})
        else:
            return jsonify({'success': False, 'message': f'Login failed: {resp.status_code}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# --- CONTROL ROUTES ---

@quotient_bp.route('/control/start-engine/<int:engine_id>', methods=['POST'])
@login_required
@role_required('admin')
def start_engine(engine_id):
    """Start a specific engine"""
    import tasks.quotient
    task = celery.tasks['tasks.quotient.start_engine'].delay(engine_id)
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/stop-engine/<int:engine_id>', methods=['POST'])
@login_required
@role_required('admin')
def stop_engine(engine_id):
    """Stop a specific engine"""
    import tasks.quotient
    task = celery.tasks['tasks.quotient.stop_engine'].delay(engine_id)
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/start-competition/<int:engine_id>', methods=['POST'])
@login_required
@role_required('admin')
def start_competition(engine_id):
    """Start competition on a specific engine"""
    import tasks.quotient
    task = celery.tasks['tasks.quotient.start_competition'].delay(engine_id)
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/stop-competition/<int:engine_id>', methods=['POST'])
@login_required
@role_required('admin')
def stop_competition(engine_id):
    """Stop competition on a specific engine"""
    import tasks.quotient
    task = celery.tasks['tasks.quotient.stop_competition'].delay(engine_id)
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/start-all-engines', methods=['POST'])
@login_required
@role_required('admin')
def start_all_engines():
    import tasks.quotient
    task = celery.tasks['tasks.quotient.start_all_engines'].delay()
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/stop-all-engines', methods=['POST'])
@login_required
@role_required('admin')
def stop_all_engines():
    import tasks.quotient
    task = celery.tasks['tasks.quotient.stop_all_engines'].delay()
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/start-all-competitions', methods=['POST'])
@login_required
@role_required('admin')
def start_all_competitions():
    import tasks.quotient
    task = celery.tasks['tasks.quotient.start_all_competitions'].delay()
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/stop-all-competitions', methods=['POST'])
@login_required
@role_required('admin')
def stop_all_competitions():
    import tasks.quotient
    task = celery.tasks['tasks.quotient.stop_all_competitions'].delay()
    return jsonify({'success': True, 'task_id': task.id})

# --- EXISTING ROUTES (Updated) ---

@quotient_bp.route('/import-quotient-teams', methods=['POST'])
@login_required
@role_required('admin')
def import_quotient_teams():
    """Imports teams from Quotient (via Valkey cache) into the Vantage database."""
    user = session.get('user_info', {})
    engines = get_engine_config()
    
    # Only import from primary engine
    primary_engine = next((e for e in engines if e.get('is_primary')), engines[0] if engines else None)
    
    if not primary_engine:
        flash('No primary engine found. Cannot import teams.', 'error')
        return redirect(url_for('quotient.quotient_dashboard'))

    try:
        valkey_client = get_valkey_client()
        teams_raw = valkey_client.get(f"quotient:engine{primary_engine['id']}:teams")
        if not teams_raw:
            teams_raw = valkey_client.get("quotient:teams")
        
        if not teams_raw:
            flash('No Quotient team data found.', 'error')
            return redirect(url_for('quotient.quotient_dashboard'))
            
        parsed = json.loads(teams_raw)
        teams_data = []
        if isinstance(parsed, dict) and 'data' in parsed:
            inner = json.loads(parsed['data'])
            if isinstance(inner, list): teams_data = inner
            elif isinstance(inner, dict): teams_data = inner.get('teams') or inner.get('Teams') or []
        elif isinstance(parsed, list):
            teams_data = parsed
            
        if not teams_data:
            flash('No team data found in the Quotient payload.', 'error')
            return redirect(url_for('quotient.quotient_dashboard'))
            
    except Exception as e:
        current_app.logger.error(f"Failed to fetch Quotient teams: {str(e)}")
        flash('Failed to retrieve Quotient data.', 'error')
        return redirect(url_for('quotient.quotient_dashboard'))

    confirm = request.form.get('confirm_import')
    if confirm != 'yes':
        flash('Confirmation required to proceed.', 'error')
        return redirect(url_for('quotient.quotient_dashboard'))

    try:
        deleted_teams_count = Team.query.count()
        deleted_scores_count = Score.query.count()
        deleted_ext_accounts_count = extAccounts.query.count()
        
        Score.query.delete()
        extAccounts.query.delete()
        Team.query.delete()
        db.session.commit()
        
        imported_count = 0
        for t_data in teams_data:
            tid = t_data.get('ID') or t_data.get('id')
            tname = t_data.get('Name') or t_data.get('name') or f"Team {tid}"
            
            if tid is None: continue
                
            new_team = Team(
                team_number=int(tid),
                team_name=str(tname),
                school_name="",
                is_active=True
            )
            db.session.add(new_team)
            imported_count += 1
        
        db.session.commit()
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='import_quotient_teams',
            entity_type='team',
            entity_id=None,
            old_value={'deleted_teams': deleted_teams_count, 'deleted_scores': deleted_scores_count, 'deleted_ext_accounts': deleted_ext_accounts_count},
            new_value={'imported_teams': imported_count, 'source': 'Quotient API'}
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash(f'Successfully imported {imported_count} teams from Quotient.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Import failed: {str(e)}")
        flash('An error occurred during import. Database changes were rolled back.', 'error')
    
    return redirect(url_for('quotient.quotient_dashboard'))

@quotient_bp.route('/celery-task/configure', methods=['POST'])
@login_required
@role_required('admin')
def configure_celery_task():
    user = session.get('user_info', {})
    enabled = 'task_enabled' in request.form
    schedule_seconds = request.form.get('schedule_seconds', '300')
    
    try:
        schedule_seconds = int(schedule_seconds)
        if schedule_seconds < 30 or schedule_seconds > 3600:
            flash('Schedule must be between 30 seconds and 1 hour.', 'error')
            return redirect(url_for('quotient.quotient_dashboard'))
    except ValueError:
        flash('Invalid schedule value.', 'error')
        return redirect(url_for('quotient.quotient_dashboard'))
    
    current_status = get_celery_task_status()
    old_enabled = current_status['enabled']
    
    success = update_celery_task_schedule(enabled, schedule_seconds)
    
    if not success:
        flash('Failed to update Celery schedule.', 'error')
        return redirect(url_for('quotient.quotient_dashboard'))
    
    try:
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='configure_celery_quotient_task',
            entity_type='celery_task_config',
            entity_id=None,
            old_value={'enabled': old_enabled, 'schedule_seconds': current_status.get('schedule_seconds', 300)},
            new_value={'enabled': enabled, 'schedule_seconds': schedule_seconds}
        )
        db.session.add(audit_entry)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log: {e}")

    if enabled and not old_enabled:
        flash(f'Celery task enabled with {schedule_seconds}s interval.', 'success')
    elif not enabled and old_enabled:
        flash('Celery task disabled.', 'success')
    else:
        flash(f'Celery task configuration updated. Enabled: {enabled}, Interval: {schedule_seconds}s.', 'success')

    # CRITICAL FIX: Added the missing return statement
    return redirect(url_for('quotient.quotient_dashboard'))

@quotient_bp.route('/control/reset-engine/<int:engine_id>', methods=['POST'])
@login_required
@role_required('admin')
def reset_engine(engine_id):
    """Reset a specific engine"""
    import tasks.quotient
    task = celery.tasks['tasks.quotient.reset_engine'].delay(engine_id)
    return jsonify({'success': True, 'task_id': task.id})

@quotient_bp.route('/control/reset-all-engines', methods=['POST'])
@login_required
@role_required('admin')
def reset_all_engines():
    import tasks.quotient
    task = celery.tasks['tasks.quotient.reset_all_engines'].delay()
    return jsonify({'success': True, 'task_id': task.id})