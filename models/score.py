# models/score.py
from . import db
from sqlalchemy import func, Index

class Score(db.Model):
    __tablename__ = 'scores'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False, index=True)    
    scorer_username = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False, index=True)
    points = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text, nullable=False)
    reference_id = db.Column(db.String(100), index=True)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp(), index=True)
    is_approved = db.Column(db.Boolean, default=True, index=True)
    score_metadata = db.Column('metadata', db.JSON)
    
    # Composite indexes
    __table_args__ = (
        Index('idx_score_team_category', 'team_id', 'category'),
        Index('idx_score_category_timestamp', 'category', 'timestamp'),
        Index('idx_score_team_timestamp', 'team_id', 'timestamp'),
        Index('idx_score_approved', 'is_approved', 'team_id'),
    )
    
    def __repr__(self):
        return f'<Score {self.points}pts for Team {self.team_id} ({self.category})>'