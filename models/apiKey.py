from . import db

class ApiKey(db.Model):
    __tablename__ = 'api_keys'

    id = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(128), nullable=False, unique=True)  # never store plaintext
    name = db.Column(db.String(128), nullable=False)       # human-readable label
    prefix = db.Column(db.String(8), nullable=False)       # e.g. "vnt_" for display/identification
    roles = db.Column(db.JSON, default=list)               # same role system you already have
    created_by = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=True)     # optional expiration
    last_used_at = db.Column(db.DateTime, nullable=True)
    is_revoked = db.Column(db.Boolean, default=False)