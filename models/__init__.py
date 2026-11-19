# models/__init__.py
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

# Initialize the DB object here so it can be imported by other modules
db = SQLAlchemy()

# Import models to ensure they are registered with the db instance
# This prevents circular import issues
from .audit import AuditLog
from .team import Team
from .extAccounts import extAccounts
from .score import Score
from .apiKey import ApiKey
from .orange_tasks import ORANGE_RATINGS, OrangeTaskInstance, OrangeTaskTemplate

__all__ = ['db', 'AuditLog', 'Team', 'extAccounts', 'Score', 'ApiKey', 'ORANGE_RATINGS', 'OrangeTaskInstance', 'OrangeTaskTemplate']