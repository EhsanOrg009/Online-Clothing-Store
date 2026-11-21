"""Shared scheduler fencing and durable last-run status, without HTTP locks."""

from dataclasses import dataclass
from datetime import timezone as datetime_timezone
import logging
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from .models import PaymentMaintenanceJob
from .services import PaymentError
from .torobpay_services import _unlocked, reconciliation_lease_duration


logger = logging.getLogger(__name__)
JOB_NAME = "torobpay_and_order_expiration"


def expiration_due(last_completed_at, *, now=None):
    """UTC 00/05/10/... minute slots; completion seconds must not add drift."""
    now = now or timezone.now()
    if last_completed_at is None:
        return True

    def slot(moment):
        utc = moment.astimezone(datetime_timezone.utc)
        return utc.replace(minute=utc.minute // 5 * 5, second=0, microsecond=0)

    return slot(now) > slot(last_completed_at)


class MaintenanceLeaseLost(PaymentError):
    def __init__(self):
        super().__init__("مالکیت اجرای نگهداری پرداخت تغییر کرده است؛ ادامهٔ اجرای قبلی مجاز نیست.")


@dataclass(frozen=True)
class MaintenanceLease:
    lease_id: UUID

    @transaction.atomic
    def pulse(self):
        job = PaymentMaintenanceJob.objects.select_for_update().get(pk=JOB_NAME)
        now = timezone.now()
        if job.lease_id != self.lease_id or not job.lease_expires_at or now >= job.lease_expires_at:
            raise MaintenanceLeaseLost()
        job.lease_expires_at = now + reconciliation_lease_duration()
        job.save(update_fields=["lease_expires_at"])

    @transaction.atomic
    def finish(self, *, success, summary, error="", expiration_completed=False):
        job = PaymentMaintenanceJob.objects.select_for_update().get(pk=JOB_NAME)
        if job.lease_id != self.lease_id:
            raise MaintenanceLeaseLost()
        now = timezone.now()
        job.lease_id = job.lease_expires_at = None
        job.last_finished_at, job.last_error, job.summary = now, error, summary
        if success:
            job.last_success_at = now
        if expiration_completed:
            job.last_expiration_at = now
        job.save()


def claim_maintenance_job():
    _unlocked()
    with transaction.atomic():
        PaymentMaintenanceJob.objects.get_or_create(name=JOB_NAME)
        job = PaymentMaintenanceJob.objects.select_for_update().get(pk=JOB_NAME)
        now = timezone.now()
        if job.lease_id and job.lease_expires_at and now < job.lease_expires_at:
            return job, None
        recovered = bool(job.lease_id)
        job.lease_id = uuid4()
        job.lease_expires_at = now + reconciliation_lease_duration()
        job.last_started_at = now
        job.save(update_fields=["lease_id", "lease_expires_at", "last_started_at"])
    logger.info("Payment maintenance claimed lease_id=%s recovered=%s", job.lease_id, recovered)
    return job, MaintenanceLease(job.lease_id)


def maintenance_status():
    """Read only; no job creation, HTTP or payment transitions."""
    job = PaymentMaintenanceJob.objects.filter(pk=JOB_NAME).first()
    if job is None:
        return {"job": JOB_NAME, "has_run": False, "state": "not_started"}
    now = timezone.now()
    return {"job": JOB_NAME, "has_run": True,
        "state": "running" if job.lease_id and job.lease_expires_at and now < job.lease_expires_at else "idle",
        **{name: getattr(job, name).isoformat() if getattr(job, name) else None for name in (
            "lease_expires_at", "last_started_at", "last_finished_at", "last_success_at", "last_expiration_at")},
        "last_error": job.last_error, "summary": job.summary}
