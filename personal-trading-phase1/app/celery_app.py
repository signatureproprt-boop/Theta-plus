from celery import Celery
from app.config import settings

celery_app = Celery('personal_trading', broker=settings().redis_url, include=['app.tasks', 'app.gaps'])
celery_app.conf.update(
    task_default_queue='history',
    task_serializer='json', accept_content=['json'], result_serializer='json',
    task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    broker_transport_options={'visibility_timeout': 900, 'socket_timeout': 3, 'socket_connect_timeout': 3},
    task_publish_retry_policy={'max_retries': 2, 'interval_start': 0, 'interval_step': 1, 'interval_max': 2},
    task_soft_time_limit=150, task_time_limit=180,
    task_publish_retry=True, timezone='UTC', enable_utc=True,
)
