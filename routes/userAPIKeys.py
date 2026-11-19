import os
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from routes.decorators import login_required, role_required
from models import db, AuditLog, ApiKey

# Create the blueprint
user_api_keys_bp = Blueprint('user_api_keys', __name__, url_prefix='/userAPIKeys')

# Define expiration options (in hours)
EXPIRATION_OPTIONS = {
    '30min': 0.5,
    '1hour': 1,
    '4hours': 4,
    '24hours': 24,
    '3days': 72,
    '7days': 168,
    '30days': 720
}

@user_api_keys_bp.route('/', methods=['GET'])
@login_required
@role_required('red', 'admin')
def list_keys():
    """List all API keys for the current user"""
    user = session.get('user_info', {})
    username = user.get('username', 'UNKNOWN')
    
    # Fetch keys for the current user that are not revoked
    keys = ApiKey.query.filter_by(created_by=username, is_revoked=False).order_by(ApiKey.created_at.desc()).all()
    
    # Annotate each key with expiration status
    # We use datetime.now(timezone.utc) for the "current time"
    now_utc = datetime.now(timezone.utc)
    
    for key in keys:
        # Handle the timezone mismatch:
        # If key.expires_at is naive (common in DB), we treat it as UTC for comparison
        # by attaching UTC timezone to it temporarily, OR by stripping timezone from now_utc.
        # The safest approach for naive DB columns is to assume they are UTC and make 'now' naive too.
        
        if key.expires_at is None:
            key.is_expired = False
            continue
            
        # If expires_at is naive, make 'now' naive for comparison
        if key.expires_at.tzinfo is None:
            now_compare = now_utc.replace(tzinfo=None)
        else:
            now_compare = now_utc
            
        key.is_expired = key.expires_at <= now_compare
    
    return render_template('userAPIKeys.html', 
                         is_user_page=True,
                         keys=keys)

@user_api_keys_bp.route('/revoke/<int:key_id>', methods=['POST'])
@login_required
@role_required('red', 'admin')
def revoke_key(key_id):
    """Permanently revoke an API key"""
    user = session.get('user_info', {})
    username = user.get('username', 'UNKNOWN')
    
    key = ApiKey.query.get_or_404(key_id)
    
    # Ensure the key belongs to the current user
    if key.created_by != username:
        flash('Unauthorized action.', 'error')
        return redirect(url_for('user_api_keys.list_keys'))
    
    if key.is_revoked:
        flash('Key is already revoked.', 'info')
        return redirect(url_for('user_api_keys.list_keys'))
    
    old_value = {
        'name': key.name,
        'prefix': key.prefix,
        'is_revoked': False
    }
    
    key.is_revoked = True
    
    # Audit Log
    audit_entry = AuditLog(
        username=username,
        action='api_key_revoke',
        entity_type='api_key',
        entity_id=key.id,
        old_value=old_value,
        new_value={'is_revoked': True},
    )
    db.session.add(audit_entry)
    db.session.commit()
    
    flash(f'API key "{key.name}" has been permanently revoked.', 'success')
    return redirect(url_for('user_api_keys.list_keys'))

@user_api_keys_bp.route('/create', methods=['POST'])
@login_required
@role_required('red', 'admin')
def create_key():
    """Create a new API key"""
    user = session.get('user_info', {})
    username = user.get('username', 'UNKNOWN')
    user_roles = session.get('user_roles', [])
    
    name = request.form.get('name')
    expiration_option = request.form.get('expiration')
    
    if not name:
        flash('Key name is required.', 'error')
        return redirect(url_for('user_api_keys.list_keys'))
    
    if not expiration_option or expiration_option not in EXPIRATION_OPTIONS:
        flash('Invalid expiration time selected.', 'error')
        return redirect(url_for('user_api_keys.list_keys'))
    
    # Generate Key
    # 7 random characters + underscore
    raw_prefix = secrets.token_urlsafe(7).replace('-', '').replace('_', '')[:7]
    # Ensure it's alphanumeric for safety
    raw_prefix = ''.join(c for c in raw_prefix if c.isalnum())
    if len(raw_prefix) < 7:
        raw_prefix = raw_prefix + secrets.token_hex(7-len(raw_prefix))
    
    raw_key = f"{raw_prefix}_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    
    # Calculate Expiration
    now = datetime.now(timezone.utc)
    hours = EXPIRATION_OPTIONS[expiration_option]
    expires_at = now + timedelta(hours=hours)
    
    new_key = ApiKey(
        key_hash=key_hash,
        name=name,
        prefix=raw_prefix,
        roles=user_roles, # Copy current user roles
        created_by=username,
        created_at=now,
        expires_at=expires_at,
        is_revoked=False
    )
    
    db.session.add(new_key)
    db.session.commit()
    
    # Audit Log
    audit_entry = AuditLog(
        username=username,
        action='api_key_create',
        entity_type='api_key',
        entity_id=new_key.id,
        old_value=None,
        new_value={
            'name': name,
            'prefix': raw_prefix,
            'expires_at': expires_at.isoformat(),
            'roles': user_roles
        },
    )
    db.session.add(audit_entry)
    db.session.commit()
    
    # Show the key ONCE to the user
    flash(f'Key created successfully! Copy this key now, it will not be shown again: `{raw_key}`', 'success')
    
    return redirect(url_for('user_api_keys.list_keys'))