import os
import yaml
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, make_response, Blueprint
from flask_wtf.csrf import generate_csrf, CSRFProtect
from authlib.integrations.flask_client import OAuth
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from datetime import datetime, timezone
import pytz
import valkey
import json
import logging
from routes.decorators import login_required, role_required, api_key_required
from utils.valkey_client import get_valkey_client
from utils.scoreCalculation import calculate_team_standings, calculate_overall_stats
from routes.quotient import get_celery_task_status as get_quotient_celery_task_status
from routes.redAttack import red_attacks

# Import DB and Models from the models package
from models import db, AuditLog, Team, extAccounts, Score, ApiKey, OrangeTaskInstance, OrangeTaskTemplate
from models.orange_tasks import ORANGE_RATINGS

app = Flask(__name__)

# Initialize CSRF protection
csrf = CSRFProtect(app)

# --- Configuration ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise RuntimeError("SECRET_KEY environment variable is required!")

app.config['VALKEY_HOST'] = os.environ.get('VALKEY_HOST', 'valkey')
app.config['VALKEY_PORT'] = int(os.environ.get('VALKEY_PORT', 6379))
app.config['VALKEY_PASSWORD'] = os.environ.get('VALKEY_PASSWORD')

# SMTP Configuration (passed via .env through docker-compose)
app.config['SMTP_HOST'] = os.environ.get('SMTP_HOST')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['SMTP_USER'] = os.environ.get('SMTP_USER')
app.config['SMTP_PASS'] = os.environ.get('SMTP_PASS')
app.config['SMTP_FROM'] = os.environ.get('SMTP_FROM')
app.config['SMTP_FROM_NAME'] = os.environ.get('SMTP_FROM_NAME', 'Vantage')
app.config['SMTP_REPLY_TO'] = os.environ.get('SMTP_REPLY_TO')
app.config['SMTP_FORCE_CC'] = os.environ.get('SMTP_FORCE_CC')
app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() in ('1', 'true', 'yes', 'on')
app.config['SMTP_TIMEOUT'] = int(os.environ.get('SMTP_TIMEOUT', 10))

# Tell flask we're behind a proxy
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
)

# CSRF Protection
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 hour

# Session cookie security
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Only send over HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # Prevent JavaScript access
    SESSION_COOKIE_SAMESITE='Lax',   # CSRF protection
    PERMANENT_SESSION_LIFETIME=3600  # Idle timeout: 1 hour of inactivity
)

# Absolute session lifetime cap: even continuously active sessions must
# re-authenticate after this many hours (default 16) so a stolen session
# cookie cannot be kept alive indefinitely by ongoing activity.
ABSOLUTE_SESSION_HOURS = int(os.environ.get('ABSOLUTE_SESSION_HOURS', '16'))
ABSOLUTE_SESSION_LIFETIME_SECONDS = ABSOLUTE_SESSION_HOURS * 3600

# Database Configuration
# The URI is passed via environment variable in docker-compose
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI', 'postgresql://postgres:postgres@db:5432/vantage')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,  # Prevents stale connections
}

db.init_app(app)

# Load Config
CONFIG_FILE = 'config.yaml'
if not os.path.exists(CONFIG_FILE):
    raise FileNotFoundError(f"Configuration file {CONFIG_FILE} not found.")

with open(CONFIG_FILE, 'r') as f:
    VANTAGE_CONFIG = yaml.safe_load(f)

app.config['COMPETITION_TITLE'] = VANTAGE_CONFIG.get('competition_title', 'Vantage')

oidc_settings = VANTAGE_CONFIG.get('oidc', {})
ROLE_MAPPING = VANTAGE_CONFIG.get('roles', {})
DEFAULT_ROLE = VANTAGE_CONFIG.get('default_role')

# --- Sensitive Credentials from Environment Variables ---
oidc_settings['client_id'] = os.environ.get('OIDC_CLIENT_ID')
oidc_settings['client_secret'] = os.environ.get('OIDC_CLIENT_SECRET')

if not oidc_settings['client_id']:
    raise ValueError("OIDC_CLIENT_ID environment variable is required!")
if not oidc_settings['client_secret']:
    raise ValueError("OIDC_CLIENT_SECRET environment variable is required!")

# Initialize OAuth
oauth = OAuth(app)

# Register OIDC Provider
oidc = oauth.register(
    name='oidc_provider',
    client_id=oidc_settings['client_id'],
    client_secret=oidc_settings['client_secret'],
    server_metadata_url=oidc_settings.get('discovery_url'),
    client_kwargs={
        'scope': ' '.join(oidc_settings.get('scopes', ['openid', 'profile', 'email', 'groups'])),
        'response_type': 'code',
        'prompt': 'select_account'
    }
)

# Get Quotient status from valkey
def get_valkey_status():
    """Fetch Quotient status from Valkey"""
    try:
        valkey_client = get_valkey_client()
        status_data = valkey_client.get("quotient:status")
        
        if status_data:
            parsed = json.loads(status_data)
            data = json.loads(parsed['data'])
            synced_at = parsed['synced_at']
            
            # Determine running status
            running = data.get('running', False)
            status = "Running" if running else "Paused"
            
            return {
                'synced_at': synced_at,
                'running': running,
                'status': status,
                'raw_data': data
            }
    except Exception as e:
        app.logger.error(f"Failed to fetch Valkey status: {str(e)}")
    
    return None

# --- Role Mapping Logic ---
def get_user_roles(user_groups):
    """
    Maps a list of OIDC groups to a list of internal roles.
    Returns a list of role strings (e.g., ['red', 'white']).
    """
    matched_roles = set()
    user_group_set = set(user_groups)
    
    for role_name, allowed_groups in ROLE_MAPPING.items():
        allowed_group_set = set(allowed_groups)
        # Check for intersection
        if user_group_set & allowed_group_set:
            matched_roles.add(role_name)
    
    # If no roles matched and a default is set, use it
    if not matched_roles and DEFAULT_ROLE:
        matched_roles.add(DEFAULT_ROLE)
        
    return sorted(list(matched_roles))

# --- Routes ---

# Import Routes
from routes import team_bp, ext_accounts_bp, scores_bp, quotient_bp, user_api_keys_bp, api_bp, red_config_bp, orange_config_bp, red_attack_bp, drop_flag_bp, scorecard_bp
app.register_blueprint(team_bp)
app.register_blueprint(ext_accounts_bp)
app.register_blueprint(scores_bp)
app.register_blueprint(quotient_bp)
app.register_blueprint(user_api_keys_bp)
app.register_blueprint(api_bp)
app.register_blueprint(red_config_bp)
app.register_blueprint(orange_config_bp)
app.register_blueprint(red_attack_bp)
app.register_blueprint(drop_flag_bp)
app.register_blueprint(scorecard_bp)

# Exempt from CSRF token requirement
csrf.exempt(api_bp)

# Context processors
from routes.context_processors import competition_title, user_session_data
app.context_processor(competition_title)
app.context_processor(user_session_data)

# Session lifetime management: sliding idle timeout + absolute cap.
# PERMANENT_SESSION_LIFETIME (1 hour) handles the idle timeout, refreshed
# on every request. The absolute cap guarantees re-authentication after
# ABSOLUTE_SESSION_HOURS (default 16) from the original OIDC login, no
# matter how active the session is.
@app.before_request
def enforce_session_limits():
    if 'user_info' not in session:
        return
    
    issued_at = session.get('login_issued_at', 0)
    try:
        age = time.time() - float(issued_at)
    except (TypeError, ValueError):
        age = float('inf')  # missing or corrupt timestamp: treat as expired
    
    if age > ABSOLUTE_SESSION_LIFETIME_SECONDS:
        session.clear()
        flash('Your session has expired. Please log in again.', 'warning')
        return redirect(url_for('home'))
    
    session.permanent = True
    session.modified = True

@app.template_filter('commaformat')
def commaformat_filter(value):
    if value is None:
        return '0'
    return "{:,}".format(int(value))

@app.route('/')
def home():
    """Home page - Public"""
    is_logged_in = 'user_info' in session
    return render_template('login.html', 
                         is_logged_in=is_logged_in)

@app.route('/login')
def login():
    """Initiate OIDC login"""
    next_url = request.args.get('next', url_for('dashboard'))
    # Store next URL in session for callback to use
    session['login_next'] = next_url
    return oidc.authorize_redirect(redirect_uri=oidc_settings.get('redirect_uri'))

@app.route('/callback')
def callback():
    """Handle OIDC callback"""
    try:
        token = oidc.authorize_access_token()
        userinfo = token.get('userinfo')
        
        # Extract groups
        user_groups = userinfo.get('groups', [])
        
        # Determine Roles
        user_roles = get_user_roles(user_groups)
        
        if not user_roles:
            flash('Your account does not have a recognized role. Please contact an administrator.', 'error')
            return redirect(url_for('home'))
        
        # Store user info and roles in session
        session['user_info'] = {
            'sub': userinfo.get('sub'),
            'username': userinfo.get('nickname'),
            'email': userinfo.get('email'),
            'name': userinfo.get('name'),
            'groups': user_groups
        }
        session['user_roles'] = user_roles
        session['login_issued_at'] = time.time()  # anchor for the absolute session cap

        try:
            audit_entry = AuditLog(
                username=userinfo.get('nickname'),
                action='user_login',
                entity_type=None,
                entity_id=None,
                old_value=None,
                new_value={
                    'username': userinfo.get('nickname'),
                    'roles': user_roles,
                    "ip": request.remote_addr
                },
            )
            db.session.add(audit_entry)
            db.session.commit()

            app.logger.info(f'User {session["user_info"]["username"]} logged in, and audit log created.')
        except Exception as e:
            app.logger.error(f"Failed to create audit log for login: {str(e)}")
        
        flash(f'Welcome back, {userinfo.get("name")}! You have access as: {", ".join(r.upper() for r in user_roles)}', 'success')
        
        # Redirect to the original URL if available, otherwise dashboard
        next_url = session.pop('login_next', None)
        if next_url:
            # Security: only allow unambiguous same-site paths. Relative
            # paths start with '/' and must not start with '//' (which
            # browsers resolve as protocol-relative URLs to a foreign host).
            # This rejects absolute URLs entirely — including javascript:,
            # data: and https://evil.com — regardless of scheme or netloc.
            if next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
        
        return redirect(url_for('dashboard'))
    except Exception as e:
        app.logger.error(f"OIDC Callback Error: {str(e)}")
        flash('Authentication failed. Please try again.', 'error')
        return redirect(url_for('home'))

@app.route('/logout')
def logout():
    """Clear session and logout"""
    session.clear()
    response = make_response(redirect(url_for('home')))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    flash('You have been logged out.', 'info')
    return response

# Audit Logs
@app.route('/auditLogs')
@login_required
@role_required('admin', 'red')
def audit_logs():
    """View all audit logs"""
    user = session.get('user_info', {})
    user_roles = session.get('user_roles', [])
    
    # Fetch all audit logs ordered by timestamp descending
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(1000).all()
    
    # Convert timestamps to ISO format for the frontend to handle local time conversion
    # We send UTC timestamps and let the browser convert them to local time
    log_data = []
    for log in logs:
        log_data.append({
            'id': log.id,
            'username': log.username,
            'action': log.action,
            'entity_type': log.entity_type,
            'entity_id': log.entity_id,
            'old_value': log.old_value,
            'new_value': log.new_value,
            'timestamp_utc': log.timestamp.isoformat(), # Send as ISO string
            'timestamp_display': log.timestamp.strftime('%Y-%m-%d %H:%M:%S') # Fallback display
        })
    
    return render_template('auditLogs.html', 
                         is_admin_page=True,
                         logs=log_data)

@app.route('/dashboard')
@login_required
def dashboard():
    """Dashboard page - Protected"""
    quotient_status = get_valkey_status()

    # All scoring math lives in utils/scoreCalculation.py (single source of truth)
    teams_data, calc_stats = calculate_team_standings()
    overall_stats = calculate_overall_stats(teams_data)

    # Read the syncScores config from Valkey
    sync_scores_enabled = False
    try:
        vk = get_valkey_client()
        val = vk.get("quotient:syncScores")
        sync_scores_enabled = val == 'true'
    except Exception:
        pass

    return render_template('dashboard.html',
                         is_dashboard_page=True,
                         teams_data=teams_data,
                         quotient_status=quotient_status,
                         sync_scores_enabled=sync_scores_enabled,
                         quotient_celery_status=get_quotient_celery_task_status(),
                         red_normalization_enabled=calc_stats['red_normalization_stats'].get('enabled', False),
                         red_normalization_stats=calc_stats['red_normalization_stats'],
                         orange_normalization_stats=calc_stats['orange_normalization_stats'],
                         overall_stats=overall_stats)

@app.route('/injectManagement')
@login_required
def injectManagement():
    """Inject management page - Protected"""
    user = session.get('user_info', {})
    user_roles = session.get('user_roles', [])
    
    return render_template('injectManagement.html', 
                         is_config_page=True,
                         ) # Pass roles to template

if __name__ == '__main__':    
    app.run(debug=os.environ.get('FLASK_DEBUG', 'False').lower() == 'true')




# At the very bottom of app.py, after everything else
if os.environ.get('RUN_MAIN') != 'false':
    try:
        from tasks.dropFlag import register_drop_flag_tick
        register_drop_flag_tick(interval_seconds=3)
    except Exception as e:
        logging.getLogger(__name__).error(f"Drop flag tick registration failed: {e}")
