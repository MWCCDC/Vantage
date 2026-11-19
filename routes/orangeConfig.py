# routes/orangeConfig.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from routes.decorators import login_required, role_required
from models import db, AuditLog, Team, OrangeTaskTemplate, OrangeTaskInstance, Score
from models.orange_tasks import ORANGE_RATINGS
import json
from utils.valkey_client import get_valkey_client
from utils.scoreCalculation import calculate_orange_team_scores, get_orange_scoring_config

# Create the blueprint
orange_config_bp = Blueprint('orange_config', __name__, url_prefix='/orangeConfig')

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
    config = get_orange_scoring_config()
    return jsonify(config)