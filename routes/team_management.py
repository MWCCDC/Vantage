# routes/team_management.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from models import db, AuditLog
from models.team import Team
from routes.decorators import login_required, role_required
import re

# Create the blueprint
team_bp = Blueprint('team_management', __name__, url_prefix='/teamManagement')

# Permissive email check: catches obvious typos without rejecting valid-but-unusual addresses
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _normalize_emails(raw):
    """
    Turn a raw comma/semicolon separated string into a clean, deduplicated,
    comma-separated string suitable for team_metadata['emails'].

    Returns (normalized_string, invalid_list).
    """
    if not raw:
        return '', []

    # Split on commas and semicolons, strip whitespace, drop empties
    candidates = [part.strip() for part in re.split(r'[,;]', raw) if part.strip()]

    seen = set()
    valid = []
    invalid = []

    for addr in candidates:
        lowered = addr.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        if EMAIL_RE.match(addr):
            valid.append(addr)
        else:
            invalid.append(addr)

    return ', '.join(valid), invalid


@team_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def team_management():
    """Team Management page - Admin only"""
    user = session.get('user_info', {})
    user_roles = session.get('user_roles', [])

    # Fetch all teams ordered by team number
    teams = Team.query.order_by(Team.team_number).all()

    # Pre-compute the emails string per team so the template stays dumb
    team_emails = {}
    for team in teams:
        meta = team.team_metadata or {}
        team_emails[team.id] = meta.get('emails', '') or ''

    return render_template('teamManagement.html',
                         is_config_page=True,
                         teams=teams,
                         team_emails=team_emails)


@team_bp.route('/', methods=['POST'])
@login_required
@role_required('admin')
def team_management_update():
    """Handle team management form submission"""
    user = session.get('user_info', {})
    form_type = request.form.get('form_type')

    try:
        if form_type == 'update_existing':
            # --- Update Existing Teams ---
            team_ids = request.form.getlist('existing_id[]')
            team_names = request.form.getlist('existing_team_name[]')
            school_names = request.form.getlist('existing_school_name[]')
            email_values = request.form.getlist('existing_emails[]')

            updated_count = 0
            invalid_warnings = []

            for i, tid in enumerate(team_ids):
                team = Team.query.get(int(tid))
                if not team:
                    continue

                # Capture the emails value defensively (list lengths should match,
                # but guard against malformed submissions)
                raw_emails = email_values[i] if i < len(email_values) else ''
                normalized_emails, invalid = _normalize_emails(raw_emails)
                if invalid:
                    invalid_warnings.append(
                        f'Team {team.team_number}: {", ".join(invalid)}'
                    )

                # Snapshot old state BEFORE any mutation
                old_meta = dict(team.team_metadata or {})
                old_data = {
                    'team_number': team.team_number,
                    'team_name': team.team_name,
                    'school_name': team.school_name,
                    'is_active': team.is_active,
                    'emails': old_meta.get('emails', ''),
                }

                team.team_name = team_names[i]
                team.school_name = school_names[i] or None
                # Checkbox: if present in form data, it was checked; if absent, unchecked
                team.is_active = f'existing_is_active_{tid}' in request.form

                # Reassign a NEW dict so SQLAlchemy detects the change
                # (works with or without MutableDict on the column)
                new_meta = dict(team.team_metadata or {})
                if normalized_emails:
                    new_meta['emails'] = normalized_emails
                else:
                    new_meta.pop('emails', None)
                team.team_metadata = new_meta

                audit_entry = AuditLog(
                    username=user.get('username', 'UNKNOWN'),
                    action='team_update',
                    entity_type='team',
                    entity_id=team.id,
                    old_value=old_data,
                    new_value={
                        'team_number': team.team_number,
                        'team_name': team.team_name,
                        'school_name': team.school_name,
                        'is_active': team.is_active,
                        'emails': new_meta.get('emails', ''),
                    },
                )
                db.session.add(audit_entry)
                updated_count += 1

            db.session.commit()
            flash(f'{updated_count} team(s) updated successfully.', 'success')

            if invalid_warnings:
                flash(
                    'Some addresses looked invalid and were dropped — '
                    + ' | '.join(invalid_warnings),
                    'warning'
                )

        elif form_type == 'add_new':
            # --- Add New Teams ---
            team_numbers = request.form.getlist('new_team_number[]')
            team_names = request.form.getlist('new_team_name[]')
            school_names = request.form.getlist('new_school_name[]')
            email_values = request.form.getlist('new_emails[]')

            created_count = 0
            invalid_warnings = []

            for i, num in enumerate(team_numbers):
                # Skip rows where team number or name is empty
                if not num or not team_names[i]:
                    continue

                # Checkbox: row 0 uses new_is_active_0, JS-added rows use new_is_active_1, etc.
                is_active = f'new_is_active_{i}' in request.form

                raw_emails = email_values[i] if i < len(email_values) else ''
                normalized_emails, invalid = _normalize_emails(raw_emails)
                if invalid:
                    invalid_warnings.append(
                        f'Team {num}: {", ".join(invalid)}'
                    )

                new_team = Team(
                    team_number=int(num),
                    team_name=team_names[i],
                    school_name=school_names[i] or None,
                    is_active=is_active,
                    team_metadata={'emails': normalized_emails} if normalized_emails else {},
                )
                db.session.add(new_team)
                db.session.flush()  # Get the ID before logging

                audit_entry = AuditLog(
                    username=user.get('username', 'UNKNOWN'),
                    action='team_create',
                    entity_type='team',
                    entity_id=new_team.id,
                    old_value=None,
                    new_value={
                        'team_number': new_team.team_number,
                        'team_name': new_team.team_name,
                        'school_name': new_team.school_name,
                        'is_active': new_team.is_active,
                        'emails': normalized_emails,
                    },
                )
                db.session.add(audit_entry)
                created_count += 1

            db.session.commit()
            flash(f'{created_count} new team(s) added successfully.', 'success')

            if invalid_warnings:
                flash(
                    'Some addresses looked invalid and were dropped — '
                    + ' | '.join(invalid_warnings),
                    'warning'
                )

        else:
            flash('Invalid form submission.', 'error')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Team management update error: {str(e)}')
        flash('An error occurred while updating teams. Please try again.', 'error')

    return redirect(url_for('team_management.team_management'))