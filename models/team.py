# models/team.py
from . import db

class Team(db.Model):
    __tablename__ = 'teams'
    
    id = db.Column(db.Integer, primary_key=True)
    team_number = db.Column(db.Integer, unique=True, nullable=False, index=True)
    team_name = db.Column(db.String(100), nullable=False)
    school_name = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    team_metadata = db.Column('metadata', db.JSON)
    
    # Relationships
    scores = db.relationship('Score', backref='team', lazy='dynamic', cascade='all, delete-orphan')
    ext_accounts = db.relationship('extAccounts', backref='team', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Team {self.team_number} - {self.team_name}>'