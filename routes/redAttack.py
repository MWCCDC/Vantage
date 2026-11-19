# routes/redAttack.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from routes.decorators import login_required, role_required
from models import db, AuditLog, Team
from models.score import Score
from utils.valkey_client import get_valkey_client
import json
from collections import defaultdict

# Create the blueprint
red_attack_bp = Blueprint('red_attack', __name__, url_prefix='/red')

# Valkey key holding the red team score reference configuration
REFS_VALKEY_KEY = "vantage:config:redScoreRefs"


def _load_score_refs():
    """
    Loads the red team scoring reference configuration from Valkey.

    Returns:
        tuple: (score_refs list, ref_lookup dict, error string or None)
        - score_refs: [{'ref_id', 'type', 'desc', 'points'}] sorted by ref_id
        - ref_lookup: {ref_id: {'type', 'desc', 'points'}} for fast access
    """
    try:
        vk = get_valkey_client()
        raw_data = vk.get(REFS_VALKEY_KEY)
        if not raw_data:
            return [], {}, "No reference configuration found in Valkey."

        refs_data = json.loads(raw_data)
        if not isinstance(refs_data, dict):
            return [], {}, "Reference configuration in Valkey is malformed."

        score_refs = []
        ref_lookup = {}
        for ref_id, ref_val in refs_data.items():
            if isinstance(ref_val, dict):
                entry = {
                    'ref_id': ref_id,
                    'type': ref_val.get('type', 'unknown'),
                    'desc': ref_val.get('desc', 'No description'),
                    'points': ref_val.get('points', 0)
                }
                score_refs.append(entry)
                ref_lookup[ref_id] = entry

        score_refs.sort(key=lambda r: r['ref_id'])
        return score_refs, ref_lookup, None

    except json.JSONDecodeError as e:
        current_app.logger.error(f"Failed to parse red score refs config: {str(e)}")
        return [], {}, f"Invalid reference configuration format: {str(e)}"
    except Exception as e:
        current_app.logger.error(f"Failed to fetch red score refs config: {str(e)}")
        return [], {}, f"Failed to load scoring configuration: {str(e)}"


@red_attack_bp.route('/attacks', methods=['GET'])
@login_required
@role_required('red', 'admin')
def red_attacks():
    """Red team attack entry page"""
    # --- 1. Fetch reference configuration ---
    score_refs, ref_lookup, refs_error = _load_score_refs()

    # --- 2. Fetch active teams for the checkbox grid ---
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()

    # --- 3. Fetch all red category scores, grouped by team then refID ---
    # Structure: { team_id: { 'team': TeamObj, 'refs': {ref_id: {'scores': [...], 'total': N}}, 'team_total': N } }
    teams_scores = {}

    try:
        scores = (Score.query
                  .filter(Score.category == 'red')
                  .join(Team, Score.team_id == Team.id)
                  .order_by(Team.team_number, Score.timestamp.asc())
                  .all())

        for score in scores:
            if score.team_id not in teams_scores:
                teams_scores[score.team_id] = {
                    'team': Team.query.get(score.team_id),
                    'refs': defaultdict(lambda: {'scores': [], 'total': 0}),
                    'team_total': 0
                }

            ref_id = score.reference_id or 'NO_REF'
            ref_data = teams_scores[score.team_id]['refs'][ref_id]
            ref_data['scores'].append(score)
            ref_data['total'] += score.points
            teams_scores[score.team_id]['team_total'] += score.points

        # Convert defaultdicts to regular dicts for safer template rendering
        for team_data in teams_scores.values():
            team_data['refs'] = dict(team_data['refs'])

        # Sort refID groups so unknown/no-ref entries land last for readability
        for team_data in teams_scores.values():
            team_data['refs'] = dict(sorted(
                team_data['refs'].items(),
                key=lambda item: (item[0] in ('NO_REF',) or item[0] not in ref_lookup, item[0])
            ))

    except Exception as e:
        current_app.logger.error(f"Failed to fetch red scores: {str(e)}")
        teams_scores = {}

    return render_template('redAttack.html',
                           red_team_page=True,

                           # Form data
                           score_refs=score_refs,
                           ref_lookup=ref_lookup,
                           refs_error=refs_error,
                           active_teams=active_teams,

                           # Table data
                           teams_scores=teams_scores)


@red_attack_bp.route('/attacks', methods=['POST'])
@login_required
@role_required('red', 'admin')
def submit_attack():
    """Handle red team attack submission from the web form."""
    user = session.get('user_info', {})
    username = user.get('username', 'UNKNOWN')

    # --- 1. Parse Form Data ---
    ref_id = request.form.get('refID', '').strip()
    selected_team_ids = request.form.getlist('teams')

    # Optional fields
    src_ip = request.form.get('srcIP', '').strip() or None
    dst_ip = request.form.get('dstIP', '').strip() or None
    dst_host = request.form.get('dstHost', '').strip() or None
    dst_service = request.form.get('dstService', '').strip() or None
    notes = request.form.get('notes', '').strip() or None

    # --- 2. Validation ---
    if not selected_team_ids:
        flash('At least one team must be selected.', 'error')
        return redirect(url_for('red_attack.red_attacks'))

    if not ref_id:
        flash('A reference ID is required.', 'error')
        return redirect(url_for('red_attack.red_attacks'))

    # --- 3. Look up the refID configuration ---
    _, ref_lookup, refs_error = _load_score_refs()

    if refs_error:
        flash(refs_error, 'error')
        return redirect(url_for('red_attack.red_attacks'))

    if ref_id not in ref_lookup:
        flash(f"refID '{ref_id}' not found in scoring configuration.", 'error')
        return redirect(url_for('red_attack.red_attacks'))

    ref_config = ref_lookup[ref_id]
    ref_type = ref_config.get('type', 'Unknown')
    ref_points = ref_config.get('points', 0)
    ref_desc = ref_config.get('desc', ref_type)

    # --- 4. Resolve Teams ---
    target_teams = []
    skipped_teams = []
    for team_id_str in selected_team_ids:
        try:
            team = Team.query.filter_by(id=int(team_id_str), is_active=True).first()
        except ValueError:
            continue
        if team:
            target_teams.append(team)
        else:
            skipped_teams.append(team_id_str)

    if not target_teams:
        flash('No valid active teams were selected.', 'error')
        return redirect(url_for('red_attack.red_attacks'))

    # --- 5. Prepare Metadata (mirrors the /red/addRefAttack API structure) ---
    optional_data = {k: v for k, v in {
        'srcIP': src_ip,
        'dstIP': dst_ip,
        'dstHost': dst_host,
        'dstService': dst_service,
        'notes': notes
    }.items() if v is not None}

    # --- 6. Create Scores ---
    inserted_team_numbers = []
    for team in target_teams:
        new_score = Score(
            team_id=team.id,
            scorer_username=username,
            category='red',
            points=ref_points,
            description=ref_type,
            reference_id=ref_id,
            is_approved=True,
            score_metadata={
                "source": "red_web_attack",
                "refID": ref_id,
                "refType": ref_type,
                "refPoints": ref_points,
                "refDesc": ref_desc,
                "submissionData": optional_data
            }
        )
        db.session.add(new_score)
        inserted_team_numbers.append(team.team_number)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to create red attack scores: {str(e)}")
        flash(f'A database error occurred while adding scores. Check server logs.', 'error')
        return redirect(url_for('red_attack.red_attacks'))

    # --- 7. Audit Log ---
    try:
        audit_entry = AuditLog(
            username=username,
            action='red_web_attack_add',
            entity_type='score_batch',
            entity_id=None,
            old_value=None,
            new_value={
                "category": "red",
                "refID": ref_id,
                "points": ref_points,
                "type": ref_type,
                "teams_added": inserted_team_numbers,
                "teams_skipped": skipped_teams,
                "count": len(inserted_team_numbers)
            }
        )
        db.session.add(audit_entry)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log for red web attack: {str(e)}")

    flash(f'Attack "{ref_type}" ({ref_points} pts) recorded for {len(inserted_team_numbers)} team(s).', 'success')
    return redirect(url_for('red_attack.red_attacks'))


@red_attack_bp.route('/attacks/metadata/<int:score_id>', methods=['GET'])
@login_required
@role_required('red', 'admin')
def get_attack_metadata(score_id):
    """Return metadata for a red team score as JSON"""
    score = Score.query.filter_by(id=score_id, category='red').first()
    if not score:
        return jsonify({'error': 'Score not found'}), 404

    metadata = score.score_metadata if score.score_metadata else {}
    return jsonify(metadata)