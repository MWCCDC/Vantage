# celery_app.py
import os
from celery import Celery
from celery.schedules import crontab

# Redis URLs for different purposes
VALKEY_HOST = os.environ.get('VALKEY_HOST', 'valkey')
VALKEY_PORT = os.environ.get('VALKEY_PORT', 6379)
VALKEY_PASSWORD = os.environ.get('VALKEY_PASSWORD', '')

# Broker: /1
broker_url = f'redis://:{VALKEY_PASSWORD}@{VALKEY_HOST}:{VALKEY_PORT}/1'

# Backend: /2  
result_backend = f'redis://:{VALKEY_PASSWORD}@{VALKEY_HOST}:{VALKEY_PORT}/2'

# Initialize Celery
celery = Celery(
    'vantage_runner',
    broker=broker_url,
    backend=result_backend,
    include=[
        'tasks.quotient',
        'tasks.shell_tasks',
        'tasks.dropFlag'
    ]
)

# Worker configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_acks_late=True,  # Re-queue on worker failure
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_max_loop_interval=10,
    
    # RedBeat scheduler configuration
    beat_scheduler='redbeat.RedBeatScheduler',
    redbeat_redis_url=f'redis://:{VALKEY_PASSWORD}@{VALKEY_HOST}:{VALKEY_PORT}/3',

)