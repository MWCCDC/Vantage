# routes/incidentResponse.py
"""
Incident Response scoring routes.

Base URI: /IR

Currently scoped to red/admin for scoring blue-team IR submissions at
/IR/scoreIRs. The blueprint is competition-generic and permissions are
declared per-endpoint via IR_SCORING_ROLES, so a future blue-team
self-submission page (e.g. /IR/blueIRs) can live in this same module with
its own role restrictions without disturbing the red scoring flow.

Data model (Score table, category='incident-response'):
    reference_id  -> IR name / report filename (unique per team)
    description   -> grader notes / feedback
    points        -> total integer points returned to the team
    score_metadata -> {
        "source": "red_ir_form",
        "attackRefs": [{"score_id": <red score id>, "points": <int>}, ...]
    }

IMPORTANT: 'points' is fully independent of the per-attack point boxes.
The client auto-sums the boxes into the points field as a convenience, but
the grader may override the total freely (e.g. awarding points for a
well-written IR when red team never recorded the attack, or deducting for
a weak writeup). attackRefs is purely attribution bookkeeping — which
recorded attacks this IR covers and roughly how much credit each earned.

attackRefs is the single source of truth for which recorded attacks an IR
covers; edits rebuild it from the form submission. Referenced-attack
detection scans other IR submissions' attackRefs so overlap warnings stay
current.

All POST endpoints expect/return JSON (wizard is fetch-driven) and still
require the CSRF token via the X-CSRFToken header.
"""

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, session

from routes.decorators import login_required, role_required
from models import db, AuditLog, Team
from models.score import Score

# Create the blueprint
ir_bp = Blueprint('incident_response', __name__, url_prefix='/IR')

# Roles allowed to score IR submissions from the red-side page. Module-level
# so future endpoints (blue team self-submission) can use a different tuple.
IR_SCORING_ROLES = ('red', 'admin')

IR_CATEGORY = 'incident-response'
ATTACK_CATEGORY = 'red'

METADATA_SOURCE = 'red_ir_form'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_ir_references(team_id, exclude_ir_id=None):
    """
    Scan IR submissions for a team and map attack score_id -> list of
    referencing IRs, built from each submission's metadata.attackRefs.

    Args:
        team_id: team whose IR submissions to scan
        exclude_ir_id: IR score id to skip (used when editing, so a
            submission never flags its own references)

    Returns:
        dict: {attack_score_id: [{'ir_id': int, 'ir_name': str}, ...]}
    """
    references = {}

    ir_scores = Score.query.filter_by(
        team_id=team_id, category=IR_CATEGORY
    ).all()

    for ir_score in ir_scores:
        if exclude_ir_id is not None and ir_score.id == exclude_ir_id:
            continue

        metadata = ir_score.score_metadata or {}
        attack_refs = metadata.get('attackRefs')
        if not isinstance(attack_refs, list):
            continue

        for ref in attack_refs:
            if not isinstance(ref, dict) or 'score_id' not in ref:
                continue
            references.setdefault(ref['score_id'], []).append({
                'ir_id': ir_score.id,
                'ir_name': ir_score.reference_id or '(unnamed)',
            })

    return references


def _json_body():
    """Parse the request body as JSON. Returns (data, error_response)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({'error': 'Request body must be a JSON object.'}), 400)
    return data, None


def _parse_attack_points(payload):
    """
    Extract and validate the attack_points mapping from a submit payload.

    Expected shape: {"attack_points": {"<score_id>": <int points>, ...}}

    Entries that are empty, non-numeric, or zero are silently dropped
    (nothing to attribute to that attack). Non-integer values are rejected
    so fractional points can never sneak into an integer column.

    NOTE: These per-attack points are attribution metadata only. They do
    NOT have to sum to the submission's 'points' total — the grader may
    award more (quality writeup on an unrecorded attempt) or less (weak
    writeup penalty) than the per-attack boxes suggest.

    Returns:
        (refs, error_response) where refs is
        {score_id: {'points': int, 'score_obj': Score}} or an error tuple.
    """
    raw = payload.get('attack_points')
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, (jsonify({'error': "'attack_points' must be an object."}), 400)

    refs = {}
    for score_id_str, points_raw in raw.items():
        try:
            score_id = int(score_id_str)
        except (TypeError, ValueError):
            return None, (jsonify({'error': f"Invalid attack score id: {score_id_str!r}"}), 400)

        if points_raw in (None, ''):
            continue  # empty box: attack not referenced

        if isinstance(points_raw, float) and not points_raw.is_integer():
            return None, (jsonify({'error': 'Attack points must be whole integers.'}), 400)
        if isinstance(points_raw, str):
            try:
                points_val = float(points_raw)
            except ValueError:
                return None, (jsonify({'error': f"Attack points for score {score_id} is not a number."}), 400)
            if not points_val.is_integer():
                return None, (jsonify({'error': 'Attack points must be whole integers.'}), 400)
        elif isinstance(points_raw, (int, float)):
            points_val = float(points_raw)
        else:
            return None, (jsonify({'error': f"Attack points for score {score_id} is not a number."}), 400)

        points = int(points_val)
        if points == 0:
            continue  # zero points: nothing to attribute, don't store a ref

        refs[score_id] = {'points': points}

    return refs, None


def _validate_attack_scores(refs, team_id):
    """
    Confirm every referenced attack score exists, belongs to the selected
    team, and is a red-category score. Attaches the Score object to refs.

    Returns:
        (ok, error_response)
    """
    if not refs:
        return True, None

    for score_id in refs.keys():
        score = Score.query.filter_by(id=score_id, team_id=team_id,
                                       category=ATTACK_CATEGORY).first()
        if not score:
            return False, (jsonify({
                'error': f"Attack score {score_id} was not found for this team "
                         f"(it may have been deleted). Remove it and try again."
            }), 400)
        refs[score_id]['score_obj'] = score

    return True, None


def _prior_missing_attack_refs(ir_score):
    """
    For an IR being edited, find metadata attackRefs whose underlying red
    score no longer exists in the database.

    Returns:
        list of {'score_id': int, 'points': int}
    """
    metadata = ir_score.score_metadata or {}
    attack_refs = metadata.get('attackRefs') or []

    missing = []
    for ref in attack_refs:
        if not isinstance(ref, dict) or 'score_id' not in ref:
            continue
        exists = Score.query.filter_by(
            id=ref['score_id'],
            team_id=ir_score.team_id,
            category=ATTACK_CATEGORY
        ).first()
        if not exists:
            missing.append({
                'score_id': ref['score_id'],
                'points': ref.get('points', 0),
            })
    return missing


def _current_username():
    return session.get('user_info', {}).get('username', 'UNKNOWN')


# ---------------------------------------------------------------------------
# Stage 1: Page + team selection
# ---------------------------------------------------------------------------

@ir_bp.route('/scoreIRs', methods=['GET'])
@login_required
@role_required(*IR_SCORING_ROLES)
def score_irs():
    """Incident Response scoring wizard (team select -> source -> form)."""
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
    return render_template('redIR.html',
                           red_team_page=True,
                           active_teams=active_teams)


# ---------------------------------------------------------------------------
# Team data (drives stages 2 and 3 of the wizard)
# ---------------------------------------------------------------------------

@ir_bp.route('/scoreIRs/team/<int:team_id>/data', methods=['GET'])
@login_required
@role_required(*IR_SCORING_ROLES)
def team_data(team_id):
    """
    JSON payload for a selected team:
      - ir_submissions: existing IR scores for this team (for the
        'edit a previous submission' option), newest first
      - attacks: every recorded red-category score for this team, sorted
        unreferenced-first then time ascending, annotated with which other
        IR submissions reference each attack

    Query params:
      exclude_ir (int, optional): IR score id to exclude from
        referenced-by detection (pass the id being edited so a submission
        never flags its own references)
    """
    team = Team.query.filter_by(id=team_id, is_active=True).first()
    if not team:
        return jsonify({'error': 'Active team not found.'}), 404

    exclude_ir_id = request.args.get('exclude_ir', type=int)

    # --- Existing IR submissions for this team (newest first) ---
    ir_submissions = Score.query.filter_by(
        team_id=team_id, category=IR_CATEGORY
    ).order_by(Score.timestamp.desc()).all()

    ir_rows = [{
        'id': s.id,
        'name': s.reference_id or '(unnamed)',
        'notes': s.description or '',
        'points': s.points,
        'timestamp': s.timestamp.isoformat() if s.timestamp else None,
        'scorer': s.scorer_username,
    } for s in ir_submissions]

    # --- Recorded attacks, time ascending (stable) ---
    attacks = Score.query.filter_by(
        team_id=team_id, category=ATTACK_CATEGORY
    ).order_by(Score.timestamp.asc()).all()

    ref_map = _collect_ir_references(team_id, exclude_ir_id=exclude_ir_id)

    attack_rows = []
    for a in attacks:
        referenced_by = ref_map.get(a.id, [])
        attack_rows.append({
            'id': a.id,
            'timestamp': a.timestamp.isoformat() if a.timestamp else None,
            'points': a.points,
            'description': a.description or '',
            'reference_id': a.reference_id,
            'scorer': a.scorer_username,
            'referenced_by': referenced_by,
            'is_referenced': bool(referenced_by),
        })

    # Stable sort: unreferenced (False) group first, each group retains
    # its time-ascending order.
    attack_rows.sort(key=lambda r: r['is_referenced'])

    resp = jsonify({
        'team': {
            'id': team.id,
            'number': team.team_number,
            'name': team.team_name,
        },
        'ir_submissions': ir_rows,
        'attacks': attack_rows,
    })
    # Reference flags change the moment any IR is saved, so this payload
    # must never be served from a browser or intermediary cache.
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


# ---------------------------------------------------------------------------
# IR detail (edit reload)
# ---------------------------------------------------------------------------

@ir_bp.route('/scoreIRs/<int:ir_score_id>', methods=['GET'])
@login_required
@role_required(*IR_SCORING_ROLES)
def ir_detail(ir_score_id):
    """
    Full detail for one IR submission, used when reloading a previous
    submission for editing.

    Response includes:
      - name / notes / points (form fields)
      - attack_refs: metadata references whose attack scores still exist
      - missing_attacks: metadata references whose attack scores no
        longer exist in the database (the client should show the
        confirmation popup explaining these will be dropped on save)
    """
    ir_score = Score.query.filter_by(id=ir_score_id, category=IR_CATEGORY).first()
    if not ir_score:
        return jsonify({'error': 'Incident response score not found.'}), 404

    metadata = ir_score.score_metadata or {}
    attack_refs = metadata.get('attackRefs') or []

    valid_refs = []
    missing = []
    for ref in attack_refs:
        if not isinstance(ref, dict) or 'score_id' not in ref:
            continue
        attack = Score.query.filter_by(
            id=ref['score_id'],
            team_id=ir_score.team_id,
            category=ATTACK_CATEGORY
        ).first()
        entry = {'score_id': ref['score_id'], 'points': ref.get('points', 0)}
        if attack:
            valid_refs.append(entry)
        else:
            missing.append(entry)

    return jsonify({
        'id': ir_score.id,
        'team_id': ir_score.team_id,
        'name': ir_score.reference_id or '',
        'notes': ir_score.description or '',
        'points': ir_score.points,
        'attack_refs': valid_refs,
        'missing_attacks': missing,
    })


# ---------------------------------------------------------------------------
# Submission (create + in-place edit)
# ---------------------------------------------------------------------------

@ir_bp.route('/scoreIRs', methods=['POST'])
@login_required
@role_required(*IR_SCORING_ROLES)
def submit_ir():
    """
    Create a new IR score, or update an existing one in place.

    JSON payload:
      team_id (int, required)
      ir_name (str, required)        -> Score.reference_id
      notes (str, optional)          -> Score.description
      points (int, required)         -> Score.points. INDEPENDENT of the
                                        per-attack boxes: the client
                                        auto-sums the boxes as a helper,
                                        but the grader may override it
                                        (award points with no recorded
                                        attacks, partial credit, etc.)
      attack_points (obj, optional)   -> {"<attack score id>": <int points>}
                                        attribution metadata only
      ir_score_id (int, optional)    -> present = edit that submission
      overwrite_missing (bool, opt)  -> set after the client confirmed the
                                        'missing attack' popup; without it,
                                        an edit that would drop refs to
                                        deleted attacks is rejected with
                                        status 'confirm_missing'

    Returns:
      200 {"status": "ok", "ir_id": ...}
      400 {"error": ...}              validation failure
      409 {"status": "confirm_missing", "missing": [...]}
    """
    payload, err = _json_body()
    if err:
        return err

    username = _current_username()

    # --- Basic payload validation ---
    team_id = payload.get('team_id')
    if not isinstance(team_id, int):
        return jsonify({'error': "'team_id' is required."}), 400

    ir_name = (payload.get('ir_name') or '').strip()
    if not ir_name:
        return jsonify({'error': 'An IR name is required.'}), 400

    notes = (payload.get('notes') or '').strip()

    raw_points = payload.get('points')
    if raw_points is None:
        return jsonify({'error': "'points' is required."}), 400
    if isinstance(raw_points, float) and not raw_points.is_integer():
        return jsonify({'error': 'Points must be whole integers.'}), 400
    if isinstance(raw_points, str):
        try:
            raw_points = float(raw_points)
        except ValueError:
            return jsonify({'error': 'Points must be a whole number.'}), 400
        if not raw_points.is_integer():
            return jsonify({'error': 'Points must be whole integers.'}), 400
    if not isinstance(raw_points, (int, float)):
        return jsonify({'error': 'Points must be a whole number.'}), 400
    points = int(raw_points)

    # --- Team validation ---
    team = Team.query.filter_by(id=team_id, is_active=True).first()
    if not team:
        return jsonify({'error': 'Active team not found.'}), 404

    # --- Edit target validation ---
    ir_score_id = payload.get('ir_score_id')
    editing = ir_score_id is not None
    ir_score = None

    if editing:
        if not isinstance(ir_score_id, int):
            return jsonify({'error': "'ir_score_id' must be an integer."}), 400
        ir_score = Score.query.filter_by(id=ir_score_id, category=IR_CATEGORY).first()
        if not ir_score:
            return jsonify({'error': 'Incident response score not found.'}), 404
        if ir_score.team_id != team_id:
            return jsonify({'error': 'This IR submission belongs to a different team.'}), 400

    # --- Duplicate-name guard (new submissions only) ---
    if not editing:
        duplicate = Score.query.filter_by(
            team_id=team_id, category=IR_CATEGORY, reference_id=ir_name
        ).first()
        if duplicate:
            return jsonify({
                'error': f"An IR named '{ir_name}' already exists for this team. "
                         f"Edit that submission instead of creating a duplicate."
            }), 400

    # --- Attack reference parsing + validation ---
    refs, err = _parse_attack_points(payload)
    if err:
        return err

    ok, err = _validate_attack_scores(refs, team_id)
    if err:
        return err

    # NOTE: No sum verification between 'points' and the per-attack boxes.
    # The total is authoritative on its own; per-attack values are stored
    # purely as attribution metadata (see module docstring).

    # --- Missing-attack confirmation gate (edits only) ---
    # If a previous version of this submission referenced attacks that have
    # since been deleted, refuse to silently drop them: return the list so
    # the client can show the confirmation popup, then resubmit with
    # overwrite_missing=true.
    overwrite_missing = bool(payload.get('overwrite_missing'))
    if editing:
        missing_prior = _prior_missing_attack_refs(ir_score)
        if missing_prior and not overwrite_missing:
            return jsonify({
                'status': 'confirm_missing',
                'missing': missing_prior,
                'message': (
                    f"{len(missing_prior)} attack(s) previously referenced by this "
                    f"submission no longer exist in the database and would be "
                    f"removed from it. Please report this to an admin."
                ),
            }), 409

    # --- Build metadata (attackRefs ordered by attack time, ascending) ---
    attack_ids = list(refs.keys())
    score_objs = {sid: refs[sid]['score_obj'] for sid in attack_ids}
    attack_ids.sort(key=lambda sid: (
        score_objs[sid].timestamp or datetime.min.replace(tzinfo=timezone.utc)
    ))

    attack_refs_metadata = [
        {'score_id': sid, 'points': refs[sid]['points']}
        for sid in attack_ids
    ]

    # --- Persist (create or in-place update) ---
    if editing:
        old_snapshot = {
            'reference_id': ir_score.reference_id,
            'points': ir_score.points,
            'description': ir_score.description,
            'attackRefs': (ir_score.score_metadata or {}).get('attackRefs', []),
        }

        ir_score.reference_id = ir_name
        ir_score.description = notes
        ir_score.points = points

        metadata = dict(ir_score.score_metadata or {})
        metadata['source'] = METADATA_SOURCE
        metadata['attackRefs'] = attack_refs_metadata

        # Lightweight edit trail; keeps full history out of the row while
        # still capturing who touched it and what the totals were.
        history = metadata.setdefault('editHistory', [])
        history.append({
            'edited_by': username,
            'edited_at': datetime.now(timezone.utc).isoformat(),
            'previous_points': old_snapshot['points'],
            'previous_attack_count': len(old_snapshot['attackRefs']),
        })
        # Cap the trail so metadata can't grow unbounded across many edits.
        metadata['editHistory'] = history[-20:]

        ir_score.score_metadata = metadata
        audit_action = 'ir_score_edit'
    else:
        ir_score = Score(
            team_id=team_id,
            scorer_username=username,
            category=IR_CATEGORY,
            points=points,
            description=notes,
            reference_id=ir_name,
            is_approved=True,
            score_metadata={
                'source': METADATA_SOURCE,
                'attackRefs': attack_refs_metadata,
            }
        )
        db.session.add(ir_score)
        audit_action = 'ir_score_add'

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to save IR score: {str(e)}")
        return jsonify({'error': 'A database error occurred while saving. '
                                  'Check server logs.'}), 500

    # --- Audit log ---
    new_value = {
        'category': IR_CATEGORY,
        'team_number': team.team_number,
        'reference_id': ir_name,
        'points': points,
        'attack_count': len(attack_refs_metadata),
        'attackRefs': attack_refs_metadata,
    }
    old_value = old_snapshot if editing else None

    try:
        audit_entry = AuditLog(
            username=username,
            action=audit_action,
            entity_type='score',
            entity_id=ir_score.id,
            old_value=old_value,
            new_value=new_value
        )
        db.session.add(audit_entry)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log for IR score: {str(e)}")

    return jsonify({'status': 'ok', 'ir_id': ir_score.id}), 200
