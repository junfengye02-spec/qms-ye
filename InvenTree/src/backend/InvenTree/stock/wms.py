"""WMS current inventory, posted scan records and atomic preview imports."""

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as ModelValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from drf_spectacular.utils import extend_schema
from oauth2_provider.models import AccessToken
from rest_framework import serializers
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from InvenTree.helpers import hash_barcode
from InvenTree.permissions import InvenTreeRoleScopeMixin
from part.models import Part, PartCategory
from stock.warranty import warranty_values
from users.authentication import ApiTokenAuthentication, ExtendedOAuth2Authentication
from users.oauth2_scopes import DEFAULT_READ, get_granular_scope
from users.permissions import check_user_permission

from .models import (
    ScanInboundBarcodeLock,
    ScanStockEvent,
    StockItem,
    StockLocation,
    WmsImportTask,
)

# Chinese business messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001
CHINA = ZoneInfo('Asia/Shanghai')


def current_stock_filter():
    """Keep native stock semantics while allowing retained zero balances."""
    return Q(
        *[
            child
            for child in StockItem.IN_STOCK_FILTER.children
            if child[0] != 'quantity__gt'
        ]
    )


PART_COLUMNS = {
    '设备ID': 'IPN',
    '设备名称': 'name',
    '品牌': 'brand',
    '设备分类完整路径': 'category',
    '型号': 'model_number',
    '单位': 'units',
    '规格': 'specification',
    '备注': 'notes',
}
COUNT_COLUMNS = {
    **{k: v for k, v in PART_COLUMNS.items() if k != '备注'},
    '项目': 'batch',
    '地点完整路径': 'location',
    '明细标识': 'detail_uid',
    '盘点数量': 'quantity',
    '盘点日期': 'count_date',
    '盘点备注': 'remark',
    '合同号': 'contract_number',
    '验收日期': 'acceptance_date',
    '质保月数': 'warranty_months',
    '质保到期日': 'warranty_end',
    '冻结数量': 'frozen',
    '可用数量': 'available',
}
INVENTORY_COLUMNS = {
    **PART_COLUMNS,
    '项目': 'batch',
    '合同号': 'contract_number',
    '明细标识': 'detail_uid',
    '验收日期': 'acceptance_date',
    '质保月数': 'warranty_months',
    '质保到期日': 'warranty_end',
    '地点完整路径': 'location',
    '库存数量': 'quantity',
    '冻结数量': 'frozen',
    '可用数量': 'available',
    '项目数量': 'project_count',
    '最后库存变动时间': 'changed_at',
    '库存备注': 'stock_notes',
}
RECORD_COLUMNS = {
    '设备ID': 'IPN',
    '设备名称': 'name',
    '单位': 'units',
    '数量': 'quantity',
    '项目': 'batch',
    '合同号': 'contract_number',
    '明细标识': 'detail_uid',
    '验收日期': 'acceptance_date',
    '质保月数': 'warranty_months',
    '质保到期日': 'warranty_end',
    '地点完整路径': 'location',
    '用途': 'purpose',
    '发起人': 'operator',
    '正式时间': 'completed_at',
    '备注': 'remark',
    '审批状态': 'approval_status',
    '审批人': 'approver',
}


def require(request, model, action):
    """Apply the same native roles to reads, exports and every imported mutation."""
    if not check_user_permission(request.user, model, action):
        raise PermissionDenied('没有对应设备或库存权限')
    if isinstance(request.auth, AccessToken):
        scope = (
            DEFAULT_READ
            if action == 'view'
            else get_granular_scope(action, 'part' if model == Part else 'stock')
        )
        allowed = request.auth.is_valid([scope])
        if action == 'view':
            allowed = allowed or request.auth.is_valid(
                [get_granular_scope('view', 'part' if model == Part else 'stock')]
            )
        if not allowed:
            raise PermissionDenied('令牌权限不足')


def clean_json(value):
    """Normalize decimals and timestamps for immutable JSON snapshots."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True))


def stamp(value):
    """Display business timestamps in China local time."""
    return value.astimezone(CHINA).isoformat() if value else ''


def part_row(part):
    """Return business catalogue fields without database implementation labels."""
    return {
        'pk': part.pk,
        # New WMS equipment uses the native internal Part barcode. Keep legacy
        # IPNs visible for existing records and fall back to the generated ID.
        'IPN': part.IPN or part.barcode,
        'name': part.name,
        'brand': part.brand,
        'category': part.category.pathstring if part.category else '',
        'category_id': part.category_id,
        'model_number': part.model_number,
        'units': part.units,
        'specification': part.specification,
        'notes': part.notes,
        'active': part.active,
    }


def filter_parts(query, params, prefix=''):
    """Apply current catalogue filters, including category descendants."""
    for field in ('brand', 'model_number'):
        if params.get(field):
            query = query.filter(**{f'{prefix}{field}__icontains': params[field]})
    if params.get('category'):
        try:
            category = PartCategory.objects.get(pk=int(params['category']))
        except (PartCategory.DoesNotExist, ValueError):
            raise ValidationError('分类不存在')
        query = query.filter(
            **{f'{prefix}category__in': category.get_descendants(include_self=True)}
        )
    return query


def inventory_rows(params, queryset=None):
    """Aggregate native in-stock rows only inside the selected query scope."""
    query = (
        (
            queryset
            if queryset is not None
            else StockItem.objects.filter(current_stock_filter())
        )
        .select_related('part__category', 'location')
        .order_by('part_id', 'batch', 'location_id', 'pk')
    )
    query = filter_parts(query, params, 'part__')
    if params.get('batch'):
        query = query.filter(batch=params['batch'].strip())
    if params.get('location'):
        try:
            location = StockLocation.objects.get(pk=int(params['location']))
        except (StockLocation.DoesNotExist, ValueError):
            raise ValidationError('地点不存在')
        locations = (
            location.get_descendants(include_self=True)
            if params.get('include_children', 'true') == 'true'
            else [location]
        )
        query = query.filter(location__in=locations)
    if params.get('search'):
        text = params['search']
        query = query.filter(
            Q(part__IPN__icontains=text)
            | Q(part__name__icontains=text)
            | Q(batch__icontains=text)
            | Q(part__brand__icontains=text)
            | Q(part__model_number__icontains=text)
            | Q(part__notes__icontains=text)
            | Q(notes__icontains=text)
        )
    if params.get('contract'):
        query = query.filter(
            source_detail__contract__number__icontains=params['contract']
        )
    if params.get('detail_uid'):
        query = query.filter(source_detail__uid=params['detail_uid'])
    query = query.select_related('source_detail__contract')
    groups = {}
    items = list(query)
    frozen = StockItem.bulk_scan_frozen_quantity(items)
    allocated = StockItem.bulk_allocation_count(items)
    for item in items:
        key = (item.part_id, item.batch, item.location_id, item.source_detail_id)
        row = groups.setdefault(
            key,
            {
                **part_row(item.part),
                **warranty_values(item.source_detail),
                'batch': item.batch,
                'location': item.location.pathstring if item.location else '',
                'location_id': item.location_id,
                'quantity': Decimal(0),
                'frozen': Decimal(0),
                'allocated': Decimal(0),
                'available': Decimal(0),
                'changed_at': '',
                'stock_notes': [],
                'stock_ids': [],
            },
        )
        row['quantity'] += item.quantity
        row['frozen'] += frozen.get(item.pk, Decimal(0))
        row['allocated'] += allocated.get(item.pk, Decimal(0))
        row['available'] += max(
            item.quantity
            - frozen.get(item.pk, Decimal(0))
            - allocated.get(item.pk, Decimal(0)),
            Decimal(0),
        )
        row['changed_at'] = max(row['changed_at'], stamp(item.inventory_changed_at))
        row['stock_ids'].append(item.pk)
        if item.notes:
            row['stock_notes'].append(f'#{item.pk}: {item.notes}')
    rows = list(groups.values())
    if params.get('include_zero') != 'true' and params.get('has_stock') != 'false':
        rows = [r for r in rows if r['quantity'] > 0 or r['frozen'] > 0]
    for field, param in [('quantity', 'has_stock'), ('frozen', 'has_frozen')]:
        if params.get(param) in ('true', 'false'):
            rows = [r for r in rows if (r[field] > 0) == (params[param] == 'true')]
    if params.get('view') == 'summary':
        totals = {}
        for row in rows:
            total = totals.setdefault(
                row['pk'],
                {
                    **{k: row[k] for k in part_row_fields()},
                    'quantity': Decimal(0),
                    'frozen': Decimal(0),
                    'allocated': Decimal(0),
                    'available': Decimal(0),
                    'warranty_variants': {
                        key: set()
                        for key in (
                            'contract_number',
                            'acceptance_date',
                            'warranty_months',
                            'warranty_end',
                        )
                    },
                    'projects': set(),
                },
            )
            for field, variants in total['warranty_variants'].items():
                variants.add(row[field])
            for field in ('quantity', 'frozen', 'allocated', 'available'):
                total[field] += row[field]
            if row['quantity'] > 0 or row['frozen'] > 0:
                total['projects'].add(row['batch'])
        rows = list(totals.values())
        for row in rows:
            row['project_count'] = len(row.pop('projects'))
            for field, variants in row.pop('warranty_variants').items():
                row[field] = next(iter(variants)) if len(variants) == 1 else '多种'

    return clean_json(rows)


def part_row_fields():
    """Return shared equipment columns."""
    return (
        'pk',
        'IPN',
        'name',
        'brand',
        'category',
        'category_id',
        'model_number',
        'units',
        'specification',
        'notes',
        'active',
    )


def records_rows(params):
    """Use successful posting time with an inclusive China date range."""
    operation = params.get('operation', 'INBOUND')
    if operation not in ScanStockEvent.Operation.values:
        raise ValidationError('请选择入库或出库')
    query = (
        ScanStockEvent.objects.filter(
            outcome='SUCCESS', operation=operation, completed_at__isnull=False
        )
        .select_related('approval_request')
        .order_by('-completed_at', '-pk')
    )
    try:
        start = (
            date.fromisoformat(params['start'])
            if params.get('start')
            else timezone.now().astimezone(CHINA).date().replace(day=1)
        )
        end = (
            date.fromisoformat(params['end'])
            if params.get('end')
            else timezone.now().astimezone(CHINA).date()
        )
        if start > end:
            raise ValueError
        query = query.filter(
            completed_at__gte=datetime.combine(start, time.min, CHINA),
            completed_at__lt=datetime.combine(end + timedelta(days=1), time.min, CHINA),
        )
    except (ValueError, OverflowError):
        raise ValidationError('日期区间无效，开始日期不能晚于结束日期')
    query = filter_parts(query, params, 'part__')
    if params.get('search'):
        query = query.filter(
            Q(part_ipn_snapshot__icontains=params['search'])
            | Q(part_name_snapshot__icontains=params['search'])
        )
    for param, field in [
        ('batch', 'batch_snapshot'),
        ('purpose', 'purpose'),
        ('operator', 'operator_display_name_snapshot'),
    ]:
        if params.get(param) and (param != 'purpose' or operation == 'OUTBOUND'):
            if param == 'operator':
                query = query.filter(
                    Q(operator_display_name_snapshot__icontains=params[param])
                    | Q(operator_username_snapshot__icontains=params[param])
                )
            else:
                query = query.filter(
                    **{
                        field + ('__icontains' if param == 'purpose' else ''): params[
                            param
                        ]
                    }
                )
    if params.get('contract'):
        query = query.filter(contract_number_snapshot__icontains=params['contract'])
    if params.get('location'):
        field = (
            'destination_location_id'
            if operation == 'INBOUND'
            else 'source_location_id'
        )
        try:
            query = query.filter(**{field: int(params['location'])})
        except ValueError:
            raise ValidationError('地点无效')
    return clean_json(
        [
            {
                'pk': event.pk,
                'part_id': event.part_pk_snapshot,
                'IPN': event.part_ipn_snapshot,
                'name': event.part_name_snapshot,
                'units': event.units_snapshot,
                'quantity': event.applied_quantity,
                'batch': event.batch_snapshot,
                'contract_number': event.contract_number_snapshot,
                'detail_uid': str(event.detail_uid_snapshot or ''),
                'acceptance_date': event.acceptance_date_snapshot,
                'warranty_months': event.warranty_months_snapshot,
                'warranty_end': event.warranty_end_snapshot,
                'location': event.destination_location_path_snapshot
                if operation == 'INBOUND'
                else event.source_location_path_snapshot,
                'purpose': event.purpose,
                'operator': event.operator_display_name_snapshot
                or event.operator_username_snapshot,
                'completed_at': stamp(event.completed_at),
                'remark': event.remark,
                'approval_status': '审批通过' if event.approval_request else '直接落账',
                'approver': event.approver_snapshot,
            }
            for event in query
        ]
    )


def export_csv(rows, columns, filename):
    """Export all matched rows with Chinese headings and safe spreadsheet cells."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        cells = []
        for field in columns.values():
            value = row.get(field, '')
            if value and field in ('completed_at', 'changed_at'):
                value = (
                    datetime.fromisoformat(value)
                    .astimezone(CHINA)
                    .strftime('%Y-%m-%d %H:%M:%S')
                )
            if isinstance(value, list):
                value = '\n'.join(str(v) for v in value)
            if isinstance(value, str) and value.lstrip().startswith(
                ('=', '+', '-', '@')
            ):
                value = "'" + value
            cells.append(value)
        writer.writerow(cells)
    response = HttpResponse(
        '\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
    return response


def list_response(request, rows, columns, filename, statistics=None):
    """Keep export and list backed by exactly the same filtered results."""
    if request.query_params.get('export') == 'csv':
        return export_csv(rows, columns, filename)
    try:
        offset = max(0, int(request.query_params.get('offset', 0)))
        limit = max(1, min(500, int(request.query_params.get('limit', 50))))
    except ValueError:
        raise ValidationError('分页参数无效')
    return Response(
        {
            'count': len(rows),
            'results': rows[offset : offset + limit],
            'statistics': statistics or {},
        }
    )


class WmsResultSerializer(serializers.Serializer):
    """Document list, preview and commit response envelopes."""

    count = serializers.IntegerField(required=False)
    results = serializers.ListField(child=serializers.DictField(), required=False)
    statistics = serializers.DictField(required=False)
    task = serializers.IntegerField(required=False)
    can_commit = serializers.BooleanField(required=False)
    replayed = serializers.BooleanField(required=False)
    completed_at = serializers.CharField(required=False)


class WmsImportInputSerializer(serializers.Serializer):
    """A commit references its owned task; a preview accepts a file or rows."""

    task = serializers.IntegerField(required=False, min_value=1)
    kind = serializers.ChoiceField(choices=['parts', 'count'], required=False)
    file = serializers.FileField(required=False)
    rows = serializers.ListField(child=serializers.DictField(), required=False)


class WmsScopePermission(InvenTreeRoleScopeMixin, IsAuthenticated):
    """Require OAuth read scope without falling back to session authentication."""

    def has_permission(self, request, view):
        """Authorize the initial read; each row separately requires write scopes."""
        if (
            request.method not in ('GET', 'HEAD', 'OPTIONS')
            and not (request.user.is_staff or request.user.is_superuser)
            and not getattr(view, 'allow_operator_writes', False)
        ):
            from common.settings import get_global_setting

            if get_global_setting('SCAN_APPROVAL_ENABLED', False):
                return False
        if self.is_oauth2ed(request):
            return self.check_oauth2_authentication(request, view)
        return IsAuthenticated.has_permission(self, request, view)


class WmsView(APIView):
    """Shared login boundary and safe transactional model errors."""

    permission_classes = [WmsScopePermission]
    authentication_classes = [
        ExtendedOAuth2Authentication,
        ApiTokenAuthentication,
        BasicAuthentication,
        SessionAuthentication,
    ]
    serializer_class = WmsResultSerializer
    required_alternate_scopes = {
        method: [
            [DEFAULT_READ],
            [get_granular_scope('view', 'stock')],
            [get_granular_scope('view', 'part')],
        ]
        for method in ('GET', 'POST', 'OPTIONS')
    }

    def handle_exception(self, exc):
        """Return actionable errors after atomic business writes have rolled back."""
        if isinstance(exc, ModelValidationError):
            exc = ValidationError(getattr(exc, 'message_dict', None) or exc.messages)
        elif isinstance(exc, IntegrityError):
            exc = ValidationError('目标数据已变化或产生冲突，请重新预览；整批未提交')
        return super().handle_exception(exc)


class WmsInventory(WmsView):
    """Current detail and summary with exact project/location scope."""

    permission_classes = [WmsScopePermission]

    def get(self, request):
        """List or export all matching current inventory."""
        require(request, StockItem, 'view')
        rows = inventory_rows(request.query_params)
        columns = dict(INVENTORY_COLUMNS)
        if request.query_params.get('view') == 'summary':
            for field in ('项目', '地点完整路径', '最后库存变动时间', '库存备注'):
                columns.pop(field)
        else:
            columns = COUNT_COLUMNS
        return list_response(request, rows, columns, 'inventory')


class WmsRecords(WmsView):
    """Successful posted business records, distinct equipment and unit subtotals."""

    permission_classes = [WmsScopePermission]

    def get(self, request):
        """List successful scans and calculate statistics over all matching rows."""
        require(request, StockItem, 'view')
        rows = records_rows(request.query_params)
        units = defaultdict(Decimal)
        parts = defaultdict(Decimal)
        for row in rows:
            units[row['units']] += Decimal(row['quantity'])
            parts[row['IPN']] += Decimal(row['quantity'])
        columns = dict(RECORD_COLUMNS)
        if request.query_params.get('operation', 'INBOUND') == 'INBOUND':
            columns.pop('用途')
        return list_response(
            request,
            rows,
            columns,
            'posted-records',
            clean_json(
                {
                    'record_count': len(rows),
                    'part_count': len({r['part_id'] for r in rows}),
                    'units': dict(units),
                    'parts': dict(parts),
                    'part_units': {row['IPN']: row['units'] for row in rows},
                }
            ),
        )


class WmsOptions(WmsView):
    """Reference choices reuse existing categories, locations and projects."""

    permission_classes = [WmsScopePermission]

    def get(self, request):
        """Return complete paths and existing project choices."""
        require(request, StockItem, 'view')
        query = StockItem.objects.filter(current_stock_filter())
        params = request.query_params
        outbound = params.get('outbound') == 'true'
        status = {'state': 'INCOMPLETE'}
        resolved = None
        scanned_part = None
        location = None
        code = params.get('barcode', '').strip()
        batch = params.get('batch', '').strip()
        if params.get('location'):
            try:
                location = StockLocation.objects.get(
                    pk=int(params['location']), structural=False
                )
                query = query.filter(location=location)
            except (ValueError, StockLocation.DoesNotExist):
                status = {
                    'state': 'INVALID_LOCATION',
                    'error_message': '请选择有效的实际库位',
                }
                query = query.none()
        if code and status['state'] != 'INVALID_LOCATION':
            from .services import ScanStockError, ScanStockService

            try:
                service = ScanStockService(
                    user=request.user,
                    operation='OUTBOUND',
                    data={
                        'barcode': code,
                        'quantity': 1,
                        **({'batch': batch} if batch else {}),
                    },
                )
                resolved = service._resolve_barcode()
                if resolved.part is None:
                    raise ScanStockError(
                        'UNKNOWN_BARCODE', '设备码无法识别，请检查设备ID或库存项码'
                    )
                if not resolved.part.active:
                    raise ScanStockError('PART_INACTIVE', '设备已停用')
                if resolved.stock_item and location:
                    service._validate_part_and_item(
                        resolved.part, resolved.stock_item, location
                    )
                    if not StockItem.objects.filter(
                        current_stock_filter(), pk=resolved.stock_item.pk
                    ).exists():
                        raise ScanStockError(
                            'STOCK_ITEM_CONTEXT_MISMATCH', '库存项不属于当前有效库存'
                        )
                query = query.filter(part=resolved.part)
                scanned_part = {
                    'pk': resolved.part.pk,
                    'IPN': resolved.part.IPN,
                    'name': resolved.part.name,
                }
                if location:
                    rows = inventory_rows(
                        {'batch': batch, 'include_zero': 'true'}, queryset=query
                    )
                    totals = {
                        key: sum((Decimal(row[key]) for row in rows), Decimal(0))
                        for key in ('quantity', 'frozen', 'allocated', 'available')
                    }
                    status = {
                        'state': (
                            'NO_STOCK'
                            if totals['quantity'] == 0
                            else 'NO_AVAILABLE_STOCK'
                            if totals['available'] == 0
                            else 'AVAILABLE'
                        ),
                        'scope': 'project' if batch else 'all_projects',
                        'part_id': resolved.part.pk,
                        'location_id': location.pk,
                        'batch': batch or None,
                        'units': resolved.part.units,
                        **{key: str(value) for key, value in totals.items()},
                    }
            except ScanStockError as exc:
                status = {
                    'state': 'CONTEXT_MISMATCH'
                    if exc.code.startswith('STOCK_ITEM_')
                    else 'INVALID_BARCODE',
                    'error_code': exc.code,
                    'error_message': str(exc.message),
                }
                query = query.none()
        if outbound:
            query = query.filter(quantity__gt=0)
            if resolved and resolved.stock_item:
                query = query.filter(pk=resolved.stock_item.pk)
            if not location or not code:
                query = query.none()
        return Response(
            {
                'locations': [
                    {'value': str(x.pk), 'label': x.pathstring}
                    for x in StockLocation.objects.filter(structural=False).order_by(
                        'pk'
                    )
                ],
                'categories': [
                    {'value': str(x.pk), 'label': x.pathstring}
                    for x in PartCategory.objects.filter(structural=False).order_by(
                        'pk'
                    )
                ],
                'stock_status': status,
                'scanned_part': scanned_part,
                'projects': list(
                    query.exclude(batch__isnull=True)
                    .exclude(batch='')
                    .order_by('batch')
                    .values_list('batch', flat=True)
                    .distinct()
                ),
            }
        )


def has_business(part):
    """Protect identity and units after any inventory or business activity."""
    return (
        part.stock_items.exists()
        or part.scan_stock_events.exists()
        or part.stock_tracking_info.exists()
    )


def unique_path(model, path):
    """Require a unique existing usable full tree path."""
    matches = [
        obj for obj in model.objects.filter(structural=False) if obj.pathstring == path
    ]
    if len(matches) != 1:
        raise ValidationError(f'路径不存在、不允许存货或不唯一：{path}')
    return matches[0]


def equipment_values(row, part=None, preserve_blank=True):
    """Validate catalogue data while preserving unspecified import cells."""
    for required in ('name', 'units', 'category'):
        if required in row and not row[required]:
            raise ValidationError(f'设备资料必填：{required}')
    values = {}
    for field in (
        'IPN',
        'name',
        'brand',
        'model_number',
        'units',
        'specification',
        'notes',
    ):
        if field in row and (row[field] != '' or not preserve_blank):
            values[field] = row[field]
    if row.get('category'):
        values['category'] = unique_path(PartCategory, row['category'])
    elif 'category' in row and not preserve_blank:
        values['category'] = None
    for field in ('name', 'units', 'category'):
        if not values.get(field, getattr(part, field) if part else None):
            raise ValidationError(f'设备资料必填：{field}')
    if (
        part
        and values.get('IPN', part.IPN or '').casefold() == (part.IPN or '').casefold()
    ):
        values['IPN'] = part.IPN
    if part and has_business(part):
        if (
            values.get('units', part.units) != part.units
            or values.get('IPN', part.IPN or '').casefold()
            != (part.IPN or '').casefold()
        ):
            raise ValidationError('有业务数据的设备不能修改设备ID或单位')
    candidate = Part.objects.get(pk=part.pk) if part else Part(description='')
    for field, value in values.items():
        setattr(candidate, field, value)
    try:
        candidate.full_clean()
    except ModelValidationError as exc:
        raise ValidationError(exc.message_dict)
    return values


class WmsParts(WmsView):
    """Required equipment master data and reversible deactivation."""

    permission_classes = [WmsScopePermission]

    def get(self, request):
        """List or export equipment using the import template columns."""
        require(request, Part, 'view')
        query = filter_parts(
            Part.objects.select_related('category').order_by('IPN', 'pk'),
            request.query_params,
        )
        if request.query_params.get('search'):
            query = query.filter(
                Q(IPN__icontains=request.query_params['search'])
                | Q(name__icontains=request.query_params['search'])
            )
        return list_response(
            request, [part_row(p) for p in query], PART_COLUMNS, 'equipment'
        )

    @transaction.atomic
    def post(self, request):
        """Create or edit validated equipment; never physically delete it."""
        pk = request.data.get('pk')
        require(request, Part, 'change' if pk else 'add')
        part = Part.objects.select_for_update().filter(pk=pk).first() if pk else None
        if pk and not part:
            raise ValidationError('设备不存在')
        values = equipment_values(request.data, part, preserve_blank=False)
        if not part:
            part = Part(description='')
        for key, value in values.items():
            setattr(part, key, value)
        if 'active' in request.data:
            if not isinstance(request.data['active'], bool):
                raise ValidationError('停用状态无效')
            part.active = request.data['active']
        part.save()
        if not part.IPN:
            part.IPN = part.barcode
            part.save(update_fields=['IPN', 'ipn_hash'])
        return Response(part_row(part))


def read_upload(request, kind):
    """Parse UTF-8 CSV or XLSX with explicit business column names."""
    columns = PART_COLUMNS if kind == 'parts' else COUNT_COLUMNS
    upload = request.FILES.get('file')
    if not upload:
        rows = request.data.get('rows')
        if not isinstance(rows, list) or not rows or len(rows) > 5000:
            raise ValidationError('请上传文件或提供1至5000行数据')
        if any(not isinstance(row, dict) for row in rows):
            raise ValidationError('每一行必须是字段对象')
        return [
            {
                columns.get(k, k): str(v).strip() if v is not None else ''
                for k, v in row.items()
            }
            for row in rows
        ]
    if upload.size > 10 * 1024 * 1024:
        raise ValidationError('文件不能超过10MB')
    try:
        if upload.name.lower().endswith('.xlsx'):
            from openpyxl import load_workbook

            workbook = load_workbook(upload, read_only=True, data_only=False)
            sheet = workbook.active
            values = list(sheet.iter_rows(max_row=5002, values_only=True))
            workbook.close()
        elif upload.name.lower().endswith('.csv'):
            values = list(csv.reader(io.StringIO(upload.read().decode('utf-8-sig'))))
        else:
            raise ValidationError('支持.xlsx或UTF-8 CSV文件')
        if not values or len(values) < 2 or len(values) > 5001:
            raise ValidationError('文件必须包含1至5000行数据')
        headers = [str(v or '').strip() for v in values[0]]
        if len(set(headers)) != len(headers) or any(h not in columns for h in headers):
            raise ValidationError('表头重复或包含未知列，请使用专用模板')
        rows = []
        for values_row in values[1:]:
            if not any(v is not None and str(v).strip() for v in values_row):
                continue
            row = {}
            for header, value in zip(headers, values_row, strict=False):
                if isinstance(value, datetime):
                    value = value.date().isoformat()
                row[columns[header]] = str(value).strip() if value is not None else ''
            rows.append(row)
        return rows
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError('文件无法读取，请检查格式和内容') from exc


def inspect_rows(rows, kind, request):
    """Rebuild every target snapshot; any row error blocks the entire task."""
    results, seen, new_parts = [], set(), {}
    for index, row in enumerate(rows, 2):
        result = {
            'row': index,
            'IPN': row.get('IPN', ''),
            'batch': row.get('batch', ''),
            'location': row.get('location', ''),
        }
        try:
            ipn = row.get('IPN', '').strip()
            if not ipn and kind != 'parts':
                raise ValidationError('设备ID必填')
            part = Part.objects.filter(IPN__iexact=ipn).first() if ipn else None
            state = {'part': clean_json(part_row(part)) if part else None}
            if kind == 'parts':
                key = ipn.casefold() if ipn else f'new-{index}'
                require(request, Part, 'change' if part else 'add')
                values = equipment_values(row, part)
                result.update(
                    {
                        'action': '更新设备' if part else '新增设备',
                        'before': state['part'],
                        'after': {
                            k: v.pathstring if k == 'category' else v
                            for k, v in values.items()
                        },
                    }
                )
            else:
                if not row.get('batch') or not row.get('location'):
                    raise ValidationError('项目和实际地点必填')
                if len(row['batch']) > StockItem._meta.get_field('batch').max_length:
                    raise ValidationError('项目号最多100个字符')
                key = (
                    ipn.casefold(),
                    row['batch'],
                    row['location'],
                    row.get('detail_uid', ''),
                )
                location = unique_path(StockLocation, row['location'])
                state['location'] = {
                    'pk': location.pk,
                    'path': location.pathstring,
                    'structural': location.structural,
                }
                if part:
                    if not part.active or part.trackable:
                        raise ValidationError(
                            '设备已停用或属于序列号设备，不能盘点导入'
                        )
                    for field in ('name', 'units', 'category'):
                        current = (
                            part.category.pathstring
                            if field == 'category' and part.category
                            else getattr(part, field)
                        )
                        if row.get(field) and row[field] != current:
                            raise ValidationError(f'表中{field}与现有设备资料不一致')
                else:
                    require(request, Part, 'add')
                    values = equipment_values(row)
                    normalized = {
                        k: v.pk if k == 'category' else v for k, v in values.items()
                    }
                    if ipn:
                        normalized['IPN'] = ipn.casefold()
                    conflict_key = ipn.casefold() if kind == 'count' else key
                    if (
                        conflict_key in new_parts
                        and new_parts[conflict_key] != normalized
                    ):
                        raise ValidationError('同一新设备的资料冲突')
                    new_parts[conflict_key] = normalized
                    state['new_part'] = normalized
                try:
                    quantity = Decimal(row.get('quantity', ''))
                    if (
                        not quantity.is_finite()
                        or quantity < 0
                        or quantity >= Decimal('10000000000')
                        or quantity.as_tuple().exponent < -5
                    ):
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    raise ValidationError('盘点数量须为非负数，最多5位小数、10位整数')
                count_date = (
                    row.get('count_date')
                    or timezone.now().astimezone(CHINA).date().isoformat()
                )
                try:
                    date.fromisoformat(count_date)
                except ValueError:
                    raise ValidationError('盘点日期格式应为YYYY-MM-DD')
                items = (
                    list(
                        StockItem.objects.filter(
                            part=part,
                            location=location,
                            batch=row['batch'],
                            **(
                                {'source_detail__uid': row['detail_uid']}
                                if row.get('detail_uid')
                                else {}
                            ),
                        ).order_by('pk')
                    )
                    if part
                    else []
                )
                if len(items) > 1:
                    raise ValidationError('匹配到多条原生库存项，请处理歧义后重新预览')
                if row.get('detail_uid') and not items:
                    raise ValidationError('明细标识与设备、项目或库位不匹配')
                item = items[0] if items else None
                if item and (
                    not StockItem.objects.filter(
                        current_stock_filter(), pk=item.pk
                    ).exists()
                    or item.serialized
                    or item.is_expired()
                ):
                    raise ValidationError('目标库存状态不支持盘点')
                require(request, StockItem, 'change')
                if not item:
                    require(request, StockItem, 'add')
                before = item.quantity if item else Decimal(0)
                frozen = item.scan_frozen_quantity() if item else Decimal(0)
                allocated = item.allocation_count() if item else Decimal(0)
                if quantity < frozen + allocated:
                    references = '、'.join(item.active_scan_approval_references())
                    raise ValidationError(
                        f'盘点数量低于冻结及业务占用{frozen + allocated}；关联申请：{references}'
                    )
                state['stock'] = (
                    {
                        'pk': item.pk,
                        'source_detail': item.source_detail_id,
                        'quantity': str(before),
                        'frozen': str(frozen),
                        'allocated': str(allocated),
                        'changed_at': stamp(item.inventory_changed_at),
                        'batch': item.batch,
                        'location': item.location_id,
                        'status': item.status,
                        'packaging': item.packaging,
                        'expiry_date': str(item.expiry_date),
                        'supplier_part': item.supplier_part_id,
                        'freezes': list(
                            item.active_scan_freezes()
                            .order_by('pk')
                            .values_list('pk', 'requested_quantity')
                        ),
                    }
                    if item
                    else None
                )
                result.update(
                    {
                        'before': str(before),
                        'quantity': str(quantity),
                        'delta': str(quantity - before),
                        'frozen': str(frozen),
                        'count_date': count_date,
                        'remark': row.get('remark', ''),
                        'new_part': part is None,
                        'action': '新增设备及库存'
                        if not part
                        else '新增库存'
                        if not item
                        else '增加'
                        if quantity > before
                        else '减少'
                        if quantity < before
                        else '无变化',
                    }
                )
            if key in seen:
                raise ValidationError('文件内重复设备或设备＋项目＋实际地点')
            seen.add(key)
            result['state'] = clean_json(state)
        except (ValidationError, ModelValidationError) as exc:
            detail = (
                getattr(exc, 'detail', None)
                or getattr(exc, 'messages', None)
                or str(exc)
            )
            result['error'] = (
                '; '.join(str(v) for v in detail)
                if isinstance(detail, list)
                else str(detail)
            )
        results.append(result)
    return clean_json(results)


class WmsImports(WmsView):
    """Owned preview tasks with atomic optimistic concurrency and safe retries."""

    permission_classes = [WmsScopePermission]

    def get(self, request):
        """Download a dedicated catalogue or count template, or an owned result."""
        kind = request.query_params.get('kind', 'count')
        require(request, Part if kind == 'parts' else StockItem, 'view')
        if request.query_params.get('task'):
            task = WmsImportTask.objects.filter(
                pk=request.query_params['task'], operator=request.user
            ).first()
            if not task:
                raise ValidationError('导入任务不存在')
            return Response(
                {
                    'task': task.pk,
                    'completed_at': stamp(task.completed_at),
                    'results': task.result or task.preview,
                }
            )
        return export_csv(
            [], PART_COLUMNS if kind == 'parts' else COUNT_COLUMNS, f'{kind}-template'
        )

    @extend_schema(request=WmsImportInputSerializer, responses=WmsResultSerializer)
    @transaction.atomic
    def post(self, request):
        """Preview first, then lock and revalidate the complete task before commit."""
        if request.data.get('task'):
            task = (
                WmsImportTask.objects.select_for_update()
                .filter(pk=request.data['task'], operator=request.user)
                .first()
            )
            if not task:
                raise ValidationError('导入任务不存在')
            require(request, Part if task.kind == 'parts' else StockItem, 'view')
            if task.completed_at:
                return Response(
                    {
                        'task': task.pk,
                        'replayed': True,
                        'completed_at': stamp(task.completed_at),
                        'results': task.result,
                    }
                )
            if not task.preview or any(r.get('error') for r in task.preview):
                raise ValidationError('预览包含错误，整批不能提交')
            # Existing scans lock Part before StockItem. Preserve that order here.
            ipns = sorted(
                {r.get('IPN', '').casefold() for r in task.rows if r.get('IPN')}
            )
            list(
                Part.objects.select_for_update()
                .filter(IPN__in=[r.get('IPN') for r in task.rows if r.get('IPN')])
                .order_by('pk')
            )
            for ipn in ipns:
                key = hash_barcode(f'ipn:{ipn}')
                lock, _ = ScanInboundBarcodeLock.objects.get_or_create(barcode_hash=key)
                ScanInboundBarcodeLock.objects.select_for_update().get(pk=lock.pk)
            for ipn in ipns:
                part = Part.objects.filter(IPN__iexact=ipn).first()
                if part:
                    Part.objects.select_for_update().get(pk=part.pk)
                    list(
                        StockItem.objects.select_for_update()
                        .filter(part=part)
                        .order_by('pk')
                    )
            current = inspect_rows(task.rows, task.kind, request)
            if current != task.preview:
                raise ValidationError(
                    '预览后设备、库存或占用已变化，请重新预览；本次整批未提交'
                )
            for row, preview in zip(task.rows, current, strict=True):
                ipn = row.get('IPN', '').strip()
                part = Part.objects.filter(IPN__iexact=ipn).first() if ipn else None
                if task.kind == 'parts' or part is None:
                    values = equipment_values(row, part)
                    if not part:
                        part = Part(description='')
                    for key, value in values.items():
                        setattr(part, key, value)
                    part.save()
                    if not part.IPN:
                        part.IPN = part.barcode
                        part.save(update_fields=['IPN', 'ipn_hash'])
                if task.kind == 'count':
                    location = unique_path(StockLocation, row['location'])
                    stock_state = preview['state']['stock']
                    item = (
                        StockItem.objects.get(pk=stock_state['pk'])
                        if stock_state
                        else None
                    )
                    if item is None:
                        item = StockItem(
                            part=part,
                            location=location,
                            batch=row['batch'],
                            quantity=0,
                            delete_on_deplete=False,
                        )
                        item.save(user=request.user)
                    if Decimal(preview['delta']) != 0:
                        item.stocktake(
                            Decimal(preview['quantity']),
                            request.user,
                            notes=f'盘点任务#{task.pk}；盘点日期{preview["count_date"]}；{preview["remark"]}',
                        )
                        if item.quantity != Decimal(preview['quantity']):
                            raise ValidationError('库存调整失败，整批已回滚')
                    preview['stock_id'] = item.pk
            task.completed_at = timezone.now()
            task.result = [
                {
                    **r,
                    'operator': request.user.get_full_name() or request.user.username,
                    'completed_at': stamp(task.completed_at),
                }
                for r in current
            ]
            task.save(update_fields=['result', 'completed_at'])
            return Response(
                {
                    'task': task.pk,
                    'replayed': False,
                    'completed_at': stamp(task.completed_at),
                    'results': task.result,
                }
            )
        kind = request.data.get('kind', 'count')
        if kind not in ('parts', 'count'):
            raise ValidationError('导入类型无效')
        require(request, Part if kind == 'parts' else StockItem, 'view')
        rows = read_upload(request, kind)
        preview = inspect_rows(rows, kind, request)
        task = WmsImportTask.objects.create(
            kind=kind, operator=request.user, rows=rows, preview=preview
        )
        return Response(
            {
                'task': task.pk,
                'can_commit': bool(preview)
                and not any(r.get('error') for r in preview),
                'results': preview,
            },
            status=201,
        )
