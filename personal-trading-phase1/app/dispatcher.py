"""PostgreSQL is the job outbox; Redis is delivery, never the source of truth."""
import logging
import time
from datetime import timedelta
from sqlalchemy import or_, select
from app.celery_app import celery_app
from app.db import session
from app.models import FetchJob, utcnow

log = logging.getLogger(__name__)


def dispatch_once():
    count = 0
    with session() as db:
        clock = utcnow()
        jobs = db.scalars(select(FetchJob).where(
            FetchJob.status.in_(['QUEUED', 'RETRY_WAIT', 'RUNNING']),
            FetchJob.next_attempt_at <= clock,
            or_(FetchJob.last_dispatched_at.is_(None),
                FetchJob.last_dispatched_at < clock - timedelta(minutes=5))
        ).order_by(FetchJob.next_attempt_at).limit(25).with_for_update(skip_locked=True)).all()
        for job in jobs:
            celery_app.send_task('history.fetch', args=[str(job.id)], queue='history')
            job.last_dispatched_at = clock
            count += 1
        db.commit()
    return count


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            count = dispatch_once()
            if count:
                log.info('Queued %d durable historical jobs', count)
        except Exception as exc:
            # Never log exception strings that may contain connection credentials.
            log.warning('Dispatch deferred: %s', type(exc).__name__)
        time.sleep(5)


if __name__ == '__main__':
    main()
