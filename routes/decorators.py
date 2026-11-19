# routes/decorators.py
from functools import wraps
from flask import session, redirect, url_for, flash, request, current_app, jsonify, g, abort
from models import db, AuditLog, ApiKey
from datetime import datetime, timezone
import hashlib

def login_required(f):
    """Decorator to protect routes requiring authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_info' not in session:
            # Capture the URL the user was trying to access
            next_url = request.path
            if request.query_string:
                next_url += '?' + request.query_string.decode('utf-8')
            
            try:
                audit_entry = AuditLog(
                    username='UNKNOWN',
                    action='login_required',
                    entity_type=None,
                    entity_id=None,
                    old_value=None,
                    new_value={
                        'path': request.path,
                        'ip': request.remote_addr,
                    },
                )
                db.session.add(audit_entry)
                db.session.commit()
            except Exception as e:
                current_app.logger.error(f"Failed to create audit log for login required: {str(e)}")
            
            flash('Please log in to access this page.', 'warning')
            # Redirect to login with next parameter
            return redirect(url_for('login', next=next_url))
        return f(*args, **kwargs)
    return decorated_function

def role_required(*allowed_roles):
    """Decorator to restrict access to specific roles"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Check login first
            if 'user_info' not in session:
                try:
                    audit_entry = AuditLog(
                        username='UNKNOWN',
                        action='login_required',
                        entity_type=None,
                        entity_id=None,
                        old_value=None,
                        new_value={
                            'path': request.path,
                            'ip': request.remote_addr,
                        },
                    )
                    db.session.add(audit_entry)
                    db.session.commit()
                except Exception as e:
                    current_app.logger.error(f"Failed to create audit log for login required: {str(e)}")
                
                flash('Please log in to access this page.', 'warning')
                return redirect(url_for('home'))
            
            # Check roles
            user_roles = session.get('user_roles', [])
            if not any(role in allowed_roles for role in user_roles):
                try:
                    audit_entry = AuditLog(
                        username=session.get('user_info', {}).get('username', 'UNKNOWN'),
                        action='role_required',
                        entity_type=None,
                        entity_id=None,
                        old_value=None,
                        new_value={
                            'path': request.path,
                            'ip': request.remote_addr,
                        },
                    )
                    db.session.add(audit_entry)
                    db.session.commit()
                except Exception as e:
                    current_app.logger.error(f"Failed to create audit log for role denied: {str(e)}")
                
                current_app.logger.info(f'User {session.get("user_info", {}).get("username", "UNKNOWN")} attempted to access {request.path} without having a required role!')
                flash('Access denied.', 'error')
                return redirect(url_for('dashboard'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def api_key_required(*allowed_roles):
    """Authenticate via X-API-Key header instead of session."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            raw_key = request.headers.get('X-API-Key')
            if not raw_key:
                abort(401, description='Missing X-API-Key header')

            key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
            api_key = ApiKey.query.filter_by(key_hash=key_hash, is_revoked=False).first()

            if not api_key:
                abort(401, description='Invalid API key')

            # --- FIX: Handle timezone-aware vs naive comparison ---
            if api_key.expires_at:
                now_utc = datetime.now(timezone.utc)
                
                # Normalize stored time to naive UTC if it's aware
                if api_key.expires_at.tzinfo is not None:
                    expires_at_naive = api_key.expires_at.replace(tzinfo=None)
                else:
                    expires_at_naive = api_key.expires_at
                
                # Normalize current time to naive UTC
                now_naive = now_utc.replace(tzinfo=None)

                if expires_at_naive < now_naive:
                    abort(401, description='API key expired')

            # Role check (reuses your existing role system)
            if allowed_roles and not any(r in allowed_roles for r in (api_key.roles or [])):
                abort(403, description='Insufficient permissions')

            # Update last_used_at (store as naive UTC to match DB column expectation)
            api_key.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()

            # Attach identity for audit logging
            g.api_key_identity = api_key

            return f(*args, **kwargs)
        return decorated_function
    return decorator