# routes/context_processors.py
from flask import current_app, session

def competition_title():
    """Make competition title available to all templates"""
    return {'compTitle': current_app.config.get('COMPETITION_TITLE', 'Vantage')}

def user_session_data():
    """Provide user session data to all templates"""
    user_info = session.get('user_info', {})
    user_roles = session.get('user_roles', [])
    
    return {
        'user_name': user_info.get('name', ''),
        'user_email': user_info.get('email', ''),
        'user_roles': user_roles,
        'is_logged_in': bool(user_info)
    }