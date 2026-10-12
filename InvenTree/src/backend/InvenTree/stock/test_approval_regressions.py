"""Regression tests for stock quantities reserved by native and scan flows."""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import company.models
import order.models
import part.models
from InvenTree.unit_test import InvenTreeAPITestCase
from order.status_codes import SalesOrderStatus, TransferOrderStatus

from .models import (
    ScanStockApprovalRequest,
    ScanStockEvent,
    StockItem,
    StockLocation,
)
from .serializers import StockItemSerializer
from .services import ApprovalRequestService


class StockItemAllocationAnnotationRegressionTest(InvenTreeAPITestCase):
    """Keep API stock quantities aligned with ``StockItem.allocation_count``."""

    roles = [
        'part.view',
        'part_category.view',
        'stock.view',
        'stock_location.view',
    ]

    def setUp(self):
        """Create active and inactive sales and transfer allocations."""
        super().setUp()

        self.location = StockLocation.objects.create(name='Allocation regression shelf')
        self.part = part.models.Part.objects.create(
            name='Allocation regression part', description='Allocation regression part'
        )
        self.item = StockItem.objects.create(
            part=self.part, location=self.location, quantity=50
        )
        customer = company.models.Company.objects.create(
            name='Allocation regression customer', is_customer=True
        )

        def sales_allocation(reference, status, quantity, *, shipped=False):
            sales_order = order.models.SalesOrder.objects.create(
                reference=reference,
                customer=customer,
                status=SalesOrderStatus.PENDING.value,
            )
            line = order.models.SalesOrderLineItem.objects.create(
                order=sales_order, part=self.part, quantity=quantity
            )
            shipment = None
            if shipped:
                shipment = order.models.SalesOrderShipment.objects.create(
                    order=sales_order,
                    reference='1',
                    shipment_date=timezone.now().date(),
                )
            order.models.SalesOrderAllocation.objects.create(
                line=line, shipment=shipment, item=self.item, quantity=quantity
            )
            order.models.SalesOrder.objects.filter(pk=sales_order.pk).update(
                status=status
            )

        # Active: open order and no shipped shipment.
        sales_allocation('SO-OPEN', SalesOrderStatus.PENDING.value, Decimal(2))
        # Active by the model contract: SHIPPED is still an open order status while
        # this particular allocation has no shipment date.
        sales_allocation('SO-SHIPPED-OPEN', SalesOrderStatus.SHIPPED.value, Decimal(3))
        # Inactive: closed order, even though the allocation itself is unshipped.
        sales_allocation('SO-CLOSED', SalesOrderStatus.COMPLETE.value, Decimal(4))
        # Inactive: the allocation belongs to an already shipped shipment.
        sales_allocation(
            'SO-SHIPMENT-CLOSED',
            SalesOrderStatus.PENDING.value,
            Decimal(5),
            shipped=True,
        )

        def transfer_allocation(reference, status, quantity):
            transfer_order = order.models.TransferOrder.objects.create(
                reference=reference, status=TransferOrderStatus.PENDING.value
            )
            line = order.models.TransferOrderLineItem.objects.create(
                order=transfer_order, part=self.part, quantity=quantity
            )
            order.models.TransferOrderAllocation.objects.create(
                line=line, item=self.item, quantity=quantity
            )
            order.models.TransferOrder.objects.filter(pk=transfer_order.pk).update(
                status=status
            )

        transfer_allocation('TO-OPEN', TransferOrderStatus.PENDING.value, Decimal(6))
        transfer_allocation('TO-ISSUED', TransferOrderStatus.ISSUED.value, Decimal(7))
        transfer_allocation(
            'TO-CLOSED', TransferOrderStatus.COMPLETE.value, Decimal(8)
        )

        self.approval = ScanStockApprovalRequest.objects.create(
            direction=ScanStockApprovalRequest.Direction.OUTBOUND,
            location=self.location,
            requester=self.user,
            expires_at=timezone.now() + timedelta(days=1),
        )
        ScanStockEvent.objects.create(
            client_event_id=uuid.uuid4(),
            request_fingerprint='f' * 64,
            operation=ScanStockEvent.Operation.OUTBOUND,
            outcome=ScanStockEvent.Outcome.PENDING,
            barcode=f'INV-SI{self.item.pk}',
            barcode_hash='b' * 64,
            requested_quantity=Decimal(4),
            approval_request=self.approval,
            request_line_number=1,
            part=self.part,
            stock_item=self.item,
            source_location=self.location,
        )

    def assert_quantities(self, data):
        """Assert native active allocations and scan freezes are separated."""
        self.assertEqual(Decimal(data['allocated']), Decimal(18))
        self.assertEqual(Decimal(data['scan_frozen']), Decimal(4))
        self.assertEqual(Decimal(data['available_quantity']), Decimal(28))

    def test_annotated_queryset_matches_model_active_allocation_contract(self):
        """Direct queryset evaluation must use the same active allocation scope."""
        annotated = StockItemSerializer.annotate_queryset(
            StockItem.objects.filter(pk=self.item.pk)
        ).get()

        self.assertEqual(self.item.allocation_count(), Decimal(18))
        self.assertEqual(self.item.scan_frozen_quantity(), Decimal(4))
        self.assert_quantities({
            'allocated': annotated.allocated,
            'scan_frozen': annotated.scan_frozen,
            'available_quantity': annotated.available_quantity,
        })

    def test_stock_list_and_detail_return_consistent_quantities(self):
        """Both stock GET endpoints evaluate the annotation without a server error."""
        list_response = self.get(reverse('api-stock-list'), expected_code=200)
        records = list_response.data
        if isinstance(records, dict):
            records = records.get('results', records)
        list_data = next(row for row in records if row['pk'] == self.item.pk)
        self.assert_quantities(list_data)

        detail_response = self.get(
            reverse('api-stock-detail', kwargs={'pk': self.item.pk}),
            expected_code=200,
        )
        self.assert_quantities(detail_response.data)

    def test_stock_list_and_detail_options_do_not_error(self):
        """Serializer metadata remains available on both stock endpoints."""
        self.options(reverse('api-stock-list'), expected_code=200)
        self.options(
            reverse('api-stock-detail', kwargs={'pk': self.item.pk}),
            expected_code=200,
        )

    def test_cancel_locks_request_without_locking_nullable_relations(self):
        """A valid cancel must work on PostgreSQL when optional owners are null."""
        self.assertIsNone(self.approval.approval_owner_id)

        with CaptureQueriesContext(connection) as queries:
            cancelled = ApprovalRequestService.cancel(
                request_id=self.approval.pk, user=self.user
            )

        if connection.features.has_select_for_update:
            request_locks = [
                query['sql']
                for query in queries
                if 'FOR UPDATE' in query['sql'].upper()
                and 'stock_scanstockapprovalrequest' in query['sql']
            ]
            self.assertEqual(len(request_locks), 1)
            self.assertNotIn(' JOIN ', request_locks[0].upper())

        self.assertEqual(cancelled.status, ScanStockApprovalRequest.Status.CANCELLED)
        line = ScanStockEvent.objects.get(approval_request=self.approval)
        self.assertEqual(line.outcome, ScanStockEvent.Outcome.FAILED)
        self.assertEqual(line.terminal_reason, 'REQUEST_CANCELLED')
