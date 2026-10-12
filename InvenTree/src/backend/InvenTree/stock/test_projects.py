"""Project archive identity, warehouse references and operator permissions."""

import uuid

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from rest_framework.test import APIClient

from common.models import ProjectCode
from part.models import Part
from stock.models import (
    ScanStockEvent,
    StockItem,
    StockLocation,
    WmsContract,
    WmsReceipt,
)
from stock.serializers import ScanStockRequestSerializer


class ProjectArchiveTest(TestCase):
    """One archive record can be selected by many preparations and stock rows."""

    def setUp(self):
        """Use real warehouse endpoints and a project with no inventory yet."""
        self.user = User.objects.create_superuser('project-admin', '', 'test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.project = ProjectCode.objects.create(code='00123')
        self.part = Part.objects.create(name='Project equipment', IPN='PROJECT-DEVICE')
        self.location = StockLocation.objects.create(name='Project warehouse')
        self.contract = WmsContract.objects.create(number='HT001')

    def prepare(self, **overrides):
        """Build one selected-project preparation without receiving stock."""
        payload = {
            'request_key': str(uuid.uuid4()),
            'project_id': self.project.pk,
            'contract': self.contract.pk,
            'location': self.location.pk,
            'details': [{'part': self.part.pk, 'prepared_quantity': 2}],
            **overrides,
        }
        if 'project' in overrides and 'project_id' not in overrides:
            payload.pop('project_id')
        return self.client.post('/api/stock/wms/receipts/', payload, format='json')

    def test_unique_normalized_code_and_character_boundaries(self):
        """Whitespace does not create a second identity; leading zeros and 100 characters survive."""
        for value in ('00123', ' 00123 '):
            response = self.client.post(
                '/api/stock/wms/projects/', {'code': value}, format='json'
            )
            self.assertEqual(response.status_code, 400, response.data)
        number = '中文 / A-001' + 'x' * 90
        self.assertEqual(len(number), 100)
        response = self.client.post(
            '/api/stock/wms/projects/', {'code': f' {number} '}, format='json'
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['code'], number)
        for invalid in (' ', 'x' * 101):
            response = self.client.post(
                '/api/stock/wms/projects/', {'code': invalid}, format='json'
            )
            self.assertEqual(response.status_code, 400, response.data)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ProjectCode.objects.create(code=' 00123 ')
        self.assertEqual(ProjectCode.objects.count(), 2)

    def test_same_project_reused_across_contracts_and_receipts(self):
        """Selecting one project for two contracts stores one shared foreign key."""
        first = self.prepare()
        other = WmsContract.objects.create(number='HT002')
        second = self.prepare(contract=other.pk)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        for response in (first, second):
            self.assertEqual(response.data['project_id'], self.project.pk)
            self.assertEqual(response.data['project'], '00123')
            self.assertEqual(response.data['details'][0]['project_id'], self.project.pk)
        self.assertEqual(ProjectCode.objects.count(), 1)
        self.assertFalse(StockItem.objects.exists())
        self.assertFalse(ScanStockEvent.objects.exists())
        retry = self.client.post(
            '/api/stock/wms/receipts/',
            WmsReceipt.objects.get(pk=first.data['pk']).input_snapshot,
            format='json',
        )
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(WmsReceipt.objects.count(), 2)

    def test_unknown_mismatched_or_inactive_project_rejected_without_side_effects(self):
        """Text cannot silently establish a new project or contradict a selected archive."""
        for values in (
            {'project_id': 999999},
            {'project': 'UNKNOWN'},
            {'project_id': self.project.pk, 'project': 'OTHER'},
        ):
            response = self.prepare(**values)
            self.assertEqual(response.status_code, 400, response.data)
        self.project.active = False
        self.project.save()
        response = self.prepare()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(WmsReceipt.objects.exists())
        self.assertEqual(ProjectCode.objects.count(), 1)

    def test_stock_links_archive_and_protects_referenced_identity(self):
        """Native stock keeps the shared reference; splitting retains it and deletion is protected."""
        item = StockItem.objects.create(
            part=self.part, location=self.location, batch='00123', quantity=5
        )
        self.assertEqual(item.project_code_id, self.project.pk)
        split = item.splitStock(2, user=self.user)
        self.assertEqual(split.project_code_id, self.project.pk)
        self.assertEqual(ProjectCode.objects.count(), 1)
        self.project.code = 'RENAMED'
        with self.assertRaises(ValidationError):
            self.project.save()
        self.project.refresh_from_db()
        with self.assertRaises(ProtectedError):
            self.project.delete()
        self.assertEqual(sum(StockItem.objects.values_list('quantity', flat=True)), 5)

    def test_scan_project_reference_validates_before_creating_events(self):
        """A forged project ID or code cannot reach stock processing."""
        for project_id, batch in ((999999, '00123'), (self.project.pk, 'OTHER')):
            response = self.client.post(
                '/api/stock/scan/inbound/',
                {
                    'barcode': self.part.IPN,
                    'quantity': 1,
                    'client_event_id': str(uuid.uuid4()),
                    'destination_location': self.location.pk,
                    'project_id': project_id,
                    'batch': batch,
                },
                format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(StockItem.objects.exists())
        self.assertFalse(ScanStockEvent.objects.exists())

    def test_operator_can_create_project_without_native_settings_access(self):
        """Business project registration follows stock role permissions, including denial without a role."""
        operator = User.objects.create_user('project-operator')
        self.client.force_authenticate(operator)
        self.assertEqual(
            self.client.post(
                '/api/stock/wms/projects/', {'code': 'DENIED'}, format='json'
            ).status_code,
            403,
        )
        group = Group.objects.create(name='Project operators')
        group.rule_sets.filter(name='stock').update(can_view=True, can_change=True)
        operator.groups.add(group)
        self.client.force_authenticate(User.objects.get(pk=operator.pk))
        response = self.client.post(
            '/api/stock/wms/projects/', {'code': 'OP001'}, format='json'
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(operator.is_staff)
        self.assertEqual(
            self.client.post(
                '/api/project-code/', {'code': 'NATIVE'}, format='json'
            ).status_code,
            403,
        )
        projects = self.client.get('/api/stock/wms/projects/').data['results']
        self.assertIn('00123', [row['code'] for row in projects])

    def test_inactive_project_blocks_new_inbound_but_keeps_existing_outbound_available(
        self,
    ):
        """Retiring a project must not prevent existing stock from being issued."""
        self.project.active = False
        self.project.save()
        common = {
            'barcode': self.part.IPN,
            'quantity': 1,
            'client_event_id': str(uuid.uuid4()),
            'project_id': self.project.pk,
            'batch': self.project.code,
        }
        inbound = ScanStockRequestSerializer(
            data={**common, 'destination_location': self.location.pk},
            context={'operation': 'INBOUND'},
        )
        outbound = ScanStockRequestSerializer(
            data={
                **common,
                'source_location': self.location.pk,
                'purpose': 'Issue existing stock',
            },
            context={'operation': 'OUTBOUND'},
        )
        self.assertFalse(inbound.is_valid())
        self.assertTrue(outbound.is_valid(), outbound.errors)
