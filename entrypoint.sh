#!/bin/sh

# Check if a custom command was passed (for worker or other tasks)
if [ $# -gt 0 ]; then
    echo "Running custom command: $@"
    exec "$@"
fi

# Set defaults if environment variables are not set
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"

echo "Starting Vantage with:"
echo "  Workers: ${GUNICORN_WORKERS}"
echo "  Threads: ${GUNICORN_THREADS}"

# Run database initialization
echo "Initializing database..."
python -c "
from app import app, db
from models import AuditLog

with app.app_context():
    db.create_all()
    print('Database tables created successfully.')
"

# Start Gunicorn with configured workers and threads
exec gunicorn \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS}" \
    --threads "${GUNICORN_THREADS}" \
    --worker-class gthread \
    --timeout 30 \
    --graceful-timeout 30 \
    app:app