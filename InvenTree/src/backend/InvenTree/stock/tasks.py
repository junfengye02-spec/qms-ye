"""Background tasks for the stock app."""

from datetime import datetime, timedelta

from django.db import connection, transaction

import structlog
from opentelemetry import trace

from common.settings import get_global_setting
import InvenTree.helpers
from InvenTree.tasks import ScheduledTask, scheduled_task

tracer = trace.get_tracer(__name__)
logger = structlog.get_logger('inventree')


@tracer.start_as_current_span('expire_scan_stock_approval_requests')
@scheduled_task(ScheduledTask.MINUTES, 1)
def expire_scan_stock_approval_requests(batch_size: int = 100):
    """Expire due drafts/submissions in bounded, idempotent locked batches."""
    from stock.models import ScanStockApprovalRequest
    from stock.services import ApprovalRequestService

    # Match the project's USE_TZ mode. Test deployments deliberately use naive
    # datetimes, while normal deployments use timezone-aware values.
    now = InvenTree.helpers.current_time()
    processed = 0

    while True:
        with transaction.atomic():
            queryset = ScanStockApprovalRequest.objects.filter(
                status__in=ScanStockApprovalRequest.active_statuses(),
                expires_at__lte=now,
            ).order_by('expires_at', 'pk')
            if connection.features.has_select_for_update_skip_locked:
                queryset = queryset.select_for_update(skip_locked=True)
            else:
                queryset = queryset.select_for_update()
            request_ids = list(queryset.values_list('pk', flat=True)[:batch_size])

            if not request_ids:
                break

            # Keep selected rows locked until each terminal update completes.
            batch_processed = 0
            for request_id in request_ids:
                if ApprovalRequestService.expire(request_id):
                    processed += 1
                    batch_processed += 1

        if batch_processed == 0:
            break

    if processed:
        logger.info('Expired scan stock approval requests', count=processed)
    return processed


@tracer.start_as_current_span('delete_old_stock_tracking')
@scheduled_task(ScheduledTask.DAILY)
def delete_old_stock_tracking():
    """Remove old stock tracking entries before a certain date."""
    from stock.models import StockItemTracking

    if not get_global_setting('STOCK_TRACKING_DELETE_OLD_ENTRIES', False):
        return

    delete_n_days = int(get_global_setting('STOCK_TRACKING_DELETE_DAYS', 365))

    threshold = datetime.now() - timedelta(days=delete_n_days)

    old_entries = StockItemTracking.objects.filter(date__lte=threshold)

    if old_entries.exists():
        logger.info(
            'Deleting old stock tracking entries',
            count=old_entries.count(),
            threshold=threshold,
        )
        old_entries.delete()
