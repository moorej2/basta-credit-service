from celery import Celery
from kombu import Queue

from credit_service.config import get_settings

celery_app = Celery("credits", broker=get_settings().broker_url, include=["credit_service.tasks"])
celery_app.conf.update(
    task_queues=(Queue("credits", durable=True),),
    task_default_queue="credits",
    task_default_delivery_mode="persistent",
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=2,
    broker_transport_options={"confirm_publish": True},
    task_publish_retry=False,  # The durable dispatch loop handles publish failures.
)
