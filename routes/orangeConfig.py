# routes/orangeConfig.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from routes.decorators import login_required, role_required
from models import db, AuditLog, Team, OrangeTaskTemplate, OrangeTaskInstance, Score
from models.orange_tasks import ORANGE_RATINGS
from sqlalchemy import func, distinct
import json
import statistics

# Create the blueprint
orange_config_bp = Blueprint('orange_config', __name__, url_prefix='/orangeConfig')


def get_valkey_client():
    """Get a Valkey connection from the app config"""
    from utils.valkey_client import get_valkey_client as get_vk
    return get_vk()


def get_scoring_config():
    """
    Retrieves scoring configuration from Valkey.
    Falls back to defaults if not set.
    """
    try:
        vk = get_valkey_client()
        config_raw = vk.get("vantage:config:scoring:orange")
        
        if config_raw:
            return json.loads(config_raw)
    except Exception as e:
        current_app.logger.warning(f"Failed to read orange scoring config: {e}")
    
    # Default Configuration
    return {
        'normalize_enabled': False,
        'min_score': 0,
        'max_score': 100,       
        'formula': 'raw_score'
    }


def calculate_orange_team_scores():
    """
    Calculates orange team scores based on the 'orange' category in the Score table.
    
    Logic:
    1. Fetch all scores where category='orange' and is_approved=True.
    2. Group by team_id and sum raw scores.
    3. Determine the actual min and max raw scores across all teams.
    4. If normalization is enabled:
       - Calculate weight: (team_raw - min_team_raw) / (max_team_raw - min_team_raw)
       - Map to configured range: (weight * (config_max - config_min)) + config_min
    5. If normalization is disabled, final score = raw sum.
    """
    config = get_scoring_config()
    
    # 1. Fetch approved orange scores
    scores = Score.query.filter_by(category='orange', is_approved=True).all()
    
    if not scores:
        return [], {
            'total_tasks': 0, 
            'avg_score': 0, 
            'median_score': 0,
            'min_team_score': 0,
            'max_team_score': 0,
            'teams_with_scores': 0,
            'config': config
        }

    # 2. Group by team and sum raw scores
    team_totals = {}
    for score in scores:
        if score.team_id not in team_totals:
            team_totals[score.team_id] = {
                'raw_sum': 0,
                'task_count': 0,
                'team_obj': None
            }
        team_totals[score.team_id]['raw_sum'] += score.points
        team_totals[score.team_id]['task_count'] += 1

    # 3. Resolve Team Objects
    for team_id in team_totals:
        team_obj = Team.query.get(team_id)
        if team_obj:
            team_totals[team_id]['team_obj'] = team_obj

    # 4. Determine Min/Max Raw Scores across all teams
    raw_scores_list = [data['raw_sum'] for data in team_totals.values()]
    actual_min_raw = min(raw_scores_list)
    actual_max_raw = max(raw_scores_list)
    
    # 5. Calculate Final Scores
    team_calculations = []
    
    min_val = float(config.get('min_score', 0))
    max_val = float(config.get('max_score', 100))
    do_normalize = config.get('normalize_enabled', False)
    
    range_config = max_val - min_val

    for team_id, data in team_totals.items():
        if not data['team_obj']:
            continue
            
        raw_score = data['raw_sum']
        final_score = raw_score
        
        if do_normalize:
            # Avoid division by zero if all teams have the exact same score
            if actual_max_raw == actual_min_raw:
                # If everyone is equal, map to the middle of the configured range or just min?
                # Usually, if everyone is equal, they all get the max or the average. 
                # Let's map to the max of the range to reward completion, or min? 
                # Standard practice: If range is 0, everyone gets the max possible normalized score.
                normalized_weight = 1.0
            else:
                # Normalization Weight: (Raw - Min) / (Max - Min)
                normalized_weight = (raw_score - actual_min_raw) / (actual_max_raw - actual_min_raw)
            
            # Map weight to configured range: (weight * range) + min
            final_score = (normalized_weight * range_config) + min_val
            
            # Clamp just in case of floating point errors
            final_score = max(min_val, min(max_val, final_score))

        team_calculations.append({
            'team_number': data['team_obj'].team_number,
            'team_name': data['team_obj'].team_name,
            'raw_sum': raw_score,
            'task_count': data['task_count'],
            'normalized_score': round(final_score, 2) if do_normalize else None,
            'final_score': round(final_score, 2)
        })

    # 6. Sort by Final Score Descending
    sorted_teams = sorted(team_calculations, key=lambda x: x['final_score'], reverse=True)

    # 7. Calculate Stats
    total_tasks = sum(t['task_count'] for t in team_calculations)
    final_scores = [t['final_score'] for t in team_calculations]
    
    avg_final = statistics.mean(final_scores) if final_scores else 0
    median_final = statistics.median(final_scores) if final_scores else 0

    stats = {
        'total_tasks': total_tasks,
        'avg_score': round(avg_final, 2),
        'median_score': round(median_final, 2),
        'min_team_score': actual_min_raw,
        'max_team_score': actual_max_raw,
        'teams_with_scores': len(team_calculations),
        'config': config
    }

    return sorted_teams, stats


@orange_config_bp.route('/', methods=['GET'])
@login_required
@role_required('orange', 'admin')
def orange_config():
    """Orange team configuration page - Task Templates and Scoring"""
    user = session.get('user_info', {})
    
    # Fetch all task templates
    templates = OrangeTaskTemplate.query.order_by(OrangeTaskTemplate.task_type, OrangeTaskTemplate.name).all()
    
    # Calculate score preview using the renamed function
    score_preview, scoring_stats = calculate_orange_team_scores()
    
    # Get current config for the form
    current_config = scoring_stats.pop('config', {})

    return render_template('orangeConfig.html',
                         orange_team_page=True,
                         
                         # Task Templates
                         templates=templates,
                         
                         # Score Preview & Stats
                         score_preview=score_preview,
                         scoring_stats=scoring_stats,
                         
                         # Config Form Values
                         current_config=current_config,
                         
                         # Constants
                         orange_ratings=ORANGE_RATINGS
                         )


@orange_config_bp.route('/template/add', methods=['POST'])
@login_required
@role_required('orange', 'admin')
def add_template():
    """Add a new orange task template"""
    user = session.get('user_info', {})
    
    task_type = request.form.get('task_type', '').strip()
    name = request.form.get('name', '').strip()
    scenario_prompt = request.form.get('scenario_prompt', '').strip()
    points_possible = request.form.get('points_possible', '0')
    outcome_guidelines = request.form.get('outcome_guidelines', '').strip()
    
    if not task_type or not name or not scenario_prompt:
        flash('Task type, name, and scenario prompt are required.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    try:
        points = int(points_possible)
    except ValueError:
        flash('Points must be a valid integer.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    try:
        new_template = OrangeTaskTemplate(
            task_type=task_type,
            name=name,
            scenario_prompt=scenario_prompt,
            points_possible=points,
            outcome_guidelines=outcome_guidelines,
            is_active=True
        )
        
        db.session.add(new_template)
        db.session.commit()
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='orange_template_add',
            entity_type='orange_task_template',
            entity_id=new_template.id,
            old_value=None,
            new_value={
                'task_type': task_type,
                'name': name,
                'points_possible': points
            }
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash(f'Task template "{name}" created successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to create template: {str(e)}')
        flash(f'Failed to create template. Check server logs.', 'error')
    
    return redirect(url_for('orange_config.orange_config'))


@orange_config_bp.route('/template/<int:template_id>/toggle', methods=['POST'])
@login_required
@role_required('orange', 'admin')
def toggle_template(template_id):
    """Toggle template active status"""
    user = session.get('user_info', {})
    
    template = OrangeTaskTemplate.query.get(template_id)
    if not template:
        flash('Template not found.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    template.is_active = not template.is_active
    
    try:
        db.session.commit()
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='orange_template_toggle',
            entity_type='orange_task_template',
            entity_id=template_id,
            old_value={'is_active': not template.is_active},
            new_value={'is_active': template.is_active}
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        status = "activated" if template.is_active else "deactivated"
        flash(f'Template "{template.name}" {status}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to toggle template: {str(e)}')
        flash(f'Failed to update template. Check server logs.', 'error')
    
    return redirect(url_for('orange_config.orange_config'))

@orange_config_bp.route('/template/<int:template_id>/edit-form', methods=['GET'])
@login_required
@role_required('orange', 'admin')
def get_edit_form(template_id):
    """Render the edit form snippet for the modal"""
    template = OrangeTaskTemplate.query.get_or_404(template_id)
    
    # Render just the form part, not the full page
    # We use a string template here to avoid needing a separate file, 
    # or you could create a partial template 'editTemplateForm.html'
    from flask import render_template_string
    
    form_html = render_template_string("""
    <form id="editTemplateForm" action="{{ url_for('orange_config.edit_template', template_id=template.id) }}" method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px;">
            <div>
                <label style="font-weight: 600; display: block; margin-bottom: 5px;">Task Type *</label>
                <input type="text" name="task_type" value="{{ template.task_type }}" required
                       style="width: 100%; padding: 8px; border: 1px solid var(--border-color); border-radius: 4px;">
            </div>
            <div>
                <label style="font-weight: 600; display: block; margin-bottom: 5px;">Template Name *</label>
                <input type="text" name="name" value="{{ template.name }}" required
                       style="width: 100%; padding: 8px; border: 1px solid var(--border-color); border-radius: 4px;">
            </div>
        </div>

        <div style="margin-bottom: 15px;">
            <label style="font-weight: 600; display: block; margin-bottom: 5px;">Scenario Prompt *</label>
            <textarea name="scenario_prompt" required rows="4"
                      style="width: 100%; padding: 8px; border: 1px solid var(--border-color); border-radius: 4px;">{{ template.scenario_prompt }}</textarea>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px;">
            <div>
                <label style="font-weight: 600; display: block; margin-bottom: 5px;">Points Possible</label>
                <input type="number" name="points_possible" value="{{ template.points_possible }}" min="0"
                       style="width: 100%; padding: 8px; border: 1px solid var(--border-color); border-radius: 4px;">
                <small style="color: var(--text-secondary);">Optional reference points</small>
            </div>
            <div>
                <label style="font-weight: 600; display: block; margin-bottom: 5px;">Outcome Guidelines</label>
                <input type="text" name="outcome_guidelines" value="{{ template.outcome_guidelines or '' }}"
                       placeholder="What defines success?"
                       style="width: 100%; padding: 8px; border: 1px solid var(--border-color); border-radius: 4px;">
            </div>
        </div>

        <div style="display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px;">
            <button type="button" class="refresh-btn" onclick="closeEditModal()">Cancel</button>
            <button type="submit" class="login-btn">Save Changes</button>
        </div>
    </form>
    """, template=template)
    
    return form_html


@orange_config_bp.route('/template/<int:template_id>/edit', methods=['POST'])
@login_required
@role_required('orange', 'admin')
def edit_template(template_id):
    """Edit an existing task template"""
    user = session.get('user_info', {})
    
    template = OrangeTaskTemplate.query.get(template_id)
    if not template:
        flash('Template not found.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    # CAPTURE OLD VALUES BEFORE MODIFYING
    old_data = {
        'task_type': template.task_type,
        'name': template.name,
        'points_possible': template.points_possible
    }
    
    task_type = request.form.get('task_type', '').strip()
    name = request.form.get('name', '').strip()
    scenario_prompt = request.form.get('scenario_prompt', '').strip()
    points_possible = request.form.get('points_possible', '0')
    outcome_guidelines = request.form.get('outcome_guidelines', '').strip()
    
    if not task_type or not name or not scenario_prompt:
        flash('Task type, name, and scenario prompt are required.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    try:
        points = int(points_possible)
    except ValueError:
        flash('Points must be a valid integer.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    # NOW modify the template
    template.task_type = task_type
    template.name = name
    template.scenario_prompt = scenario_prompt
    template.points_possible = points
    template.outcome_guidelines = outcome_guidelines
    
    try:
        db.session.commit()
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='orange_template_edit',
            entity_type='orange_task_template',
            entity_id=template_id,
            old_value=old_data,  # Use the captured old values
            new_value={
                'task_type': task_type,
                'name': name,
                'points_possible': points
            }
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash(f'Task template "{name}" updated successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to update template: {str(e)}')
        flash(f'Failed to update template. Check server logs.', 'error')
    
    return redirect(url_for('orange_config.orange_config'))


@orange_config_bp.route('/template/<int:template_id>/delete', methods=['POST'])
@login_required
@role_required('orange', 'admin')
def delete_template(template_id):
    """Delete a task template"""
    user = session.get('user_info', {})
    
    template = OrangeTaskTemplate.query.get(template_id)
    if not template:
        flash('Template not found.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    # Check for existing instances
    instance_count = OrangeTaskInstance.query.filter_by(template_id=template_id).count()
    
    if instance_count > 0:
        flash(f'Cannot delete template: {instance_count} task instance(s) exist.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    template_name = template.name
    
    try:
        db.session.delete(template)
        db.session.commit()
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='orange_template_delete',
            entity_type='orange_task_template',
            entity_id=template_id,
            old_value={'name': template_name},
            new_value={'status': 'deleted'}
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash(f'Task template "{template_name}" deleted successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to delete template: {str(e)}')
        flash(f'Failed to delete template. Check server logs.', 'error')
    
    return redirect(url_for('orange_config.orange_config'))


@orange_config_bp.route('/scoring/update', methods=['POST'])
@login_required
@role_required('orange', 'admin')
def update_scoring_config():
    """Update orange team scoring calculation configuration"""
    user = session.get('user_info', {})
    
    # Get form values
    normalize_enabled = 'normalize_enabled' in request.form
    min_score = float(request.form.get('min_score', '0'))
    max_score = float(request.form.get('max_score', '100'))
    
    # Validation
    if normalize_enabled and max_score <= min_score:
        flash('Max score must be greater than Min score for normalization.', 'error')
        return redirect(url_for('orange_config.orange_config'))
    
    # Set formula string based on normalization status
    formula_str = 'normalized_score' if normalize_enabled else 'raw_score'
    
    config_data = {
        'normalize_enabled': normalize_enabled,
        'min_score': min_score,
        'max_score': max_score,
        'formula': formula_str
    }
    
    try:
        vk = get_valkey_client()
        config_key = "vantage:config:scoring:orange"
        vk.set(config_key, json.dumps(config_data, indent=2))
        
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='orange_scoring_update',
            entity_type='config',
            entity_id=None,
            old_value=None,
            new_value=config_data
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash('Scoring configuration updated successfully.', 'success')
        
    except Exception as e:
        current_app.logger.error(f'Failed to save scoring config: {str(e)}')
        flash(f'Failed to save configuration. Check server logs.', 'error')
    
    return redirect(url_for('orange_config.orange_config'))


@orange_config_bp.route('/scoring/import-export', methods=['GET', 'POST'])
@login_required
@role_required('orange', 'admin')
def scoring_import_export():
    """
    Unified endpoint for JSON import/export.
    GET: Returns current config as JSON.
    POST: Accepts JSON data to update config.
    """
    user = session.get('user_info', {})
    
    if request.method == 'POST':
        # --- IMPORT LOGIC ---
        try:
            json_data = request.get_json()
            
            if not json_data:
                flash('No JSON data provided.', 'error')
                return redirect(url_for('orange_config.orange_config'))
            
            # Validate required fields
            required_fields = ['normalize_enabled', 'min_score', 'max_score']
            for field in required_fields:
                if field not in json_data:
                    flash(f'Missing required field: {field}', 'error')
                    return redirect(url_for('orange_config.orange_config'))
            
            # Validate types
            try:
                min_score = float(json_data['min_score'])
                max_score = float(json_data['max_score'])
                normalize_enabled = bool(json_data['normalize_enabled'])
                
                if max_score <= min_score and normalize_enabled:
                    flash('Max score must be greater than Min score for normalization.', 'error')
                    return redirect(url_for('orange_config.orange_config'))
            except (ValueError, TypeError) as e:
                flash(f'Invalid data type in JSON: {str(e)}', 'error')
                return redirect(url_for('orange_config.orange_config'))
            
            # Set formula string based on normalization status
            formula_str = 'normalized_score' if normalize_enabled else 'raw_score'
            
            # Save to Valkey
            config_data = {
                'normalize_enabled': normalize_enabled,
                'min_score': min_score,
                'max_score': max_score,
                'formula': json_data.get('formula', formula_str)
            }
            
            vk = get_valkey_client()
            vk.set("vantage:config:scoring:orange", json.dumps(config_data, indent=2))
            
            # Audit log
            audit_entry = AuditLog(
                username=user.get('username', 'UNKNOWN'),
                action='orange_scoring_import',
                entity_type='config',
                entity_id=None,
                old_value=None,
                new_value=config_data
            )
            db.session.add(audit_entry)
            db.session.commit()
            
            flash('Scoring configuration imported successfully.', 'success')
            
        except Exception as e:
            current_app.logger.error(f'Failed to import scoring config: {str(e)}')
            flash(f'Import failed. Check server logs.', 'error')
        
        return redirect(url_for('orange_config.orange_config'))
    
    # --- EXPORT LOGIC (GET Request) ---
    config = get_scoring_config()
    return jsonify(config)