"""Fresh-database upgrade rehearsal for historical WMS stock and approval audit."""

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag


@tag('migration_test')
class ContractUpgradeTest(TransactionTestCase):
    """Add nullable links without guessing a contract or changing historic quantities."""

    def test_project_archive_links_existing_records_without_changing_labels(self):
        """Existing stock and preparations share one archive, preserving text, UUID and quantity."""
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [
            (app, '0135_complete_label_length' if app == 'stock' else name)
            for app, name in latest
        ]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            project = apps.get_model('common', 'ProjectCode').objects.create(
                code='00123'
            )
            part = apps.get_model('part', 'Part').objects.create(
                name='Project migration equipment',
                IPN='MIGRATION-PROJECT',
                level=0,
                tree_id=0,
                lft=0,
                rght=0,
            )
            location = apps.get_model('stock', 'StockLocation').objects.create(
                name='Migration warehouse', level=0, tree_id=0, lft=0, rght=0
            )
            contract = apps.get_model('stock', 'WmsContract').objects.create(
                number='MIGRATION-HT'
            )
            receipt = apps.get_model('stock', 'WmsReceipt').objects.create(
                project='00123', contract=contract, location=location
            )
            detail = apps.get_model('stock', 'WmsReceiptDetail').objects.create(
                receipt=receipt,
                part=part,
                project='00123',
                contract=contract,
                prepared_quantity=17,
            )
            item = apps.get_model('stock', 'StockItem').objects.create(
                part=part, quantity=17, batch='00123', source_detail=detail
            )
            other = apps.get_model('stock', 'StockItem').objects.create(
                part=part, quantity=2, batch='历史另一个项目'
            )
            uid, version = detail.uid, detail.label_version
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            apps = executor.loader.project_state(latest).apps
            for name, pk in (
                ('WmsReceipt', receipt.pk),
                ('WmsReceiptDetail', detail.pk),
                ('StockItem', item.pk),
            ):
                row = apps.get_model('stock', name).objects.get(pk=pk)
                self.assertEqual(row.project_code_id, project.pk)
            detail = apps.get_model('stock', 'WmsReceiptDetail').objects.get(
                pk=detail.pk
            )
            item = apps.get_model('stock', 'StockItem').objects.get(pk=item.pk)
            other = apps.get_model('stock', 'StockItem').objects.get(pk=other.pk)
            self.assertEqual(
                (detail.project, detail.uid, detail.label_version),
                ('00123', uid, version),
            )
            self.assertEqual((item.batch, item.quantity), ('00123', 17))
            self.assertEqual(other.project_code.code, '历史另一个项目')
            self.assertEqual(
                apps.get_model('common', 'ProjectCode')
                .objects.filter(code='00123')
                .count(),
                1,
            )
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_upgrade_preserves_existing_values(self):
        """Existing project, quantity, audit and null contract association survive."""
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [
            (
                app,
                '0133_scanstockevent_approver_snapshot_and_more'
                if app == 'stock'
                else name,
            )
            for app, name in latest
        ]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            part = apps.get_model('part', 'Part').objects.create(
                name='Historic equipment',
                IPN='HISTORIC',
                level=0,
                tree_id=0,
                lft=0,
                rght=0,
            )
            stock_pk = (
                apps.get_model('stock', 'StockItem')
                .objects.create(part=part, quantity=17, batch='历史项目')
                .pk
            )
            event_pk = (
                apps.get_model('stock', 'ScanStockEvent')
                .objects.create(
                    client_event_id=uuid.uuid4(),
                    request_fingerprint='migration-test',
                    operation='INBOUND',
                    outcome='FAILED',
                    barcode='HISTORIC',
                    barcode_hash='migration-test',
                    requested_quantity=3,
                    batch_snapshot='历史项目',
                )
                .pk
            )
            executor = MigrationExecutor(connection)
            after = latest
            executor.migrate(after)
            apps = executor.loader.project_state(after).apps
            item = apps.get_model('stock', 'StockItem').objects.get(pk=stock_pk)
            event = apps.get_model('stock', 'ScanStockEvent').objects.get(pk=event_pk)
            self.assertEqual(item.quantity, 17)
            self.assertEqual(item.batch, '历史项目')
            self.assertIsNone(item.source_detail_id)
            self.assertEqual(event.batch_snapshot, '历史项目')
            self.assertEqual(event.outcome, 'FAILED')
            self.assertEqual(event.contract_number_snapshot, '')
            self.assertFalse(apps.get_model('stock', 'WmsContract').objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
