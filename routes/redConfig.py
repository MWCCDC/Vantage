# routes/redConfig.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from routes.decorators import login_required, role_required
import valkey
import json
from models import db, Score, Team
from utils.valkey_client import get_valkey_client
from utils.scoreCalculation import calculate_red_team_scores

# Create the blueprint
red_config_bp = Blueprint('red_config', __name__, url_prefix='/redConfig')


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