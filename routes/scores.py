# routes/scores.py
import io
import csv
import json
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify, Response
from models import db, AuditLog
from models.score import Score
from models.team import Team
from routes.decorators import login_required, role_required
from sqlalchemy import distinct, select
from sqlalchemy.orm import joinedload
from collections import defaultdict
from utils.scoreCalculation import calculate_team_standings

# Create the blueprint
scores_bp = Blueprint('scores', __name__, url_prefix='/scores')


def _build_filter_url(category, approved):
    """Helper to build redirect URL preserving active filters."""
    params = {}
    if category:
        params['category'] = category
    if approved:
        params['is_approved'] = approved
    return url_for('scores.score_list', **params)


@scores_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def score_list():
    """List all scores, grouped by team, with filtering."""
    user = session.get('user_info', {})

    # Get filters
    category_filter = request.args.get('category', '').strip()
    approved_filter = request.args.get('is_approved', '')

    # Build Query
    query = Score.query

    if category_filter:
        query = query.filter(Score.category == category_filter)

    if approved_filter.lower() == 'true':
        query = query.filter(Score.is_approved == True)
    elif approved_filter.lower() == 'false':
        query = query.filter(Score.is_approved == False)

    # Order by team number, then timestamp
    scores = query.join(Team, Score.team_id == Team.id).order_by(Team.team_number, Score.timestamp.desc()).all()

    # Get all active teams for the "Add Score" dropdown
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()

    # Get distinct categories for the filter dropdown
    categories_query = db.session.execute(
        select(Score.category)
        .distinct()
        .filter(Score.category != None)
        .order_by(Score.category)
    ).scalars().all()
    categories = [cat for cat in categories_query if cat is not None]

    # --- GROUPING AND CALCULATION LOGIC (Python-side) ---
    # Structure: { team_id: { 'team': TeamObj, 'categories': { 'CatName': {'scores': [...], 'total': N, 'unapproved': N} }, 'team_total': N, 'team_unapproved': N } }
    teams_scores = {}
    
    for score in scores:
        if score.team_id not in teams_scores:
            team = Team.query.get(score.team_id)
            teams_scores[score.team_id] = {
                'team': team,
                'categories': defaultdict(lambda: {'scores': [], 'total': 0, 'unapproved': 0}),
                'team_total': 0,
                'team_unapproved': 0
            }
        
        # Add score to category
        cat_data = teams_scores[score.team_id]['categories'][score.category]
        cat_data['scores'].append(score)
        cat_data['total'] += score.points
        if not score.is_approved:
            cat_data['unapproved'] += 1
        
        # Add to team totals
        teams_scores[score.team_id]['team_total'] += score.points
        if not score.is_approved:
            teams_scores[score.team_id]['team_unapproved'] += 1

    # Convert defaultdicts to regular dicts for safer template rendering
    for team_data in teams_scores.values():
        team_data['categories'] = dict(team_data['categories'])

    return render_template('scores.html',
                         teams_scores=teams_scores,
                         active_teams=active_teams,
                         categories=categories,
                         current_category=category_filter,
                         current_approved=approved_filter,
                         is_admin_page=True)


@scores_bp.route('/approve/<int:team_id>', methods=['POST'])
@login_required
@role_required('admin')
def approve_team_scores(team_id):
    """Bulk approve all unapproved scores for a specific team."""
    user = session.get('user_info', {})

    # Preserve filters from hidden fields
    category = request.form.get('filter_category', '')
    approved = request.form.get('filter_approved', '')

    team = Team.query.get(team_id)
    if not team:
        flash('Team not found.', 'error')
        return redirect(_build_filter_url(category, approved))

    unapproved_count = Score.query.filter_by(team_id=team_id, is_approved=False).count()

    if unapproved_count == 0:
        flash('No unapproved scores found for this team.', 'info')
        return redirect(_build_filter_url(category, approved))

    updated_count = Score.query.filter_by(team_id=team_id, is_approved=False).update({'is_approved': True})

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='bulk_approve_scores',
        entity_type='score',
        entity_id=team_id,
        old_value={'status': 'unapproved'},
        new_value={'status': 'approved', 'count': updated_count}
    )
    db.session.add(audit_entry)
    db.session.commit()

    flash(f'Successfully approved {updated_count} score(s) for Team {team.team_number}.', 'success')
    return redirect(_build_filter_url(category, approved))


@scores_bp.route('/add', methods=['POST'])
@login_required
@role_required('admin')
def add_score():
    """Handle manual score entry."""
    user = session.get('user_info', {})

    # Preserve filters from hidden fields
    category = request.form.get('filter_category', '')
    approved = request.form.get('filter_approved', '')

    team_selection = request.form.get('team_selection')
    score_category = request.form.get('category', '').strip()
    points_str = request.form.get('points', '')
    description = request.form.get('description', '').strip()
    reference_id = request.form.get('reference_id', '').strip()

    # Validation
    if not score_category:
        flash('Category is required.', 'error')
        return redirect(_build_filter_url(category, approved))

    try:
        points = int(points_str)
    except ValueError:
        flash('Points must be a valid integer.', 'error')
        return redirect(_build_filter_url(category, approved))

    if not description:
        flash('Description is required.', 'error')
        return redirect(_build_filter_url(category, approved))

    # Determine target teams
    if team_selection == 'all':
        target_teams = Team.query.filter_by(is_active=True).all()
    else:
        try:
            target_team = Team.query.get(int(team_selection))
            if not target_team:
                flash('Selected team not found.', 'error')
                return redirect(_build_filter_url(category, approved))
            target_teams = [target_team]
        except ValueError:
            flash('Invalid team selection.', 'error')
            return redirect(_build_filter_url(category, approved))

    added_count = 0
    for team in target_teams:
        new_score = Score(
            team_id=team.id,
            scorer_username=user.get('username', 'UNKNOWN'),
            category=score_category,
            points=points,
            description=description,
            reference_id=reference_id,
            is_approved=True,
            score_metadata={}
        )
        db.session.add(new_score)
        added_count += 1

        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='manual_score_add',
            entity_type='score',
            entity_id=team.id,
            old_value=None,
            new_value={
                'category': score_category,
                'points': points,
                'description': description,
                'team_number': team.team_number
            }
        )
        db.session.add(audit_entry)

    db.session.commit()

    count_text = f"{added_count} score(s)" if added_count > 1 else "1 score"
    flash(f'Successfully added {count_text}.', 'success')
    return redirect(_build_filter_url(category, approved))


@scores_bp.route('/delete_all', methods=['POST'])
@login_required
@role_required('admin')
def delete_all_scores():
    """Delete all scores from the database."""
    user = session.get('user_info', {})

    # Preserve filters from hidden fields
    category = request.form.get('filter_category', '')
    approved = request.form.get('filter_approved', '')

    confirm = request.form.get('confirm_delete')

    if confirm != 'yes':
        flash('Deletion cancelled. Confirmation required.', 'error')
        return redirect(_build_filter_url(category, approved))

    total_count = Score.query.count()

    if total_count == 0:
        flash('No scores to delete.', 'info')
        return redirect(_build_filter_url(category, approved))

    Score.query.delete()
    db.session.commit()

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='delete_all_scores',
        entity_type='score',
        entity_id=None,
        old_value={'total_count': total_count},
        new_value={'status': 'deleted'}
    )
    db.session.add(audit_entry)
    db.session.commit()

    flash(f'Successfully deleted {total_count} score(s).', 'warning')
    return redirect(_build_filter_url(category, approved))

@scores_bp.route('/delete/<int:score_id>', methods=['POST'])
@login_required
@role_required('admin')
def delete_score(score_id):
    """Delete a single score"""
    user = session.get('user_info', {})
    
    # Preserve filters
    category = request.form.get('filter_category', '')
    approved = request.form.get('filter_approved', '')
    
    score = Score.query.get(score_id)
    if not score:
        flash('Score not found.', 'error')
        return redirect(_build_filter_url(category, approved))
    
    team_number = score.team_id # We need to get team number for logging if needed, or just use team_id
    
    # Get team number for the redirect context (optional, but good for logging)
    team_obj = Team.query.get(score.team_id)
    team_num_display = team_obj.team_number if team_obj else 'Unknown'

    old_data = {
        'points': score.points,
        'category': score.category,
        'description': score.description
    }

    db.session.delete(score)
    db.session.commit()

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='score_delete',
        entity_type='score',
        entity_id=score_id,
        old_value=old_data,
        new_value={'status': 'deleted'}
    )
    db.session.add(audit_entry)
    db.session.commit()

    flash(f'Successfully deleted score ({score.points} pts) for Team {team_num_display}.', 'warning')
    return redirect(_build_filter_url(category, approved))

@scores_bp.route('/loadSampleData', methods=['GET'])
@login_required
@role_required('admin')
def load_sample_data():
    """Hidden route to load sample data for testing. Checks if tables are empty first."""
    user = session.get('user_info', {})
    
    # 1. Check if tables are empty
    team_count = Team.query.count()
    score_count = Score.query.count()
    
    if team_count > 0 or score_count > 0:
        flash('Cannot load sample data: Tables are not empty. Please clear data first.', 'error')
        return redirect(url_for('scores.score_list'))
    
    # 2. Define Sample Data
    # Teams
    sample_teams = [
        {'team_number': 1, 'team_name': 'team01', 'school_name': 'CyberDefense University'},
        {'team_number': 2, 'team_name': 'team02', 'school_name': 'TechGlobal College'},
        {'team_number': 3, 'team_name': 'team03', 'school_name': 'InfoSec College'},
        {'team_number': 4, 'team_name': 'team04', 'school_name': 'Acme University'},
        {'team_number': 5, 'team_name': 'team05', 'school_name': 'University of SecureNet'},
    ]
    
    # Scores (Mapped to team numbers for easier lookup)
    # Note: We will map these to team IDs after inserting teams
    sample_scores_raw = [
        # Team 4
        {'team_num': 4, 'category': 'blue', 'points': 1000, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 4, 'category': 'red', 'points': 450, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 4, 'category': 'white', 'points': 320, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 4, 'category': 'orange', 'points': 180, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 4, 'category': 'green', 'points': 210, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        # Team 1
        {'team_num': 1, 'category': 'blue', 'points': -130, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 1, 'category': 'red', 'points': 310, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 1, 'category': 'white', 'points': 250, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 1, 'category': 'orange', 'points': 130, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 1, 'category': 'green', 'points': 160, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        # Team 2
        {'team_num': 2, 'category': 'blue', 'points': 800, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 2, 'category': 'red', 'points': -1500, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 2, 'category': 'white', 'points': 290, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 2, 'category': 'orange', 'points': 150, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 2, 'category': 'green', 'points': 190, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        # Team 3
        {'team_num': 3, 'category': 'blue', 'points': 200, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 3, 'category': 'red', 'points': 280, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 3, 'category': 'white', 'points': 230, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 3, 'category': 'orange', 'points': -150, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 3, 'category': 'green', 'points': 150, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        # Team 5
        {'team_num': 5, 'category': 'blue', 'points': -3000, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 5, 'category': 'red', 'points': -350, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 5, 'category': 'white', 'points': 270, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 5, 'category': 'orange', 'points': 140, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
        {'team_num': 5, 'category': 'green', 'points': 170, 'desc': 'Sample data loaded', 'ref': 'SAMPLE'},
    ]

    try:
        # 3. Insert Teams
        team_map = {} # Map team_number -> team_id
        for t_data in sample_teams:
            new_team = Team(
                team_number=t_data['team_number'],
                team_name=t_data['team_name'],
                school_name=t_data['school_name'],
                is_active=True
            )
            db.session.add(new_team)
            db.session.flush() # Get ID
            team_map[t_data['team_number']] = new_team.id

        # 4. Insert Scores
        for s_data in sample_scores_raw:
            team_id = team_map.get(s_data['team_num'])
            if not team_id:
                continue
                
            new_score = Score(
                team_id=team_id,
                scorer_username='SampleDataUser',
                category=s_data['category'],
                points=s_data['points'],
                description=s_data['desc'],
                reference_id=s_data['ref'],
                is_approved=False, # Matches your dump (f)
                score_metadata={
                    "loadedBy": "loadSampleData",
                    "testArray": [0, 1, 2, 3],
                    "test2": {
                        "testing": "tested",
                        "testing2": 2,
                        "testing3": [1, 2, 3],
                    }
                }
            )
            db.session.add(new_score)

        # 5. Commit and Log
        db.session.commit()

        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='load_sample_data',
            entity_type='database',
            entity_id=None,
            old_value={'teams_cleared': False, 'scores_cleared': False},
            new_value={'teams_loaded': len(sample_teams), 'scores_loaded': len(sample_scores_raw)}
        )
        db.session.add(audit_entry)
        db.session.commit()

        flash(f'Successfully loaded {len(sample_teams)} teams and {len(sample_scores_raw)} scores.', 'success')
        return redirect(url_for('dashboard'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to load sample data: {str(e)}')
        flash('An error occurred while loading sample data.', 'error')
        return redirect(url_for('scores.score_list'))

@scores_bp.route('/approve-score/<int:score_id>', methods=['POST'])
@login_required
@role_required('admin')
def approve_single_score(score_id):
    """Approve a single score"""
    user = session.get('user_info', {})
    
    # Preserve filters
    category = request.form.get('filter_category', '')
    approved = request.form.get('filter_approved', '')
    
    score = Score.query.get(score_id)
    if not score:
        flash('Score not found.', 'error')
        return redirect(_build_filter_url(category, approved))
    
    if score.is_approved:
        flash('Score is already approved.', 'info')
        return redirect(_build_filter_url(category, approved))

    # Safely get team number
    team_num_display = 'Unknown'
    if score.team_id:
        team_obj = Team.query.get(score.team_id)
        if team_obj:
            team_num_display = team_obj.team_number

    score.is_approved = True
    
    db.session.commit()

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='score_approve',
        entity_type='score',
        entity_id=score_id,
        old_value={'is_approved': False},
        new_value={'is_approved': True}
    )
    db.session.add(audit_entry)
    db.session.commit()

    flash(f'Successfully approved score ({score.points} pts) for Team {team_num_display}.', 'success')
    return redirect(_build_filter_url(category, approved))

@scores_bp.route('/metadata/<int:score_id>', methods=['GET'])
@login_required
@role_required('admin')
def get_score_metadata(score_id):
    """Return metadata for a specific score as JSON"""
    score = Score.query.get(score_id)
    if not score:
        return jsonify({'error': 'Score not found'}), 404
    
    # Return the metadata field (which is a dict/JSON)
    # If it's None, return an empty object
    metadata = score.score_metadata if score.score_metadata else {}
    
    return jsonify(metadata)

@scores_bp.route('/export/json', methods=['GET'])
@login_required
@role_required('admin')
def export_json():
    """Export all scores grouped by team, with raw and calculated sections."""
    user = session.get('user_info', {})

    # --- RAW: all scores (approved + pending), grouped team -> category ---
    all_scores = (
        Score.query
        .options(joinedload(Score.team))
        .order_by(Score.timestamp.asc())
        .all()
    )

    raw = {}
    for score in all_scores:
        team_number = score.team.team_number if score.team else 0
        team_key = f"Team{team_number:02d}"
        raw.setdefault(team_key, {}).setdefault(score.category, []).append({
            'id': score.id,
            'points': score.points,
            'description': score.description,
            'reference_id': score.reference_id,
            'scorer': score.scorer_username,
            'timestamp': score.timestamp.isoformat(),
            'approved': score.is_approved,
        })

    # --- CALCULATED: standings identical to the dashboard ---
    teams_data, _ = calculate_team_standings()

    payload = {}
    for team in teams_data:
        team_key = f"Team{team['team_number']:02d}"
        payload[team_key] = {
            'teamName': team['team_name'],
            'schoolName': team['school_name'],
            'raw': raw.pop(team_key, {}),
            'calculated': {
                'overallPlacement': team['rank'],
                'blue': team['blue'],
                'red': team['red'],
                'white': team['white'],
                'orange': team['orange'],
                'green': team['green'],
                'total': team['total'],
                'redNormalized': team['red_normalized'],
                'orangeNormalized': team['orange_normalized'],
            },
        }

    # Any leftover teams had raw scores but are not active (no standings entry).
    # Keep their raw data for completeness, with calculated set to null.
    for team_key, categories in raw.items():
        payload[team_key] = {
            'raw': categories,
            'calculated': None,
        }

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='export_scores_json',
        entity_type='score',
        entity_id=None,
        old_value=None,
        new_value={'teams': len(payload)}
    )
    db.session.add(audit_entry)
    db.session.commit()

    filename = f"scores_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}Z.json"
    return Response(
        json.dumps(payload, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@scores_bp.route('/export/csv', methods=['GET'])
@login_required
@role_required('admin')
def export_csv():
    """Export all scores as a flat CSV, ordered by time ascending."""
    user = session.get('user_info', {})

    all_scores = (
        Score.query
        .join(Team, Score.team_id == Team.id)
        .options(joinedload(Score.team))
        .order_by(Score.timestamp.asc(), Team.team_number.asc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['team', 'category', 'refid', 'points', 'scorer', 'time'])

    for score in all_scores:
        writer.writerow([
            score.team.team_number if score.team else '',
            score.category,
            score.reference_id or '',
            score.points,
            score.scorer_username,
            score.timestamp.isoformat(),
        ])

    audit_entry = AuditLog(
        username=user.get('username', 'UNKNOWN'),
        action='export_scores_csv',
        entity_type='score',
        entity_id=None,
        old_value=None,
        new_value={'rows': len(all_scores)}
    )
    db.session.add(audit_entry)
    db.session.commit()

    filename = f"scores_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}Z.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )