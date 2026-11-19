# models/orange_tasks.py
from . import db
from sqlalchemy import Index, CheckConstraint

# Rating scale constants
ORANGE_RATINGS = {
    1: 'No Response',
    2: 'Poor',
    3: 'Fair',
    4: 'Good',
    5: 'Excellent'
}


class OrangeTaskTemplate(db.Model):
    """Reusable orange team task scenario templates"""
    __tablename__ = 'orange_task_templates'

    id = db.Column(db.Integer, primary_key=True)
    task_type = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(200), nullable=False)  # Human-readable task name
    scenario_prompt = db.Column(db.Text, nullable=False)
    points_possible = db.Column(db.Integer, nullable=False, default=0)
    outcome_guidelines = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

    # Relationship to instances
    instances = db.relationship('OrangeTaskInstance', back_populates='template', lazy='dynamic')

    def __repr__(self):
        return f'<OrangeTaskTemplate {self.name} - {self.points_possible}pts>'


class OrangeTaskInstance(db.Model):
    """Individual orange team task instances for specific teams"""
    __tablename__ = 'orange_task_instances'

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('orange_task_templates.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    scheduled_start = db.Column(db.DateTime, nullable=False)
    claimed_by = db.Column(db.String(100))
    completed_at = db.Column(db.DateTime)
    resolution_notes = db.Column(db.Text)
    outcome_rating = db.Column(db.Integer)  # 1-5 scale (see ORANGE_RATINGS)
    score_id = db.Column(db.Integer, db.ForeignKey('scores.id'), nullable=True)
    instance_metadata = db.Column('metadata', db.JSON)

    # Relationships
    template = db.relationship('OrangeTaskTemplate', back_populates='instances')
    team = db.relationship('Team', backref='orange_task_instances')
    score = db.relationship('Score', backref='orange_task_instance')

    # Indexes and constraints
    __table_args__ = (
        Index('idx_orange_available', 'claimed_by', 'completed_at', 'scheduled_start'),
        Index('idx_orange_team', 'team_id'),
        Index('idx_orange_template', 'template_id'),
        CheckConstraint(
            'outcome_rating IS NULL OR (outcome_rating BETWEEN 1 AND 5)',
            name='chk_orange_outcome_rating'
        ),
    )

    @property
    def is_claimed(self):
        return self.claimed_by is not None

    @property
    def is_completed(self):
        return self.completed_at is not None

    @property
    def is_available(self):
        return not self.is_claimed and not self.is_completed

    @property
    def rating_label(self):
        if self.outcome_rating is None:
            return None
        return ORANGE_RATINGS.get(self.outcome_rating, 'Unknown')

    def __repr__(self):
        status = "completed" if self.is_completed else ("claimed" if self.is_claimed else "available")
        return f'<OrangeTaskInstance Team {self.team_id} - {self.template.name} ({status})>'