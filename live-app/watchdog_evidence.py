"""Scope a recorded scheduler-recovery acceptance result conservatively."""
from datetime import datetime,timedelta,timezone
RESOURCE='projects/theta-pluse/locations/asia-south1/jobs/theta-plus-collector'


def verified(report,clock=None):
    if not (report.get('verified') is True and report.get('basis')=='natural-scheduler-restart-v1'
            and report.get('worker')==RESOURCE and report.get('restart_observed') is True
            and report.get('checkpoint_verified') is True and isinstance(report.get('checkpoints_checked'),int)
            and not isinstance(report.get('checkpoints_checked'),bool) and report['checkpoints_checked']>0):return False
    try:
        at=datetime.fromisoformat(report['checked_at']);clock=clock or datetime.now(timezone.utc)
        return at.tzinfo is not None and timedelta(seconds=-5)<=clock-at<=timedelta(days=7)
    except (KeyError,TypeError,ValueError):return False
