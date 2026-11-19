# routes/dropFlag.py
"""
Drop Flag automation scheduling for Quotient engines.

Allows administrators to schedule competition start (drop) and end times,
with an optional engine reset 1 minute before the drop.

Schedule configuration lives in Valkey:

    vantage:schedule:dropflag
        {
            "enabled": bool,
            "drop_time_utc": "ISO8601 UTC timestamp",
            "end_time_utc": "ISO8601 UTC timestamp",
            "reset_before_drop": bool,
            "selected_engines": [engine_id] or null for all,
            "created_at": "ISO8601 UTC",
            "updated_at": "ISO8601 UTC",
            "last_executed": {
                "reset": "ISO8601 UTC" or null,
                "drop":  "ISO8601 UTC" or null,
                "end":   "ISO8601 UTC" or null
            }
        }

Execution is performed by the recurring watchdog task
`tasks.drop_flag.tick`, registered in RedBeat at app startup via
`tasks.drop_flag.register_drop_flag_tick()`. This module only writes the
schedule blob; the watchdog compares stored timestamps against the clock
and fires due actions. There is therefore no task queue to revoke when a
schedule is adjusted, toggled, or cancelled.

`last_executed` is the completion ledger: the watchdog stamps an action
before executing it so a worker restart cannot double-fire. Routes that
move an action into the future clear that action's stamp so it can fire
again.
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, session, jsonify
)
from routes.decorators import login_required, role_required
from models import db, AuditLog
import json
import logging
from datetime import datetime, timezone, timedelta
from utils.valkey_client import get_valkey_client

try:
    from redbeat import RedBeatSchedulerEntry
    from celery_app import celery
except ImportError:
    RedBeatSchedulerEntry = None
    celery = None

logger = logging.getLogger(__name__)

# Blueprint creation
drop_flag_bp = Blueprint('drop_flag', __name__, url_prefix='/dropFlag')

# --- Constants ---
SCHEDULE_KEY = "vantage:schedule:dropflag"
TICK_ENTRY_NAME = 'dropflag-tick'
TEST_ACTIONS = ('reset', 'drop', 'end')


# --- Datetime helpers ---

def _as_datetime(value):
    """Convert various input formats to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None \
            else value.astimezone(timezone.utc)

    if isinstance(value, str):
        value = value.strip()
        for fmt in (
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S+00:00',
            '%Y-%m-%dT%H:%M:%S.%fZ',
            '%Y-%m-%dT%H:%M:%S.%f+00:00',
            '%Y-%m-%dT%H:%M:%S',
        ):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    raise ValueError(f"Cannot parse datetime from {value!r}")


def _as_iso(dt):
    """Format a datetime as an ISO8601 UTC string."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _now_iso():
    return _as_iso(datetime.now(timezone.utc))


# --- Valkey helpers ---

def _get_schedule():
    """Load the current schedule from Valkey, or None if unset."""
    try:
        raw = get_valkey_client().get(SCHEDULE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"Failed to read drop flag schedule: {e}")
    return None


def _save_schedule(schedule):
    """Persist the schedule to Valkey."""
    try:
        get_valkey_client().set(SCHEDULE_KEY, json.dumps(schedule))
        return True
    except Exception as e:
        logger.error(f"Failed to save drop flag schedule: {e}")
        return False


def _get_engine_config():
    """Retrieve the list of configured Quotient engines from Valkey."""
    try:
        raw = get_valkey_client().get("quotient:engines")
        if raw:
            engines = json.loads(raw)
            if isinstance(engines, list):
                return engines
    except Exception as e:
        logger.error(f"Failed to read quotient engines config: {e}")
    return []


def _parse_engine_selection(raw):
    """
    Parse the comma-separated engine id string from the form.
    Returns a list of ints, or None meaning "all engines".
    """
    if not raw or not raw.strip():
        return None

    ids = []
    for chunk in raw.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            logger.warning(f"Ignoring non-numeric engine id in selection: {chunk!r}")

    return ids or None


def _watchdog_registered():
    """
    Report whether the Drop Flag watchdog tick exists in RedBeat.
    A saved schedule does nothing without it, so surface this loudly.
    """
    if RedBeatSchedulerEntry is None or celery is None:
        return False
    try:
        RedBeatSchedulerEntry.from_key(f'redbeat:{TICK_ENTRY_NAME}', app=celery)
        return True
    except Exception:
        return False


def _warn_if_watchdog_missing():
    """Flash a warning when a schedule is saved but nothing will run it."""
    if not _watchdog_registered():
        flash(
            'Warning: the Drop Flag watchdog task is not registered, so nothing '
            'will execute. Restart the app to register it.',
            'warning'
        )


def _audit(user, action, old_value, new_value):
    """Write an audit log entry, tolerating DB failures."""
    try:
        db.session.add(AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action=action,
            entity_type='schedule',
            entity_id=None,
            old_value=old_value,
            new_value=new_value,
        ))
        db.session.commit()
    except Exception as e:
        logger.error(f"Audit log write failed for {action}: {e}")
        db.session.rollback()


# --- Routes ---

@drop_flag_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def drop_flag_dashboard():
    """Drop Flag configuration and status page."""
    schedule = _get_schedule()
    engines = _get_engine_config()
    engine_options = [{'id': e['id'], 'name': e['name']} for e in engines]

    return render_template(
        'dropFlag.html',
        is_admin_page=True,
        schedule=schedule,
        engine_options=engine_options,
        watchdog_active=_watchdog_registered(),
    )


@drop_flag_bp.route('/save', methods=['POST'])
@login_required
@role_required('admin')
def save_schedule():
    """
    Save a new or updated drop flag schedule.

    Times arrive as UTC ISO strings produced by the browser, so the result
    never depends on the server's local timezone.
    """
    user = session.get('user_info', {})

    drop_time_utc = request.form.get('drop_time_utc')
    end_time_utc = request.form.get('end_time_utc')
    reset_checkbox = 'reset_engines' in request.form
    selected_engines_str = request.form.get('selected_engines', '')
    enabled = 'schedule_enabled' in request.form

    if not drop_time_utc or not end_time_utc:
        flash('Both a drop time and an end time are required.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    try:
        drop_dt = _as_datetime(drop_time_utc)
        end_dt = _as_datetime(end_time_utc)
    except ValueError as e:
        flash(f'Invalid datetime format: {e}', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    now = datetime.now(timezone.utc)

    if end_dt <= drop_dt:
        flash('End time must be after the drop time.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    if drop_dt <= now:
        flash('Drop time must be in the future.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    selected_engines = _parse_engine_selection(selected_engines_str)

    # May legitimately be None on the very first save.
    old_schedule = _get_schedule() or {}

    new_schedule = {
        'enabled': enabled,
        'drop_time_utc': _as_iso(drop_dt),
        'end_time_utc': _as_iso(end_dt),
        'reset_before_drop': reset_checkbox,
        'selected_engines': selected_engines,
        'created_at': old_schedule.get('created_at') or _now_iso(),
        'updated_at': _now_iso(),
        # A full save is a fresh configuration: clear the ledger so every
        # action can fire again at its new time.
        'last_executed': {'reset': None, 'drop': None, 'end': None},
    }

    if not _save_schedule(new_schedule):
        flash('Failed to save schedule. Check server logs.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(user, 'drop_flag_schedule_save', old_schedule or {}, new_schedule)

    flash(
        f'Schedule saved. Competition starts at {_as_iso(drop_dt)} UTC.',
        'success'
    )
    if not enabled:
        flash('The schedule is saved but disabled; enable it to arm the drop.', 'info')
    _warn_if_watchdog_missing()

    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/adjust', methods=['POST'])
@login_required
@role_required('admin')
def adjust_schedule():
    """
    Move an existing schedule's times without changing other settings.

    Intentionally ignores non-time fields (engine selection, reset flag) so
    that the "Adjust Time" action cannot silently reconfigure a live event.
    Unlike save, this permits a drop time in the past so the end time can be
    extended after the competition has already started.
    """
    user = session.get('user_info', {})

    schedule = _get_schedule()
    if not schedule:
        flash('No schedule exists to adjust.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    drop_time_utc = request.form.get('drop_time_utc')
    end_time_utc = request.form.get('end_time_utc')

    if not drop_time_utc or not end_time_utc:
        flash('Both a drop time and an end time are required.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    try:
        drop_dt = _as_datetime(drop_time_utc)
        end_dt = _as_datetime(end_time_utc)
    except ValueError as e:
        flash(f'Invalid datetime format: {e}', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    now = datetime.now(timezone.utc)

    if end_dt <= drop_dt:
        flash('End time must be after the drop time.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    if end_dt <= now:
        flash('End time must be in the future.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    old_times = {
        'drop_time_utc': schedule.get('drop_time_utc'),
        'end_time_utc': schedule.get('end_time_utc'),
    }

    schedule['drop_time_utc'] = _as_iso(drop_dt)
    schedule['end_time_utc'] = _as_iso(end_dt)
    schedule['updated_at'] = _now_iso()

    # Any action now sitting in the future has not happened yet, so clear its
    # ledger stamp — otherwise a rescheduled action would never fire.
    ledger = schedule.setdefault(
        'last_executed', {'reset': None, 'drop': None, 'end': None}
    )
    if drop_dt > now:
        ledger['drop'] = None
        if schedule.get('reset_before_drop'):
            ledger['reset'] = None
    if end_dt > now:
        ledger['end'] = None

    if not _save_schedule(schedule):
        flash('Failed to update schedule.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(
        user, 'drop_flag_schedule_adjust', old_times,
        {'drop_time_utc': _as_iso(drop_dt), 'end_time_utc': _as_iso(end_dt)}
    )

    flash(f'Schedule timing updated. Drop now at {_as_iso(drop_dt)} UTC.', 'success')
    _warn_if_watchdog_missing()

    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/toggle', methods=['POST'])
@login_required
@role_required('admin')
def toggle_schedule():
    """Enable or disable the schedule without changing times."""
    user = session.get('user_info', {})

    schedule = _get_schedule()
    if not schedule:
        flash('No schedule exists to toggle.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    old_enabled = schedule.get('enabled', False)
    new_enabled = request.form.get('enabled', 'false').lower() == 'true'

    if old_enabled == new_enabled:
        flash(f'Schedule is already {"enabled" if new_enabled else "disabled"}.', 'info')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    schedule['enabled'] = new_enabled
    schedule['updated_at'] = _now_iso()

    if not _save_schedule(schedule):
        flash('Failed to update schedule.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(
        user, 'drop_flag_schedule_toggle',
        {'enabled': old_enabled}, {'enabled': new_enabled}
    )

    flash(f'Schedule {"enabled" if new_enabled else "disabled"}.', 'success')
    if new_enabled:
        _warn_if_watchdog_missing()

    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/cancel', methods=['POST'])
@login_required
@role_required('admin')
def cancel_schedule():
    """Completely remove the schedule. The watchdog idles once it's gone."""
    user = session.get('user_info', {})

    schedule = _get_schedule()
    if not schedule:
        flash('No schedule exists to cancel.', 'info')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    try:
        get_valkey_client().delete(SCHEDULE_KEY)
    except Exception as e:
        logger.error(f"Failed to clear schedule from Valkey: {e}")
        flash('Failed to clear the schedule. Check server logs.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(user, 'drop_flag_schedule_cancel', schedule, {'status': 'cancelled'})

    flash('Schedule cancelled.', 'success')
    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/clear-ledger', methods=['POST'])
@login_required
@role_required('admin')
def clear_ledger():
    """
    Clear the execution ledger so scheduled actions can fire again.

    Useful when a drop ran but needs to be repeated, or after a manual test
    trigger. Optionally pass `action` to clear a single entry.
    """
    user = session.get('user_info', {})

    schedule = _get_schedule()
    if not schedule:
        flash('No schedule exists.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    target = (request.form.get('action') or '').strip().lower()
    old_ledger = schedule.get('last_executed', {})

    if target:
        if target not in TEST_ACTIONS:
            flash(f'Unknown action: {target}', 'error')
            return redirect(url_for('drop_flag.drop_flag_dashboard'))
        schedule.setdefault('last_executed', {})[target] = None
        detail = f"'{target}'"
    else:
        schedule['last_executed'] = {'reset': None, 'drop': None, 'end': None}
        detail = 'all actions'

    schedule['updated_at'] = _now_iso()

    if not _save_schedule(schedule):
        flash('Failed to update schedule.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(
        user, 'drop_flag_clear_ledger',
        {'last_executed': old_ledger},
        {'cleared': detail}
    )

    flash(
        f'Execution ledger cleared for {detail}. '
        f'Actions will fire again when their scheduled time arrives.',
        'success'
    )
    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/test/<action>', methods=['POST'])
@login_required
@role_required('admin')
def test_action(action):
    """
    Fire a drop flag action immediately, ignoring the schedule clock but
    honouring the saved engine selection. Does not touch the ledger.
    """
    user = session.get('user_info', {})

    action = (action or '').strip().lower()
    if action not in TEST_ACTIONS:
        flash(f'Unknown action: {action}', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    # Lazy import: keeps app startup free of a routes -> tasks -> celery
    # import cycle.
    try:
        from tasks.dropFlag import EXECUTORS
    except ImportError as e:
        logger.error(f"Could not import drop flag executors: {e}")
        flash('Drop Flag task module is unavailable. Check server logs.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    executor = EXECUTORS.get(action)
    if executor is None:
        flash(f'No executor registered for action: {action}', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    # Build a minimal schedule context carrying only the engine selection.
    schedule = _get_schedule() or {}
    context = {'selected_engines': schedule.get('selected_engines')}

    engines = _get_engine_config()
    if not engines:
        flash('No Quotient engines are configured.', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    selected = context['selected_engines']
    targets = [e for e in engines if e['id'] in selected] if selected else engines

    logger.info(f"Drop flag: manual '{action}' triggered by "
                f"{user.get('username', 'UNKNOWN')} on {len(targets)} engine(s)")

    try:
        success = executor(context)
    except Exception as e:
        logger.error(f"Manual '{action}' raised: {e}", exc_info=True)
        flash(f'Manual {action} failed: {e}', 'error')
        return redirect(url_for('drop_flag.drop_flag_dashboard'))

    _audit(
        user, f'drop_flag_test_{action}', None,
        {
            'action': action,
            'engines_affected': len(targets),
            'engine_ids': [e['id'] for e in targets],
            'success': bool(success),
        }
    )

    if success:
        flash(f'Manual {action} completed on {len(targets)} engine(s).', 'success')
    else:
        flash(
            f'Manual {action} finished with errors on some engines. '
            f'See worker logs for details.',
            'warning'
        )

    return redirect(url_for('drop_flag.drop_flag_dashboard'))


@drop_flag_bp.route('/status', methods=['GET'])
@login_required
@role_required('admin')
def get_status():
    """AJAX endpoint exposing current schedule state."""
    schedule = _get_schedule()

    if not schedule:
        return jsonify({
            'exists': False,
            'watchdog_active': _watchdog_registered(),
        })

    now = datetime.now(timezone.utc)
    remaining = {}

    try:
        drop_dt = _as_datetime(schedule.get('drop_time_utc'))
        if drop_dt:
            remaining['drop_seconds'] = max(0, int((drop_dt - now).total_seconds()))
    except ValueError:
        pass

    try:
        end_dt = _as_datetime(schedule.get('end_time_utc'))
        if end_dt:
            remaining['end_seconds'] = max(0, int((end_dt - now).total_seconds()))
    except ValueError:
        pass

    return jsonify({
        'exists': True,
        'enabled': schedule.get('enabled', False),
        'drop_time_utc': schedule.get('drop_time_utc'),
        'end_time_utc': schedule.get('end_time_utc'),
        'reset_before_drop': schedule.get('reset_before_drop', False),
        'selected_engines': schedule.get('selected_engines'),
        'remaining': remaining,
        'executed': schedule.get('last_executed', {}),
        'watchdog_active': _watchdog_registered(),
    })
