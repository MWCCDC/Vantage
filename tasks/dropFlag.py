# tasks/dropFlag.py
"""
Drop Flag execution engine.

A recurring watchdog task (`tasks.dropFlag.tick`, registered in RedBeat)
runs every few seconds, reads the schedule from Valkey, and executes any
action whose time has come:

    reset  -> drop_time_utc - 60s  (only when reset_before_drop is set)
    drop   -> resume engine, then start competition
    end    -> pause engine, then stop competition

Because execution is driven by comparing stored timestamps against the
clock (rather than pre-queued ETA tasks), adjusting the schedule in Valkey
immediately changes behavior — no task revocation needed.

State is tracked in the schedule blob's `last_executed` map, which is
claimed atomically via a Valkey lock before execution to prevent double
fires from overlapping ticks.

IMPORTANT: tasks.quotient is imported lazily (inside functions, never at
module level). tasks.quotient imports app.py, and app.py imports this
module to register the watchdog — a module-level import here would create
a cycle that crashes celery beat and workers at startup.
"""

import json
import logging
from datetime import datetime, timezone

from celery_app import celery
from utils.valkey_client import get_valkey_client

logger = logging.getLogger(__name__)

SCHEDULE_KEY = "vantage:schedule:dropflag"
LOCK_KEY = "vantage:schedule:dropflag:lock"
LOCK_TTL_SECONDS = 120          # upper bound on one full tick execution
STALE_GRACE_SECONDS = 120       # don't fire an action more than this late

# --- Verified Quotient control actions ---
# Quotient does NOT use distinct endpoints per direction. Both directions
# of each operation hit the SAME URL; the boolean in the JSON body selects
# the direction. These were verified against tasks/quotient.py:
#
#   resume engine  -> POST /api/engine/pause      body {"pause": false}
#   pause engine   -> POST /api/engine/pause      body {"pause": true}
#   start comp     -> POST /api/competition/start body {"started": true}
#   stop comp      -> POST /api/competition/start body {"started": false}
CONTROL_ACTIONS = {
    'engine_start':      ('engine/pause',      {'pause': False}),
    'engine_stop':       ('engine/pause',      {'pause': True}),
    'competition_start': ('competition/start', {'started': True}),
    'competition_stop':  ('competition/start', {'started': False}),
}


def _quotient():
    """
    Lazily import and cache the quotient tasks module.

    Done at call time rather than import time because tasks.quotient
    imports app.py; if we pulled it in at module level, the chain
    beat -> tasks.quotient -> app -> tasks.dropFlag -> tasks.quotient
    would crash startup with a partially-initialized-module ImportError.
    """
    import tasks.quotient
    return tasks.quotient


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(value):
    """Parse the schedule blob's UTC ISO strings back to aware datetimes."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_schedule():
    try:
        raw = get_valkey_client().get(SCHEDULE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.error(f"Drop flag: failed to read schedule: {e}")
    return None


def _save_schedule(schedule):
    try:
        get_valkey_client().set(SCHEDULE_KEY, json.dumps(schedule))
        return True
    except Exception as e:
        logger.error(f"Drop flag: failed to write schedule: {e}")
        return False


def _target_engines(schedule):
    """
    Resolve the schedule's engine selection to engine config dicts.

    Filters to active engines only (matching _global_control's behavior
    in tasks/quotient.py), then narrows to the schedule's selection if
    one is set. An empty selected_engines list means "all engines".
    """
    engines = [e for e in _quotient().get_engine_config()
               if e.get('is_active', True)]

    selected = schedule.get('selected_engines')
    if selected:
        engines = [e for e in engines if e['id'] in selected]

    return engines


def _do_control(engine, action_key):
    """
    Run a single control action on one engine using the verified
    Quotient endpoint/payload mapping. Returns the result dict.
    """
    action_type, payload = CONTROL_ACTIONS[action_key]
    return _quotient()._control_engine_action(engine, action_type, payload)


# --- Action executors (sequenced per the requirements) ---

def _execute_reset(schedule):
    """
    Pause each selected engine, THEN reset it via GET /api/engine/reset.

    Newer Quotient builds no longer implicitly pause the engine when
    scores are reset, so we pause explicitly first. This guarantees no
    scoring rounds run between the reset and the scheduled drop (which
    resumes the engine and starts the competition).
    """
    q = _quotient()
    engines = _target_engines(schedule)
    failures = []

    for engine in engines:
        engine_id = engine['id']

        pause = _do_control(engine, 'engine_stop')
        if not pause.get('success'):
            # Log loudly, but still attempt the reset — the drop will
            # try to resume/start again anyway, and a reset on a
            # running engine is still better than no reset.
            failures.append((engine_id, 'engine/pause', pause.get('error')))
            logger.warning(
                f"Drop flag: pause before reset FAILED for engine "
                f"{engine_id}: {pause.get('error')} — attempting reset anyway")

        reset = q._reset_engine(engine)
        if not reset.get('success'):
            failures.append((engine_id, 'engine/reset', reset.get('error')))

    for engine_id, step, error in failures:
        logger.error(f"Drop flag: reset FAILED for engine {engine_id} "
                     f"at step {step}: {error}")

    logger.info(f"Drop flag: reset executed on {len(engines)} engine(s), "
                f"{len(failures)} failure(s)")
    return not failures


def _execute_drop(schedule):
    """Resume each engine, THEN start the competition on it."""
    engines = _target_engines(schedule)
    failures = []

    for engine in engines:
        engine_id = engine['id']

        resume = _do_control(engine, 'engine_start')
        if not resume.get('success'):
            failures.append((engine_id, 'engine/resume', resume.get('error')))
            continue  # don't start a competition on an unresponsive engine

        start = _do_control(engine, 'competition_start')
        if not start.get('success'):
            failures.append((engine_id, 'competition/start', start.get('error')))

    for engine_id, step, error in failures:
        logger.error(f"Drop flag: drop FAILED for engine {engine_id} "
                     f"at step {step}: {error}")

    logger.info(f"Drop flag: drop executed on {len(engines)} engine(s), "
                f"{len(failures)} failure(s)")
    return not failures


def _execute_end(schedule):
    """Pause each engine, THEN stop the competition on it."""
    engines = _target_engines(schedule)
    failures = []

    for engine in engines:
        engine_id = engine['id']

        pause = _do_control(engine, 'engine_stop')
        if not pause.get('success'):
            failures.append((engine_id, 'engine/pause', pause.get('error')))

        stop = _do_control(engine, 'competition_stop')
        if not stop.get('success'):
            failures.append((engine_id, 'competition/stop', stop.get('error')))

    for engine_id, step, error in failures:
        logger.error(f"Drop flag: end FAILED for engine {engine_id} "
                     f"at step {step}: {error}")

    logger.info(f"Drop flag: end executed on {len(engines)} engine(s), "
                f"{len(failures)} failure(s)")
    return not failures


EXECUTORS = {
    'reset': _execute_reset,
    'drop': _execute_drop,
    'end': _execute_end,
}


def _due_times(schedule):
    """Return {action_name: epoch seconds} for scheduled actions."""
    due = {}

    drop_dt = _parse_iso(schedule.get('drop_time_utc'))
    if drop_dt:
        if schedule.get('reset_before_drop'):
            due['reset'] = drop_dt.timestamp() - 60
        due['drop'] = drop_dt.timestamp()

    end_dt = _parse_iso(schedule.get('end_time_utc'))
    if end_dt:
        due['end'] = end_dt.timestamp()

    return due


@celery.task(name='tasks.dropFlag.tick')
def drop_flag_tick():
    """
    Watchdog: run every N seconds via RedBeat. Executes any due action
    exactly once, using last_executed as the completion ledger.

    Runs inside a Flask app context because utils/valkey_client reads
    current_app.config for connection settings.
    """
    # Lazy import to avoid the circular-import chain at module load time
    from app import app

    with app.app_context():
        return _tick_inner()


def _tick_inner():
    """Inner tick logic, called within Flask app context."""
    vk = get_valkey_client()

    # Single-flight lock: prevents overlapping ticks (e.g. during a slow
    # engine round-trip) from double-firing an action.
    if not vk.set(LOCK_KEY, '1', nx=True, ex=LOCK_TTL_SECONDS):
        return {'status': 'skipped', 'reason': 'tick already running'}

    try:
        schedule = _load_schedule()
        if not schedule or not schedule.get('enabled'):
            return {'status': 'idle'}

        last_executed = schedule.setdefault('last_executed',
                                            {'reset': None, 'drop': None, 'end': None})
        now_ts = _now().timestamp()

        executed_any = False
        for action, due_ts in _due_times(schedule).items():
            if last_executed.get(action):
                continue  # already done

            lateness = now_ts - due_ts
            if lateness < 0:
                continue  # not yet due

            if lateness > STALE_GRACE_SECONDS:
                # Missed the window entirely (scheduler was down, or the
                # schedule was re-enabled long after the fact). Mark as
                # skipped so we don't fire a surprise drop hours late.
                logger.warning(
                    f"Drop flag: action '{action}' was due {int(lateness)}s ago; "
                    f"outside grace window, marking skipped")
                last_executed[action] = _now().strftime(
                    '%Y-%m-%dT%H:%M:%SZ') + '|skipped'
                executed_any = True
                continue

            # Claim the slot BEFORE executing: if the worker dies mid-action,
            # the claim persists and we don't re-run it next tick.
            last_executed[action] = _now().strftime('%Y-%m-%dT%H:%M:%SZ')
            _save_schedule(schedule)
            executed_any = True

            logger.info(f"Drop flag: executing '{action}' "
                        f"({int(lateness)}s after due time)")

            try:
                EXECUTORS[action](schedule)
            except Exception as e:
                logger.error(f"Drop flag: executor for '{action}' raised: {e}")

        if executed_any:
            _save_schedule(schedule)

        return {'status': 'ok'}
    finally:
        try:
            vk.delete(LOCK_KEY)
        except Exception:
            pass


# --- RedBeat registration (call once at app startup) ---

def register_drop_flag_tick(interval_seconds=10):
    """
    Ensure the watchdog tick is registered in RedBeat. Safe to call
    repeatedly (e.g. every app boot); re-registers only if missing.

    Should be called from the very bottom of app.py, after all blueprints
    and modules are loaded, so the import cannot land mid-cycle.
    """
    try:
        from redbeat import RedBeatSchedulerEntry
        from celery.schedules import schedule as celery_schedule
        from datetime import timedelta
    except ImportError:
        logger.warning("Drop flag: RedBeat not available; watchdog not registered")
        return False

    entry_name = 'dropflag-tick'

    try:
        try:
            existing = RedBeatSchedulerEntry.from_key(
                f'redbeat:{entry_name}', app=celery)
            # Only skip re-registration when BOTH the interval AND the
            # task name match — otherwise a renamed/stale entry would
            # linger forever and keep dispatching an unknown task.
            if (existing.task == 'tasks.dropFlag.tick'
                    and existing.schedule.run_every.total_seconds() == interval_seconds):
                return True
        except Exception:
            pass  # doesn't exist yet

        entry = RedBeatSchedulerEntry(
            name=entry_name,
            task='tasks.dropFlag.tick',
            schedule=celery_schedule(run_every=timedelta(seconds=interval_seconds)),
            app=celery,
        )
        entry.save()
        logger.info(f"Drop flag: watchdog tick registered ({interval_seconds}s)")
        return True
    except Exception as e:
        logger.error(f"Drop flag: failed to register tick: {e}")
        return False
