"""Operator-owned preparation and contract maintenance, with atomic attribution changes."""

# Business messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from common.models import ProjectCode
from part.models import Part
from stock.models import (
    StockItem,
    StockLocation,
    WmsBusinessChange,
    WmsContract,
    WmsReceipt,
    WmsReceiptDetail,
)
from stock.projects import bind_project
from stock.warranty import (
    MAX_CODE_LENGTH,
    encode_label,
    resolve_label,
    warranty_end,
    warranty_values,
)
from stock.wms import WmsView, clean_json, current_stock_filter, require
from users.oauth2_scopes import get_granular_scope


class ContractBusinessView(WmsView):
    """Allow role-checked operator business edits while native stock writes stay restricted."""

    allow_operator_writes = True
    required_alternate_scopes = {
        **WmsView.required_alternate_scopes,
        'POST': [[get_granular_scope('change', 'stock')]],
    }


class ContractInput(serializers.ModelSerializer):
    """Validate positive defaults and normalized, unique contract numbers."""

    class Meta:
        """Writable contract business fields."""

        model = WmsContract
        fields = ['number', 'acceptance_date', 'warranty_months']

    def validate_number(self, value):
        """Trim before enforcing uniqueness."""
        value = value.strip()
        query = WmsContract.objects.filter(number=value)
        if self.instance:
            query = query.exclude(pk=self.instance.pk)
        if not value or query.exists():
            raise ValidationError('合同号必填且不能重复')
        return value


class DetailInput(serializers.ModelSerializer):
    """Validate a prepared line without creating inventory."""

    part = serializers.PrimaryKeyRelatedField(
        queryset=Part.objects.filter(active=True, trackable=False)
    )

    class Meta:
        """Dates and warranty terms are maintained on the contract."""

        model = WmsReceiptDetail
        fields = ['part', 'prepared_quantity']

    def to_internal_value(self, data):
        """Do not silently accept obsolete per-item warranty edits."""
        reject_warranty_overrides(data)
        return super().to_internal_value(data)


class ContractWriteInput(ContractInput):
    """Document optional update, impact preview and concurrency fields."""

    pk = serializers.IntegerField(required=False, min_value=1)
    preview = serializers.BooleanField(required=False)
    reason = serializers.CharField(required=False, max_length=2000)
    expected_updated_at = serializers.CharField(required=False)

    class Meta(ContractInput.Meta):
        """Updates require the last read timestamp and a reason."""

        fields = [
            *ContractInput.Meta.fields,
            'pk',
            'preview',
            'reason',
            'expected_updated_at',
        ]


class LabelResolveInput(serializers.Serializer):
    """Resolve the full label in optional preparation and actual location context."""

    barcode = serializers.CharField(max_length=MAX_CODE_LENGTH)
    receipt = serializers.IntegerField(required=False, min_value=1)
    location = serializers.IntegerField(required=False, min_value=1)


class ReceiptInput(serializers.Serializer):
    """One project, contract and location for each preparation."""

    request_key = serializers.UUIDField()
    project = serializers.CharField(max_length=100, allow_blank=False, required=False)
    project_id = serializers.IntegerField(min_value=1, required=False)
    contract = serializers.PrimaryKeyRelatedField(queryset=WmsContract.objects.all())
    location = serializers.PrimaryKeyRelatedField(
        queryset=StockLocation.objects.filter(structural=False)
    )
    details = DetailInput(many=True, allow_empty=False, max_length=100)

    def validate(self, attrs):
        """Bind every new preparation to an existing active project archive."""
        return bind_project(attrs)


class DetailChangeInput(serializers.Serializer):
    """Change attribution; acceptance and warranty belong to the contract."""

    project = serializers.CharField(max_length=100, required=False, allow_blank=False)
    project_id = serializers.IntegerField(min_value=1, required=False)
    contract = serializers.PrimaryKeyRelatedField(
        queryset=WmsContract.objects.all(), required=False
    )
    prepared_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=5, min_value=Decimal('0.00001'), required=False
    )
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    label_version = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        """Reject item-level dates instead of reporting a misleading success."""
        reject_warranty_overrides(self.initial_data)
        return bind_project(attrs, required=False)


class ProjectInput(serializers.Serializer):
    """Create a unique warehouse project without granting native settings permissions."""

    code = serializers.CharField(max_length=100, allow_blank=False)
    description = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )

    def validate_code(self, value):
        """Duplicate project numbers must be selected rather than created again."""
        if ProjectCode.objects.filter(code=value).exists():
            raise ValidationError('项目号已存在，请选择已有项目')
        return value


class WmsProjects(ContractBusinessView):
    """Expose the shared project archive to warehouse operators."""

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        """Return reusable active project records independently of current stock."""
        require(request, StockItem, 'view')
        return Response(
            {
                'results': list(
                    ProjectCode.objects.filter(active=True)
                    .order_by('code')
                    .values('pk', 'code', 'description', 'active')
                )
            }
        )

    @extend_schema(request=ProjectInput, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        """Create one archive record; a duplicate never creates a second project."""
        require(request, StockItem, 'change')
        serializer = ProjectInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                project = ProjectCode.objects.create(**serializer.validated_data)
                row = {
                    'pk': project.pk,
                    'code': project.code,
                    'description': project.description,
                    'active': project.active,
                }
                audit(request.user, 'project_create', after=row)
        except IntegrityError as exc:
            raise ValidationError({'code': '项目号已存在，请选择已有项目'}) from exc
        return Response(row, status=201)


def reject_warranty_overrides(data):
    """A contract has one acceptance/start date and one warranty term."""
    if any(
        field in data
        for field in ('acceptance_date_override', 'warranty_months_override')
    ):
        raise ValidationError(
            '验收日期即合同起始时间，请在合同中统一维护验收日期和质保月数'
        )


def audit(
    user,
    action,
    *,
    contract=None,
    receipt=None,
    detail=None,
    before=None,
    after=None,
    reason='',
):
    """Record business changes in the same transaction as their mutation."""
    WmsBusinessChange.objects.create(
        actor=user,
        action=action,
        contract=contract,
        receipt=receipt,
        detail=detail,
        before=clean_json(before or {}),
        after=clean_json(after or {}),
        reason=reason,
    )


def contract_row(contract):
    """Include impact counts for a review before changing defaults."""
    return {
        'pk': contract.pk,
        'number': contract.number,
        'acceptance_date': contract.acceptance_date,
        'warranty_months': contract.warranty_months,
        'updated_at': contract.updated_at.isoformat(),
        'acceptance_followers': contract.details.count(),
        'warranty_followers': contract.details.count(),
    }


def detail_row(detail):
    """Expose current effective values separately from immutable event snapshots."""
    events = (
        detail.events.filter(operation='INBOUND')
        .exclude(outcome='FAILED')
        .order_by('-pk')
    )
    event = events.first()
    return {
        'pk': detail.pk,
        'receipt': detail.receipt_id,
        'receipt_location': detail.receipt.location_id,
        'part': detail.part_id,
        'equipment': detail.part.IPN or detail.part.barcode,
        'name': detail.part.name,
        'project': detail.project,
        'project_id': detail.project_code_id,
        'contract': detail.contract_id,
        'prepared_quantity': detail.prepared_quantity,
        'label_version': detail.label_version,
        'barcode': encode_label(detail),
        'legacy': detail.legacy,
        'cancelled': detail.receipt.cancelled,
        'state': 'CANCELLED'
        if detail.receipt.cancelled
        else (
            'RECEIVED'
            if detail.legacy or (event and event.outcome == 'SUCCESS')
            else ('IN_REQUEST' if event else 'READY')
        ),
        'active_request': event.approval_request_id
        if event and event.outcome == 'PENDING'
        else None,
        'actual_quantity': event.requested_quantity if event else None,
        **warranty_values(detail),
    }


def receipt_row(receipt):
    """Return resumable preparation with checked and unchecked line states."""
    return {
        'pk': receipt.pk,
        'request_key': str(receipt.request_key),
        'project': receipt.project,
        'project_id': receipt.project_code_id,
        'contract': receipt.contract_id,
        'location': receipt.location_id,
        'operator': receipt.operator_id,
        'cancelled': receipt.cancelled,
        'created_at': receipt.created_at,
        'details': [
            detail_row(detail)
            for detail in receipt.details.select_related(
                'part', 'contract', 'receipt'
            ).order_by('pk')
        ],
    }


def lock_parts(ids):
    """Use the existing stock service's Part lock as the shared serialization point."""
    list(Part.objects.select_for_update().filter(pk__in=ids).order_by('pk'))


def ensure_changeable(detail):
    """Pending or successful inbound lines have different attribution constraints."""
    if detail.events.filter(outcome='PENDING').exists():
        raise ValidationError('明细存在草稿或待审批申请，请先移除或取消申请')
    items = list(
        StockItem.objects.select_for_update()
        .filter(source_detail=detail)
        .order_by('pk')
    )
    for item in items:
        if item.scan_frozen_quantity() or item.allocation_count():
            raise ValidationError('明细库存存在冻结或业务占用，不能修改归属')
    return items


class WmsContracts(ContractBusinessView):
    """Operators can maintain contract data without gaining catalogue edit rights."""

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='Search contracts. Returns results with effective defaults, updated_at and per-field follower counts.',
    )
    def get(self, request):
        """Search contracts, or read one contract and its change history."""
        require(request, StockItem, 'view')
        query = WmsContract.objects.order_by('number')
        if request.query_params.get('search'):
            query = query.filter(number__icontains=request.query_params['search'])
        return Response({'results': [contract_row(c) for c in query[:500]]})

    @extend_schema(
        request=ContractWriteInput,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
        description='Create, preview or update contract defaults. Updates require pk, reason and expected_updated_at; preview=true returns before/after without saving.',
    )
    @transaction.atomic
    def post(self, request):
        """Create a contract or review and update the defaults atomically."""
        require(request, StockItem, 'change')
        instance = None
        if request.data.get('pk'):
            pk = serializers.IntegerField(min_value=1).run_validation(
                request.data['pk']
            )
            seed = get_object_or_404(WmsContract, pk=pk)
            lock_parts(seed.details.values_list('part_id', flat=True))
            instance = WmsContract.objects.select_for_update().get(pk=seed.pk)
        serializer = ContractInput(
            instance, data=request.data, partial=instance is not None
        )
        serializer.is_valid(raise_exception=True)
        if instance:
            before = contract_row(instance)
            if request.data.get('preview') is True:
                return Response(
                    {'before': before, 'after': clean_json(serializer.validated_data)}
                )
            if not str(request.data.get('reason', '')).strip():
                raise ValidationError('修改原因必填')
            if (
                request.data.get('expected_updated_at')
                != instance.updated_at.isoformat()
            ):
                raise ValidationError('合同已变化，请刷新并重新核对影响数量')
            renumber = (
                serializer.validated_data.get('number', instance.number)
                != instance.number
            )
            details = list(instance.details.select_for_update().order_by('pk'))
            if renumber:
                for detail in details:
                    ensure_changeable(detail)
            accepted = serializer.validated_data.get(
                'acceptance_date', instance.acceptance_date
            )
            months = serializer.validated_data.get(
                'warranty_months', instance.warranty_months
            )
            warranty_end(accepted, months)
            serializer.save(updated_by=request.user)
            if renumber:
                for detail in details:
                    detail.label_version += 1
                    detail.save(update_fields=['label_version', 'updated_at'])
            audit(
                request.user,
                'contract_update',
                contract=instance,
                before=before,
                after=contract_row(instance),
                reason=request.data['reason'],
            )
        else:
            instance = serializer.save(created_by=request.user, updated_by=request.user)
            warranty_end(instance.acceptance_date, instance.warranty_months)
            audit(
                request.user,
                'contract_create',
                contract=instance,
                after=contract_row(instance),
            )
        return Response(
            contract_row(instance), status=200 if request.data.get('pk') else 201
        )


class WmsReceipts(ContractBusinessView):
    """Create and recover the current operator's preparation without stock effects."""

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='Return owned preparations with nested detail state, complete barcode, effective warranty and nullable overrides.',
    )
    def get(self, request):
        """Read only owned preparations; administrators may inspect all."""
        require(request, StockItem, 'view')
        query = WmsReceipt.objects.order_by('-pk')
        if not request.user.is_superuser:
            query = query.filter(operator=request.user)
        if request.query_params.get('pk'):
            pk = serializers.IntegerField(min_value=1).run_validation(
                request.query_params['pk']
            )
            query = query.filter(pk=pk)
        return Response({'results': [receipt_row(r) for r in query[:100]]})

    @extend_schema(
        request=ReceiptInput,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
        description='Save or replay preparation by request_key. Saving does not create stock or scan events.',
    )
    @transaction.atomic
    def post(self, request):
        """Save once per client key, including retries after a lost response."""
        require(request, StockItem, 'change')
        serializer = ReceiptInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        details = values.pop('details')
        if not values['location'].check_ownership(request.user):
            raise PermissionDenied('没有此库位的业务权限')
        lock_parts([d['part'].pk for d in details])
        existing = WmsReceipt.objects.filter(request_key=values['request_key']).first()
        fingerprint = clean_json(
            {
                **{
                    k: getattr(v, 'pk', v)
                    for k, v in values.items()
                    if k != 'project_code'
                },
                'details': [
                    {k: getattr(v, 'pk', v) for k, v in d.items()} for d in details
                ],
            }
        )
        if existing:
            if (
                existing.operator_id != request.user.pk
                or existing.input_snapshot != fingerprint
            ):
                raise ValidationError('保存标识已用于其他待入库内容')
            return Response(receipt_row(existing))
        receipt = WmsReceipt.objects.create(
            **values, operator=request.user, input_snapshot=fingerprint
        )
        for values in details:
            detail = WmsReceiptDetail.objects.create(
                receipt=receipt,
                project=receipt.project,
                project_code=receipt.project_code,
                contract=receipt.contract,
                **values,
            )
            warranty_end(
                detail.effective_acceptance_date, detail.effective_warranty_months
            )
        audit(
            request.user, 'receipt_create', receipt=receipt, after=receipt_row(receipt)
        )
        return Response(receipt_row(receipt), status=201)


class WmsReceiptCancel(ContractBusinessView):
    """Cancel an unused preparation, preserving its identifiers and audit."""

    @extend_schema(
        request=inline_serializer(
            'WmsReceiptCancelInput',
            fields={'reason': serializers.CharField(max_length=2000)},
        ),
        responses=OpenApiTypes.OBJECT,
    )
    @transaction.atomic
    def post(self, request, pk):
        """Require removal of active requests before cancellation."""
        require(request, StockItem, 'change')
        receipt = get_object_or_404(WmsReceipt.objects.select_for_update(), pk=pk)
        if receipt.operator_id != request.user.pk:
            raise PermissionDenied('仅填单人可取消待入库记录')
        reason = str(request.data.get('reason', '')).strip()
        if not reason:
            raise ValidationError('取消原因必填')
        lock_parts(receipt.details.values_list('part_id', flat=True))
        for detail in receipt.details.all():
            if (
                detail.legacy
                or detail.events.exclude(outcome='FAILED').exists()
                or detail.stock_items.exists()
            ):
                raise ValidationError('已有库存或有效申请的记录不能取消；请先处理申请')
        receipt.cancelled = True
        receipt.save(update_fields=['cancelled', 'updated_at'])
        audit(request.user, 'receipt_cancel', receipt=receipt, reason=reason)
        return Response(receipt_row(receipt))


class WmsDetailChange(ContractBusinessView):
    """Update current attribution and inventory while retaining all event snapshots."""

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='Read current detail, complete barcode, effective warranty and inheritance flags.',
    )
    def get(self, request, pk):
        """Read a current detail for stock maintenance and replacement labels."""
        require(request, StockItem, 'view')
        return Response(detail_row(get_object_or_404(WmsReceiptDetail, pk=pk)))

    @extend_schema(
        request=DetailChangeInput,
        responses=OpenApiTypes.OBJECT,
        description='Update current detail. Attribution increments label_version and updates linked stock; historical event snapshots are preserved.',
    )
    @transaction.atomic
    def post(self, request, pk):
        """Replace label version only when project or contract attribution changes."""
        require(request, StockItem, 'change')
        serializer = DetailChangeInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        seed = get_object_or_404(WmsReceiptDetail, pk=pk)
        lock_parts([seed.part_id])
        detail = WmsReceiptDetail.objects.select_for_update().get(pk=pk)
        if detail.receipt.cancelled:
            raise ValidationError('已取消记录不能修改')
        if values.pop('label_version') != detail.label_version:
            raise ValidationError('标签版本已变化，请刷新')
        reason = values.pop('reason')
        before = detail_row(detail)
        attribution = any(
            getattr(detail, field) != value
            for field, value in values.items()
            if field in ('project', 'contract')
        )
        quantity_changed = (
            'prepared_quantity' in values
            and values['prepared_quantity'] != detail.prepared_quantity
        )
        if attribution or quantity_changed:
            ensure_changeable(detail)
        if quantity_changed and (
            detail.legacy or detail.events.filter(outcome='SUCCESS').exists()
        ):
            raise ValidationError('已入库明细不能修改准备数量')
        for field, value in values.items():
            setattr(detail, field, value)
        warranty_end(detail.effective_acceptance_date, detail.effective_warranty_months)
        if attribution:
            detail.label_version += 1
        detail.save()
        if attribution:
            StockItem.objects.filter(source_detail=detail).update(
                batch=detail.project, project_code=detail.project_code
            )
        audit(
            request.user,
            'detail_update',
            detail=detail,
            before=before,
            after=detail_row(detail),
            reason=reason,
        )
        return Response(detail_row(detail))


class WmsReceiptChange(ContractBusinessView):
    """Edit shared preparation fields while all lines are still unused."""

    @extend_schema(
        operation_id='stock_wms_receipt_shared_update',
        request=inline_serializer(
            'WmsReceiptSharedInput',
            fields={
                'project': serializers.CharField(max_length=100),
                'project_id': serializers.IntegerField(min_value=1, required=False),
                'contract': serializers.IntegerField(min_value=1),
                'location': serializers.IntegerField(min_value=1),
                'reason': serializers.CharField(max_length=2000),
            },
        ),
        responses=OpenApiTypes.OBJECT,
    )
    @transaction.atomic
    def post(self, request, pk):
        """Update every prepared label together, requiring replacement on attribution changes."""
        require(request, StockItem, 'change')
        receipt = get_object_or_404(WmsReceipt.objects.select_for_update(), pk=pk)
        if receipt.operator_id != request.user.pk:
            raise PermissionDenied('仅填单人可修改待入库公共信息')
        if receipt.cancelled:
            raise ValidationError('已取消记录不能修改')
        project_values = bind_project(
            {
                **(
                    {
                        'project': serializers.CharField(max_length=100).run_validation(
                            request.data['project']
                        )
                    }
                    if 'project' in request.data
                    else {}
                ),
                **(
                    {
                        'project_id': serializers.IntegerField(
                            min_value=1
                        ).run_validation(request.data['project_id'])
                    }
                    if 'project_id' in request.data
                    else {}
                ),
            }
        )
        project = project_values['project']
        contract = serializers.PrimaryKeyRelatedField(
            queryset=WmsContract.objects.all()
        ).run_validation(request.data.get('contract'))
        location = serializers.PrimaryKeyRelatedField(
            queryset=StockLocation.objects.filter(structural=False)
        ).run_validation(request.data.get('location'))
        reason = serializers.CharField(max_length=2000).run_validation(
            request.data.get('reason')
        )
        if not location.check_ownership(request.user):
            raise PermissionDenied('没有此库位的业务权限')
        lock_parts(receipt.details.values_list('part_id', flat=True))
        details = list(receipt.details.select_for_update().order_by('pk'))
        before = receipt_row(receipt)
        for detail in details:
            ensure_changeable(detail)
            if (
                detail.legacy
                or detail.stock_items.exists()
                or detail.events.filter(outcome='SUCCESS').exists()
            ):
                raise ValidationError('已入库记录请逐条修改当前库存明细归属')
            if detail.project != project or detail.contract_id != contract.pk:
                detail.label_version += 1
            detail.project = project
            detail.project_code = project_values['project_code']
            detail.contract = contract
            warranty_end(
                detail.effective_acceptance_date, detail.effective_warranty_months
            )
            detail.save()
        receipt.project = project
        receipt.project_code = project_values['project_code']
        receipt.contract = contract
        receipt.location = location
        receipt.save()
        audit(
            request.user,
            'receipt_update',
            receipt=receipt,
            before=before,
            after=receipt_row(receipt),
            reason=reason,
        )
        return Response(receipt_row(receipt))


class WmsLabelResolve(ContractBusinessView):
    """Resolve current complete labels and show exact actual-location balances."""

    required_alternate_scopes = WmsView.required_alternate_scopes

    @extend_schema(
        request=LabelResolveInput,
        responses=OpenApiTypes.OBJECT,
        description='Verify protocol and current database attribution. Returns authoritative detail and exact-location quantity, frozen, allocated and available balances.',
    )
    def post(self, request):
        """Never treat QR business fields as authoritative transaction inputs."""
        require(request, StockItem, 'view')
        serializer = LabelResolveInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        detail = resolve_label(values['barcode'])
        if values.get('receipt') and str(detail.receipt_id) != str(values['receipt']):
            raise ValidationError('标签属于其他待入库记录')
        row = detail_row(detail)
        query = StockItem.objects.filter(
            current_stock_filter(), source_detail=detail
        ).select_related('location')
        if values.get('location'):
            query = query.filter(location_id=values['location'])
        row['stock'] = [
            {
                'pk': item.pk,
                'location': item.location_id,
                'location_name': item.location.pathstring if item.location else '',
                'quantity': item.quantity,
                'frozen': item.scan_frozen_quantity(),
                'allocated': item.allocation_count(),
                'available': item.unallocated_quantity(),
            }
            for item in query
        ]
        return Response(row)


class WmsLegacyAttribute(ContractBusinessView):
    """Associate verified historical quantities without fabricating an inbound event."""

    @extend_schema(
        request=inline_serializer(
            'WmsLegacyAttributionInput',
            fields={
                'request_key': serializers.UUIDField(),
                'project': serializers.CharField(max_length=100, required=False),
                'contract': serializers.IntegerField(min_value=1),
                'quantity': serializers.DecimalField(
                    max_digits=15, decimal_places=5, min_value=Decimal('0.00001')
                ),
                'confirmed_stock_quantity': serializers.DecimalField(
                    max_digits=15, decimal_places=5, min_value=0
                ),
                'confirmed_single_contract': serializers.BooleanField(),
                'reason': serializers.CharField(max_length=2000),
            },
        ),
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
        description='Link an explicitly verified historical quantity, splitting if necessary. Never creates an inbound event. request_key replays an identical saved backfill.',
    )
    @transaction.atomic
    def post(self, request, pk):
        """Split only an explicitly confirmed portion when several contracts share a row."""
        require(request, StockItem, 'change')
        seed = get_object_or_404(StockItem, pk=pk)
        lock_parts([seed.part_id])
        request_key = serializers.UUIDField().run_validation(
            request.data.get('request_key')
        )
        snapshot = {'legacy_stock': pk, 'payload': clean_json(request.data)}
        existing = WmsReceipt.objects.filter(request_key=request_key).first()
        if existing:
            if (
                existing.operator_id != request.user.pk
                or existing.input_snapshot != snapshot
            ):
                raise ValidationError('补录保存标识已用于其他内容')
            return Response(detail_row(existing.details.get()))
        item = StockItem.objects.select_for_update().get(pk=pk)
        if item.location and not item.location.check_ownership(request.user):
            raise PermissionDenied('没有此库位的业务权限')
        if item.source_detail_id:
            raise ValidationError('该行已补录，请刷新后修改当前明细')
        if (
            item.scan_frozen_quantity()
            or item.allocation_count()
            or not item.in_stock
            or item.serialized
        ):
            raise ValidationError('库存存在占用或状态不支持补录')
        contract_pk = serializers.IntegerField(min_value=1).run_validation(
            request.data.get('contract')
        )
        contract = get_object_or_404(WmsContract, pk=contract_pk)
        payload = {
            'request_key': request.data.get('request_key'),
            'project': request.data.get('project', item.batch),
            'contract': contract.pk,
            'location': item.location_id,
            'details': [
                {
                    'part': item.part_id,
                    'prepared_quantity': request.data.get('quantity'),
                }
            ],
        }
        if 'project_id' in request.data:
            payload['project_id'] = request.data['project_id']
            if 'project' not in request.data:
                payload.pop('project')
        serializer = ReceiptInput(data=payload)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        quantity = values['details'][0]['prepared_quantity']
        confirmed = serializers.DecimalField(
            max_digits=15, decimal_places=5, min_value=0
        ).run_validation(request.data.get('confirmed_stock_quantity'))
        if (
            confirmed != item.quantity
            or quantity > item.quantity
            or request.data.get('confirmed_single_contract') is not True
        ):
            raise ValidationError('请明确核对归属数量；混合合同必须分别拆分补录')
        reason = str(request.data.get('reason', '')).strip()
        if not reason:
            raise ValidationError('补录原因必填')
        if WmsReceipt.objects.filter(request_key=values['request_key']).exists():
            raise ValidationError('补录保存标识已使用，请刷新库存')
        receipt = WmsReceipt.objects.create(
            request_key=values['request_key'],
            project=values['project'],
            project_code=values['project_code'],
            contract=contract,
            location=item.location,
            operator=request.user,
            input_snapshot=snapshot,
        )
        detail = WmsReceiptDetail.objects.create(
            receipt=receipt,
            project=receipt.project,
            project_code=receipt.project_code,
            contract=contract,
            part=item.part,
            prepared_quantity=quantity,
            legacy=True,
        )
        before = {
            'stock': item.pk,
            'quantity': str(item.quantity),
            'project': item.batch,
        }
        if quantity < item.quantity:
            item = item.splitStock(quantity, user=request.user, notes=reason)
        StockItem.objects.filter(pk=item.pk).update(
            source_detail=detail, batch=detail.project, project_code=detail.project_code
        )
        audit(
            request.user,
            'legacy_attribute',
            detail=detail,
            before=before,
            after={'stock': item.pk, **detail_row(detail)},
            reason=reason,
        )
        return Response(detail_row(detail), status=201)


class WmsChangeHistory(ContractBusinessView):
    """Provide operator, timestamp, reason and before/after values for maintenance."""

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='Return results with actor, timestamp, reason and before/after values; filter by contract, receipt or detail.',
    )
    def get(self, request):
        """Read a bounded history filtered to a contract, preparation or detail."""
        require(request, StockItem, 'view')
        query = WmsBusinessChange.objects.select_related('actor').order_by('-pk')
        filters = {
            f'{field}_id': serializers.IntegerField(min_value=1).run_validation(
                request.query_params[field]
            )
            for field in ('contract', 'receipt', 'detail')
            if request.query_params.get(field)
        }
        if not filters:
            raise ValidationError('请选择合同、待入库记录或明细')
        return Response(
            {
                'results': [
                    {
                        'pk': row.pk,
                        'action': row.action,
                        'actor': row.actor.username if row.actor else '',
                        'created_at': row.created_at,
                        'reason': row.reason,
                        'before': row.before,
                        'after': row.after,
                    }
                    for row in query.filter(**filters)[:100]
                ]
            }
        )
