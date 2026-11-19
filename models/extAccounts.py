# models/extAccounts.py
from . import db
from sqlalchemy import func, Index

class extAccounts(db.Model):
    __tablename__ = 'ext_accts'

    id = db.Column(db.Integer, primary_key=True)
    team_number = db.Column(db.Integer, db.ForeignKey('teams.team_number'), nullable=False, index=True)
    account_name = db.Column(db.String(100), nullable=False, index=True)
    account_url = db.Column(db.String(500))  # Increased for longer URLs
    account_username = db.Column(db.String(100), nullable=False)
    account_password = db.Column(db.String(255), nullable=False)  # Increased for hashed passwords
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    
    # Composite unique constraint
    __table_args__ = (
        db.UniqueConstraint('team_number', 'account_name', 'account_username', name='uq_team_account_username'),
        Index('idx_ext_team_account', 'team_number', 'account_name'),
    )
    
    def __repr__(self):
        return f'<extAccounts {self.account_username} for Team {self.team_number}>'