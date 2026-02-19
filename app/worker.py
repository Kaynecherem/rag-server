"""Celery worker for asynchronous document processing.

For MVP, processing is done synchronously in the API routes.
This worker will be used in Phase 2+ for background processing.
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "insurance_rag",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=600,  # 10 min max per task
    task_soft_time_limit=540,  # Soft limit at 9 min
)


@celery_app.task(bind=True, name="process_document")
def process_document_task(self, document_id: str, tenant_id: str):
    """
    Async document processing task.

    Pipeline:
    1. Download PDF from S3
    2. Extract text + chunk
    3. Generate embeddings
    4. Store in Pinecone
    5. Update document status

    TODO: Implement in Phase 2 when moving to async processing.
    """
    self.update_state(state="PROCESSING", meta={"progress": 0})
    # Implementation will be added when migrating to async processing
    pass
