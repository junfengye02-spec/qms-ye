"""Backfill legacy scan events and stock tracking operator snapshots."""

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

import InvenTree.helpers
from stock.models import ScanInboundRecord, ScanStockEvent, StockItemTracking
from users.models import operator_snapshot


class Command(BaseCommand):
    """Perform resumable, high-water bounded audit backfills."""

    help = (
        'Backfill ScanInboundRecord rows into ScanStockEvent and snapshot tracking '
        'operators. Run during the controlled write-stop window.'
    )

    def add_arguments(self, parser):
        """Register command options."""
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--after-pk', type=int, default=0)
        parser.add_argument('--scan-high-water', type=int)
        parser.add_argument('--tracking-high-water', type=int)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        """Backfill in independent batches and print restart checkpoints."""
        batch_size = options['batch_size']
        if batch_size < 1 or batch_size > 10000:
            raise CommandError('--batch-size must be between 1 and 10000')

        scan_high_water = options['scan_high_water']
        if scan_high_water is None:
            scan_high_water = (
                ScanInboundRecord.objects.order_by('-pk')
                .values_list('pk', flat=True)
                .first()
                or 0
            )
        tracking_high_water = options['tracking_high_water']
        if tracking_high_water is None:
            tracking_high_water = (
                StockItemTracking.objects.order_by('-pk')
                .values_list('pk', flat=True)
                .first()
                or 0
            )

        self.stdout.write(
            f'scan_high_water={scan_high_water} '
            f'tracking_high_water={tracking_high_water}'
        )
        scan_count = self._backfill_scans(
            after_pk=options['after_pk'],
            high_water=scan_high_water,
            batch_size=batch_size,
            dry_run=options['dry_run'],
        )
        tracking_count = self._backfill_tracking(
            high_water=tracking_high_water,
            batch_size=batch_size,
            dry_run=options['dry_run'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'legacy_scan_rows={scan_count} tracking_rows={tracking_count}'
            )
        )

    def _backfill_scans(self, *, after_pk, high_water, batch_size, dry_run):
        """Copy legacy inbound rows without changing any stock balance."""
        processed = 0
        cursor = after_pk

        while cursor < high_water:
            rows = list(
                ScanInboundRecord.objects.filter(pk__gt=cursor, pk__lte=high_water)
                .select_related('part', 'stock_item', 'location', 'user')
                .order_by('pk')[:batch_size]
            )
            if not rows:
                break

            with transaction.atomic():
                for row in rows:
                    fingerprint_source = json.dumps(
                        {
                            'legacy': True,
                            'barcode': row.barcode,
                            'operator': row.user_id,
                        },
                        sort_keys=True,
                    ).encode()
                    stock_after = row.stock_item_quantity
                    total_after = row.new_total_stock
                    defaults = {
                        'client_event_id': row.client_event_id,
                        'request_fingerprint': hashlib.sha256(
                            fingerprint_source
                        ).hexdigest(),
                        'fingerprint_version': 'legacy-v0',
                        'operation': ScanStockEvent.Operation.INBOUND,
                        'outcome': ScanStockEvent.Outcome.SUCCESS,
                        'barcode': row.barcode,
                        'barcode_hash': InvenTree.helpers.hash_barcode(row.barcode),
                        'barcode_kind': ScanStockEvent.BarcodeKind.EXTERNAL,
                        'requested_quantity': row.quantity,
                        'applied_quantity': row.quantity,
                        'part': row.part,
                        'part_pk_snapshot': row.part_id,
                        'part_name_snapshot': row.part.name,
                        'part_ipn_snapshot': row.part.IPN or '',
                        'stock_item': row.stock_item,
                        'stock_item_pk_snapshot': row.stock_item_id,
                        'batch_snapshot': row.stock_item.batch or '',
                        'destination_location': row.location,
                        'destination_location_path_snapshot': row.location.pathstring,
                        'stock_item_quantity_before': stock_after - row.quantity,
                        'stock_item_quantity_after': stock_after,
                        'part_total_before': total_after - row.quantity,
                        'part_total_after': total_after,
                        'operator': row.user,
                        'actor_kind': ScanStockEvent.ActorKind.USER,
                        'scanned_at': row.scanned_at,
                        'completed_at': row.scanned_at,
                        **operator_snapshot(row.user),
                    }
                    ScanStockEvent.objects.update_or_create(
                        legacy_inbound_record_pk=row.pk, defaults=defaults
                    )
                    cursor = row.pk
                    processed += 1

                if dry_run:
                    transaction.set_rollback(True)

            self.stdout.write(f'legacy_scan_checkpoint={cursor}')

        return processed

    def _backfill_tracking(self, *, high_water, batch_size, dry_run):
        """Snapshot known tracking users in bounded update batches."""
        processed = 0
        cursor = 0
        while cursor < high_water:
            rows = list(
                StockItemTracking.objects.filter(
                    pk__gt=cursor,
                    pk__lte=high_water,
                    user__isnull=False,
                    operator_user_id_snapshot__isnull=True,
                )
                .select_related('user')
                .order_by('pk')[:batch_size]
            )
            if not rows:
                break

            for row in rows:
                snapshot = operator_snapshot(row.user)
                row.operator_user_id_snapshot = snapshot[
                    'operator_user_id_snapshot'
                ]
                row.operator_username_snapshot = snapshot[
                    'operator_username_snapshot'
                ]
                row.operator_display_name_snapshot = snapshot[
                    'operator_display_name_snapshot'
                ]

            with transaction.atomic():
                StockItemTracking.objects.bulk_update(
                    rows,
                    [
                        'operator_user_id_snapshot',
                        'operator_username_snapshot',
                        'operator_display_name_snapshot',
                    ],
                    batch_size=batch_size,
                )
                if dry_run:
                    transaction.set_rollback(True)

            cursor = rows[-1].pk
            processed += len(rows)
            self.stdout.write(f'tracking_checkpoint={cursor}')

        return processed
