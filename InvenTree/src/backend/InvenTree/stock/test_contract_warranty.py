"""Acceptance of complete receipt labels, inheritance, stock identity and audit boundaries."""

import json
import threading
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from rest_framework.exceptions import ValidationError as ApiValidationError
from rest_framework.test import APIClient

from common.models import ProjectCode
from common.settings import set_global_setting
from part.models import Part
from stock.models import (
    ScanStockEvent,
    StockItem,
    StockLocation,
    WmsContract,
    WmsReceipt,
    WmsReceiptDetail,
)
from stock.services import ApprovalRequestService, ScanApprovalError, ScanStockService
from stock.warranty import (
    encode_label,
    label_payload,
    parse_label,
    resolve_label,
    warranty_end,
)
from users.models import Owner


class ContractWarrantyTest(TestCase):
    """Drive public operator APIs through the existing approval stock service."""

    def setUp(self):
        """Use separate requester and approver with a real approval owner."""
        self.user = User.objects.create_superuser(
            'receipt-user', 'receipt@example.test', 'test'
        )
        self.approver = User.objects.create_superuser(
            'receipt-approver', 'approver@example.test', 'test'
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.location = StockLocation.objects.create(
            name='合同库位', approval_owner=Owner.get_owner(self.approver)
        )
        self.part = Part.objects.create(name='设备 中文', IPN='设备-01', units='count')
        self.contract = WmsContract.objects.create(
            number=' 合同 / A " 特殊 '.strip(),
            acceptance_date=date(2024, 2, 29),
            warranty_months=12,
        )
        set_global_setting('SCAN_APPROVAL_ENABLED', True)
        for code in ('项目 / 一 " 空格', '新项目', '其他', '调整项目', '改归属'):
            ProjectCode.objects.get_or_create(code=code)

    def post(self, path, values, expected=200):
        """Assert API success with the complete business response on failure."""
        response = self.client.post('/api/stock/' + path, values, format='json')
        self.assertEqual(response.status_code, expected, response.data)
        return response.data

    def prepare(self, **overrides):
        """Save a single ten-unit line without stock effects."""
        values = {
            'request_key': str(uuid.uuid4()),
            'project': '项目 / 一 " 空格',
            'contract': self.contract.pk,
            'location': self.location.pk,
            'details': [{'part': self.part.pk, 'prepared_quantity': 10, **overrides}],
        }
        row = self.post('wms/receipts/', values, 201)
        return WmsReceiptDetail.objects.get(pk=row['details'][0]['pk'])

    def request(self, direction='INBOUND', location=None):
        """Create one draft with native ownership and expiry rules."""
        return ApprovalRequestService.create_request(
            user=self.user, direction=direction, location=location or self.location
        )

    def scan(self, detail, request, quantity=10, **overrides):
        """Scan a complete label with no client project or contract fields."""
        direction = request.direction.lower()
        values = {
            'barcode': encode_label(detail),
            'client_event_id': str(uuid.uuid4()),
            'quantity': quantity,
            'approval_request_id': request.pk,
            'receipt_id': detail.receipt_id,
            'destination_location'
            if direction == 'inbound'
            else 'source_location': request.location_id,
            'purpose': '安装',
            **overrides,
        }
        return self.client.post(f'/api/stock/scan/{direction}/', values, format='json')

    def receive(self, detail, quantity=10):
        """Approve exactly one inbound and return its source-linked stock."""
        request = self.request()
        response = self.scan(detail, request, quantity, quantity_confirmed=True)
        self.assertEqual(response.status_code, 201, response.data)
        ApprovalRequestService.submit(request_id=request.pk, user=self.user)
        ApprovalRequestService.approve(request_id=request.pk, user=self.approver)
        return StockItem.objects.get(source_detail=detail), request

    def test_calendar_months_and_protocol_boundaries(self):
        """Leap days and month ends clamp; missing acceptance remains absent."""
        self.assertEqual(warranty_end(date(2024, 2, 29), 12), date(2025, 2, 28))
        self.assertEqual(warranty_end(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertIsNone(warranty_end(None, 24))
        detail = self.prepare()
        code = encode_label(detail)
        legacy_code = 'WMS1:' + json.dumps(label_payload(detail), ensure_ascii=False)
        self.assertEqual(len(code), 39)
        self.assertEqual(resolve_label(legacy_code).pk, detail.pk)
        self.assertEqual(resolve_label(code).pk, detail.pk)
        for invalid in (
            '设备-01',
            'WMS2:{}',
            'WMS1:[]',
            code[:-1] + '0',
            code[:-1] + 'true',
            code.replace(detail.uid.hex.upper(), '123'),
            legacy_code.replace('"version": 1', '"version": true'),
            legacy_code.replace(f'"id": "{detail.uid}"', '"id": 123'),
            'WMS1:' + 'x' * 2000,
        ):
            with self.subTest(code=invalid), self.assertRaises(ValidationError):
                parse_label(invalid)
        tampered = label_payload(detail)
        tampered['project'] = '伪造'
        with self.assertRaises(ValidationError):
            resolve_label('WMS1:' + json.dumps(tampered))

    def test_save_retry_no_stock_and_contract_dates(self):
        """Preparation is retryable and all item dates follow the contract."""
        detail = self.prepare()
        self.assertFalse(StockItem.objects.exists())
        self.assertFalse(ScanStockEvent.objects.exists())
        receipt = detail.receipt
        retry = self.post('wms/receipts/', receipt.input_snapshot)
        self.assertEqual(retry['pk'], receipt.pk)
        self.assertEqual(WmsReceiptDetail.objects.count(), 1)
        code = encode_label(detail)
        update = {
            'pk': self.contract.pk,
            'warranty_months': 24,
            'acceptance_date': '2026-03-01',
            'reason': '延期',
            'expected_updated_at': self.contract.updated_at.isoformat(),
        }
        self.post('wms/contracts/', update)
        detail.refresh_from_db()
        self.assertEqual(detail.effective_acceptance_date, date(2026, 3, 1))
        self.assertEqual(detail.effective_warranty_months, 24)
        self.assertEqual(code, encode_label(detail))
        self.assertEqual(self.contract.changes.count(), 1)

    def test_acceptance_can_be_filled_after_inbound_for_whole_contract(self):
        """Post-inbound acceptance updates current stock together, preserving audit."""
        self.contract.acceptance_date = None
        self.contract.save()
        first, second = self.prepare(), self.prepare()
        self.receive(first)
        self.receive(second)
        codes = [encode_label(first), encode_label(second)]
        self.assertTrue(
            all(
                row['acceptance_date'] is None
                for row in self.client.get('/api/stock/wms/inventory/').data['results']
            )
        )
        self.post(
            'wms/contracts/',
            {
                'pk': self.contract.pk,
                'acceptance_date': '2026-10-08',
                'warranty_months': 24,
                'reason': '入库后补填合同起始时间',
                'expected_updated_at': self.contract.updated_at.isoformat(),
            },
        )
        rows = self.client.get('/api/stock/wms/inventory/').data['results']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(str(row['acceptance_date']), '2026-10-08')
            self.assertEqual(str(row['warranty_end']), '2028-10-08')
        for detail, code in zip((first, second), codes, strict=True):
            detail.refresh_from_db()
            self.assertEqual(encode_label(detail), code)
            self.assertIsNone(
                detail.events.get(outcome='SUCCESS').acceptance_date_snapshot
            )

    def test_item_warranty_edits_are_rejected_and_direct_label_loads_receipt(self):
        """There is one contract date; direct scans resolve their preparation context."""
        detail = self.prepare()
        resolved = self.post('wms/labels/resolve/', {'barcode': encode_label(detail)})
        self.assertEqual(resolved['receipt'], detail.receipt_id)
        self.assertEqual(resolved['receipt_location'], self.location.pk)
        self.post(
            f'wms/details/{detail.pk}/',
            {
                'acceptance_date_override': '2026-01-01',
                'reason': '单独验收',
                'label_version': 1,
            },
            400,
        )
        self.post(
            'wms/receipts/',
            {
                'request_key': str(uuid.uuid4()),
                'project': '项目',
                'contract': self.contract.pk,
                'location': self.location.pk,
                'details': [
                    {
                        'part': self.part.pk,
                        'prepared_quantity': 1,
                        'warranty_months_override': 36,
                    }
                ],
            },
            400,
        )

    def test_contract_validation_impact_preview_and_stale_edit(self):
        """Normalized uniqueness, positive months and optimistic updates apply on the server."""
        self.post('wms/contracts/', {'number': ' 新合同 ', 'warranty_months': 24}, 201)
        self.assertTrue(WmsContract.objects.filter(number='新合同').exists())
        self.post('wms/contracts/', {'number': '新合同', 'warranty_months': 12}, 400)
        self.post('wms/contracts/', {'number': '无效', 'warranty_months': 0}, 400)
        self.prepare()
        self.prepare()
        preview = self.post(
            'wms/contracts/',
            {'pk': self.contract.pk, 'warranty_months': 24, 'preview': True},
        )
        self.assertEqual(preview['before']['warranty_followers'], 2)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.warranty_months, 12)
        self.post(
            'wms/contracts/',
            {
                'pk': self.contract.pk,
                'warranty_months': 24,
                'reason': '修改',
                'expected_updated_at': 'stale',
            },
            400,
        )

    def test_quantity_confirmation_duplicate_scan_and_single_posting(self):
        """Eight of ten require confirmation; rescans never silently add the remainder."""
        detail = self.prepare()
        request = self.request()
        missing = self.scan(detail, request, 8)
        self.assertEqual(
            missing.data['error_code'], 'ACTUAL_QUANTITY_CONFIRMATION_REQUIRED'
        )
        response = self.scan(detail, request, 8, quantity_confirmed=True)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(StockItem.objects.exists())
        duplicate = self.scan(detail, request, 8, quantity_confirmed=True)
        self.assertEqual(duplicate.data['error_code'], 'RECEIPT_ALREADY_REQUESTED')
        ApprovalRequestService.submit(request_id=request.pk, user=self.user)
        ApprovalRequestService.approve(request_id=request.pk, user=self.approver)
        self.assertEqual(StockItem.objects.get(source_detail=detail).quantity, 8)
        with self.assertRaises(ScanApprovalError):
            ApprovalRequestService.approve(request_id=request.pk, user=self.approver)
        self.assertEqual(StockItem.objects.get(source_detail=detail).quantity, 8)

    def test_two_receipts_stay_separate_and_summary_shows_multiple(self):
        """Same product/project keeps separate contract sources and warranty terms."""
        first = self.prepare()
        original_contract = self.contract
        self.contract = WmsContract.objects.create(
            number='合同B', acceptance_date=date(2024, 2, 29), warranty_months=24
        )
        second = self.prepare()
        self.contract = original_contract
        first_item, _ = self.receive(first)
        second_item, _ = self.receive(second)
        self.assertNotEqual(first_item.pk, second_item.pk)
        self.assertFalse(first_item.can_merge(other=second_item))
        rows = self.client.get('/api/stock/wms/inventory/').data['results']
        self.assertEqual(len(rows), 2)
        summary = self.client.get(
            '/api/stock/wms/inventory/', {'view': 'summary'}
        ).data['results'][0]
        self.assertEqual(summary['warranty_months'], '多种')
        self.assertEqual(Decimal(summary['quantity']), 20)
        outgoing = self.request('OUTBOUND')
        self.assertEqual(self.scan(first, outgoing, 1).status_code, 201)
        self.assertEqual(self.scan(second, outgoing, 1).status_code, 201)
        requests = self.client.get('/api/stock/scan/requests/').data['results']
        row = next(row for row in requests if row['pk'] == outgoing.pk)
        self.assertEqual(row['business_summary']['project'], first.project)
        self.assertEqual(row['business_summary']['contract'], '多种')
        self.assertEqual(row['business_summary']['warranty_months'], '多种')
        self.assertNotIn('lines', row)

    def test_freeze_cancel_reject_and_exact_location_after_split(self):
        """Draft freezes the exact detail/location; terminal paths release it."""
        detail = self.prepare()
        item, _ = self.receive(detail)
        destination = StockLocation.objects.create(
            name='新库位', approval_owner=Owner.get_owner(self.approver)
        )
        split = item.splitStock(4, location=destination, user=self.user)
        self.assertEqual(split.source_detail_id, detail.pk)
        request = self.request('OUTBOUND', destination)
        response = self.scan(detail, request, 3)
        self.assertEqual(response.status_code, 201, response.data)
        split.refresh_from_db()
        self.assertEqual(split.quantity, 4)
        self.assertEqual(split.scan_frozen_quantity(), 3)
        self.assertEqual(
            self.scan(detail, request, 2).data['error_code'],
            'INSUFFICIENT_AVAILABLE_STOCK',
        )
        ApprovalRequestService.cancel(request_id=request.pk, user=self.user)
        self.assertEqual(split.scan_frozen_quantity(), 0)
        request = self.request('OUTBOUND', destination)
        self.assertEqual(self.scan(detail, request, 3).status_code, 201)
        ApprovalRequestService.submit(request_id=request.pk, user=self.user)
        ApprovalRequestService.approve(request_id=request.pk, user=self.approver)
        split.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(split.quantity, 1)
        self.assertEqual(item.quantity, 6)

    def test_attribution_version_and_immutable_event_snapshot(self):
        """Current stock follows attribution while completed transactions retain originals."""
        detail = self.prepare()
        old_code = encode_label(detail)
        item, request = self.receive(detail)
        event = request.lines.get(outcome='SUCCESS')
        changed = self.post(
            f'wms/details/{detail.pk}/',
            {'project': '新项目', 'reason': '修正', 'label_version': 1},
        )
        self.assertEqual(changed['label_version'], 2)
        with self.assertRaises(ValidationError):
            resolve_label(old_code)
        item.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(item.batch, '新项目')
        self.assertEqual(event.batch_snapshot, detail.project)
        self.assertEqual(event.label_version_snapshot, 1)
        detail.refresh_from_db()
        current_code = encode_label(detail)
        self.post(
            'wms/contracts/',
            {
                'pk': self.contract.pk,
                'acceptance_date': '2026-01-01',
                'reason': '验收',
                'expected_updated_at': self.contract.updated_at.isoformat(),
            },
        )
        detail.refresh_from_db()
        self.assertEqual(current_code, encode_label(detail))
        event.refresh_from_db()
        self.assertEqual(event.acceptance_date_snapshot, date(2024, 2, 29))

    def test_pending_attribution_and_cancelled_receipt_rejected(self):
        """Active applications block changes; cancelled codes cannot resume business."""
        detail = self.prepare()
        request = self.request()
        self.assertEqual(self.scan(detail, request).status_code, 201)
        self.post(
            f'wms/details/{detail.pk}/',
            {'project': '其他', 'reason': '修改', 'label_version': 1},
            400,
        )
        ApprovalRequestService.cancel(request_id=request.pk, user=self.user)
        self.post(f'wms/receipts/{detail.receipt_id}/cancel/', {'reason': '取消'})
        with self.assertRaises(ValidationError):
            resolve_label(encode_label(detail))

    def test_rejection_allows_retry_with_a_new_event(self):
        """Rejected history remains while a new application can post only once."""
        detail = self.prepare()
        request = self.request()
        self.assertEqual(self.scan(detail, request).status_code, 201)
        ApprovalRequestService.submit(request_id=request.pk, user=self.user)
        ApprovalRequestService.reject(
            request_id=request.pk, user=self.approver, note='复核'
        )
        item, _ = self.receive(detail)
        self.assertEqual(item.quantity, 10)
        self.assertEqual(detail.events.filter(outcome='FAILED').count(), 1)
        self.assertEqual(detail.events.filter(outcome='SUCCESS').count(), 1)

    def test_pending_warranty_edit_preserves_snapshot_and_expiry_releases(self):
        """Warranty edits keep the accepted audit; outbound expiry releases its freeze."""
        from datetime import timedelta

        from InvenTree.helpers import current_time

        detail = self.prepare()
        request = self.request()
        response = self.scan(detail, request)
        self.assertEqual(response.status_code, 201, response.data)
        self.post(
            'wms/contracts/',
            {
                'pk': self.contract.pk,
                'acceptance_date': '2026-01-31',
                'reason': '验收确认',
                'expected_updated_at': self.contract.updated_at.isoformat(),
            },
        )
        ApprovalRequestService.submit(request_id=request.pk, user=self.user)
        ApprovalRequestService.approve(request_id=request.pk, user=self.approver)
        event = request.lines.get(outcome='SUCCESS')
        self.assertEqual(event.acceptance_date_snapshot, date(2024, 2, 29))
        outbound = self.request('OUTBOUND')
        self.assertEqual(self.scan(detail, outbound, 3).status_code, 201)
        item = StockItem.objects.get(source_detail=detail)
        self.assertEqual(item.scan_frozen_quantity(), 3)
        outbound.expires_at = current_time() - timedelta(seconds=1)
        outbound.save()
        ApprovalRequestService.expire(request_id=outbound.pk)
        self.assertEqual(item.scan_frozen_quantity(), 0)
        self.assertEqual(item.quantity, 10)

    def test_shared_preparation_edit_invalidates_all_old_codes(self):
        """Shared edits replace every prepared label and preserve stock-free state."""
        detail = self.prepare()
        code = encode_label(detail)
        self.post(
            f'wms/receipts/{detail.receipt_id}/',
            {
                'project': '调整项目',
                'contract': self.contract.pk,
                'location': self.location.pk,
                'reason': '修正公共信息',
            },
        )
        detail.refresh_from_db()
        self.assertEqual(detail.project, '调整项目')
        self.assertEqual(detail.label_version, 2)
        self.assertFalse(StockItem.objects.exists())
        with self.assertRaises(ValidationError):
            resolve_label(code)

    def test_count_import_requires_unambiguous_detail_identity(self):
        """Inventory count never picks an arbitrary detail with the same project."""
        from types import SimpleNamespace

        from stock.wms import inspect_rows

        first, second = self.prepare(), self.prepare()
        self.receive(first)
        self.receive(second)
        row = {
            'IPN': self.part.IPN,
            'batch': first.project,
            'location': self.location.pathstring,
            'quantity': '9',
        }
        preview = inspect_rows(
            [row], 'count', SimpleNamespace(user=self.user, auth=None)
        )
        self.assertTrue(preview[0]['error'])
        row['detail_uid'] = str(first.uid)
        preview = inspect_rows(
            [row], 'count', SimpleNamespace(user=self.user, auth=None)
        )
        self.assertNotIn('error', preview[0])
        row['detail_uid'] = str(second.uid)
        prepared = self.post('wms/imports/', {'kind': 'count', 'rows': [row]}, 201)
        self.assertTrue(prepared['can_commit'], prepared)
        self.post('wms/imports/', {'task': prepared['task']})
        self.assertEqual(StockItem.objects.get(source_detail=first).quantity, 10)
        self.assertEqual(StockItem.objects.get(source_detail=second).quantity, 9)

    def test_history_backfill_split_and_legacy_bypass(self):
        """Backfill preserves quantity and creates no false inbound event."""
        item = StockItem.objects.create(
            part=self.part, location=self.location, quantity=10, batch='历史项目'
        )
        data = self.post(
            f'wms/legacy/{item.pk}/',
            {
                'request_key': str(uuid.uuid4()),
                'contract': self.contract.pk,
                'quantity': 4,
                'confirmed_stock_quantity': 10,
                'confirmed_single_contract': True,
                'reason': '逐项核对',
            },
            201,
        )
        self.assertEqual(sum(StockItem.objects.values_list('quantity', flat=True)), 10)
        self.assertFalse(ScanStockEvent.objects.exists())
        detail = WmsReceiptDetail.objects.get(pk=data['pk'])
        retry = self.post(
            f'wms/legacy/{item.pk}/', detail.receipt.input_snapshot['payload']
        )
        self.assertEqual(retry['pk'], detail.pk)
        self.assertEqual(sum(StockItem.objects.values_list('quantity', flat=True)), 10)
        request = self.request('OUTBOUND')
        linked = StockItem.objects.get(source_detail=detail)
        response = self.client.post(
            '/api/stock/scan/outbound/',
            {
                'barcode': f'INV-SI{linked.pk}',
                'client_event_id': str(uuid.uuid4()),
                'quantity': 1,
                'approval_request_id': request.pk,
                'source_location': self.location.pk,
                'batch': linked.batch,
                'purpose': '安装',
            },
            format='json',
        )
        self.assertEqual(response.data['error_code'], 'STOCK_DETAIL_MISMATCH')

    def test_permissions_do_not_open_equipment_edits(self):
        """View-only and anonymous callers cannot maintain contract business data."""
        viewer = User.objects.create_user('no-role')
        self.client.force_authenticate(viewer)
        self.post('wms/contracts/', {'number': '越权', 'warranty_months': 12}, 403)
        self.post('wms/receipts/', {}, 403)
        self.post('wms/labels/resolve/', {}, 403)

    def test_operator_can_prepare_and_scan_without_catalogue_edit_rights(self):
        """Real operator roles can maintain business data with approval enforcement intact."""
        from django.contrib.auth.models import Group

        from InvenTree.unit_test import addUserPermission

        operator = User.objects.create_user('contract-operator')
        group = Group.objects.create(name='Contract operators')
        group.rule_sets.filter(name='stock').update(
            can_view=True, can_add=True, can_change=True
        )
        operator.groups.add(group)
        addUserPermission(operator, 'part', 'part', 'view')
        self.user = operator
        self.client.force_authenticate(operator)
        self.post(
            'wms/contracts/', {'number': '操作员合同', 'warranty_months': 24}, 201
        )
        detail = self.prepare()
        request = self.request()
        response = self.scan(detail, request)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(StockItem.objects.exists())
        legacy = self.client.post(
            '/api/stock/scan/inbound/',
            {
                'barcode': self.part.IPN,
                'batch': detail.project,
                'quantity': 1,
                'client_event_id': str(uuid.uuid4()),
                'destination_location': self.location.pk,
                'approval_request_id': request.pk,
            },
            format='json',
        )
        self.assertEqual(legacy.data['error_code'], 'COMPLETE_LABEL_REQUIRED')
        self.assertEqual(
            self.client.post('/api/part/', {}, format='json').status_code, 403
        )
        self.assertEqual(
            self.client.post('/api/stock/add/', {}, format='json').status_code, 403
        )

    def test_oauth_scope_matches_read_only_resolution_and_contract_write(self):
        """A stock write token can maintain contracts; a read token only resolves labels."""
        from datetime import timedelta

        from django.utils import timezone

        from oauth2_provider.models import AccessToken, Application

        from users.oauth2_scopes import DEFAULT_READ, get_granular_scope

        detail = self.prepare()
        application = Application.objects.create(
            user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_PASSWORD,
        )
        client = APIClient()
        for scope, expected in (
            (DEFAULT_READ, 403),
            (get_granular_scope('change', 'stock'), 201),
        ):
            token = str(uuid.uuid4())
            AccessToken.objects.create(
                user=self.user,
                application=application,
                token=token,
                expires=timezone.now() + timedelta(hours=1),
                scope=scope,
            )
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
            response = client.post(
                '/api/stock/wms/contracts/',
                {'number': 'OAuth合同', 'warranty_months': 12},
                format='json',
            )
            self.assertEqual(response.status_code, expected, response.data)
            if scope == DEFAULT_READ:
                response = client.post(
                    '/api/stock/wms/labels/resolve/',
                    {'barcode': encode_label(detail)},
                    format='json',
                )
                self.assertEqual(response.status_code, 200, response.data)

    @override_settings(LOCAL_PRINT_ENABLED=True)
    def test_per_item_label_pages_and_old_job_snapshot(self):
        """Different quantities render once, and old JSON snapshots stay valid."""
        import io

        from django.apps import apps

        from pypdf import PdfReader

        from report.local_print import create_job, generate_job, job_access, read_pdf
        from stock.models import WmsBusinessChange

        apps.get_app_config('report').create_default_labels()
        set_global_setting('LABEL_ENABLE', True)
        first = self.prepare()
        second = self.prepare()
        values = {
            'request_key': uuid.uuid4(),
            'model_type': 'wmsreceiptdetail',
            'items': [first.pk, second.pk],
            'copies': 1,
            'item_copies': {str(first.pk): 2, str(second.pk): 5},
            'queue': 'Test queue',
        }
        with patch('report.local_print.enqueue_job'):
            job = create_job(self.user, values)
            self.assertEqual(create_job(self.user, values).pk, job.pk)
        self.assertEqual(job.total_labels, 7)
        self.assertEqual(job.output.total, 7)
        job.item_versions = {
            str(detail.pk): 'WMS1:' + json.dumps(label_payload(detail))
            for detail in (first, second)
        }
        job.save(update_fields=['item_versions'])
        job_access(self.user, job)
        generate_job(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.state, 'ready', job.detail)
        pages = PdfReader(io.BytesIO(read_pdf(job))).pages
        self.assertEqual(len(pages), 7)
        for page in pages:
            text = page.extract_text()
            self.assertIn('广州南沙智慧城市大数据有限公司', text)
            self.assertNotIn('Label version', text)
            self.assertNotIn('标签版本', text)
        self.assertFalse(StockItem.objects.exists())
        self.assertEqual(WmsReceiptDetail.objects.count(), 2)
        audits = WmsBusinessChange.objects.filter(action='label_print').order_by('pk')
        self.assertEqual([entry.after['copies'] for entry in audits], [2, 5])

    @override_settings(LOCAL_PRINT_ENABLED=True)
    def test_pre_receipt_print_retry_and_stale_pdf_access(self):
        """Multiple copies share one line and print retry never creates inventory."""
        from django.apps import apps

        from report.local_print import create_job, job_access

        apps.get_app_config('report').create_default_labels()
        set_global_setting('LABEL_ENABLE', True)
        detail = self.prepare()
        values = {
            'request_key': uuid.uuid4(),
            'model_type': 'wmsreceiptdetail',
            'items': [detail.pk],
            'copies': 4,
            'queue': 'Test queue',
        }
        with patch('report.local_print.enqueue_job'):
            first = create_job(self.user, values)
            second = create_job(self.user, values)
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(StockItem.objects.exists())
        self.assertEqual(WmsReceiptDetail.objects.count(), 1)
        import io

        from pypdf import PdfReader

        from report.local_print import generate_job, read_pdf

        generate_job(str(first.pk))
        first.refresh_from_db()
        self.assertEqual(first.state, 'ready', first.detail)
        pdf = PdfReader(io.BytesIO(read_pdf(first)))
        self.assertEqual(len(pdf.pages), 4)
        self.assertAlmostEqual(
            float(pdf.pages[0].mediabox.width) * 25.4 / 72, 50, places=1
        )
        self.assertAlmostEqual(
            float(pdf.pages[0].mediabox.height) * 25.4 / 72, 30, places=1
        )
        self.post(
            f'wms/details/{detail.pk}/',
            {'project': '改归属', 'reason': '修正', 'label_version': 1},
        )
        with self.assertRaises(ApiValidationError):
            job_access(self.user, first)


class CompleteLabelConcurrencyTest(TransactionTestCase):
    """Use real PostgreSQL locks to check competing complete-code admissions."""

    def test_two_inbounds_for_one_detail(self):
        """Competing drafts cannot claim the same prepared detail twice."""
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locks')
        operator = User.objects.create_superuser(
            'race-user', 'race@example.test', 'test'
        )
        approver = User.objects.create_superuser(
            'race-approver', 'race-approver@example.test', 'test'
        )
        location = StockLocation.objects.create(
            name='Race shelf', approval_owner=Owner.get_owner(approver)
        )
        part = Part.objects.create(name='Race equipment', IPN='RACE')
        contract = WmsContract.objects.create(number='RACE')
        receipt = WmsReceipt.objects.create(
            project='P', contract=contract, location=location, operator=operator
        )
        detail = WmsReceiptDetail.objects.create(
            receipt=receipt,
            project='P',
            contract=contract,
            part=part,
            prepared_quantity=10,
        )
        set_global_setting('SCAN_APPROVAL_ENABLED', True)
        requests = [
            ApprovalRequestService.create_request(
                user=operator, direction='INBOUND', location=location
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(request):
            try:
                barrier.wait(timeout=10)
                result = ScanStockService(
                    user=operator,
                    operation='INBOUND',
                    data={
                        'barcode': encode_label(detail),
                        'client_event_id': uuid.uuid4(),
                        'quantity': Decimal(10),
                        'approval_request_id': request.pk,
                        'receipt_id': receipt.pk,
                        'destination_location': location,
                    },
                ).process()
                outcomes.append(result.event.outcome)
            except Exception as error:
                outcomes.append(type(error).__name__ + ': ' + str(error))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(request,)) for request in requests
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), 'Concurrent scan did not finish')
        self.assertEqual(sorted(outcomes), ['FAILED', 'PENDING'], outcomes)
        self.assertEqual(detail.events.filter(outcome='PENDING').count(), 1)
