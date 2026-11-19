# routes/redConfig.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from routes.decorators import login_required, role_required
import valkey
import json
from models import db, Score, Team
from utils.valkey_client import get_valkey_client

# Create the blueprint
red_config_bp = Blueprint('red_config', __name__, url_prefix='/redConfig')


def calculate_red_team_scores(scoring_data):
    """
    Calculates team scores based on the current configuration.
    
    Args:
        scoring_data: Dictionary containing scoring configuration (weights, ranges, etc.)
    
    Returns:
        tuple: (list of team dicts sorted by final score desc, dict of normalization stats including average, median, mode, highScore, lowScore)
    """
    if not scoring_data:
        return [], {}

    # Extract config values
    enable_ranges = scoring_data.get('enable_ranges', False)
    attack_weight = scoring_data.get('attack_weight', 1.0) if scoring_data.get('attack_weight') is not None else 1.0
    ir_weight = scoring_data.get('incident_response_weight', 1.0) if scoring_data.get('incident_response_weight') is not None else 1.0
    range_min = scoring_data.get('range', {}).get('min', -100000) if scoring_data.get('range') else -100000
    range_max = scoring_data.get('range', {}).get('max', 100000) if scoring_data.get('range') else 100000

    # Fetch approved scores
    scores = Score.query.filter_by(is_approved=True).all()
    
    if not scores:
        return [], {}

    # Group by team and category
    team_scores = {}
    for score in scores:
        if score.team_id not in team_scores:
            team_scores[score.team_id] = {'red': 0, 'incident-response': 0, 'team_obj': None}
        
        cat = score.category.lower()
        if cat == 'red':
            team_scores[score.team_id]['red'] += score.points
        elif cat == 'incident-response':
            team_scores[score.team_id]['incident-response'] += score.points
    
    # Resolve team objects
    for team_id, data in team_scores.items():
        team_obj = Team.query.get(team_id)
        if team_obj:
            data['team_obj'] = team_obj

    # Build team calculations
    team_calculations = []
    for team_id, data in team_scores.items():
        if not data['team_obj']:
            continue
        
        # Apply weights
        weighted_red = data['red'] * attack_weight if attack_weight != 0 else None
        weighted_ir = data['incident-response'] * ir_weight if ir_weight != 0 else None
        
        # Calculate raw final sum (weighted sum)
        raw_sum_parts = []
        if weighted_red is not None:
            raw_sum_parts.append(weighted_red)
        if weighted_ir is not None:
            raw_sum_parts.append(weighted_ir)
        
        if raw_sum_parts:
            raw_sum = sum(raw_sum_parts)
        else:
            # Both weights are 0, fall back to unweighted sum
            raw_sum = data['red'] + data['incident-response']
        
        team_calculations.append({
            'team_number': data['team_obj'].team_number,
            'team_name': data['team_obj'].team_name,
            'red_score': data['red'],
            'ir_score': data['incident-response'],
            'weighted_red': weighted_red,
            'weighted_ir': weighted_ir,
            'raw_sum': raw_sum,
            'normalized_score': None,
            'normalization_weight': None,
            'final_sum': raw_sum
        })
    
    normalization_stats = {}

    # Apply normalization if ranges are enabled and we have >1 team
    if enable_ranges and len(team_calculations) > 1:
        raw_sums = [t['raw_sum'] for t in team_calculations]
        min_score = min(raw_sums)
        max_score = max(raw_sums)
        
        normalization_stats = {
            'min': min_score,
            'max': max_score,
            'enabled': True
        }
        
        for team in team_calculations:
            if max_score == min_score:
                team['normalized_score'] = None
                team['normalization_weight'] = None  # ADD THIS LINE
            else:
                # Normalize based on the weighted raw_sum
                norm_weight = (team['raw_sum'] - min_score) / (max_score - min_score)
                team['normalization_weight'] = round(norm_weight, 4)  # ADD THIS LINE
                normalized = (norm_weight * (range_max - range_min)) + range_min
                team['normalized_score'] = round(normalized)
            
            if team['normalized_score'] is not None:
                team['final_sum'] = team['normalized_score']
    
    # Sort by Final Sum Descending
    sorted_teams = sorted(team_calculations, key=lambda x: x['final_sum'], reverse=True)
    
    # Calculate statistics if we have teams
    if sorted_teams:
        final_scores = [t['final_sum'] for t in sorted_teams]
        
        # High / Low scores
        high_score = max(final_scores)
        low_score = min(final_scores)
        
        # Average
        avg_final_score = round(sum(final_scores) / len(final_scores), 2)
        
        # Median
        sorted_scores = sorted(final_scores)
        n = len(sorted_scores)
        if n % 2 == 0:
            median_score = round((sorted_scores[n//2 - 1] + sorted_scores[n//2]) / 2, 2)
        else:
            median_score = round(sorted_scores[n//2], 2)
        
        # Mode (most frequent value)
        from collections import Counter
        score_counts = Counter(final_scores)
        max_count = max(score_counts.values())
        modes = [score for score, count in score_counts.items() if count == max_count]
        
        # If all values appear once, there's no mode
        if max_count == 1:
            mode_score = None
        elif len(modes) == 1:
            mode_score = modes[0]
        else:
            # Multiple modes - return the smallest one for consistency
            mode_score = min(modes)
        
        normalization_stats['highScore'] = high_score
        normalization_stats['lowScore'] = low_score
        normalization_stats['average'] = avg_final_score
        normalization_stats['median'] = median_score
        normalization_stats['mode'] = mode_score
    else:
        normalization_stats['highScore'] = 0
        normalization_stats['lowScore'] = 0
        normalization_stats['average'] = 0
        normalization_stats['median'] = 0
        normalization_stats['mode'] = None
    
    return sorted_teams, normalization_stats


@red_config_bp.route('/', methods=['GET'])
@login_required
@role_required('red', 'admin')
def red_config():
    """Red team configuration page"""
    user = session.get('user_info', {})
    user_roles = session.get('user_roles', [])
    
    # --- 1. Fetch Scoring Calculation Config ---
    scoring_key = "vantage:config:scoring:red"
    scoring_data = None
    scoring_error = None
    
    try:
        vk = get_valkey_client()
        raw_scoring = vk.get(scoring_key)
        if raw_scoring:
            scoring_data = json.loads(raw_scoring)
    except Exception as e:
        current_app.logger.error(f"Failed to fetch scoring config: {str(e)}")
        scoring_error = f"Failed to load scoring configuration: {str(e)}"

    # --- 2. Fetch Reference IDs Config ---
    refs_key = "vantage:config:redScoreRefs"
    refs_data = None
    refs_error = None
    
    try:
        vk = get_valkey_client()
        raw_data = vk.get(refs_key)
        if raw_data:
            refs_data = json.loads(raw_data)
    except Exception as e:
        current_app.logger.error(f"Failed to fetch red score refs config: {str(e)}")
        refs_error = f"Failed to load reference configuration: {str(e)}"
    
    # Parse refs into table format
    score_refs = []
    if refs_data and isinstance(refs_data, dict):
        for ref_id, ref_val in refs_data.items():
            if isinstance(ref_val, dict):
                score_refs.append({
                    'ref_id': ref_id,
                    'type': ref_val.get('type', 'unknown'),
                    'desc': ref_val.get('desc', 'No description'),
                    'points': ref_val.get('points', 0)
                })

    # --- 3. Calculate Score Preview using the new function ---
    score_preview = []
    score_preview_error = None
    normalization_stats = {}

    try:
        score_preview, normalization_stats = calculate_red_team_scores(scoring_data)
    except Exception as e:
        current_app.logger.error(f"Failed to calculate score preview: {str(e)}")
        score_preview_error = f"Failed to load score preview!"
        score_preview = []

    return render_template('redConfig.html', 
                         is_config_page=False,
                         red_team_page=True,
                         
                         # Scoring Data
                         scoring_data=scoring_data,
                         scoring_json=json.dumps(scoring_data, indent=2) if scoring_data else '',
                         scoring_key=scoring_key,
                         scoring_error=scoring_error,
                         
                         # Refs Data
                         score_refs=score_refs,
                         refs_json=json.dumps(refs_data, indent=2) if refs_data else '',
                         refs_key=refs_key,
                         refs_error=refs_error,
                         
                         # Score Preview & Stats
                         score_preview=score_preview,
                         score_preview_error=score_preview_error,
                         normalization_stats=normalization_stats
                         )


@red_config_bp.route('/update-scoring', methods=['POST'])
@login_required
@role_required('red', 'admin')
def update_red_scoring():
    """Update red team scoring calculation configuration via form or JSON"""
    user = session.get('user_info', {})
    config_key = "vantage:config:scoring:red"
    
    is_json_mode = 'scoring_config_json' in request.form
    
    if is_json_mode:
        config_json = request.form.get('scoring_config_json', '')
        try:
            parsed_data = json.loads(config_json) if config_json.strip() else {}
            if not isinstance(parsed_data, dict):
                flash('Scoring configuration must be a JSON object.', 'error')
                return redirect(url_for('red_config.red_config'))
        except json.JSONDecodeError as e:
            flash(f'Invalid JSON format: {str(e)}', 'error')
            return redirect(url_for('red_config.red_config'))
    else:
        parsed_data = {
            "enable_ranges": 'enable_ranges' in request.form,
            "attack_weight": float(request.form.get('attack_weight', 1.0)),
            "incident_response_weight": float(request.form.get('ir_weight', 1.0)),
            "range": {
                "min": int(request.form.get('range_min', -100000)),
                "max": int(request.form.get('range_max', 100000))
            }
        }
    
    try:
        vk = get_valkey_client()
        vk.set(config_key, json.dumps(parsed_data, indent=2))
        
        from models import db, AuditLog
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='red_config_update_scoring',
            entity_type='config',
            entity_id=None,
            old_value=None,
            new_value={'config_key': config_key, 'updated': True}
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash('Scoring calculation configuration updated successfully.', 'success')
        
    except Exception as e:
        current_app.logger.error(f"Failed to save scoring config: {str(e)}")
        flash(f'Failed to save configuration: {str(e)}', 'error')
    
    return redirect(url_for('red_config.red_config'))


@red_config_bp.route('/update-refs', methods=['POST'])
@login_required
@role_required('red', 'admin')
def update_red_refs():
    """Update red team scoring reference configuration"""
    user = session.get('user_info', {})
    config_key = "vantage:config:redScoreRefs"
    
    config_json = request.form.get('config_json', '')
    
    try:
        parsed_data = json.loads(config_json) if config_json.strip() else {}
        if not isinstance(parsed_data, dict):
            flash('Reference configuration must be a JSON object.', 'error')
            return redirect(url_for('red_config.red_config'))
            
        vk = get_valkey_client()
        vk.set(config_key, json.dumps(parsed_data, indent=2))
        
        from models import db, AuditLog
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='red_config_update_refs',
            entity_type='config',
            entity_id=None,
            old_value=None,
            new_value={'config_key': config_key, 'updated': True}
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash('Reference IDs configuration updated successfully.', 'success')
        
    except json.JSONDecodeError as e:
        flash(f'Invalid JSON format: {str(e)}', 'error')
    except Exception as e:
        current_app.logger.error(f"Failed to save refs config: {str(e)}")
        flash(f'Failed to save configuration: {str(e)}', 'error')
    
    return redirect(url_for('red_config.red_config'))