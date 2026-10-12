"""Transactional domain service for inbound and outbound stock scans."""

# Business messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils.translation import gettext_lazy as _

from rest_framework.exceptions import ValidationError as DRFValidationError

import company.models as company_models
import InvenTree.helpers
import part.models as part_models
import part.serializers as part_serializers
import plugin.base.barcodes.helper as barcode_helper
import stock.notifications as stock_notifications
from common.models import NotificationMessage
from common.settings import get_global_setting
from stock.models import (
    ScanInboundBarcodeLock,
    ScanStockApprovalRequest,
    ScanStockEvent,
    StockItem,
    StockLocation,
    WmsReceiptDetail,
)
from stock.status_codes import StockStatus
from users.models import Owner, operator_snapshot
from users.permissions import check_user_permission


class ScanStockError(Exception):
    """Expected stock scan error with a stable machine-readable code."""

    def __init__(self, code: str, message, *, details: dict | None = None):
        """Initialize a business error."""
        self.code = code
        self.message = str(message)
        self.details = details or {}
        super().__init__(self.message)


class ScanStockPreflight(ScanStockError):  # noqa: N818
    """Non-terminal result which must not claim the client event identifier."""


class ScanStockIdempotencyConflict(ScanStockError):  # noqa: N818
    """A client event identifier was reused for a different request."""


class ScanApprovalError(ScanStockError):
    """Expected approval lifecycle error with a stable conflict classification."""

    def __init__(
        self, code: str, message, *, details: dict | None = None, conflict: bool = True
    ):
        """Initialize an approval lifecycle error."""
        self.conflict = conflict
        super().__init__(code, message, details=details)


@dataclass
class ResolvedBarcode:
    """Barcode resolution result."""

    kind: str
    part: part_models.Part | None = None
    stock_item: StockItem | None = None
    location: StockLocation | None = None
    detail: WmsReceiptDetail | None = None


@dataclass
class ScanStockResult:
    """Result returned to the API layer."""

    event: ScanStockEvent
    replayed: bool = False
    part_created: bool = False
    details: dict | None = None

    @property
    def success(self) -> bool:
        """Return whether the event was accepted or completed successfully."""
        return self.event.outcome != ScanStockEvent.Outcome.FAILED


class ScanStockService:
    """Perform one idempotent stock scan against native InvenTree stock models."""

    INTERNAL_BARCODE_PATTERN = re.compile(r'^INV-(PA|SI|SL)(\d+)$')

    def __init__(
        self, *, user, operation: str, data: dict, context: dict | None = None
    ):
        """Initialize the service for one validated request."""
        self.user = user
        self.operation = operation
        self.data = data
        self.context = context or {}
        self.barcode = data['barcode']
        self.quantity = Decimal(data['quantity'])
        self.approval_request_id = data.get('approval_request_id')
        self.part_created = False
        self.source_detail = None
        self.fingerprint = self._fingerprint()

    def process(self) -> ScanStockResult:
        """Preflight and execute the scan in one atomic business transaction."""
        if self.approval_request_id is not None:
            return self._process_approval_scan()

        try:
            resolved = self._resolve_barcode()
        except ScanStockError as exc:
            return self._record_preflight_business_failure(exc.code, exc.message)

        if resolved.location is not None:
            raise ScanStockPreflight(
                'LOCATION_BARCODE_REQUIRES_CONTEXT',
                _('Use the location barcode to set the source or destination location'),
                details={'location': resolved.location.pk},
            )

        if resolved.part is None and resolved.stock_item is None:
            if self.operation == ScanStockEvent.Operation.INBOUND:
                if not self.data.get('create_part'):
                    raise ScanStockPreflight(
                        'SETUP_REQUIRED',
                        _(
                            'Unknown barcode; product setup is required before inbound stock'
                        ),
                    )
            else:
                return self._record_preflight_business_failure(
                    'UNKNOWN_BARCODE',
                    _('Unknown products cannot be scanned out of stock'),
                )

        if resolved.part and (
            resolved.part.trackable
            or (resolved.stock_item is not None and resolved.stock_item.serialized)
        ):
            return self._record_preflight_business_failure(
                'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                _(
                    'Serialized stock must use the native receiving or shipping workflow'
                ),
                resolved=resolved,
            )

        self._selection_preflight(resolved)

        with transaction.atomic():
            existing = self._locked_existing_event()
            if existing is not None:
                return self._replay(existing)

            try:
                with transaction.atomic():
                    event = ScanStockEvent.objects.create(
                        **self._pending_event_fields()
                    )
            except IntegrityError:
                existing = self._locked_existing_event()
                if existing is None:
                    raise
                return self._replay(existing)

            failure_details = {}
            try:
                with transaction.atomic():
                    resolved = self._resolve_locked(resolved)
                    self._apply_event_context(event, resolved)

                    if self.operation == ScanStockEvent.Operation.INBOUND:
                        self._apply_inbound(event, resolved)
                    else:
                        self._apply_outbound(event, resolved)
            except ScanStockPreflight:
                # Selection and setup responses are non-terminal. Propagating out of
                # the outer transaction rolls back the PENDING event claim, allowing
                # the client to reuse the UUID with its completed selection/setup DTO.
                self.part_created = False
                raise
            except ScanStockError as exc:
                self._fail_event(event, exc, resolved=resolved)
                self.part_created = False
                failure_details = exc.details
            except (DjangoValidationError, DRFValidationError) as exc:
                # Model and nested-serializer validation failures are expected
                # business failures once a well-formed scan entered the service.
                # Roll back the inventory savepoint, then persist the FAILED event.
                validation_error = ScanStockError(
                    'STOCK_VALIDATION_FAILED', self._validation_error_message(exc)
                )
                self._fail_event(event, validation_error, resolved=resolved)
                self.part_created = False

            if (
                event.outcome == ScanStockEvent.Outcome.SUCCESS
                and self.operation == ScanStockEvent.Operation.INBOUND
            ):
                transaction.on_commit(
                    lambda event_id=event.pk: (
                        stock_notifications.queue_direct_inbound_notification(event_id)
                    )
                )

            return ScanStockResult(
                event=event,
                replayed=False,
                part_created=self.part_created,
                details=failure_details,
            )

    def _process_approval_scan(self) -> ScanStockResult:
        """Append one pending audit line without changing physical stock."""
        with transaction.atomic():
            try:
                request = ScanStockApprovalRequest.objects.select_for_update().get(
                    pk=self.approval_request_id
                )
            except ScanStockApprovalRequest.DoesNotExist as exc:
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_NOT_FOUND',
                    _('Scan approval request does not exist'),
                ) from exc

            # Replay is defined by the immutable fingerprint, not by mutable
            # request state. A retry after submit or timeout still returns the
            # original event instead of failing a later lifecycle check.
            existing = self._locked_existing_event()
            if existing is not None:
                if existing.approval_request_id != request.pk:
                    raise ScanStockIdempotencyConflict(
                        'IDEMPOTENCY_CONFLICT',
                        _(
                            'Client event ID was already used for another stock scan request'
                        ),
                    )
                return self._replay(existing)

            ApprovalRequestService.validate_append(request, self.user, self.operation)
            scan_location = (
                self.data['source_location']
                if self.operation == ScanStockEvent.Operation.OUTBOUND
                else self.data['destination_location']
            )
            if scan_location.pk != request.location_id:
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_LOCATION_MISMATCH',
                    _('Scan location does not match the approval request'),
                )

            initial_failure = None
            try:
                resolved = self._resolve_barcode()
            except ScanStockError as exc:
                resolved = ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.EXTERNAL)
                initial_failure = exc

            if resolved.location is not None:
                raise ScanStockPreflight(
                    'LOCATION_BARCODE_REQUIRES_CONTEXT',
                    _(
                        'Use the location barcode to set the source or destination location'
                    ),
                    details={'location': resolved.location.pk},
                )
            if (
                resolved.part is None
                and resolved.stock_item is None
                and initial_failure is None
            ):
                if self.operation == ScanStockEvent.Operation.INBOUND:
                    if not self.data.get('create_part'):
                        raise ScanStockPreflight(
                            'SETUP_REQUIRED',
                            _(
                                'Unknown barcode; product setup is required before inbound stock'
                            ),
                        )
                elif initial_failure is None:
                    initial_failure = ScanStockError(
                        'UNKNOWN_BARCODE',
                        _('Unknown products cannot be scanned out of stock'),
                    )
            if resolved.part and (
                resolved.part.trackable
                or (resolved.stock_item is not None and resolved.stock_item.serialized)
            ):
                initial_failure = ScanStockError(
                    'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                    _(
                        'Serialized stock must use the native receiving or shipping workflow'
                    ),
                )
            if initial_failure is None:
                self._selection_preflight(resolved)

            max_lines = int(get_global_setting('SCAN_APPROVAL_MAX_LINES', 100))
            active_lines = request.lines.filter(
                outcome=ScanStockEvent.Outcome.PENDING
            ).count()
            if active_lines >= max_lines:
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_MAX_LINES',
                    _('Scan approval request has reached its maximum line count'),
                )

            line_number = (
                request.lines.aggregate(value=Max('request_line_number'))['value'] or 0
            ) + 1
            fields = self._pending_event_fields()
            fields.update(
                {
                    'approval_request': request,
                    'request_line_number': line_number,
                    'requested_part_setup': self._part_setup_snapshot(),
                    'requested_stock_context': self._stock_context_snapshot(),
                }
            )

            try:
                with transaction.atomic():
                    event = ScanStockEvent.objects.create(**fields)
            except IntegrityError:
                existing = self._locked_existing_event()
                if existing is None:
                    raise
                return self._replay(existing)

            failure_details = {}
            try:
                with transaction.atomic():
                    if initial_failure is not None:
                        raise initial_failure
                    resolved, item = self._prepare_approval_line(resolved)
                    self._apply_event_context(event, resolved, item=item)
                    event.save()
            except ScanStockPreflight:
                raise
            except ScanStockError as exc:
                self._fail_event(event, exc, resolved=resolved)
                event.terminal_reason = exc.code
                event.save(update_fields=['terminal_reason'])
                failure_details = exc.details
            except (DjangoValidationError, DRFValidationError) as exc:
                failure = ScanStockError(
                    'STOCK_VALIDATION_FAILED', self._validation_error_message(exc)
                )
                self._fail_event(event, failure, resolved=resolved)
                event.terminal_reason = failure.code
                event.save(update_fields=['terminal_reason'])

            if event.outcome == ScanStockEvent.Outcome.PENDING:
                now = InvenTree.helpers.current_time()
                timeout = int(
                    get_global_setting('SCAN_APPROVAL_DRAFT_TIMEOUT_MINUTES', 30)
                )
                request.activity_at = now
                request.expires_at = now + timedelta(minutes=timeout)
                request.save(update_fields=['activity_at', 'expires_at', 'updated_at'])

            return ScanStockResult(event=event, details=failure_details)

    def _prepare_approval_line(
        self, resolved: ResolvedBarcode
    ) -> tuple[ResolvedBarcode, StockItem | None]:
        """Lock and validate line context while deliberately avoiding stock changes."""
        current = self._resolve_barcode()

        if current.part is None:
            if self.operation != ScanStockEvent.Operation.INBOUND:
                raise ScanStockError('UNKNOWN_BARCODE', _('Unknown product barcode'))
            self._validate_pending_part_setup()
            return current, None

        current.part = part_models.Part.objects.select_for_update().get(
            pk=current.part.pk
        )
        self._bind_receipt_detail(current, claim_inbound=True)
        if not current.part.active:
            raise ScanStockError('PART_INACTIVE', _('设备已停用'))
        if current.part.trackable:
            raise ScanStockError(
                'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                _('Serialized stock must use the native order workflow'),
            )

        if current.stock_item is not None:
            current.stock_item = (
                StockItem.objects.select_for_update()
                .select_related('part')
                .get(pk=current.stock_item.pk)
            )

        if self.operation == ScanStockEvent.Operation.INBOUND:
            item = self._prepare_pending_inbound(current)
        else:
            item = self._prepare_pending_outbound(current)

        resolved.kind = current.kind
        resolved.part = current.part
        resolved.stock_item = current.stock_item
        resolved.detail = current.detail
        return resolved, item

    def _bind_receipt_detail(self, resolved, *, claim_inbound):
        """Serialize line admission with attribution changes using the existing Part lock."""
        if resolved.detail is None:
            if self.operation == ScanStockEvent.Operation.INBOUND and not (
                self.user.is_staff
                or self.user.is_superuser
                or self.context.get('approval_application')
            ):
                location = self.data.get('destination_location')
                legacy = StockItem.objects.filter(
                    part=resolved.part,
                    location=location,
                    batch=self.data.get('batch') or '',
                    source_detail__isnull=True,
                )
                if not legacy.exists():
                    raise ScanStockError(
                        'COMPLETE_LABEL_REQUIRED',
                        _('新入库请先填写合同明细并打印完整标签'),
                    )
            return
        from stock.warranty import resolve_label

        try:
            detail = resolve_label(self.barcode, lock=True)
        except DjangoValidationError as exc:
            raise ScanStockError(
                'INVALID_COMPLETE_LABEL', self._validation_error_message(exc)
            ) from exc
        self.source_detail = detail
        resolved.detail = detail
        if self.data.get('batch') and self.data['batch'] != detail.project:
            raise ScanStockError('LABEL_PROJECT_MISMATCH', _('项目与完整标签不一致'))
        self.data['batch'] = detail.project
        if not self.approval_request_id:
            raise ScanStockError(
                'COMPLETE_LABEL_REQUIRES_APPROVAL', _('完整标签业务必须提交审批')
            )
        if self.operation == ScanStockEvent.Operation.INBOUND:
            if detail.legacy:
                raise ScanStockError(
                    'LEGACY_DETAIL_ALREADY_RECEIVED', _('历史补录明细不能再次入库')
                )
            if not self.context.get('approval_application'):
                if detail.receipt.operator_id != self.user.pk:
                    raise ScanStockError(
                        'RECEIPT_OWNER_MISMATCH', _('请由该待入库记录的填单人扫码核对')
                    )
                if self.data.get('receipt_id') != detail.receipt_id:
                    raise ScanStockError(
                        'RECEIPT_MISMATCH', _('标签不属于当前待入库记录')
                    )
                if self.quantity != detail.prepared_quantity and not self.data.get(
                    'quantity_confirmed'
                ):
                    raise ScanStockError(
                        'ACTUAL_QUANTITY_CONFIRMATION_REQUIRED',
                        _('实际数量与准备数量不同，请明确核对确认'),
                    )
            if detail.receipt.location_id != self.data['destination_location'].pk:
                raise ScanStockError(
                    'RECEIPT_LOCATION_MISMATCH', _('入库库位与准备记录不一致')
                )
            active = detail.events.filter(operation='INBOUND').exclude(outcome='FAILED')
            active = active.exclude(client_event_id=self.data['client_event_id'])
            if active.exists():
                raise ScanStockError(
                    'RECEIPT_ALREADY_REQUESTED',
                    _('同一码已核对或已入库，请定位原行；后续到货请另建明细'),
                )
        elif (
            not detail.legacy
            and not detail.events.filter(
                operation='INBOUND', outcome='SUCCESS'
            ).exists()
        ):
            raise ScanStockError('RECEIPT_NOT_RECEIVED', _('该明细尚未审批入库'))

    def _prepare_pending_inbound(self, resolved: ResolvedBarcode) -> StockItem | None:
        """Resolve the future inbound target without creating or increasing stock."""
        destination = self.data['destination_location']
        if destination.structural:
            raise ScanStockError(
                'STRUCTURAL_LOCATION', _('Structural locations cannot contain stock')
            )

        if self.source_detail is not None:
            if self.data.get('stock_item_id') or self.data.get('create_stock_item'):
                raise ScanStockError(
                    'RECEIPT_STOCK_SELECTION_FORBIDDEN',
                    _('完整码入库由服务端定位来源明细'),
                )
            self._require_add_stockitem_permission()
            return None

        item = self._selected_stock_item(resolved)
        if item is not None:
            if self.data.get('create_stock_item'):
                raise ScanStockError(
                    'STOCK_ITEM_CREATE_CONFLICT',
                    _('A scanned stock item cannot also request a new stock item'),
                )
            self._validate_part_and_item(resolved.part, item, destination)
            return item

        if self.data.get('create_stock_item'):
            self._require_add_stockitem_permission()
            return None

        candidates = list(
            StockItem.objects.select_for_update()
            .filter(
                part=resolved.part,
                location=destination,
                batch=self.data.get('batch') or '',
                source_detail=self.source_detail,
            )
            .order_by('pk')
        )
        if not candidates:
            self._require_add_stockitem_permission()
            return None
        if (
            not self._has_explicit_stock_context()
            and len(candidates) == 1
            and self._is_neutral(candidates[0])
        ):
            return candidates[0]
        raise self._selection_required(candidates)

    def _prepare_pending_outbound(self, resolved: ResolvedBarcode) -> StockItem:
        """Resolve and reserve one exact outbound stock item under its row lock."""
        source = self.data['source_location']
        item = self._selected_stock_item(resolved)
        if item is None:
            candidates = list(
                StockItem.objects.select_for_update()
                .filter(
                    part=resolved.part,
                    location=source,
                    batch=self.data.get('batch') or '',
                    source_detail=self.source_detail,
                )
                .filter(StockItem.IN_STOCK_FILTER)
                .filter(
                    Q(expiry_date__isnull=True)
                    | Q(expiry_date__gte=InvenTree.helpers.current_date())
                )
                .order_by('pk')
            )
            candidates = [item for item in candidates if self._outbound_eligible(item)]
            if not candidates:
                raise ScanStockError(
                    'NO_AVAILABLE_STOCK',
                    _('No available stock exists in this location'),
                )
            if len(candidates) > 1:
                raise self._selection_required(candidates)
            item = candidates[0]

        self._validate_part_and_item(resolved.part, item, source)
        available = item.unallocated_quantity()
        if available < self.quantity:
            raise ScanStockError(
                'INSUFFICIENT_AVAILABLE_STOCK',
                _('Available stock is lower than the requested quantity'),
                details={'available_quantity': str(available)},
            )
        return item

    def _validate_pending_part_setup(self) -> None:
        """Validate unknown inbound setup without creating a Part."""
        create_part = self.data.get('create_part')
        if not create_part:
            raise ScanStockPreflight('SETUP_REQUIRED', _('Product setup is required'))
        if not self.context.get('approval_application') and not (
            check_user_permission(self.user, part_models.Part, 'add')
            and check_user_permission(self.user, StockItem, 'add')
        ):
            raise ScanStockError(
                'QUICK_SETUP_PERMISSION_DENIED',
                _('You do not have permission to create products'),
            )
        if len(self.barcode) > part_models.Part._meta.get_field('IPN').max_length:
            raise ScanStockError(
                'IPN_TOO_LONG', _('The scanned IPN is too long to create a product')
            )

    def _part_setup_snapshot(self) -> dict:
        """Return a JSON-safe normalized unknown-Part setup snapshot."""
        setup = self.data.get('create_part') or {}
        if not setup:
            return {}
        return {
            'name': setup.get('name') or '',
            'units': setup.get('units') or '',
            'brand': setup.get('brand') or '',
            'model_number': setup.get('model_number') or '',
            'specification': setup.get('specification') or '',
            'description': setup.get('description') or '',
            'category_id': getattr(setup.get('category'), 'pk', None),
            'default_location_id': getattr(setup.get('default_location'), 'pk', None),
        }

    def _stock_context_snapshot(self) -> dict:
        """Return a JSON-safe snapshot of target selection and stock semantics."""
        expiry = self.data.get('expiry_date')
        return {
            'stock_item_id': self.data.get('stock_item_id'),
            'create_stock_item': bool(self.data.get('create_stock_item')),
            'batch': self.data.get('batch') or '',
            'status': self.data.get('status', StockStatus.OK.value),
            'packaging': self.data.get('packaging') or '',
            'supplier_part_id': getattr(self.data.get('supplier_part'), 'pk', None),
            'expiry_date': expiry.isoformat() if expiry else None,
            'explicit_fields': [
                field
                for field in (
                    'batch',
                    'status',
                    'packaging',
                    'supplier_part',
                    'expiry_date',
                )
                if field in self.data
            ],
        }

    def _record_preflight_business_failure(
        self, code: str, message, *, resolved: ResolvedBarcode | None = None
    ) -> ScanStockResult:
        """Claim and persist a known business failure which needs no stock selection."""
        with transaction.atomic():
            existing = self._locked_existing_event()
            if existing is not None:
                return self._replay(existing)

            try:
                with transaction.atomic():
                    event = ScanStockEvent.objects.create(
                        **self._pending_event_fields()
                    )
            except IntegrityError:
                existing = self._locked_existing_event()
                if existing is None:
                    raise
                return self._replay(existing)

            failure = ScanStockError(code, message)
            self._fail_event(event, failure, resolved=resolved)
            return ScanStockResult(event=event, details=failure.details)

    @staticmethod
    def _validation_error_message(exc) -> str:
        """Return a stable, non-stacktrace message for framework validation errors."""
        if isinstance(exc, DRFValidationError):
            detail = exc.detail
        else:
            detail = getattr(exc, 'message_dict', None) or getattr(
                exc, 'messages', None
            )

        if isinstance(detail, dict):
            parts = []
            for field, messages in detail.items():
                if not isinstance(messages, (list, tuple)):
                    messages = [messages]
                parts.extend(f'{field}: {message}' for message in messages)
            return '; '.join(parts)[:500]

        if isinstance(detail, (list, tuple)):
            return '; '.join(str(message) for message in detail)[:500]

        return str(detail or exc)[:500]

    def _fingerprint(self) -> str:
        """Return a stable v1 fingerprint for every side-effecting input."""
        create_part = self.data.get('create_part') or {}
        normalized_create_part = {
            key: getattr(value, 'pk', value)
            for key, value in sorted(create_part.items())
        }
        payload = {
            'v': 'v1',
            'operator': self.user.pk,
            'operation': self.operation,
            'barcode': self.barcode,
            'quantity': str(self.quantity),
            'source_location': getattr(self.data.get('source_location'), 'pk', None),
            'destination_location': getattr(
                self.data.get('destination_location'), 'pk', None
            ),
            'stock_item_id': self.data.get('stock_item_id'),
            'create_stock_item': self.data.get('create_stock_item', False),
            'batch': self.data.get('batch') or '',
            'status': self.data.get('status'),
            'packaging': self.data.get('packaging') or '',
            'supplier_part': getattr(self.data.get('supplier_part'), 'pk', None),
            'expiry_date': str(self.data.get('expiry_date') or ''),
            'create_part': normalized_create_part,
            'purpose': self.data.get('purpose') or '',
            'remark': self.data.get('remark') or '',
            'device_id': self.data.get('device_id') or '',
            'session_id': self.data.get('session_id') or '',
            'approval_request_id': self.approval_request_id,
            **(
                {
                    'receipt_id': self.data.get('receipt_id'),
                    'quantity_confirmed': bool(self.data.get('quantity_confirmed')),
                }
                if self.barcode.startswith('WMS1:')
                else {}
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _pending_event_fields(self) -> dict:
        """Return fields used to claim a client event identifier."""
        source = self.data.get('source_location')
        destination = self.data.get('destination_location')
        return {
            'client_event_id': self.data['client_event_id'],
            'request_fingerprint': self.fingerprint,
            'operation': self.operation,
            'barcode': self.barcode,
            'barcode_hash': InvenTree.helpers.hash_barcode(self.barcode),
            'requested_quantity': self.quantity,
            'source_location': source,
            'destination_location': destination,
            'source_location_path_snapshot': source.pathstring if source else '',
            'destination_location_path_snapshot': (
                destination.pathstring if destination else ''
            ),
            'operator': self.user,
            'actor_kind': ScanStockEvent.ActorKind.USER,
            'purpose': self.data.get('purpose') or '',
            'remark': self.data.get('remark') or '',
            'device_id': self.data.get('device_id') or '',
            'session_id': self.data.get('session_id') or '',
            **operator_snapshot(self.user),
        }

    def _locked_existing_event(self):
        """Return an existing event under a row lock."""
        return (
            ScanStockEvent.objects.select_for_update()
            .filter(client_event_id=self.data['client_event_id'])
            .first()
        )

    def _replay(self, event: ScanStockEvent) -> ScanStockResult:
        """Replay a matching terminal event without touching stock."""
        if event.fingerprint_version == 'legacy-v0':
            replay_matches = (
                event.operation == ScanStockEvent.Operation.INBOUND
                and self.operation == ScanStockEvent.Operation.INBOUND
                and event.barcode == self.barcode
                and event.operator_user_id_snapshot == self.user.pk
            )
        else:
            replay_matches = (
                event.fingerprint_version == 'v1'
                and event.request_fingerprint == self.fingerprint
            )

        if not replay_matches:
            raise ScanStockIdempotencyConflict(
                'IDEMPOTENCY_CONFLICT',
                _('Client event ID was already used for another stock scan request'),
            )

        event.replay_count += 1
        event.last_replayed_at = InvenTree.helpers.current_time()
        event.save(update_fields=['replay_count', 'last_replayed_at'])
        return ScanStockResult(event=event, replayed=True)

    def _resolve_barcode(self) -> ResolvedBarcode:
        """Resolve internal codes, a product IPN, or a linked custom barcode."""
        if re.match(r'^WMS[0-9]+:', self.barcode):
            from stock.warranty import resolve_label

            try:
                detail = resolve_label(self.barcode)
            except DjangoValidationError as exc:
                raise ScanStockError(
                    'INVALID_COMPLETE_LABEL', self._validation_error_message(exc)
                ) from exc
            return ResolvedBarcode(
                kind=ScanStockEvent.BarcodeKind.RECEIPT_DETAIL,
                part=detail.part,
                detail=detail,
            )

        match = self.INTERNAL_BARCODE_PATTERN.match(self.barcode)

        if match:
            model_code, raw_pk = match.groups()
            model = {'PA': part_models.Part, 'SI': StockItem, 'SL': StockLocation}[
                model_code
            ]
            try:
                instance = model.objects.get(pk=int(raw_pk))
            except model.DoesNotExist:
                raise ScanStockError(
                    'INTERNAL_BARCODE_NOT_FOUND',
                    _('The scanned InvenTree object no longer exists'),
                )
            return self._resolved_instance(instance)

        if self.barcode.startswith(('INV-PA', 'INV-SI', 'INV-SL')):
            raise ScanStockError(
                'INVALID_INTERNAL_BARCODE', _('The InvenTree barcode is invalid')
            )

        try:
            data = json.loads(self.barcode)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            for key, model in (
                ('part', part_models.Part),
                ('stockitem', StockItem),
                ('stocklocation', StockLocation),
            ):
                if key in data:
                    try:
                        return self._resolved_instance(model.objects.get(pk=data[key]))
                    except (model.DoesNotExist, TypeError, ValueError):
                        raise ScanStockError(
                            'INTERNAL_BARCODE_NOT_FOUND',
                            _('The scanned InvenTree object no longer exists'),
                        )

        barcode_hash = InvenTree.helpers.hash_barcode(self.barcode)
        ipn_hash = InvenTree.helpers.hash_part_ipn(self.barcode)
        matches = list(
            part_models.Part.objects.filter(
                Q(ipn_hash=ipn_hash) | Q(IPN__iexact=self.barcode)
            ).order_by('pk')[:2]
        )
        for model in (part_models.Part, StockItem, StockLocation):
            instances = list(
                model.objects.filter(
                    Q(barcode_hash=barcode_hash) | Q(barcode_data=self.barcode)
                ).order_by('pk')[:2]
            )
            matches.extend(instances)

        # A legacy Part can have an IPN and linked custom barcode with the same
        # payload. Treat that as one match, while still rejecting cross-object
        # collisions and duplicate IPNs.
        matches = list(
            {(type(instance), instance.pk): instance for instance in matches}.values()
        )

        if len(matches) > 1:
            raise ScanStockError(
                'AMBIGUOUS_BARCODE', _('Barcode is assigned to multiple objects')
            )

        if matches:
            return self._resolved_instance(matches[0])

        return ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.EXTERNAL)

    @staticmethod
    def _resolved_instance(instance) -> ResolvedBarcode:
        """Wrap a resolved model instance."""
        if isinstance(instance, part_models.Part):
            return ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.PART, part=instance)
        if isinstance(instance, StockItem):
            return ResolvedBarcode(
                kind=ScanStockEvent.BarcodeKind.STOCK_ITEM,
                part=instance.part,
                stock_item=instance,
            )
        return ResolvedBarcode(
            kind=ScanStockEvent.BarcodeKind.LOCATION, location=instance
        )

    def _resolve_locked(self, resolved: ResolvedBarcode) -> ResolvedBarcode:
        """Resolve again and lock registration, Part and exact stock rows."""
        current = self._resolve_barcode()

        if current.part is None and self.operation == ScanStockEvent.Operation.INBOUND:
            current = self._create_part_for_unknown_barcode()

        if current.part is None:
            raise ScanStockError('UNKNOWN_BARCODE', _('Unknown product barcode'))

        current.part = part_models.Part.objects.select_for_update().get(
            pk=current.part.pk
        )

        if current.stock_item is not None:
            current.stock_item = (
                StockItem.objects.select_for_update()
                .select_related('part')
                .get(pk=current.stock_item.pk)
            )

        self._bind_receipt_detail(current, claim_inbound=False)
        # Keep the caller's object synchronized before any validation can fail,
        # so a concurrent-registration conflict still persists the winner Part /
        # StockItem snapshots on the FAILED event.
        resolved.kind = current.kind
        resolved.part = current.part
        resolved.stock_item = current.stock_item
        resolved.detail = current.detail
        resolved.location = current.location

        if not current.part.active:
            raise ScanStockError('PART_INACTIVE', _('设备已停用'))

        if self.data.get('create_part'):
            if current.kind != ScanStockEvent.BarcodeKind.PART:
                raise ScanStockError(
                    'BARCODE_IN_USE', _('Barcode is already assigned to another object')
                )
            self._validate_registration(current.part, self.data['create_part'])

        return current

    def _create_part_for_unknown_barcode(self) -> ResolvedBarcode:
        """Create an unknown inbound Part using the scanned value as its IPN."""
        create_part = self.data.get('create_part')
        if not create_part:
            raise ScanStockPreflight('SETUP_REQUIRED', _('Product setup is required'))

        if not self.context.get('approval_application') and not (
            check_user_permission(self.user, part_models.Part, 'add')
            and check_user_permission(self.user, StockItem, 'add')
        ):
            raise ScanStockError(
                'QUICK_SETUP_PERMISSION_DENIED',
                _('You do not have permission to create products'),
            )

        if len(self.barcode) > part_models.Part._meta.get_field('IPN').max_length:
            raise ScanStockError(
                'IPN_TOO_LONG', _('The scanned IPN is too long to create a product')
            )

        raw_barcode_hash = InvenTree.helpers.hash_barcode(self.barcode)
        self._lock_ipn_registration(raw_barcode_hash)

        ipn_hash = InvenTree.helpers.hash_part_ipn(self.barcode)
        if (
            existing := part_models.Part.objects.select_for_update()
            .filter(Q(ipn_hash=ipn_hash) | Q(IPN__iexact=self.barcode))
            .first()
        ):
            return ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.PART, part=existing)

        self._validate_barcode_available(raw_barcode_hash)
        destination = self.data['destination_location']
        part_data = {
            **create_part,
            'IPN': self.barcode,
            'category': (
                create_part['category'].pk if create_part.get('category') else None
            ),
            'default_location': destination.pk,
        }
        serializer = part_serializers.PartSerializer(
            data=part_data, context=self.context, create=True
        )
        try:
            serializer.is_valid(raise_exception=True)
            with transaction.atomic():
                part = serializer.save()
        except (IntegrityError, DjangoValidationError, DRFValidationError) as exc:
            winner = (
                part_models.Part.objects.select_for_update()
                .filter(Q(ipn_hash=ipn_hash) | Q(IPN__iexact=self.barcode))
                .first()
            )
            if winner is None:
                raise ScanStockError(
                    'PRODUCT_CREATE_CONFLICT', self._validation_error_message(exc)
                ) from exc

            self._validate_registration(winner, create_part)
            return ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.PART, part=winner)

        self.part_created = True
        return ResolvedBarcode(kind=ScanStockEvent.BarcodeKind.PART, part=part)

    def _lock_ipn_registration(self, raw_barcode_hash: str) -> None:
        """Lock the raw barcode and canonical IPN namespaces in stable order."""
        canonical_ipn_hash = InvenTree.helpers.hash_barcode(
            f'ipn:{self.barcode.casefold()}'
        )
        lock_hashes = sorted({raw_barcode_hash, canonical_ipn_hash})

        for lock_hash in lock_hashes:
            ScanInboundBarcodeLock.objects.get_or_create(barcode_hash=lock_hash)

        for lock_hash in lock_hashes:
            ScanInboundBarcodeLock.objects.select_for_update().get(
                barcode_hash=lock_hash
            )

    def _validate_barcode_available(self, barcode_hash: str) -> None:
        """Reject unknown setup when another barcode-capable object owns the code."""
        for model in barcode_helper.get_supported_barcode_models():
            if model.objects.filter(
                Q(barcode_hash=barcode_hash) | Q(barcode_data=self.barcode)
            ).exists():
                raise ScanStockError(
                    'BARCODE_IN_USE', _('Barcode is already assigned to another object')
                )

    def _validate_registration(self, part, create_part: dict) -> None:
        """Reject stale quick-setup details which target another product."""
        category = create_part.get('category')
        expected = {
            'name': create_part['name'],
            **{
                key: create_part[key]
                for key in ('units', 'brand', 'model_number', 'specification')
                if key in create_part
            },
            'description': create_part.get('description') or '',
            'category_id': category.pk if category else None,
            'default_location_id': self.data['destination_location'].pk,
        }
        ipn_matches = (part.IPN or '').casefold() == self.barcode.casefold()
        if not ipn_matches or any(
            getattr(part, key) != value for key, value in expected.items()
        ):
            raise ScanStockError(
                'IPN_REGISTRATION_CONFLICT',
                _('IPN was registered to different product details'),
            )

    def _selection_preflight(self, resolved: ResolvedBarcode) -> None:
        """Return non-terminal selection requirements before claiming the UUID."""
        if resolved.detail is not None:
            return
        if resolved.part is None or resolved.stock_item is not None:
            return
        if self.data.get('stock_item_id') or self.data.get('create_stock_item'):
            return

        location = (
            self.data.get('destination_location')
            if self.operation == ScanStockEvent.Operation.INBOUND
            else self.data.get('source_location')
        )
        candidates = list(
            StockItem.objects.filter(
                part=resolved.part,
                location=location,
                batch=self.data.get('batch') or '',
                source_detail__isnull=True,
            ).order_by('pk')
        )

        if self.operation == ScanStockEvent.Operation.INBOUND:
            if candidates and not (
                not self._has_explicit_stock_context()
                and len(candidates) == 1
                and self._is_neutral(candidates[0])
            ):
                raise self._selection_required(candidates)
        else:
            eligible = [item for item in candidates if self._outbound_eligible(item)]
            if len(eligible) > 1:
                raise self._selection_required(eligible)

    def _selection_required(self, candidates: list[StockItem]) -> ScanStockPreflight:
        """Build a safe candidate selection response."""
        return ScanStockPreflight(
            'STOCK_SELECTION_REQUIRED',
            _('Select a stock item before completing this scan'),
            details={
                'candidates': [
                    {
                        'pk': item.pk,
                        'batch': item.batch or '',
                        'status': item.status,
                        'packaging': item.packaging or '',
                        'quantity': str(item.quantity),
                        'unallocated_quantity': str(item.unallocated_quantity()),
                        'expiry_date': (
                            item.expiry_date.isoformat() if item.expiry_date else None
                        ),
                        'legacy_scan_inbound_item': item.is_scan_inbound_item,
                    }
                    for item in candidates
                ]
            },
        )

    def _has_explicit_stock_context(self) -> bool:
        """Return whether the request specifies non-selection stock attributes."""
        return any(
            field in self.data
            for field in ('status', 'packaging', 'supplier_part', 'expiry_date')
        )

    @staticmethod
    def _is_neutral(item: StockItem) -> bool:
        """Return whether a stock item has the neutral aggregation context."""
        return all(
            [
                item.status == StockStatus.OK.value,
                not item.packaging,
                item.supplier_part_id is None,
                item.expiry_date is None,
                not item.serial,
                item.sales_order_id is None,
                item.belongs_to_id is None,
                item.customer_id is None,
                item.consumed_by_id is None,
                not item.is_building,
                not item.is_scan_inbound_item,
            ]
        )

    @staticmethod
    def _outbound_eligible(item: StockItem) -> bool:
        """Return whether an item is a valid direct outbound candidate."""
        return (
            not item.serialized
            and item.in_stock
            and not item.is_expired()
            and item.unallocated_quantity() > 0
        )

    def _selected_stock_item(self, resolved: ResolvedBarcode) -> StockItem | None:
        """Return and lock an exact explicit stock item, if supplied."""
        stock_item_id = self.data.get('stock_item_id')
        if resolved.stock_item is not None:
            if stock_item_id and resolved.stock_item.pk != stock_item_id:
                raise ScanStockError(
                    'STOCK_ITEM_CONFLICT',
                    _('Selected stock item does not match the scanned stock item'),
                )
            return resolved.stock_item

        if stock_item_id:
            try:
                return (
                    StockItem.objects.select_for_update()
                    .select_related('part')
                    .get(pk=stock_item_id)
                )
            except StockItem.DoesNotExist as exc:
                raise ScanStockError(
                    'STOCK_ITEM_NOT_FOUND', _('Selected stock item no longer exists')
                ) from exc
        return None

    def _validate_part_and_item(self, part, item, location) -> None:
        """Validate an explicit item under its database row lock."""
        if item.source_detail_id != getattr(self.source_detail, 'pk', None):
            raise ScanStockError(
                'STOCK_DETAIL_MISMATCH', _('请扫描该库存明细的当前完整标签')
            )
        if item.part_id != part.pk:
            raise ScanStockError(
                'STOCK_ITEM_PART_MISMATCH',
                _('Selected stock item belongs to another product'),
            )
        if item.location_id != location.pk:
            raise ScanStockError(
                'STOCK_ITEM_LOCATION_MISMATCH',
                _('Selected stock item is in another stock location'),
            )
        if item.serialized or part.trackable:
            raise ScanStockError(
                'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                _(
                    'Serialized stock must use the native receiving or shipping workflow'
                ),
            )

        expected_fields = {
            'batch': self.data.get('batch'),
            'status': self.data.get('status'),
            'packaging': self.data.get('packaging'),
            'supplier_part_id': getattr(self.data.get('supplier_part'), 'pk', None),
            'expiry_date': self.data.get('expiry_date'),
        }
        for field, expected in expected_fields.items():
            if field not in self.data and field.removesuffix('_id') not in self.data:
                continue
            actual = getattr(item, field)
            if (actual or None) != (expected or None):
                raise ScanStockError(
                    'STOCK_ITEM_CONTEXT_MISMATCH',
                    _('Selected stock item no longer matches the requested context'),
                )

    def _apply_inbound(self, event: ScanStockEvent, resolved: ResolvedBarcode) -> None:
        """Add stock to one compatible item through the native model method."""
        part = resolved.part
        destination = self.data['destination_location']
        if destination.structural:
            raise ScanStockError(
                'STRUCTURAL_LOCATION', _('Structural locations cannot contain stock')
            )
        if part.trackable:
            raise ScanStockError(
                'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                _('Serialized stock must use the native receiving workflow'),
            )

        if self.source_detail is not None:
            self.data['create_stock_item'] = True

        item = self._selected_stock_item(resolved)
        if item is not None:
            if self.data.get('create_stock_item'):
                raise ScanStockError(
                    'STOCK_ITEM_CREATE_CONFLICT',
                    _('A scanned stock item cannot also request a new stock item'),
                )
            self._validate_part_and_item(part, item, destination)
        elif self.data.get('create_stock_item'):
            self._require_add_stockitem_permission()
            item = self._create_stock_item(part, destination)
        else:
            candidates = list(
                StockItem.objects.select_for_update()
                .filter(
                    part=part,
                    location=destination,
                    batch=self.data.get('batch') or '',
                    source_detail=self.source_detail,
                )
                .order_by('pk')
            )
            if not candidates:
                self._require_add_stockitem_permission()
                item = self._create_stock_item(part, destination)
            elif (
                not self._has_explicit_stock_context()
                and len(candidates) == 1
                and self._is_neutral(candidates[0])
            ):
                item = candidates[0]
            else:
                raise self._selection_required(candidates)

        self._apply_event_context(event, resolved, item=item)
        before = item.quantity
        total_before = part.get_stock_count(include_variants=False)
        if not item.add_stock(
            self.quantity, self.user, notes=_('Scan stock inbound'), scan_event=event
        ):
            raise ScanStockError('STOCK_ADD_FAILED', _('Unable to add scanned stock'))
        self._complete_event(event, item, before, total_before)

    def _create_stock_item(self, part, destination) -> StockItem:
        """Create a zero-quantity item before applying a single native STOCK_ADD."""
        item = StockItem(
            part=part,
            location=destination,
            quantity=0,
            source_detail=self.source_detail,
            batch=self.data.get('batch') or '',
            status=self.data.get('status', StockStatus.OK.value),
            packaging=self.data.get('packaging') or None,
            supplier_part=self.data.get('supplier_part'),
            expiry_date=self.data.get('expiry_date'),
        )
        item.save(add_note=False)
        return item

    def _require_add_stockitem_permission(self) -> None:
        """Enforce create permission only on paths which create a stock row."""
        if not self.context.get('approval_application') and not check_user_permission(
            self.user, StockItem, 'add'
        ):
            raise ScanStockError(
                'STOCK_ITEM_CREATE_PERMISSION_DENIED',
                _('You do not have permission to create a stock item'),
            )

    def _apply_outbound(self, event: ScanStockEvent, resolved: ResolvedBarcode) -> None:
        """Strictly remove unallocated stock through the native model method."""
        part = resolved.part
        source = self.data['source_location']
        if part.trackable:
            raise ScanStockError(
                'SERIALIZED_STOCK_REQUIRES_ORDER_FLOW',
                _('Serialized stock must use the native shipping workflow'),
            )

        item = self._selected_stock_item(resolved)
        if item is None:
            candidates = list(
                StockItem.objects.select_for_update()
                .filter(
                    part=part,
                    location=source,
                    batch=self.data.get('batch') or '',
                    source_detail=self.source_detail,
                )
                .filter(StockItem.IN_STOCK_FILTER)
                .filter(
                    Q(expiry_date__isnull=True)
                    | Q(expiry_date__gte=InvenTree.helpers.current_date())
                )
                .order_by('pk')
            )
            candidates = [
                candidate
                for candidate in candidates
                if self._outbound_eligible(candidate)
            ]
            if not candidates:
                raise ScanStockError(
                    'NO_AVAILABLE_STOCK',
                    _('No available stock exists in this location'),
                )
            if len(candidates) > 1:
                raise self._selection_required(candidates)
            item = candidates[0]

        self._validate_part_and_item(part, item, source)
        if not self._outbound_eligible(item):
            raise ScanStockError(
                'STOCK_NOT_AVAILABLE', _('Selected stock is not available for outbound')
            )

        available = item.unallocated_quantity()
        if available < self.quantity:
            raise ScanStockError(
                'INSUFFICIENT_UNALLOCATED_STOCK',
                _('Available unallocated stock is lower than the requested quantity'),
                details={'available_quantity': str(available)},
            )

        self._apply_event_context(event, resolved, item=item)
        before = item.quantity
        total_before = part.get_stock_count(include_variants=False)
        if not item.take_stock(
            self.quantity, self.user, notes=_('Scan stock outbound'), scan_event=event
        ):
            raise ScanStockError(
                'STOCK_REMOVE_FAILED', _('Unable to remove scanned stock')
            )
        self._complete_event(event, item, before, total_before)

    def _apply_event_context(
        self,
        event: ScanStockEvent,
        resolved: ResolvedBarcode,
        *,
        item: StockItem | None = None,
    ) -> None:
        """Populate mutable business context while the event is still pending."""
        part = resolved.part
        item = item or resolved.stock_item
        event.barcode_kind = resolved.kind
        event.part = part
        event.part_pk_snapshot = part.pk if part else None
        event.part_name_snapshot = part.name if part else ''
        event.part_ipn_snapshot = part.IPN if part else ''
        event.units_snapshot = (
            (part.units or '')
            if part
            else (event.requested_part_setup or {}).get('units', '')
        )
        event.batch_snapshot = self.data.get('batch') or ''
        if self.source_detail is not None and event.source_detail_id is None:
            from stock.warranty import warranty_end

            detail = self.source_detail
            event.source_detail = detail
            event.detail_uid_snapshot = detail.uid
            event.label_version_snapshot = detail.label_version
            event.contract_number_snapshot = detail.contract.number
            event.acceptance_date_snapshot = detail.effective_acceptance_date
            event.warranty_months_snapshot = detail.effective_warranty_months
            event.warranty_end_snapshot = warranty_end(
                detail.effective_acceptance_date, detail.effective_warranty_months
            )
        if item:
            event.stock_item = item
            event.stock_item_pk_snapshot = item.pk
            event.batch_snapshot = item.batch or ''

    def _complete_event(
        self,
        event: ScanStockEvent,
        item: StockItem,
        before: Decimal,
        total_before: Decimal,
    ) -> None:
        """Finalize a successful event in the stock transaction."""
        after = item.quantity
        event.stock_item_quantity_before = before
        event.stock_item_quantity_after = after
        if item.pk is None:
            event.stock_item = None
        event.part_total_before = total_before
        event.part_total_after = (
            total_before + self.quantity
            if self.operation == ScanStockEvent.Operation.INBOUND
            else total_before - self.quantity
        )
        if self.context.get('approval_application'):
            event.approver_snapshot = self.user.get_full_name() or self.user.username
        event.applied_quantity = self.quantity
        event.outcome = ScanStockEvent.Outcome.SUCCESS
        event.completed_at = InvenTree.helpers.current_time()
        event.save()

    def _fail_event(
        self,
        event: ScanStockEvent,
        exc: ScanStockError,
        *,
        resolved: ResolvedBarcode | None = None,
    ) -> None:
        """Finalize an expected failure after the inventory savepoint rolls back."""
        event.refresh_from_db()
        if resolved and resolved.part:
            part = resolved.part
            event.part_pk_snapshot = part.pk
            event.part_name_snapshot = part.name
            event.part_ipn_snapshot = part.IPN or ''
            if part_models.Part.objects.filter(pk=part.pk).exists():
                event.part = part
        if resolved and resolved.stock_item:
            item = resolved.stock_item
            event.stock_item_pk_snapshot = item.pk
            event.batch_snapshot = item.batch or ''
            if StockItem.objects.filter(pk=item.pk).exists():
                event.stock_item = item
        event.outcome = ScanStockEvent.Outcome.FAILED
        event.applied_quantity = 0
        event.error_code = exc.code
        event.error_message = exc.message[:500]
        event.completed_at = InvenTree.helpers.current_time()
        event.save()


class ApprovalRequestService:
    """Transactional lifecycle service for scan stock approval requests."""

    APPROVE_PERMISSION = 'stock.approve_scanstockapprovalrequest'
    AUDIT_PERMISSION = 'stock.audit_scanstockapprovalrequest'

    @classmethod
    def create_request(cls, *, user, direction: str, location: StockLocation):
        """Create a draft with a fixed, auditable approval route."""
        if not get_global_setting('SCAN_APPROVAL_ENABLED', False):
            raise ScanApprovalError(
                'SCAN_APPROVAL_DISABLED', _('Scan stock approval is not enabled')
            )
        if not check_user_permission(user, ScanStockApprovalRequest, 'add'):
            raise ScanApprovalError(
                'SCAN_PERMISSION_DENIED',
                _('You do not have permission to create scan stock requests'),
            )
        if not location.check_ownership(user):
            raise ScanApprovalError(
                'LOCATION_PERMISSION_DENIED',
                _('You do not have access to this stock location'),
            )
        owner, approvers, route_location = cls.resolve_approval_owner(location, user)
        if owner is None or not approvers:
            raise ScanApprovalError(
                'NO_DISTINCT_APPROVER',
                _('No distinct active approver is configured for this location'),
            )

        now = InvenTree.helpers.current_time()
        timeout = int(get_global_setting('SCAN_APPROVAL_DRAFT_TIMEOUT_MINUTES', 30))
        snapshot = operator_snapshot(user)
        return ScanStockApprovalRequest.objects.create(
            direction=direction,
            location=location,
            location_path_snapshot=location.pathstring,
            requester=user,
            requester_user_id_snapshot=snapshot['operator_user_id_snapshot'],
            requester_username_snapshot=snapshot['operator_username_snapshot'],
            requester_display_name_snapshot=snapshot['operator_display_name_snapshot'],
            approval_owner=owner,
            approval_owner_snapshot={
                'pk': owner.pk,
                'name': owner.name(),
                'type': owner.label(),
                'location_id': location.pk,
                'location_path': location.pathstring,
                'route_location_id': route_location.pk,
                'route_location_path': route_location.pathstring,
            },
            activity_at=now,
            expires_at=now + timedelta(minutes=timeout),
        )

    @classmethod
    def resolve_approval_owner(
        cls, location: StockLocation, requester
    ) -> tuple[Owner | None, list[User], StockLocation | None]:
        """Resolve the nearest owner containing a distinct eligible approver."""
        for candidate in location.get_ancestors(include_self=True, ascending=True):
            owner = candidate.approval_owner
            if owner is None:
                continue
            approvers = cls.eligible_approvers(owner, exclude_user=requester)
            if approvers:
                return owner, approvers, candidate
        return None, [], None

    @classmethod
    def eligible_approvers(
        cls, owner: Owner | None, *, exclude_user=None
    ) -> list[User]:
        """Return active owner members with the explicit approval permission."""
        if owner is None or owner.owner is None:
            return []
        target = owner.owner
        if isinstance(target, User):
            users = User.objects.filter(pk=target.pk, is_active=True)
        elif isinstance(target, Group):
            users = target.user_set.filter(is_active=True).order_by('pk')
        else:
            return []
        if exclude_user is not None:
            users = users.exclude(pk=exclude_user.pk)
        return [user for user in users if user.has_perm(cls.APPROVE_PERMISSION)]

    @classmethod
    def validate_append(cls, request, user, operation: str) -> None:
        """Validate the immutable request context before appending a line."""
        if request.status != ScanStockApprovalRequest.Status.DRAFT:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_NOT_DRAFT',
                _('Only draft scan approval requests can accept lines'),
            )
        if request.requester_id != user.pk:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_NOT_REQUESTER',
                _('Only the requester can add scan lines'),
            )
        if not check_user_permission(user, ScanStockApprovalRequest, 'change'):
            raise ScanApprovalError(
                'SCAN_PERMISSION_DENIED',
                _('You no longer have permission to add scan lines'),
            )
        if not request.location.check_ownership(user):
            raise ScanApprovalError(
                'LOCATION_PERMISSION_DENIED',
                _('You no longer have access to this stock location'),
            )
        if request.direction != operation:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_DIRECTION_MISMATCH',
                _('Scan direction does not match the approval request'),
            )
        if request.expires_at <= InvenTree.helpers.current_time():
            raise ScanApprovalError(
                'APPROVAL_REQUEST_EXPIRED', _('Scan approval draft has expired')
            )
        if not cls.eligible_approvers(
            request.approval_owner, exclude_user=request.requester
        ):
            raise ScanApprovalError(
                'NO_DISTINCT_APPROVER',
                _('The configured approval route no longer has a distinct approver'),
            )

    @classmethod
    def submit(cls, *, request_id: int, user):
        """Submit a non-empty draft for whole-request approval."""
        with transaction.atomic():
            request = cls._locked_request(request_id)
            cls._require_requester(request, user)
            cls._require_status(request, ScanStockApprovalRequest.Status.DRAFT)
            if not check_user_permission(user, ScanStockApprovalRequest, 'change'):
                raise ScanApprovalError(
                    'SCAN_PERMISSION_DENIED',
                    _('You no longer have permission to submit scan requests'),
                )
            if not request.location.check_ownership(user):
                raise ScanApprovalError(
                    'LOCATION_PERMISSION_DENIED',
                    _('You no longer have access to this stock location'),
                )
            if request.expires_at <= InvenTree.helpers.current_time():
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_EXPIRED', _('Scan approval draft has expired')
                )
            if not request.lines.filter(
                outcome=ScanStockEvent.Outcome.PENDING
            ).exists():
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_EMPTY',
                    _('A scan approval request must contain at least one active line'),
                    conflict=False,
                )
            approvers = cls.eligible_approvers(
                request.approval_owner, exclude_user=request.requester
            )
            if not approvers:
                raise ScanApprovalError(
                    'NO_DISTINCT_APPROVER',
                    _(
                        'The configured approval route no longer has a distinct approver'
                    ),
                )
            now = InvenTree.helpers.current_time()
            timeout = int(
                get_global_setting('SCAN_APPROVAL_PENDING_TIMEOUT_MINUTES', 1440)
            )
            request.status = ScanStockApprovalRequest.Status.PENDING_APPROVAL
            request.submitted_at = now
            request.activity_at = now
            request.expires_at = now + timedelta(minutes=timeout)
            request.last_approval_error = ''
            request.save(
                update_fields=[
                    'status',
                    'submitted_at',
                    'activity_at',
                    'expires_at',
                    'last_approval_error',
                    'updated_at',
                ]
            )
            cls._notify_on_commit(request.pk, approvers, 'submitted')
            return request

    @classmethod
    def remove_line(cls, *, request_id: int, event_id: int, user):
        """Logically remove one pending line from a requester-owned draft."""
        with transaction.atomic():
            request = cls._locked_request(request_id)
            cls._require_requester(request, user)
            cls._require_status(request, ScanStockApprovalRequest.Status.DRAFT)
            try:
                event = request.lines.select_for_update().get(pk=event_id)
            except ScanStockEvent.DoesNotExist as exc:
                raise ScanApprovalError(
                    'APPROVAL_LINE_NOT_FOUND', _('Scan approval line does not exist')
                ) from exc
            if event.outcome != ScanStockEvent.Outcome.PENDING:
                raise ScanApprovalError(
                    'APPROVAL_LINE_NOT_ACTIVE', _('Scan approval line is not active')
                )
            cls._fail_pending_event(event, 'REMOVED_FROM_DRAFT')
            now = InvenTree.helpers.current_time()
            timeout = int(get_global_setting('SCAN_APPROVAL_DRAFT_TIMEOUT_MINUTES', 30))
            request.activity_at = now
            request.expires_at = now + timedelta(minutes=timeout)
            request.save(update_fields=['activity_at', 'expires_at', 'updated_at'])
            return request

    @classmethod
    def cancel(cls, *, request_id: int, user, note: str = ''):
        """Cancel a draft or pending request and release all pending lines."""
        with transaction.atomic():
            request = cls._locked_request(request_id)
            if request.requester_id != user.pk and not user.has_perm(
                cls.AUDIT_PERMISSION
            ):
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_NOT_REQUESTER',
                    _('Only the requester can cancel this request'),
                )
            if request.status not in ScanStockApprovalRequest.active_statuses():
                raise ScanApprovalError(
                    'APPROVAL_REQUEST_TERMINAL',
                    _('Only draft or pending requests can be cancelled'),
                )
            if (
                request.status == ScanStockApprovalRequest.Status.PENDING_APPROVAL
                and not note.strip()
            ):
                raise ScanApprovalError(
                    'CANCEL_NOTE_REQUIRED',
                    _('A cancellation reason is required after submission'),
                    conflict=False,
                )
            cls._terminalize_pending_lines(request, 'REQUEST_CANCELLED')
            now = InvenTree.helpers.current_time()
            request.status = ScanStockApprovalRequest.Status.CANCELLED
            request.cancelled_by = user
            request.cancelled_at = now
            request.cancel_note = note.strip()
            request.save(
                update_fields=[
                    'status',
                    'cancelled_by',
                    'cancelled_at',
                    'cancel_note',
                    'updated_at',
                ]
            )
            cls._notify_on_commit(request.pk, [request.requester], 'cancelled')
            return request

    @classmethod
    def reject(cls, *, request_id: int, user, note: str):
        """Reject an entire pending request and release every reservation."""
        if not note.strip():
            raise ScanApprovalError(
                'REJECTION_NOTE_REQUIRED',
                _('A rejection reason is required'),
                conflict=False,
            )
        with transaction.atomic():
            request = cls._locked_request(request_id)
            cls._require_status(
                request, ScanStockApprovalRequest.Status.PENDING_APPROVAL
            )
            cls._authorize_approver(request, user)
            cls._terminalize_pending_lines(request, 'REQUEST_REJECTED')
            now = InvenTree.helpers.current_time()
            request.status = ScanStockApprovalRequest.Status.REJECTED
            request.decided_by = user
            request.decided_at = now
            request.decision_note = note.strip()
            request.save(
                update_fields=[
                    'status',
                    'decided_by',
                    'decided_at',
                    'decision_note',
                    'updated_at',
                ]
            )
            cls._notify_on_commit(request.pk, [request.requester], 'rejected')
            return request

    @classmethod
    def approve(cls, *, request_id: int, user, note: str = ''):
        """Atomically apply all pending scan lines through native stock methods."""
        try:
            with transaction.atomic():
                request = cls._locked_request(request_id)
                cls._require_status(
                    request, ScanStockApprovalRequest.Status.PENDING_APPROVAL
                )
                cls._authorize_approver(request, user)
                if request.expires_at <= InvenTree.helpers.current_time():
                    raise ScanApprovalError(
                        'APPROVAL_REQUEST_EXPIRED',
                        _('Scan approval request has expired'),
                    )

                lines = list(
                    request.lines.select_for_update()
                    .filter(outcome=ScanStockEvent.Outcome.PENDING)
                    .order_by('request_line_number', 'pk')
                )
                if not lines:
                    raise ScanApprovalError(
                        'APPROVAL_REQUEST_EMPTY',
                        _('Scan approval request has no active lines'),
                    )

                stock_ids = sorted(
                    {
                        stock_id
                        for event in lines
                        for stock_id in (
                            event.stock_item_id,
                            (event.requested_stock_context or {}).get('stock_item_id'),
                        )
                        if stock_id
                    }
                )
                part_ids = sorted({event.part_id for event in lines if event.part_id})
                list(
                    part_models.Part.objects.select_for_update()
                    .filter(pk__in=part_ids)
                    .order_by('pk')
                )
                list(
                    StockItem.objects.select_for_update()
                    .filter(pk__in=stock_ids)
                    .order_by('pk')
                )

                # Pre-lock all unknown-IPN namespaces for the whole request in
                # stable order. Per-line registration then reuses these locks.
                barcode_lock_hashes = set()
                for event in lines:
                    if not event.requested_part_setup:
                        continue
                    barcode_lock_hashes.add(
                        InvenTree.helpers.hash_barcode(event.barcode)
                    )
                    barcode_lock_hashes.add(
                        InvenTree.helpers.hash_barcode(
                            f'ipn:{event.barcode.casefold()}'
                        )
                    )
                for barcode_hash in sorted(barcode_lock_hashes):
                    ScanInboundBarcodeLock.objects.get_or_create(
                        barcode_hash=barcode_hash
                    )
                list(
                    ScanInboundBarcodeLock.objects.select_for_update()
                    .filter(barcode_hash__in=barcode_lock_hashes)
                    .order_by('barcode_hash')
                )

                request.status = ScanStockApprovalRequest.Status.APPLYING
                request.save(update_fields=['status', 'updated_at'])

                for event in lines:
                    service = cls._service_for_event(event, request, user)
                    resolved = service._resolve_barcode()
                    resolved = service._resolve_locked(resolved)
                    cls._validate_approval_resolution(event, resolved)
                    service._apply_event_context(event, resolved)
                    if event.operation == ScanStockEvent.Operation.INBOUND:
                        service._apply_inbound(event, resolved)
                    else:
                        service._apply_outbound(event, resolved)

                now = InvenTree.helpers.current_time()
                request.status = ScanStockApprovalRequest.Status.COMPLETED
                request.decided_by = user
                request.decided_at = now
                request.decision_note = note.strip()
                request.last_approval_error = ''
                request.save(
                    update_fields=[
                        'status',
                        'decided_by',
                        'decided_at',
                        'decision_note',
                        'last_approval_error',
                        'updated_at',
                    ]
                )
                cls._notify_on_commit(request.pk, [request.requester], 'approved')
                return request
        except ScanApprovalError:
            raise
        except (ScanStockError, DjangoValidationError, DRFValidationError) as exc:
            if isinstance(exc, ScanStockError):
                code = exc.code
                message = exc.message
                details = exc.details
            else:
                code = 'APPROVAL_STOCK_VALIDATION_FAILED'
                message = ScanStockService._validation_error_message(exc)
                details = {}
            ScanStockApprovalRequest.objects.filter(
                pk=request_id, status=ScanStockApprovalRequest.Status.PENDING_APPROVAL
            ).update(last_approval_error=f'{code}: {message}'[:500])
            raise ScanApprovalError(code, message, details=details) from exc
        except (
            ObjectDoesNotExist,
            IntegrityError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            # Immutable line snapshots can refer to configuration which was
            # removed while the request waited for approval (for example a
            # supplier part or category). Keep the request retryable and expose
            # a stable, non-stacktrace conflict instead of returning HTTP 500.
            code = 'APPROVAL_SNAPSHOT_CONFLICT'
            message = str(_('The request context changed and can no longer be applied'))
            ScanStockApprovalRequest.objects.filter(
                pk=request_id, status=ScanStockApprovalRequest.Status.PENDING_APPROVAL
            ).update(last_approval_error=f'{code}: {message}'[:500])
            raise ScanApprovalError(code, message) from exc

    @classmethod
    def expire(cls, request_id: int) -> bool:
        """Idempotently expire one due active request and release its lines."""
        with transaction.atomic():
            request = cls._locked_request(request_id)
            if request.status not in ScanStockApprovalRequest.active_statuses():
                return False
            if request.expires_at > InvenTree.helpers.current_time():
                return False
            cls._terminalize_pending_lines(request, 'REQUEST_EXPIRED')
            request.status = ScanStockApprovalRequest.Status.EXPIRED
            request.save(update_fields=['status', 'updated_at'])
            users = [request.requester]
            users.extend(
                cls.eligible_approvers(
                    request.approval_owner, exclude_user=request.requester
                )
            )
            cls._notify_on_commit(request.pk, users, 'expired')
            return True

    @classmethod
    def _service_for_event(cls, event, request, user) -> ScanStockService:
        """Reconstruct a validated service payload from immutable line snapshots."""
        context = event.requested_stock_context or {}
        data = {
            'barcode': event.barcode,
            'client_event_id': event.client_event_id,
            'quantity': event.requested_quantity,
            'purpose': event.purpose,
            'remark': event.remark,
            'approval_request_id': request.pk,
        }
        if event.source_detail_id:
            data['receipt_id'] = event.source_detail.receipt_id
            data['quantity_confirmed'] = True
        if event.operation == ScanStockEvent.Operation.INBOUND:
            data['destination_location'] = request.location
        else:
            data['source_location'] = request.location
        stock_item_id = (
            event.stock_item_id
            or context.get('stock_item_id')
            or event.stock_item_pk_snapshot
        )
        if stock_item_id:
            data['stock_item_id'] = stock_item_id
        if context.get('create_stock_item'):
            data['create_stock_item'] = True

        explicit = set(context.get('explicit_fields') or [])
        for field in ('batch', 'status', 'packaging'):
            if field in explicit:
                data[field] = context.get(field)
        if 'supplier_part' in explicit and context.get('supplier_part_id'):
            data['supplier_part'] = company_models.SupplierPart.objects.get(
                pk=context['supplier_part_id']
            )
        if 'expiry_date' in explicit and context.get('expiry_date'):
            data['expiry_date'] = date.fromisoformat(context['expiry_date'])

        setup = event.requested_part_setup or {}
        if setup:
            category = None
            if setup.get('category_id'):
                category = part_models.PartCategory.objects.get(pk=setup['category_id'])
            data['create_part'] = {
                'name': setup['name'],
                'units': setup.get('units') or '',
                'brand': setup.get('brand') or '',
                'model_number': setup.get('model_number') or '',
                'specification': setup.get('specification') or '',
                'description': setup.get('description') or '',
                'category': category,
                'default_location': request.location,
            }
        return ScanStockService(
            user=user,
            operation=event.operation,
            data=data,
            context={'approval_application': True},
        )

    @staticmethod
    def _validate_approval_resolution(event, resolved: ResolvedBarcode) -> None:
        """Reject barcode remapping away from the Part frozen into the line audit."""
        expected_part_id = event.part_id or event.part_pk_snapshot
        resolved_part_id = getattr(resolved.part, 'pk', None)
        if expected_part_id and resolved_part_id != expected_part_id:
            raise ScanStockError(
                'APPROVAL_PART_MISMATCH',
                _('The scanned barcode now resolves to a different product'),
                details={
                    'line': event.request_line_number,
                    'expected_part': expected_part_id,
                    'resolved_part': resolved_part_id,
                },
            )

    @classmethod
    def _authorize_approver(cls, request, user) -> None:
        """Revalidate self-approval, permission and fixed-owner membership."""
        if request.requester_id == user.pk:
            raise ScanApprovalError(
                'SELF_APPROVAL_FORBIDDEN',
                _('Requesters cannot approve or reject their own requests'),
            )
        if not user.is_active or not user.has_perm(cls.APPROVE_PERMISSION):
            raise ScanApprovalError(
                'APPROVAL_PERMISSION_DENIED',
                _('You do not have scan approval permission'),
            )
        if user not in cls.eligible_approvers(request.approval_owner):
            raise ScanApprovalError(
                'APPROVAL_OWNER_MISMATCH',
                _('You are not a current member of the fixed approval owner'),
            )

    @staticmethod
    def _locked_request(request_id: int) -> ScanStockApprovalRequest:
        """Return an approval request under a row lock."""
        try:
            # Nullable related rows must not be part of the locking query: PostgreSQL
            # rejects FOR UPDATE against the nullable side of an outer join.
            return ScanStockApprovalRequest.objects.select_for_update().get(
                pk=request_id
            )
        except ScanStockApprovalRequest.DoesNotExist as exc:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_NOT_FOUND', _('Scan approval request does not exist')
            ) from exc

    @staticmethod
    def _require_requester(request, user) -> None:
        """Require the immutable requester identity."""
        if request.requester_id != user.pk:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_NOT_REQUESTER',
                _('Only the requester can perform this action'),
            )

    @staticmethod
    def _require_status(request, expected) -> None:
        """Require one expected lifecycle state."""
        if request.status != expected:
            raise ScanApprovalError(
                'APPROVAL_REQUEST_STATE_CONFLICT',
                _('Scan approval request is no longer in the expected state'),
                details={'current_status': request.status},
            )

    @classmethod
    def _terminalize_pending_lines(cls, request, reason: str) -> None:
        """Lock and terminalize all remaining pending lines exactly once."""
        for event in request.lines.select_for_update().filter(
            outcome=ScanStockEvent.Outcome.PENDING
        ):
            cls._fail_pending_event(event, reason)

    @staticmethod
    def _fail_pending_event(event: ScanStockEvent, reason: str) -> None:
        """Move one pending event to its stable non-applied terminal state."""
        event.outcome = ScanStockEvent.Outcome.FAILED
        event.error_code = reason
        event.error_message = str(_('Scan request line was not applied'))
        event.terminal_reason = reason
        event.applied_quantity = Decimal(0)
        event.completed_at = InvenTree.helpers.current_time()
        event.save(
            update_fields=[
                'outcome',
                'error_code',
                'error_message',
                'terminal_reason',
                'applied_quantity',
                'completed_at',
            ]
        )

    @classmethod
    def _notify_on_commit(cls, request_id: int, users, event_name: str) -> None:
        """Schedule UI notifications only after the surrounding transaction commits."""
        user_ids = sorted({user.pk for user in users if user is not None and user.pk})
        transaction.on_commit(
            lambda: cls._create_notifications(request_id, user_ids, event_name)
        )

    @staticmethod
    def _create_notifications(
        request_id: int, user_ids: list[int], event_name: str
    ) -> None:
        """Create compact per-user UI notifications for one committed transition."""
        try:
            request = ScanStockApprovalRequest.objects.get(pk=request_id)
        except ScanStockApprovalRequest.DoesNotExist:  # pragma: no cover
            return
        users = User.objects.filter(pk__in=user_ids, is_active=True)
        name = str(
            _('Scan stock request %(reference)s') % {'reference': request.reference}
        )
        message = str(
            _('%(direction)s request for %(location)s was %(event)s')
            % {
                'direction': request.get_direction_display(),
                'location': request.location_path_snapshot,
                'event': event_name,
            }
        )[:250]
        NotificationMessage.objects.bulk_create(
            [
                NotificationMessage(
                    target_object=request,
                    source_object=request.requester,
                    user=user,
                    category=f'stock.scan_approval.{event_name}',
                    name=name[:250],
                    message=message,
                    link=request.get_absolute_url(),
                )
                for user in users
            ]
        )
        stock_notifications.queue_approval_notification(request_id, event_name)
