"""JSON API for the Stock app."""

from collections import OrderedDict
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Q
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

import django_filters.rest_framework.filters as rest_filters
from django_filters.rest_framework.filterset import FilterSet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.serializers import ValidationError

import common.filters
import common.models
import common.settings
import InvenTree.helpers
import InvenTree.permissions
import stock.serializers as StockSerializers
from build.models import Build
from build.serializers import BuildSerializer
from company.models import Company, ManufacturerPart, SupplierPart
from company.serializers import CompanySerializer
from data_exporter.mixins import DataExportViewMixin
from generic.states.api import StatusView
from InvenTree.api import (
    BulkCreateMixin,
    BulkUpdateMixin,
    ListCreateDestroyAPIView,
    TreeMixin,
    meta_path,
)
from InvenTree.fields import InvenTreeOutputOption, OutputConfiguration
from InvenTree.filters import (
    SEARCH_ORDER_FILTER,
    InvenTreeDateFilter,
    NumberOrNullFilter,
)
from InvenTree.helpers import extract_serial_numbers, generateTestKey, str2bool
from InvenTree.mixins import (
    CreateAPI,
    CustomRetrieveUpdateDestroyAPI,
    ListAPI,
    ListCreateAPI,
    OutputOptionsMixin,
    RetrieveAPI,
    RetrieveUpdateDestroyAPI,
    SerializerContextMixin,
)
from order.models import PurchaseOrder, ReturnOrder, SalesOrder, TransferOrder
from order.serializers import (
    PurchaseOrderSerializer,
    ReturnOrderSerializer,
    SalesOrderSerializer,
    TransferOrderSerializer,
)
from part.models import BomItem, Part, PartCategory
from part.serializers import PartBriefSerializer
from stock.generators import generate_batch_code, generate_serial_number
from stock.models import (
    ScanStockApprovalRequest,
    ScanStockEvent,
    StockItem,
    StockItemTestResult,
    StockItemTracking,
    StockLocation,
    StockLocationType,
)
from stock.services import (
    ApprovalRequestService,
    ScanApprovalError,
    ScanStockIdempotencyConflict,
    ScanStockPreflight,
    ScanStockService,
)
from stock.status_codes import StockHistoryCode, StockStatus
from users.authentication import ApiTokenAuthentication, ExtendedOAuth2Authentication
from users.models import Owner
from users.oauth2_scopes import get_granular_scope
from users.permissions import check_user_permission


class GenerateBatchCode(GenericAPIView):
    """API endpoint for generating batch codes."""

    permission_classes = [InvenTree.permissions.IsAuthenticatedOrReadScope]
    serializer_class = StockSerializers.GenerateBatchCodeSerializer

    def post(self, request, *args, **kwargs):
        """Generate a new batch code."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = {'batch_code': generate_batch_code(**serializer.validated_data)}

        return Response(data, status=status.HTTP_201_CREATED)


class GenerateSerialNumber(GenericAPIView):
    """API endpoint for generating serial numbers."""

    permission_classes = [InvenTree.permissions.IsAuthenticatedOrReadScope]
    serializer_class = StockSerializers.GenerateSerialNumberSerializer

    def post(self, request, *args, **kwargs):
        """Generate a new serial number."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = {'serial_number': generate_serial_number(**serializer.validated_data)}

        return Response(data, status=status.HTTP_201_CREATED)


class ScanInboundPagination(LimitOffsetPagination):
    """Bounded pagination for the recent scan inbound audit trail."""

    default_limit = 50
    max_limit = 200


class ScanStockApprovalRolePermission(InvenTree.permissions.RolePermission):
    """Apply stock roles without overriding approval-domain permissions."""

    def has_permission(self, request, view):
        """Allow stock roles or the explicit approval permissions for this view."""
        if super().has_permission(request, view):
            return True

        user = request.user
        if request.method in SAFE_METHODS:
            return user.has_perm(
                ApprovalRequestService.APPROVE_PERMISSION
            ) or user.has_perm(ApprovalRequestService.AUDIT_PERMISSION)

        service_permission = getattr(view, 'approval_service_permission', None)
        if service_permission == 'approve':
            return user.has_perm(ApprovalRequestService.APPROVE_PERMISSION)
        # The service checks requester / auditor identity against the locked row.
        return service_permission == 'authenticated'


class ScanStockTokenMatchesOASRequirements(
    InvenTree.permissions.InvenTreeTokenMatchesOASRequirements
):
    """Require declared scopes for OAuth while retaining normal login access."""

    def has_permission(self, request, view):
        """Do not let the base permission fallback bypass a failed OAuth scope."""
        if self.is_oauth2ed(request):
            return self.check_oauth2_authentication(request, view)
        return bool(request.user and request.user.is_authenticated)


SCAN_STOCK_AUTHENTICATION_CLASSES = [
    ExtendedOAuth2Authentication,
    ApiTokenAuthentication,
    BasicAuthentication,
    SessionAuthentication,
]


class ScanStockActionView(GenericAPIView):
    """Shared transactional endpoint for inbound and outbound stock scans."""

    queryset = ScanStockEvent.objects.none()
    serializer_class = StockSerializers.ScanStockRequestSerializer
    pagination_class = ScanInboundPagination
    authentication_classes = SCAN_STOCK_AUTHENTICATION_CLASSES
    # This is a domain action, not creation of a ScanStockEvent by the caller.
    # Exclude the default ModelPermission so POST is governed by stock.change.
    approval_workflow = True
    permission_classes = [
        IsAuthenticated,
        InvenTree.permissions.RolePermission,
        ScanStockTokenMatchesOASRequirements,
    ]
    role_required = 'stock'
    rolemap = {'GET': 'view', 'POST': 'change'}
    operation = None
    required_alternate_scopes = InvenTree.permissions.map_scope(roles=['stock'])
    required_alternate_scopes['POST'] = [[get_granular_scope('change', 'stock')]]

    def get_serializer_class(self):
        """Expose event fields for GET metadata and scan input fields for POST."""
        if self.request.method == 'GET':
            return StockSerializers.ScanStockEventSerializer
        return StockSerializers.ScanStockRequestSerializer

    def get_serializer_context(self):
        """Provide the endpoint operation to the shared envelope serializer."""
        return {**super().get_serializer_context(), 'operation': self.operation}

    @extend_schema(responses=StockSerializers.ScanStockEventSerializer(many=True))
    def get(self, request, *args, **kwargs):
        """Return recent events for this direction (legacy compatibility)."""
        queryset = ScanStockEvent.objects.filter(operation=self.operation).order_by(
            '-scanned_at', '-pk'
        )
        page = self.paginate_queryset(queryset)
        serializer = StockSerializers.ScanStockEventSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        request=StockSerializers.ScanStockRequestSerializer,
        responses={
            200: StockSerializers.ScanStockEventSerializer,
            201: StockSerializers.ScanStockEventSerializer,
        },
    )
    def post(self, request, *args, **kwargs):
        """Process one idempotent stock scan."""
        if request.data.get('create_part') is not None and not (
            check_user_permission(request.user, Part, 'add')
            and check_user_permission(request.user, StockItem, 'add')
        ):
            # Check this before related-field validation so an unauthorized
            # caller cannot enumerate category or stock-location identifiers.
            raise PermissionDenied(_('You do not have permission to create products'))

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        approval_enabled = common.settings.get_global_setting(
            'SCAN_APPROVAL_ENABLED', False
        )
        approval_request_id = serializer.validated_data.get('approval_request_id')
        if approval_enabled and approval_request_id is None:
            return Response(
                {
                    'success': False,
                    'terminal': True,
                    'error_code': 'APPROVAL_REQUEST_REQUIRED',
                    'error_message': str(
                        _('An approval request is required for direct stock scans')
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        if not approval_enabled and approval_request_id is not None:
            return Response(
                {
                    'success': False,
                    'terminal': True,
                    'error_code': 'SCAN_APPROVAL_DISABLED',
                    'error_message': str(_('Scan stock approval is not enabled')),
                },
                status=status.HTTP_409_CONFLICT,
            )

        service = ScanStockService(
            user=request.user,
            operation=self.operation,
            data=serializer.validated_data,
            context=self.get_serializer_context(),
        )
        try:
            result = service.process()
        except ScanStockPreflight as exc:
            return Response(
                {
                    'success': False,
                    'terminal': False,
                    'error_code': exc.code,
                    'error_message': exc.message,
                    **exc.details,
                },
                status=status.HTTP_200_OK,
            )
        except ScanStockIdempotencyConflict as exc:
            return Response(
                {
                    'success': False,
                    'terminal': True,
                    'error_code': exc.code,
                    'error_message': exc.message,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ScanApprovalError as exc:
            return Response(
                {
                    'success': False,
                    'terminal': True,
                    'error_code': exc.code,
                    'error_message': exc.message,
                    **exc.details,
                },
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.conflict
                    else status.HTTP_400_BAD_REQUEST
                ),
            )

        output = StockSerializers.ScanStockEventSerializer(
            result.event, context=self.get_serializer_context()
        ).data
        response_status = (
            status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        )
        if not result.success:
            response_status = status.HTTP_400_BAD_REQUEST

        return Response(
            {
                'success': result.success,
                'terminal': True,
                'replayed': result.replayed,
                'part_created': result.part_created,
                'message': (
                    _('Scan already processed')
                    if result.replayed
                    else (
                        _('Scan added to approval request')
                        if result.event.outcome == ScanStockEvent.Outcome.PENDING
                        else _('Stock updated')
                    )
                ),
                'error_code': result.event.error_code,
                'error_message': result.event.error_message,
                **(result.details or {}),
                'event': output,
                # Compatibility alias for the retired scan-inbound response envelope.
                'record': output,
            },
            status=response_status,
        )


class ScanInboundView(ScanStockActionView):
    """Inbound scan endpoint and legacy route compatibility target."""

    operation = ScanStockEvent.Operation.INBOUND


class ScanOutboundView(ScanStockActionView):
    """Outbound scan endpoint."""

    operation = ScanStockEvent.Operation.OUTBOUND


class ScanStockEventFilter(FilterSet):
    """API filters for immutable stock scan events."""

    class Meta:
        """Filterable scan event fields."""

        model = ScanStockEvent
        fields = [
            'operation',
            'outcome',
            'error_code',
            'operator',
            'part',
            'stock_item',
            'source_location',
            'destination_location',
            'client_event_id',
            'barcode_hash',
        ]

    min_date = rest_filters.IsoDateTimeFilter(
        label=_('Scanned after'), field_name='scanned_at', lookup_expr='gte'
    )
    max_date = rest_filters.IsoDateTimeFilter(
        label=_('Scanned before'), field_name='scanned_at', lookup_expr='lte'
    )


class ScanStockEventView(DataExportViewMixin, ListAPI):
    """Read-only, filterable and exportable scan event list."""

    queryset = (
        ScanStockEvent.objects.select_related(
            'part', 'stock_item', 'source_location', 'destination_location', 'operator'
        )
        .prefetch_related('tracking_entries')
        .all()
    )
    serializer_class = StockSerializers.ScanStockEventSerializer
    pagination_class = ScanInboundPagination
    filterset_class = ScanStockEventFilter
    filter_backends = SEARCH_ORDER_FILTER
    ordering = ['-scanned_at', '-pk']
    ordering_fields = ['scanned_at', 'completed_at', 'pk']
    search_fields = [
        'error_code',
        'part_name_snapshot',
        'part_ipn_snapshot',
        'operator_username_snapshot',
        'operator_display_name_snapshot',
    ]
    role_required = 'stock'
    rolemap = {'GET': 'view'}


class ScanStockEventDetail(RetrieveAPI):
    """Read-only detail endpoint for one immutable stock scan event."""

    queryset = ScanStockEventView.queryset
    serializer_class = StockSerializers.ScanStockEventSerializer
    role_required = 'stock'
    rolemap = {'GET': 'view'}


class ScanStockApprovalRequestFilter(FilterSet):
    """Filters for the caller-visible scan approval request queue."""

    mine = rest_filters.BooleanFilter(method='filter_mine')
    pending_for_me = rest_filters.BooleanFilter(method='filter_pending_for_me')
    reviewed_by_me = rest_filters.BooleanFilter(method='filter_reviewed_by_me')

    def filter_reviewed_by_me(self, queryset, name, value):
        """Show decisions made by the current approver."""
        return queryset.filter(decided_by=self.request.user) if value else queryset

    class Meta:
        """Filterable approval request fields."""

        model = ScanStockApprovalRequest
        fields = ['direction', 'status', 'location', 'requester', 'approval_owner']

    def filter_mine(self, queryset, name, value):
        """Limit results to requests created by the current user."""
        if value:
            return queryset.filter(requester=self.request.user)
        return queryset

    def filter_pending_for_me(self, queryset, name, value):
        """Limit results to pending requests routed to the current user."""
        if not value:
            return queryset
        if not self.request.user.has_perm(ApprovalRequestService.APPROVE_PERMISSION):
            return queryset.none()
        owner_ids = [
            owner.pk for owner in Owner.get_owners_matching_user(self.request.user)
        ]
        return queryset.filter(
            approval_owner_id__in=owner_ids,
            status=ScanStockApprovalRequest.Status.PENDING_APPROVAL,
        ).exclude(requester=self.request.user)


class ScanStockApprovalRequestList(GenericAPIView):
    """List visible requests or create a requester-owned draft."""

    approval_workflow = True
    serializer_class = StockSerializers.ScanStockApprovalRequestSerializer
    authentication_classes = SCAN_STOCK_AUTHENTICATION_CLASSES
    permission_classes = [
        IsAuthenticated,
        ScanStockApprovalRolePermission,
        ScanStockTokenMatchesOASRequirements,
    ]
    pagination_class = ScanInboundPagination
    filterset_class = ScanStockApprovalRequestFilter
    role_required = 'stock'
    rolemap = {'GET': 'view', 'POST': 'add'}
    required_alternate_scopes = InvenTree.permissions.map_scope(roles=['stock'])

    def get_serializer_class(self):
        """Use the create envelope for POST and the read model otherwise."""
        if self.request.method == 'POST':
            return StockSerializers.ScanStockApprovalCreateSerializer
        return StockSerializers.ScanStockApprovalRequestSerializer

    def get_serializer_context(self):
        """Return list-safe approval serializer context for metadata."""
        return scan_approval_serializer_context(self.request, include_lines=False)

    def get_queryset(self):
        """Return requests visible to the requester, fixed owner or audit user."""
        queryset = ScanStockApprovalRequest.objects.select_related(
            'location', 'requester', 'approval_owner', 'decided_by', 'cancelled_by'
        )
        user = self.request.user
        if not user.has_perm(ApprovalRequestService.AUDIT_PERMISSION):
            owner_ids = []
            if user.has_perm(ApprovalRequestService.APPROVE_PERMISSION):
                owner_ids = [owner.pk for owner in Owner.get_owners_matching_user(user)]
            queryset = queryset.filter(
                Q(requester=user) | Q(approval_owner_id__in=owner_ids)
            )
        queryset = (
            StockSerializers.ScanStockApprovalRequestSerializer.annotate_queryset(
                queryset
            )
        )
        return ScanStockApprovalRequestFilter(
            self.request.query_params, queryset=queryset, request=self.request
        ).qs.order_by('-created_at', '-pk')

    @extend_schema(
        responses=StockSerializers.ScanStockApprovalRequestSerializer(many=True)
    )
    def get(self, request, *args, **kwargs):
        """Return the caller-visible approval queue."""
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = StockSerializers.ScanStockApprovalRequestSerializer(
            page,
            many=True,
            context=scan_approval_serializer_context(request, include_lines=False),
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        request=StockSerializers.ScanStockApprovalCreateSerializer,
        responses={201: StockSerializers.ScanStockApprovalRequestSerializer},
    )
    def post(self, request, *args, **kwargs):
        """Create a new server-side draft and fixed approval route."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval = ApprovalRequestService.create_request(
                user=request.user, **serializer.validated_data
            )
        except ScanApprovalError as exc:
            return scan_approval_error_response(exc)
        approval = self._annotated(approval.pk)
        return Response(
            StockSerializers.ScanStockApprovalRequestSerializer(
                approval,
                context=scan_approval_serializer_context(request, include_lines=True),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _annotated(pk):
        """Reload one request with shared response annotations."""
        return StockSerializers.ScanStockApprovalRequestSerializer.annotate_queryset(
            ScanStockApprovalRequest.objects.select_related(
                'location', 'requester', 'approval_owner', 'decided_by', 'cancelled_by'
            ).prefetch_related('lines__tracking_entries')
        ).get(pk=pk)


class ScanStockApprovalRequestDetail(GenericAPIView):
    """Retrieve one caller-visible request and all physical scan lines."""

    serializer_class = StockSerializers.ScanStockApprovalRequestSerializer
    authentication_classes = SCAN_STOCK_AUTHENTICATION_CLASSES
    approval_workflow = True
    permission_classes = ScanStockApprovalRequestList.permission_classes
    role_required = 'stock'
    rolemap = {'GET': 'view'}
    required_alternate_scopes = InvenTree.permissions.map_scope(roles=['stock'])

    def get_serializer_context(self):
        """Return detail approval serializer context for metadata."""
        return scan_approval_serializer_context(self.request, include_lines=True)

    def get(self, request, pk, *args, **kwargs):
        """Return one request when the caller can see it."""
        visible = ScanStockApprovalRequestList()
        visible.request = request
        visible.args = args
        visible.kwargs = kwargs
        try:
            approval = (
                visible.get_queryset()
                .prefetch_related('lines__tracking_entries')
                .get(pk=pk)
            )
        except ScanStockApprovalRequest.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(
            StockSerializers.ScanStockApprovalRequestSerializer(
                approval,
                context=scan_approval_serializer_context(request, include_lines=True),
            ).data
        )


class ScanStockApprovalLineDetail(GenericAPIView):
    """Logical deletion endpoint for a draft scan line."""

    serializer_class = StockSerializers.ScanStockApprovalRequestSerializer
    authentication_classes = SCAN_STOCK_AUTHENTICATION_CLASSES
    approval_workflow = True
    permission_classes = ScanStockApprovalRequestList.permission_classes
    role_required = 'stock'
    rolemap = {'DELETE': 'change'}
    required_alternate_scopes = InvenTree.permissions.map_scope(roles=['stock'])
    required_alternate_scopes['DELETE'] = [[get_granular_scope('change', 'stock')]]

    def get_serializer_context(self):
        """Return detail approval serializer context for metadata."""
        return scan_approval_serializer_context(self.request, include_lines=True)

    def delete(self, request, pk, event_pk, *args, **kwargs):
        """Remove one line and immediately release its reservation."""
        try:
            approval = ApprovalRequestService.remove_line(
                request_id=pk, event_id=event_pk, user=request.user
            )
        except ScanApprovalError as exc:
            return scan_approval_error_response(exc)
        approval = ScanStockApprovalRequestList._annotated(approval.pk)
        return Response(
            StockSerializers.ScanStockApprovalRequestSerializer(
                approval,
                context=scan_approval_serializer_context(request, include_lines=True),
            ).data
        )


class ScanStockApprovalAction(GenericAPIView):
    """Shared submit/cancel/approve/reject lifecycle action endpoint."""

    authentication_classes = SCAN_STOCK_AUTHENTICATION_CLASSES
    approval_workflow = True
    permission_classes = ScanStockApprovalRequestList.permission_classes
    serializer_class = StockSerializers.ScanStockApprovalActionSerializer
    role_required = 'stock'
    rolemap = {'POST': 'change'}
    required_alternate_scopes = InvenTree.permissions.map_scope(roles=['stock'])
    required_alternate_scopes['POST'] = [[get_granular_scope('change', 'stock')]]
    action = None

    @extend_schema(
        request=StockSerializers.ScanStockApprovalActionSerializer,
        responses=StockSerializers.ScanStockApprovalRequestSerializer,
    )
    def post(self, request, pk, *args, **kwargs):
        """Dispatch one named lifecycle action through the domain service."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get('note', '')
        method = getattr(ApprovalRequestService, self.action)
        call = {'request_id': pk, 'user': request.user}
        if self.action in {'cancel', 'approve', 'reject'}:
            call['note'] = note
        try:
            approval = method(**call)
        except ScanApprovalError as exc:
            return scan_approval_error_response(exc)
        approval = ScanStockApprovalRequestList._annotated(approval.pk)
        return Response(
            StockSerializers.ScanStockApprovalRequestSerializer(
                approval,
                context=scan_approval_serializer_context(request, include_lines=True),
            ).data
        )


class ScanStockApprovalSubmit(ScanStockApprovalAction):
    """Submit action."""

    action = 'submit'


class ScanStockApprovalCancel(ScanStockApprovalAction):
    """Cancel action."""

    action = 'cancel'
    approval_service_permission = 'authenticated'


class ScanStockApprovalApprove(ScanStockApprovalAction):
    """Approve action."""

    action = 'approve'
    approval_service_permission = 'approve'


class ScanStockApprovalReject(ScanStockApprovalAction):
    """Reject action."""

    action = 'reject'
    approval_service_permission = 'approve'


def scan_approval_error_response(exc: ScanApprovalError) -> Response:
    """Return the stable approval error envelope used by every lifecycle endpoint."""
    return Response(
        {
            'success': False,
            'error_code': exc.code,
            'error_message': exc.message,
            **exc.details,
        },
        status=(
            status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_400_BAD_REQUEST
        ),
    )


def scan_approval_serializer_context(request, *, include_lines: bool) -> dict:
    """Precompute current owner membership once for list-safe action flags."""
    return {
        'request': request,
        'include_lines': include_lines,
        'current_owner_ids': {
            owner.pk for owner in Owner.get_owners_matching_user(request.user)
        },
    }


class StockItemContextMixin:
    """Mixin class for adding StockItem object to serializer context."""

    role_required = 'stock.change'

    queryset = StockItem.objects.none()

    def get_serializer_context(self):
        """Extend serializer context."""
        context = super().get_serializer_context()
        context['request'] = self.request

        try:
            context['item'] = StockItem.objects.get(pk=self.kwargs.get('pk', None))
        except Exception:  # pragma: no cover
            pass

        return context


@extend_schema(responses={201: StockSerializers.StockItemSerializer(many=True)})
class StockItemSerialize(StockItemContextMixin, CreateAPI):
    """API endpoint for serializing a stock item."""

    serializer_class = StockSerializers.SerializeStockItemSerializer
    pagination_class = None

    def create(self, request, *args, **kwargs):
        """Serialize the provided StockItem."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Perform the actual serialization step
        items = serializer.save()

        queryset = StockSerializers.StockItemSerializer.annotate_queryset(items)

        # Apply any additional prefetching required by optional fields which end up
        # included in this response (e.g. 'tags', 'tests') - mirrors what
        # OutputOptionsMixin.get_queryset() does for a normal list/retrieve request,
        # which this manually-constructed response bypasses
        context = self.get_serializer_context()
        probe_serializer = StockSerializers.StockItemSerializer(context=context)
        queryset = probe_serializer.prefetch_queryset(queryset)

        response = StockSerializers.StockItemSerializer(
            queryset, many=True, context=context
        )

        return Response(response.data, status=status.HTTP_201_CREATED)


class StockItemInstall(StockItemContextMixin, CreateAPI):
    """API endpoint for installing a particular stock item into this stock item.

    - stock_item.part must be in the BOM for this part
    - stock_item must currently be "in stock"
    - stock_item must be serialized (and not belong to another item)
    """

    serializer_class = StockSerializers.InstallStockItemSerializer


class StockItemUninstall(StockItemContextMixin, CreateAPI):
    """API endpoint for removing (uninstalling) items from this item."""

    serializer_class = StockSerializers.UninstallStockItemSerializer


class StockItemConvert(StockItemContextMixin, CreateAPI):
    """API endpoint for converting a stock item to a variant part."""

    serializer_class = StockSerializers.ConvertStockItemSerializer


@extend_schema(responses={201: StockSerializers.StockItemSerializer(many=True)})
class StockItemDisassemble(StockItemContextMixin, CreateAPI):
    """API endpoint for disassembling a stock item into its component parts.

    The components are generated based on the BOM lines provided in the request,
    which must be valid BOM lines for the part associated with the stock item.
    """

    serializer_class = StockSerializers.DisassembleStockItemSerializer
    pagination_class = None

    def create(self, request, *args, **kwargs):
        """Disassemble the provided StockItem."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Perform the actual disassembly step
        items = serializer.save()

        queryset = StockSerializers.StockItemSerializer.annotate_queryset(
            StockItem.objects.filter(pk__in=[item.pk for item in items])
        )

        response = StockSerializers.StockItemSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )

        return Response(response.data, status=status.HTTP_201_CREATED)


class StockAdjustView(CreateAPI):
    """A generic class for handling stocktake actions.

    Subclasses exist for:

    - StockCount: count stock items
    - StockAdd: add stock items
    - StockRemove: remove stock items
    - StockTransfer: transfer stock items
    """

    queryset = StockItem.objects.none()

    def get_serializer_context(self):
        """Extend serializer context."""
        context = super().get_serializer_context()
        context['request'] = self.request

        return context


class StockChangeStatus(StockAdjustView):
    """API endpoint to change the status code of multiple StockItem objects."""

    serializer_class = StockSerializers.StockChangeStatusSerializer


class StockCount(StockAdjustView):
    """Endpoint for counting stock (performing a stocktake)."""

    serializer_class = StockSerializers.StockCountSerializer


class StockAdd(StockAdjustView):
    """Endpoint for adding a quantity of stock to an existing StockItem."""

    serializer_class = StockSerializers.StockAddSerializer


class StockRemove(StockAdjustView):
    """Endpoint for removing a quantity of stock from an existing StockItem."""

    serializer_class = StockSerializers.StockRemoveSerializer


class StockTransfer(StockAdjustView):
    """API endpoint for performing stock movements."""

    serializer_class = StockSerializers.StockTransferSerializer


class StockReturn(StockAdjustView):
    """API endpoint for returning items into stock.

    This API endpoint is for items that are initially considered "not in stock",
    and the user wants to return them to stock, marking them as
    "available" for further consumption or sale.
    """

    serializer_class = StockSerializers.StockReturnSerializer


class StockAssign(CreateAPI):
    """API endpoint for assigning stock to a particular customer."""

    queryset = StockItem.objects.all()
    serializer_class = StockSerializers.StockAssignmentSerializer

    def get_serializer_context(self):
        """Extend serializer context."""
        ctx = super().get_serializer_context()
        ctx['request'] = self.request

        return ctx


class StockMerge(CreateAPI):
    """API endpoint for merging multiple stock items."""

    queryset = StockItem.objects.none()
    serializer_class = StockSerializers.StockMergeSerializer

    def get_serializer_context(self):
        """Extend serializer context."""
        ctx = super().get_serializer_context()
        ctx['request'] = self.request
        return ctx


class StockLocationFilter(FilterSet):
    """Base class for custom API filters for the StockLocation endpoint."""

    class Meta:
        """Meta class options for this filterset."""

        model = StockLocation
        fields = ['name', 'structural', 'external']

    location_type = rest_filters.ModelChoiceFilter(
        queryset=StockLocationType.objects.all(), field_name='location_type'
    )

    has_location_type = rest_filters.BooleanFilter(
        label='has_location_type', method='filter_has_location_type'
    )

    def filter_has_location_type(self, queryset, name, value):
        """Filter by whether or not the location has a location type."""
        if str2bool(value):
            return queryset.exclude(location_type=None)
        return queryset.filter(location_type=None)

    depth = rest_filters.NumberFilter(
        label=_('Depth'), method='filter_depth', help_text=_('Filter by location depth')
    )

    def filter_depth(self, queryset, name, value):
        """Filter by the "depth" of the StockLocation.

        - This filter is used to limit the depth of the location tree
        - If the "parent" filter is also provided, the depth is calculated from the parent location
        """
        parent = self.data.get('parent', None)

        # Only filter if the parent filter is *not* provided
        if not parent:
            queryset = queryset.filter(level__lte=value)

        return queryset

    top_level = rest_filters.BooleanFilter(
        label=_('Top Level'),
        method='filter_top_level',
        help_text=_('Filter by top-level locations'),
    )

    def filter_top_level(self, queryset, name, value):
        """Filter by top-level locations."""
        cascade = str2bool(self.data.get('cascade', False))

        if value and not cascade:
            return queryset.filter(parent=None)

        return queryset

    cascade = rest_filters.BooleanFilter(
        label=_('Cascade'),
        method='filter_cascade',
        help_text=_('Include sub-locations in filtered results'),
    )

    def filter_cascade(self, queryset, name, value):
        """Filter by whether to include sub-locations in the filtered results.

        Note: If the "parent" filter is provided, we offload the logic to that method.
        """
        parent = self.data.get('parent', None)
        top_level = str2bool(self.data.get('top_level', None))

        # If the parent is *not* provided, update the results based on the "cascade" value
        if (not parent or top_level) and not value:
            # If "cascade" is False, only return top-level location
            queryset = queryset.filter(parent=None)

        return queryset

    parent = rest_filters.ModelChoiceFilter(
        queryset=StockLocation.objects.all(),
        method='filter_parent',
        label=_('Parent Location'),
        help_text=_('Filter by parent location'),
    )

    def filter_parent(self, queryset, name, value):
        """Filter by parent location.

        Note that the filtering behavior here varies,
        depending on whether the 'cascade' value is set.

        So, we have to check the "cascade" value here.
        """
        parent = value
        depth = self.data.get('depth', None)
        cascade = str2bool(self.data.get('cascade', False))

        if cascade:
            # Return recursive sub-locations
            queryset = queryset.filter(
                parent__in=parent.get_descendants(include_self=True)
            )
        else:
            # Return only direct children
            queryset = queryset.filter(parent=parent)

        if depth is not None:
            # Filter by depth from parent
            depth = int(depth)
            queryset = queryset.filter(level__lte=parent.level + depth)

        return queryset

    tags = common.filters.TagsFilter(label=_('Tags'))


class StockLocationMixin(SerializerContextMixin):
    """Mixin class for StockLocation API endpoints."""

    queryset = StockLocation.objects.all()
    serializer_class = StockSerializers.LocationSerializer

    def get_queryset(self, *args, **kwargs):
        """Return annotated queryset for the StockLocationList endpoint."""
        queryset = super().get_queryset(*args, **kwargs)
        queryset = StockSerializers.LocationSerializer.annotate_queryset(queryset)
        return queryset


class StockLocationOutputOptions(OutputConfiguration):
    """Output options for StockLocation serializers."""

    OPTIONS = [
        InvenTreeOutputOption(
            description='Include detailed information about the BOM item linked to this build line.',
            flag='path_detail',
        )
    ]


class StockLocationList(
    DataExportViewMixin,
    BulkUpdateMixin,
    StockLocationMixin,
    OutputOptionsMixin,
    ListCreateAPI,
):
    """API endpoint for list view of StockLocation objects.

    - GET: Return list of StockLocation objects
    - POST: Create a new StockLocation
    """

    filterset_class = StockLocationFilter
    filter_backends = SEARCH_ORDER_FILTER
    output_options = StockLocationOutputOptions

    search_fields = ['name', 'description', 'pathstring', 'tags__name', 'tags__slug']

    ordering_fields = ['name', 'pathstring', 'items', 'level', 'tree_id', 'lft']

    ordering = ['tree_id', 'lft', 'name']


class StockLocationDetail(
    StockLocationMixin, OutputOptionsMixin, CustomRetrieveUpdateDestroyAPI
):
    """API endpoint for detail view of StockLocation object."""

    output_options = StockLocationOutputOptions

    def destroy(self, request, *args, **kwargs):
        """Delete a Stock location instance via the API."""
        serializer = StockSerializers.LocationDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        delete_stock_items = InvenTree.helpers.str2bool(
            serializer.validated_data.get('delete_stock_items', False)
        )

        delete_sub_locations = InvenTree.helpers.str2bool(
            serializer.validated_data.get('delete_sub_locations', False)
        )

        return super().destroy(
            request,
            *args,
            **{
                **kwargs,
                'delete_sub_locations': delete_sub_locations,
                'delete_stock_items': delete_stock_items,
            },
        )


class LocationTreeFilter(FilterSet):
    """Custom filterset class for the StockLocationTree endpoint."""

    class Meta:
        """Metaclass options for this filterset."""

        model = StockLocation
        fields = ['parent', 'tree_id', 'level']

    max_level = rest_filters.NumberFilter(
        label=_('Max Level'),
        method='filter_max_level',
        help_text=_('Limit the depth of the category tree'),
    )

    def filter_max_level(self, queryset, name, value):
        """Filter by the maximum depth of the category tree."""
        return queryset.filter(level__lte=value)


class StockLocationTree(TreeMixin, ListAPI):
    """API endpoint for accessing a list of StockLocation objects, ready for rendering as a tree."""

    model_class = StockLocation
    queryset = StockLocation.objects.all()
    serializer_class = StockSerializers.LocationTreeSerializer
    filterset_class = LocationTreeFilter

    def get_queryset(self, *args, **kwargs):
        """Return annotated queryset for the StockLocationTree endpoint."""
        queryset = super().get_queryset(*args, **kwargs)
        queryset = StockSerializers.LocationTreeSerializer.annotate_queryset(queryset)
        return queryset


class StockLocationTypeList(ListCreateAPI):
    """API endpoint for a list of StockLocationType objects.

    - GET: Return a list of all StockLocationType objects
    - POST: Create a StockLocationType
    """

    queryset = StockLocationType.objects.all()
    serializer_class = StockSerializers.StockLocationTypeSerializer

    filter_backends = SEARCH_ORDER_FILTER

    ordering_fields = ['name', 'location_count', 'icon']

    ordering = ['-location_count']

    search_fields = ['name']

    def get_queryset(self):
        """Override the queryset method to include location count."""
        queryset = super().get_queryset()
        queryset = StockSerializers.StockLocationTypeSerializer.annotate_queryset(
            queryset
        )

        return queryset


class StockLocationTypeDetail(RetrieveUpdateDestroyAPI):
    """API detail endpoint for a StockLocationType object.

    - GET: return a single StockLocationType
    - PUT: update a StockLocationType
    - PATCH: partial update a StockLocationType
    - DELETE: delete a StockLocationType
    """

    queryset = StockLocationType.objects.all()
    serializer_class = StockSerializers.StockLocationTypeSerializer

    def get_queryset(self):
        """Override the queryset method to include location count."""
        queryset = super().get_queryset()
        queryset = StockSerializers.StockLocationTypeSerializer.annotate_queryset(
            queryset
        )

        return queryset


class StockFilter(FilterSet):
    """FilterSet for StockItem LIST API."""

    class Meta:
        """Metaclass options for this filterset."""

        model = StockItem

        # Simple filter filters
        fields = [
            'supplier_part',
            'parent',
            'belongs_to',
            'build',
            'customer',
            'consumed_by',
            'sales_order',
            'purchase_order',
            'tags__name',
            'tags__slug',
        ]

    # Relationship filters
    manufacturer = rest_filters.ModelChoiceFilter(
        label='Manufacturer',
        queryset=Company.objects.all(),
        method='filter_manufacturer',
    )

    @extend_schema_field(OpenApiTypes.INT)
    def filter_manufacturer(self, queryset, name, company):
        """Filter by manufacturer."""
        return queryset.filter(
            Q(supplier_part__manufacturer_part__manufacturer__is_manufacturer=True)
            & Q(supplier_part__manufacturer_part__manufacturer=company)
        )

    manufacturer_part = rest_filters.ModelChoiceFilter(
        label=_('Manufacturer Part'),
        queryset=ManufacturerPart.objects.all(),
        field_name='supplier_part__manufacturer_part',
    )

    supplier = rest_filters.ModelChoiceFilter(
        label=_('Supplier'),
        queryset=Company.objects.filter(is_supplier=True),
        field_name='supplier_part__supplier',
    )

    include_variants = rest_filters.BooleanFilter(
        label=_('Include Variants'), method='filter_include_variants'
    )

    def filter_include_variants(self, queryset, name, value):
        """Filter by whether or not to include variants of the selected part.

        Note:
        - This filter does nothing by itself, and requires the 'part' filter to be set.
        - Refer to the 'filter_part' method for more information.
        """
        return queryset

    part = rest_filters.ModelChoiceFilter(
        label=_('Part'), queryset=Part.objects.all(), method='filter_part'
    )

    def filter_part(self, queryset, name, part):
        """Filter StockItem list by provided Part instance.

        Note:
        - If "part" is a variant, include all variants of the selected part
        - Otherwise, filter by the selected part
        """
        include_variants = str2bool(self.data.get('include_variants', True))

        if include_variants:
            return queryset.filter(part__in=part.get_descendants(include_self=True))
        else:
            return queryset.filter(part=part)

    # Part name filters
    name = rest_filters.CharFilter(
        label=_('Part name (case insensitive)'),
        field_name='part__name',
        lookup_expr='iexact',
    )

    name_contains = rest_filters.CharFilter(
        label=_('Part name contains (case insensitive)'),
        field_name='part__name',
        lookup_expr='icontains',
    )

    name_regex = rest_filters.CharFilter(
        label=_('Part name (regex)'), field_name='part__name', lookup_expr='iregex'
    )

    # Part IPN filters
    IPN = rest_filters.CharFilter(
        label=_('Part IPN (case insensitive)'),
        field_name='part__IPN',
        lookup_expr='iexact',
    )

    IPN_contains = rest_filters.CharFilter(
        label=_('Part IPN contains (case insensitive)'),
        field_name='part__IPN',
        lookup_expr='icontains',
    )

    IPN_regex = rest_filters.CharFilter(
        label=_('Part IPN (regex)'), field_name='part__IPN', lookup_expr='iregex'
    )

    # Part attribute filters
    assembly = rest_filters.BooleanFilter(
        label=_('Assembly'), field_name='part__assembly'
    )

    active = rest_filters.BooleanFilter(label=_('Active'), field_name='part__active')
    salable = rest_filters.BooleanFilter(label=_('Salable'), field_name='part__salable')

    min_stock = rest_filters.NumberFilter(
        label=_('Minimum stock'), field_name='quantity', lookup_expr='gte'
    )

    max_stock = rest_filters.NumberFilter(
        label=_('Maximum stock'), field_name='quantity', lookup_expr='lte'
    )

    status = rest_filters.NumberFilter(label=_('Status Code'), method='filter_status')

    def filter_status(self, queryset, name, value):
        """Filter by integer status code.

        Note: Also account for the possibility of a custom status code.
        """
        q1 = Q(status=value, status_custom_key__isnull=True)
        q2 = Q(status_custom_key=value)

        return queryset.filter(q1 | q2).distinct()

    allocated = rest_filters.BooleanFilter(
        label='Is Allocated', method='filter_allocated'
    )

    def filter_allocated(self, queryset, name, value):
        """Filter by whether or not the stock item is 'allocated'."""
        if str2bool(value):
            # Filter StockItem with either build allocations or transfer order allocations or sales order allocations
            return queryset.filter(
                Q(sales_order_allocations__isnull=False)
                | Q(transfer_order_allocations__isnull=False)
                | Q(allocations__isnull=False)
            ).distinct()
        # Filter StockItem without build allocations or transfer order allocations or sales order allocations
        return queryset.filter(
            Q(sales_order_allocations__isnull=True)
            & Q(transfer_order_allocations__isnull=True)
            & Q(allocations__isnull=True)
        )

    expired = rest_filters.BooleanFilter(label='Expired', method='filter_expired')

    def filter_expired(self, queryset, name, value):
        """Filter by whether or not the stock item has expired."""
        if not common.settings.stock_expiry_enabled():
            return queryset

        if str2bool(value):
            return queryset.filter(StockItem.get_expired_filter())
        return queryset.exclude(StockItem.get_expired_filter())

    external = rest_filters.BooleanFilter(
        label=_('External Location'), method='filter_external'
    )

    def filter_external(self, queryset, name, value):
        """Filter by whether or not the stock item is located in an external location."""
        if str2bool(value):
            return queryset.filter(location__external=True)
        return queryset.exclude(location__external=True)

    in_stock = rest_filters.BooleanFilter(label='In Stock', method='filter_in_stock')

    def filter_in_stock(self, queryset, name, value):
        """Filter by if item is in stock."""
        if str2bool(value):
            return queryset.filter(StockItem.IN_STOCK_FILTER)
        return queryset.exclude(StockItem.IN_STOCK_FILTER)

    available = rest_filters.BooleanFilter(label='Available', method='filter_available')

    def filter_available(self, queryset, name, value):
        """Filter by whether the StockItem is "available" or not.

        Here, "available" means that the allocated quantity is less than the total quantity
        """
        if str2bool(value):
            # The 'quantity' field is greater than the calculated 'allocated' field
            # Note that the item must also be "in stock"
            return queryset.filter(StockItem.IN_STOCK_FILTER).filter(
                Q(quantity__gt=F('allocated'))
            )
        # The 'quantity' field is less than (or equal to) the calculated 'allocated' field
        return queryset.filter(Q(quantity__lte=F('allocated')))

    batch = rest_filters.CharFilter(
        label='Batch code filter (case insensitive)', lookup_expr='iexact'
    )

    batch_regex = rest_filters.CharFilter(
        label='Batch code filter (regex)', field_name='batch', lookup_expr='iregex'
    )

    is_building = rest_filters.BooleanFilter(label='In production')

    # Serial number filtering
    serial_gte = rest_filters.NumberFilter(
        label='Serial number GTE', field_name='serial_int', lookup_expr='gte'
    )
    serial_lte = rest_filters.NumberFilter(
        label='Serial number LTE', field_name='serial_int', lookup_expr='lte'
    )

    serial = rest_filters.CharFilter(
        label='Serial number', field_name='serial', lookup_expr='exact'
    )

    serialized = rest_filters.BooleanFilter(
        label='Has serial number', method='filter_serialized'
    )

    def filter_serialized(self, queryset, name, value):
        """Filter by whether the StockItem has a serial number (or not)."""
        q = Q(serial=None) | Q(serial='')

        if str2bool(value):
            return queryset.exclude(q)

        return queryset.filter(q).distinct()

    has_batch = rest_filters.BooleanFilter(
        label='Has batch code', method='filter_has_batch'
    )

    def filter_has_batch(self, queryset, name, value):
        """Filter by whether the StockItem has a batch code (or not)."""
        q = Q(batch=None) | Q(batch='')

        if str2bool(value):
            return queryset.exclude(q)

        return queryset.filter(q).distinct()

    tracked = rest_filters.BooleanFilter(label='Tracked', method='filter_tracked')

    def filter_tracked(self, queryset, name, value):
        """Filter by whether this stock item is *tracked*.

        Meaning either:
        - It has a serial number
        - It has a batch code
        """
        q_batch = Q(batch=None) | Q(batch='')
        q_serial = Q(serial=None) | Q(serial='')

        if str2bool(value):
            return queryset.exclude(q_batch & q_serial)

        return queryset.filter(q_batch).filter(q_serial).distinct()

    consumed = rest_filters.BooleanFilter(
        label=_('Consumed by Build Order'), method='filter_consumed'
    )

    def filter_consumed(self, queryset, name, value):
        """Filter by whether the stock item has been consumed by a build order."""
        if str2bool(value):
            return queryset.filter(consumed_by__isnull=False)
        return queryset.filter(consumed_by__isnull=True)

    installed = rest_filters.BooleanFilter(
        label=_('Installed in other stock item'), method='filter_installed'
    )

    def filter_installed(self, queryset, name, value):
        """Filter stock items by "belongs_to" field being empty."""
        if str2bool(value):
            return queryset.exclude(belongs_to=None)
        return queryset.filter(belongs_to=None)

    has_installed_items = rest_filters.BooleanFilter(
        label='Has installed items', method='filter_has_installed'
    )

    def filter_has_installed(self, queryset, name, value):
        """Filter stock items by "belongs_to" field being empty."""
        if str2bool(value):
            return queryset.filter(installed_items__gt=0)
        return queryset.filter(installed_items=0)

    has_child_items = rest_filters.BooleanFilter(
        label='Has child items', method='filter_has_child_items'
    )

    def filter_has_child_items(self, queryset, name, value):
        """Filter stock items by "belongs_to" field being empty."""
        if str2bool(value):
            return queryset.filter(child_items__gt=0)
        return queryset.filter(child_items=0)

    sent_to_customer = rest_filters.BooleanFilter(
        label='Sent to customer', method='filter_sent_to_customer'
    )

    def filter_sent_to_customer(self, queryset, name, value):
        """Filter by sent to customer."""
        if str2bool(value):
            return queryset.exclude(customer=None)
        return queryset.filter(customer=None)

    depleted = rest_filters.BooleanFilter(label='Depleted', method='filter_depleted')

    def filter_depleted(self, queryset, name, value):
        """Filter by depleted items."""
        if str2bool(value):
            return queryset.filter(quantity__lte=0)
        return queryset.exclude(quantity__lte=0)

    has_purchase_price = rest_filters.BooleanFilter(
        label='Has purchase price', method='filter_has_purchase_price'
    )

    def filter_has_purchase_price(self, queryset, name, value):
        """Filter by having a purchase price."""
        if str2bool(value):
            return queryset.exclude(purchase_price=None)
        return queryset.filter(purchase_price=None)

    category = rest_filters.ModelChoiceFilter(
        label=_('Category'),
        queryset=PartCategory.objects.all(),
        method='filter_category',
    )

    @extend_schema_field(OpenApiTypes.INT)
    def filter_category(self, queryset, name, category):
        """Filter based on part category."""
        child_categories = category.get_descendants(include_self=True)

        return queryset.filter(part__category__in=child_categories)

    bom_item = rest_filters.ModelChoiceFilter(
        label=_('BOM Item'), queryset=BomItem.objects.all(), method='filter_bom_item'
    )

    @extend_schema_field(OpenApiTypes.INT)
    def filter_bom_item(self, queryset, name, bom_item):
        """Filter based on BOM item."""
        return queryset.filter(bom_item.get_stock_filter())

    part_tree = rest_filters.ModelChoiceFilter(
        label=_('Part Tree'), queryset=Part.objects.all(), method='filter_part_tree'
    )

    @extend_schema_field(OpenApiTypes.INT)
    def filter_part_tree(self, queryset, name, part_tree):
        """Filter based on part tree."""
        return queryset.filter(part__tree_id=part_tree.tree_id)

    company = rest_filters.ModelChoiceFilter(
        label=_('Company'), queryset=Company.objects.all(), method='filter_company'
    )

    @extend_schema_field(OpenApiTypes.INT)
    def filter_company(self, queryset, name, company):
        """Filter by company (either manufacturer or supplier)."""
        return queryset.filter(
            Q(supplier_part__supplier=company)
            | Q(supplier_part__manufacturer_part__manufacturer=company)
        ).distinct()

    created_before = InvenTreeDateFilter(
        label=_('Created before'), field_name='creation_date', lookup_expr='lt'
    )

    created_after = InvenTreeDateFilter(
        label=_('Created after'), field_name='creation_date', lookup_expr='gt'
    )

    updated_before = InvenTreeDateFilter(
        label=_('Updated before'), field_name='updated', lookup_expr='lt'
    )

    updated_after = InvenTreeDateFilter(
        label=_('Updated after'), field_name='updated', lookup_expr='gt'
    )

    stocktake_before = InvenTreeDateFilter(
        label=_('Stocktake Before'), field_name='stocktake_date', lookup_expr='lt'
    )

    stocktake_after = InvenTreeDateFilter(
        label=_('Stocktake After'), field_name='stocktake_date', lookup_expr='gt'
    )

    has_stocktake = rest_filters.BooleanFilter(
        label=_('Has Stocktake Date'), method='filter_has_stocktake'
    )

    def filter_has_stocktake(self, queryset, name, value):
        """Filter by whether or not the StockItem has a stocktake date."""
        if str2bool(value):
            return queryset.exclude(stocktake_date=None)
        return queryset.filter(stocktake_date=None)

    # Stock "expiry" filters
    expiry_before = InvenTreeDateFilter(
        label=_('Expiry date before'), field_name='expiry_date', lookup_expr='lt'
    )

    expiry_after = InvenTreeDateFilter(
        label=_('Expiry date after'), field_name='expiry_date', lookup_expr='gt'
    )

    stale = rest_filters.BooleanFilter(label=_('Stale'), method='filter_stale')

    def filter_stale(self, queryset, name, value):
        """Filter by stale stock items."""
        stale_days = common.models.InvenTreeSetting.get_setting('STOCK_STALE_DAYS')

        if stale_days <= 0:
            # No filtering, does not make sense
            return queryset

        stale_date = InvenTree.helpers.current_date() + timedelta(days=stale_days)
        stale_filter = (
            StockItem.IN_STOCK_FILTER
            & ~Q(expiry_date=None)
            & Q(expiry_date__lt=stale_date)
        )

        if str2bool(value):
            return queryset.filter(stale_filter)
        else:
            return queryset.exclude(stale_filter)

    cascade = rest_filters.BooleanFilter(
        method='filter_cascade',
        label=_('Cascade Locations'),
        help_text=_('If true, include items in child locations of the given location'),
    )

    location = NumberOrNullFilter(
        method='filter_location',
        label=_('Location'),
        help_text=_("Filter by numeric Location ID or the literal 'null'"),
    )

    def filter_cascade(self, queryset, name, value):
        """Dummy filter method for 'cascade'.

        - Ensures 'cascade' appears in API documentation
        - Does NOT actually filter the queryset directly
        """
        return queryset

    def filter_location(self, queryset, name, value):
        """Filter for location that also applies cascade logic."""
        cascade = str2bool(self.data.get('cascade', True))

        if value == 'null':
            if not cascade:
                return queryset.filter(location=None)
            return queryset

        if not cascade:
            return queryset.filter(location=value)

        try:
            loc_obj = StockLocation.objects.get(pk=value)
        except StockLocation.DoesNotExist:
            return queryset

        children = loc_obj.getUniqueChildren()
        return queryset.filter(location__in=children)

    tags = common.filters.TagsFilter(label=_('Tags'))


class StockApiMixin(SerializerContextMixin):
    """Mixin class for StockItem API endpoints."""

    serializer_class = StockSerializers.StockItemSerializer
    queryset = StockItem.objects.all()

    def get_queryset(self, *args, **kwargs):
        """Annotate queryset."""
        queryset = super().get_queryset(*args, **kwargs)
        queryset = StockSerializers.StockItemSerializer.annotate_queryset(queryset)

        return queryset

    def get_serializer_context(self):
        """Extend serializer context."""
        ctx = super().get_serializer_context()
        ctx['user'] = getattr(self.request, 'user', None)

        return ctx


class StockOutputOptions(OutputConfiguration):
    """Output options for StockItem serializers."""

    OPTIONS = [
        InvenTreeOutputOption('part_detail', default=True),
        InvenTreeOutputOption('path_detail'),
        InvenTreeOutputOption('supplier_part_detail'),
        InvenTreeOutputOption('location_detail'),
        InvenTreeOutputOption('tests'),
    ]


class StockList(
    DataExportViewMixin,
    BulkUpdateMixin,
    StockApiMixin,
    OutputOptionsMixin,
    ListCreateDestroyAPIView,
):
    """API endpoint for list view of Stock objects.

    - GET: Return a list of all StockItem objects (with optional query filters)
    - POST: Create a new StockItem
    - DELETE: Delete multiple StockItem objects
    """

    filterset_class = StockFilter
    output_options = StockOutputOptions

    def filter_queryset(self, queryset):
        """Apply WMS filters after native permissions, search and table filters."""
        queryset = super().filter_queryset(queryset)
        if self.request.query_params.get('wms') != 'true':
            return queryset
        from .wms import current_stock_filter, filter_parts

        params = self.request.query_params
        queryset = filter_parts(
            queryset.filter(current_stock_filter()), params, 'part__'
        )
        if params.get('contract'):
            queryset = queryset.filter(
                source_detail__contract__number__icontains=params['contract']
            )
        if params.get('detail_uid'):
            queryset = queryset.filter(source_detail__uid=params['detail_uid'])
        items = list(queryset)
        frozen = StockItem.bulk_scan_frozen_quantity(items)
        ids = []
        for item in items:
            occupied = frozen.get(item.pk, 0) > 0
            positive = item.quantity > 0
            if (
                params.get('include_zero') != 'true'
                and params.get('has_stock') != 'false'
                and not (positive or occupied)
            ):
                continue
            if params.get('has_stock') in ('true', 'false') and positive != (
                params['has_stock'] == 'true'
            ):
                continue
            if params.get('has_frozen') in ('true', 'false') and occupied != (
                params['has_frozen'] == 'true'
            ):
                continue
            ids.append(item.pk)
        return queryset.filter(pk__in=ids)

    def list(self, request, *args, **kwargs):
        """Keep native rows and operations, with totals over the same full scope."""
        if request.query_params.get('wms') != 'true':
            return super().list(request, *args, **kwargs)
        from collections import defaultdict
        from decimal import Decimal

        from .wms import (
            COUNT_COLUMNS,
            INVENTORY_COLUMNS,
            inventory_rows,
            list_response,
            part_row,
            stamp,
        )

        queryset = self.filter_queryset(self.get_queryset())
        summary = request.query_params.get('view') == 'summary'
        rows = inventory_rows(
            {'include_zero': 'true', 'view': 'summary' if summary else 'detail'},
            queryset,
        )
        units = defaultdict(Decimal)
        for row in rows:
            units[row['units']] += Decimal(row['quantity'])
        statistics = {
            'part_count': len({row['pk'] for row in rows}),
            'units': dict(units),
        }
        if summary or request.query_params.get('export') == 'csv':
            columns = dict(INVENTORY_COLUMNS)
            for label in (
                ('项目', '地点完整路径', '最后库存变动时间', '库存备注')
                if summary
                else ('项目数量',)
            ):
                columns.pop(label)
            if not summary:
                columns = COUNT_COLUMNS
            return list_response(request, rows, columns, 'inventory', statistics)
        page = self.paginate_queryset(queryset)
        items = page if page is not None else list(queryset)
        data = self.get_serializer(items, many=True).data
        for item, record in zip(items, data, strict=True):
            record['wms_equipment'] = part_row(item.part)
            record['inventory_changed_at'] = stamp(item.inventory_changed_at)
        response = (
            self.get_paginated_response(data)
            if page is not None
            else Response({'count': len(data), 'results': data})
        )
        response.data['statistics'] = statistics
        return response

    def create(self, request, *args, **kwargs):
        """Create a new StockItem object via the API.

        We override the default 'create' implementation.

        If a location is *not* specified, but the linked *part* has a default location,
        we can pre-fill the location automatically.
        """
        user = request.user

        # Copy the request data, to side-step "mutability" issues
        data = OrderedDict()
        # Update with cleaned input data
        data.update(self.clean_data(request.data))

        quantity = data.get('quantity', None)

        if quantity is None:
            raise ValidationError({'quantity': _('Quantity is required')})

        try:
            part = Part.objects.get(pk=data.get('part', None))
        except (ValueError, Part.DoesNotExist):
            raise ValidationError({'part': _('Valid part must be supplied')})

        location = data.get('location', None)
        # Override location if not specified
        if location is None and part.default_location:
            data['location'] = part.default_location.pk

        expiry_date = data.get('expiry_date', None)

        # An expiry date was *not* specified - try to infer it!
        if expiry_date is None and part.default_expiry > 0:
            data['expiry_date'] = InvenTree.helpers.current_date() + timedelta(
                days=part.default_expiry
            )

        # Attempt to extract serial numbers from submitted data
        serials = None

        # Check if a set of serial numbers was provided
        serial_numbers = data.pop('serial_numbers', '')

        # Exclude 'serial' from submitted data
        # We use 'serial_numbers' for item creation
        data.pop('serial', None)

        # Check if the supplier_part has a package size defined, which is not 1
        if supplier_part_id := data.get('supplier_part', None):
            try:
                supplier_part = SupplierPart.objects.get(pk=supplier_part_id)
            except Exception:
                raise ValidationError(
                    {'supplier_part': _('The given supplier part does not exist')}
                )

            if supplier_part.base_quantity() != 1:
                # Skip this check if pack size is 1 - makes no difference
                # use_pack_size = True -> Multiply quantity by pack size
                # use_pack_size = False -> Use quantity as is
                if 'use_pack_size' not in data:
                    raise ValidationError(
                        {
                            'use_pack_size': _(
                                'The supplier part has a pack size defined, but flag use_pack_size not set'
                            )
                        }
                    )
                elif bool(data.get('use_pack_size')):
                    quantity = data['quantity'] = supplier_part.base_quantity(quantity)

                    # Divide purchase price by pack size, to save correct price per stock item
                    if (
                        data.get('purchase_price')
                        and supplier_part.pack_quantity_native
                    ):
                        try:
                            data['purchase_price'] = float(
                                data['purchase_price']
                            ) / float(supplier_part.pack_quantity_native)
                        except ValueError:  # pragma: no cover
                            pass

        # Now remove the flag from data, so that it doesn't interfere with saving
        # Do this regardless of results above
        data.pop('use_pack_size', None)

        # Extract 'status' flag from data
        status_raw = data.pop('status', None)
        status_custom = data.pop('status_custom_key', None)
        status_value = status_custom or status_raw

        # Assign serial numbers for a trackable part
        if serial_numbers:
            if not part.trackable:
                raise ValidationError(
                    {
                        'serial_numbers': [
                            _(
                                'Serial numbers cannot be supplied for a non-trackable part'
                            )
                        ]
                    }
                )

            # If serial numbers are specified, check that they match!
            try:
                serials = extract_serial_numbers(
                    serial_numbers, quantity, part.get_latest_serial_number(), part=part
                )

                # Determine if any of the specified serial numbers are invalid
                # Note "invalid" means either they already exist, or do not pass custom rules
                invalid = []
                errors = []

                try:
                    invalid = part.find_conflicting_serial_numbers(serials)
                except DjangoValidationError as exc:
                    errors.append(exc.message)

                if len(invalid) > 0:
                    msg = _('The following serial numbers already exist or are invalid')
                    msg += ' : '
                    msg += ','.join([str(e) for e in invalid])

                    errors.append(msg)

                if len(errors) > 0:
                    raise ValidationError({'serial_numbers': errors})

            except DjangoValidationError as e:
                raise ValidationError(
                    {'quantity': e.messages, 'serial_numbers': e.messages}
                )

        if serials is not None:
            """If the stock item is going to be serialized, set the quantity to 1."""
            data['quantity'] = 1

        # De-serialize the provided data
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Extract location information
        location = serializer.validated_data.get('location', None)

        with transaction.atomic():
            if serials:
                # Create multiple serialized StockItem objects
                items = StockItem._create_serial_numbers(
                    serials, **serializer.validated_data
                )

                # Next, bulk-create stock tracking entries for the newly created items
                tracking = []

                for item in items:
                    if status_value and not item.compare_status(status_value):
                        item.set_status(status_value)
                        item.save()

                    if entry := item.add_tracking_entry(
                        StockHistoryCode.CREATED,
                        user,
                        deltas={'status': status_value},
                        location=location,
                        quantity=float(item.quantity),
                        commit=False,
                    ):
                        tracking.append(entry)

                StockItemTracking.objects.bulk_create(tracking, batch_size=250)

                # Annotate the stock items with part information
                queryset = StockSerializers.StockItemSerializer.annotate_queryset(items)

                response = StockSerializers.StockItemSerializer(
                    queryset, many=True, context=self.get_serializer_context()
                )

                response_data = response.data

            else:
                # Create a single StockItem object
                # Note: This automatically creates a tracking entry
                item = StockItem(**serializer.validated_data)

                if status_value and not item.compare_status(status_value):
                    item.set_status(status_value)

                item.save(user=user)
                item.refresh_from_db()

                response_data = [
                    StockSerializers.StockItemSerializer(
                        item, context=self.get_serializer_context()
                    ).data
                ]

        return Response(
            response_data,
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(serializer.data),
        )

    filter_backends = SEARCH_ORDER_FILTER

    ordering_field_aliases = {
        'part': 'part__name',
        'location': 'location__pathstring',
        'IPN': 'part__IPN',
        'SKU': 'supplier_part__SKU',
        'MPN': 'supplier_part__manufacturer_part__MPN',
        'stock': ['quantity', 'serial_int', 'serial'],
    }

    ordering_fields = [
        'batch',
        'location',
        'part',
        'part__name',
        'part__IPN',
        'updated',
        'purchase_price',
        'creation_date',
        'stocktake_date',
        'expiry_date',
        'packaging',
        'quantity',
        'stock',
        'status',
        'IPN',
        'SKU',
        'MPN',
    ]

    ordering = ['part__name', 'quantity', 'location']

    search_fields = [
        'part__brand',
        'part__model_number',
        'part__specification',
        'part__notes',
        'notes',
        'serial',
        'batch',
        'location__name',
        'part__name',
        'part__IPN',
        'part__description',
        'supplier_part__SKU',
        'supplier_part__supplier__name',
        'supplier_part__manufacturer_part__MPN',
        'supplier_part__manufacturer_part__manufacturer__name',
        'tags__name',
        'tags__slug',
    ]


class StockDetail(StockApiMixin, OutputOptionsMixin, RetrieveUpdateDestroyAPI):
    """API detail endpoint for a single StockItem instance."""

    output_options = StockOutputOptions


class StockItemSerialNumbers(RetrieveAPI):
    """View extra serial number information for a given stock item.

    Provides information on the "previous" and "next" stock items,
    based on the serial number of the given stock item.
    """

    queryset = StockItem.objects.all()
    serializer_class = StockSerializers.StockItemSerialNumbersSerializer


class StockItemTestResultMixin(SerializerContextMixin):
    """Mixin class for the StockItemTestResult API endpoints."""

    queryset = StockItemTestResult.objects.all()
    serializer_class = StockSerializers.StockItemTestResultSerializer

    def get_serializer_context(self):
        """Extend serializer context."""
        ctx = super().get_serializer_context()
        ctx['request'] = self.request
        return ctx


class StockItemTestResultOutputOptions(OutputConfiguration):
    """Output options for StockItemTestResult endpoint."""

    OPTIONS = [
        InvenTreeOutputOption(flag='user_detail'),
        InvenTreeOutputOption(flag='template_detail'),
    ]


class StockItemTestResultDetail(
    StockItemTestResultMixin, OutputOptionsMixin, RetrieveUpdateDestroyAPI
):
    """Detail endpoint for StockItemTestResult."""

    output_options = StockItemTestResultOutputOptions


class StockItemTestResultFilter(FilterSet):
    """API filter for the StockItemTestResult list."""

    class Meta:
        """Metaclass options."""

        model = StockItemTestResult

        # Simple filter fields
        fields = ['user', 'template', 'result', 'value']

    build = rest_filters.ModelChoiceFilter(
        label='Build', queryset=Build.objects.all(), field_name='stock_item__build'
    )

    part = rest_filters.ModelChoiceFilter(
        label='Part', queryset=Part.objects.all(), field_name='stock_item__part'
    )

    required = rest_filters.BooleanFilter(
        label='Required', field_name='template__required'
    )

    enabled = rest_filters.BooleanFilter(
        label='Enabled', field_name='template__enabled'
    )

    test = rest_filters.CharFilter(
        label='Test name (case insensitive)', method='filter_test_name'
    )

    def filter_test_name(self, queryset, name, value):
        """Filter by test name.

        This method is provided for legacy support,
        where the StockItemTestResult model had a "test" field.
        Now the "test" name is stored against the PartTestTemplate model
        """
        key = generateTestKey(value)
        return queryset.filter(template__key=key)

    include_installed = rest_filters.BooleanFilter(
        method='filter_include_installed',
        label=_('Include Installed'),
        help_text=_(
            'If true, include test results for items installed underneath the given stock item'
        ),
    )

    stock_item = rest_filters.NumberFilter(
        method='filter_stock_item',
        label=_('Stock Item'),
        help_text=_('Filter by numeric Stock Item ID'),
    )

    def filter_include_installed(self, queryset, name, value):
        """Dummy filter method for 'include_installed'.

        - Ensures 'include_installed' appears in API documentation
        - Does NOT actually filter the queryset directly
        - The actual logic is handled in filter_stock_item method
        """
        return queryset

    def filter_stock_item(self, queryset, name, value):
        """Filter for stock_item that also applies include_installed logic."""
        include_installed = str2bool(self.data.get('include_installed', False))

        try:
            item = StockItem.objects.get(pk=value)

        except StockItem.DoesNotExist:
            raise ValidationError(
                {
                    'stock_item': _('Stock item with ID {id} does not exist').format(
                        id=value
                    )
                }
            )

        items = [item]

        if include_installed:
            # Include items which are installed "underneath" this item
            # Note that this function is recursive!
            installed_items = item.get_installed_items(cascade=True)
            items += list(installed_items)

        return queryset.filter(stock_item__in=items)


class StockItemTestResultList(
    BulkCreateMixin,
    StockItemTestResultMixin,
    OutputOptionsMixin,
    ListCreateDestroyAPIView,
):
    """API endpoint for listing (and creating) a StockItemTestResult object."""

    filterset_class = StockItemTestResultFilter
    filter_backends = SEARCH_ORDER_FILTER
    output_options = StockItemTestResultOutputOptions

    filterset_fields = ['user', 'template', 'result', 'value']
    ordering_fields = [
        'date',
        'result',
        'started_datetime',
        'finished_datetime',
        'test_station',
    ]

    ordering = 'date'

    def perform_create(self, serializer):
        """Create a new test result object.

        Also, check if an attachment was uploaded alongside the test result,
        and save it to the database if it were.
        """
        serializer.save(user=self.request.user)


class StockTrackingDetail(RetrieveAPI):
    """Detail API endpoint for StockItemTracking model."""

    queryset = StockItemTracking.objects.all()
    serializer_class = StockSerializers.StockTrackingSerializer


class StockTrackingOutputOptions(OutputConfiguration):
    """Output options for StockItemTracking endpoint."""

    OPTIONS = [
        InvenTreeOutputOption(flag='item_detail'),
        InvenTreeOutputOption(flag='part_detail'),
        InvenTreeOutputOption(flag='user_detail'),
    ]


class StockTrackingFilter(FilterSet):
    """API filter options for the StockTrackingList endpoint."""

    class Meta:
        """Metaclass options."""

        model = StockItemTracking
        fields = ['item', 'user', 'scan_event']

    include_variants = rest_filters.BooleanFilter(
        label=_('Include Part Variants'), method='filter_include_variants'
    )

    def filter_include_variants(self, queryset, name, value):
        """Filter by whether or not to include part variants.

        Note:
        - This filter does nothing by itself, and is only used to modify the behavior of the 'part' filter.
        - Refer to the 'filter_part' method for more information on how this works.
        """
        return queryset

    part = rest_filters.ModelChoiceFilter(
        label=_('Part'), queryset=Part.objects.all(), method='filter_part'
    )

    def filter_part(self, queryset, name, part):
        """Filter StockTracking entries by the linked part.

        Note:
        - This filter behavior also takes into account the 'include_variants' filter, which determines whether or not to include part variants in the results.
        """
        include_variants = str2bool(self.data.get('include_variants', False))

        if include_variants:
            return queryset.filter(part__in=part.get_descendants(include_self=True))
        else:
            return queryset.filter(part=part)

    min_date = InvenTreeDateFilter(
        label=_('Date after'), field_name='date', lookup_expr='gt'
    )

    max_date = InvenTreeDateFilter(
        label=_('Date before'), field_name='date', lookup_expr='lt'
    )


class StockTrackingList(
    SerializerContextMixin, DataExportViewMixin, OutputOptionsMixin, ListAPI
):
    """API endpoint for list view of StockItemTracking objects.

    StockItemTracking objects are read-only
    (they are created by internal model functionality)

    - GET: Return list of StockItemTracking objects
    """

    queryset = StockItemTracking.objects.all().prefetch_related('item', 'part')
    serializer_class = StockSerializers.StockTrackingSerializer
    filterset_class = StockTrackingFilter
    output_options = StockTrackingOutputOptions

    def get_delta_model_map(self) -> dict:
        """Return a mapping of delta models to their respective models and serializers.

        This is used to generate additional context information for the historical data,
        with some attempt at caching so that we can reduce the number of database hits.
        """
        return {
            'part': (Part, PartBriefSerializer),
            'location': (StockLocation, StockSerializers.LocationSerializer),
            'old_location': (StockLocation, StockSerializers.LocationSerializer),
            'customer': (Company, CompanySerializer),
            'purchaseorder': (PurchaseOrder, PurchaseOrderSerializer),
            'salesorder': (SalesOrder, SalesOrderSerializer),
            'returnorder': (ReturnOrder, ReturnOrderSerializer),
            'transferorder': (TransferOrder, TransferOrderSerializer),
            'buildorder': (Build, BuildSerializer),
            'item': (StockItem, StockSerializers.StockItemSerializer),
            'stockitem': (StockItem, StockSerializers.StockItemSerializer),
        }

    def list(self, request, *args, **kwargs):
        """List all stock tracking entries."""
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
        else:
            serializer = self.get_serializer(queryset, many=True)

        data = serializer.data

        delta_models = self.get_delta_model_map()

        # Construct a set of related models we need to lookup for later
        related_model_lookups = {key: set() for key in delta_models}

        # Run a first pass through the data to determine which related models we need to lookup
        for item in data:
            deltas = item['deltas'] or {}

            for key in delta_models:
                if key in deltas:
                    related_model_lookups[key].add(deltas[key])

        for key in delta_models:
            model, serializer = delta_models[key]

            # Fetch all related models in one go
            related_models = model.objects.filter(pk__in=related_model_lookups[key])

            # Construct a mapping of pk -> serialized data
            related_data = {obj.pk: serializer(obj).data for obj in related_models}

            # Now, update the data with the serialized data
            for item in data:
                deltas = item['deltas'] or {}

                if key in deltas:
                    item['deltas'][f'{key}_detail'] = related_data.get(deltas[key])

        if page is not None:
            return self.get_paginated_response(data)

        return Response(data)

    filter_backends = SEARCH_ORDER_FILTER

    ordering = '-date'

    ordering_fields = ['date']

    search_fields = ['notes']


from stock.contract_api import (
    WmsChangeHistory,
    WmsContracts,
    WmsDetailChange,
    WmsLabelResolve,
    WmsLegacyAttribute,
    WmsProjects,
    WmsReceiptCancel,
    WmsReceiptChange,
    WmsReceipts,
)

from .wms import WmsImports, WmsInventory, WmsOptions, WmsParts, WmsRecords

stock_api_urls = [
    path(
        'wms/receipts/<int:pk>/',
        WmsReceiptChange.as_view(),
        name='api-wms-receipt-change',
    ),
    path('wms/contracts/', WmsContracts.as_view(), name='api-wms-contracts'),
    path('wms/projects/', WmsProjects.as_view(), name='api-wms-projects'),
    path('wms/receipts/', WmsReceipts.as_view(), name='api-wms-receipts'),
    path(
        'wms/receipts/<int:pk>/cancel/',
        WmsReceiptCancel.as_view(),
        name='api-wms-receipt-cancel',
    ),
    path(
        'wms/details/<int:pk>/', WmsDetailChange.as_view(), name='api-wms-detail-change'
    ),
    path(
        'wms/labels/resolve/', WmsLabelResolve.as_view(), name='api-wms-label-resolve'
    ),
    path(
        'wms/legacy/<int:pk>/',
        WmsLegacyAttribute.as_view(),
        name='api-wms-legacy-attribute',
    ),
    path('wms/changes/', WmsChangeHistory.as_view(), name='api-wms-changes'),
    path('wms/inventory/', WmsInventory.as_view(), name='api-wms-inventory'),
    path('wms/records/', WmsRecords.as_view(), name='api-wms-records'),
    path('wms/parts/', WmsParts.as_view(), name='api-wms-parts'),
    path('wms/options/', WmsOptions.as_view(), name='api-wms-options'),
    path('wms/imports/', WmsImports.as_view(), name='api-wms-imports'),
    path(
        'location/',
        include(
            [
                path('tree/', StockLocationTree.as_view(), name='api-location-tree'),
                # Stock location detail endpoints
                path(
                    '<int:pk>/',
                    include(
                        [
                            meta_path(StockLocation),
                            path(
                                '',
                                StockLocationDetail.as_view(),
                                name='api-location-detail',
                            ),
                        ]
                    ),
                ),
                path('', StockLocationList.as_view(), name='api-location-list'),
            ]
        ),
    ),
    # Stock location type endpoints
    path(
        'location-type/',
        include(
            [
                path(
                    '<int:pk>/',
                    include(
                        [
                            meta_path(StockLocationType),
                            path(
                                '',
                                StockLocationTypeDetail.as_view(),
                                name='api-location-type-detail',
                            ),
                        ]
                    ),
                ),
                path(
                    '', StockLocationTypeList.as_view(), name='api-location-type-list'
                ),
            ]
        ),
    ),
    # Endpoints for bulk stock adjustment actions
    path('count/', StockCount.as_view(), name='api-stock-count'),
    path('add/', StockAdd.as_view(), name='api-stock-add'),
    path('remove/', StockRemove.as_view(), name='api-stock-remove'),
    path('transfer/', StockTransfer.as_view(), name='api-stock-transfer'),
    path('return/', StockReturn.as_view(), name='api-stock-return'),
    path('assign/', StockAssign.as_view(), name='api-stock-assign'),
    path('merge/', StockMerge.as_view(), name='api-stock-merge'),
    path('change_status/', StockChangeStatus.as_view(), name='api-stock-change-status'),
    path('scan/inbound/', ScanInboundView.as_view(), name='api-stock-scan-inbound-v2'),
    path('scan/outbound/', ScanOutboundView.as_view(), name='api-stock-scan-outbound'),
    path('scan/events/', ScanStockEventView.as_view(), name='api-stock-scan-events'),
    path(
        'scan/events/<int:pk>/',
        ScanStockEventDetail.as_view(),
        name='api-stock-scan-event-detail',
    ),
    path(
        'scan/requests/',
        ScanStockApprovalRequestList.as_view(),
        name='api-stock-scan-request-list',
    ),
    path(
        'scan/requests/<int:pk>/',
        ScanStockApprovalRequestDetail.as_view(),
        name='api-stock-scan-request-detail',
    ),
    path(
        'scan/requests/<int:pk>/lines/<int:event_pk>/',
        ScanStockApprovalLineDetail.as_view(),
        name='api-stock-scan-request-line-detail',
    ),
    path(
        'scan/requests/<int:pk>/submit/',
        ScanStockApprovalSubmit.as_view(),
        name='api-stock-scan-request-submit',
    ),
    path(
        'scan/requests/<int:pk>/cancel/',
        ScanStockApprovalCancel.as_view(),
        name='api-stock-scan-request-cancel',
    ),
    path(
        'scan/requests/<int:pk>/approve/',
        ScanStockApprovalApprove.as_view(),
        name='api-stock-scan-request-approve',
    ),
    path(
        'scan/requests/<int:pk>/reject/',
        ScanStockApprovalReject.as_view(),
        name='api-stock-scan-request-reject',
    ),
    path('scan-inbound/', ScanInboundView.as_view(), name='api-stock-scan-inbound'),
    # StockItemTestResult API endpoints
    path(
        'test/',
        include(
            [
                path(
                    '<int:pk>/',
                    include(
                        [
                            meta_path(StockItemTestResult),
                            path(
                                '',
                                StockItemTestResultDetail.as_view(),
                                name='api-stock-test-result-detail',
                            ),
                        ]
                    ),
                ),
                path(
                    '',
                    StockItemTestResultList.as_view(),
                    name='api-stock-test-result-list',
                ),
            ]
        ),
    ),
    # StockItemTracking API endpoints
    path(
        'track/',
        include(
            [
                path(
                    '<int:pk>/',
                    StockTrackingDetail.as_view(),
                    name='api-stock-tracking-detail',
                ),
                # Stock tracking status code information
                path(
                    'status/',
                    StatusView.as_view(),
                    {StatusView.MODEL_REF: StockHistoryCode},
                    name='api-stock-tracking-status-codes',
                ),
                path('', StockTrackingList.as_view(), name='api-stock-tracking-list'),
            ]
        ),
    ),
    # Detail views for a single stock item
    path(
        '<int:pk>/',
        include(
            [
                path(
                    'convert/',
                    StockItemConvert.as_view(),
                    name='api-stock-item-convert',
                ),
                path(
                    'disassemble/',
                    StockItemDisassemble.as_view(),
                    name='api-stock-item-disassemble',
                ),
                path(
                    'install/',
                    StockItemInstall.as_view(),
                    name='api-stock-item-install',
                ),
                meta_path(StockItem),
                path(
                    'serialize/',
                    StockItemSerialize.as_view(),
                    name='api-stock-item-serialize',
                ),
                path(
                    'uninstall/',
                    StockItemUninstall.as_view(),
                    name='api-stock-item-uninstall',
                ),
                path(
                    'serial-numbers/',
                    StockItemSerialNumbers.as_view(),
                    name='api-stock-item-serial-numbers',
                ),
                path('', StockDetail.as_view(), name='api-stock-detail'),
            ]
        ),
    ),
    # Stock item status code information
    path(
        'status/',
        StatusView.as_view(),
        {StatusView.MODEL_REF: StockStatus},
        name='api-stock-status-codes',
    ),
    # Anything else
    path('', StockList.as_view(), name='api-stock-list'),
]
