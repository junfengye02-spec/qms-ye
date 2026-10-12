"""Enterprise WeChat notifications for scan stock workflows."""

import html
import os
import re
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

import requests
import structlog

import InvenTree.tasks

logger = structlog.get_logger('inventree')

WECOM_WEBHOOK_ENV = 'INVENTREE_WECOM_BOT_WEBHOOK_URL'
WECOM_MENTION_ENV = 'INVENTREE_WECOM_BOT_MENTION_USER_IDS'
MENTION_PATTERN = re.compile(r'^[A-Za-z0-9_.@-]+$')

APPROVAL_EVENT_META = {
    'submitted': ('库存申请待审批', 'warning'),
    'approved': ('库存申请审批通过', 'info'),
    'rejected': ('库存申请已驳回', 'warning'),
    'cancelled': ('库存申请已撤销', 'comment'),
    'expired': ('库存申请已超时', 'warning'),
}


def wecom_webhook_url() -> str:
    """Return a validated official Enterprise WeChat robot webhook URL."""
    value = os.getenv(WECOM_WEBHOOK_ENV, '').strip()
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != 'https'
        or parsed.hostname != 'qyapi.weixin.qq.com'
        or parsed.path != '/cgi-bin/webhook/send'
        or not query.get('key', [''])[0]
    ):
        logger.warning('Ignoring invalid Enterprise WeChat robot webhook URL')
        return ''
    return value


def queue_direct_inbound_notification(event_id: int) -> bool | str:
    """Queue one committed direct-inbound notification when configured."""
    if not wecom_webhook_url():
        return False
    return InvenTree.tasks.offload_task(
        'stock.notifications.send_direct_inbound_notification',
        event_id,
        group='notification',
        check_duplicates=False,
    )


def queue_approval_notification(request_id: int, event_name: str) -> bool | str:
    """Queue one committed approval-transition notification when configured."""
    if event_name not in APPROVAL_EVENT_META or not wecom_webhook_url():
        return False
    return InvenTree.tasks.offload_task(
        'stock.notifications.send_approval_notification',
        request_id,
        event_name,
        group='notification',
        check_duplicates=False,
    )


def send_direct_inbound_notification(event_id: int) -> bool:
    """Send a group notification for one successful non-approval inbound scan."""
    from stock.models import ScanStockEvent

    try:
        event = ScanStockEvent.objects.select_related(
            'part', 'stock_item', 'destination_location', 'operator'
        ).get(pk=event_id)
    except ScanStockEvent.DoesNotExist:
        return False
    if (
        event.operation != ScanStockEvent.Operation.INBOUND
        or event.outcome != ScanStockEvent.Outcome.SUCCESS
        or event.approval_request_id is not None
    ):
        return False
    return _post_markdown(_direct_inbound_markdown(event))


def send_approval_notification(request_id: int, event_name: str) -> bool:
    """Send a group notification for one committed approval transition."""
    from stock.models import ScanStockApprovalRequest

    if event_name not in APPROVAL_EVENT_META:
        return False
    try:
        request = ScanStockApprovalRequest.objects.select_related(
            'requester', 'decided_by', 'cancelled_by', 'location'
        ).get(pk=request_id)
    except ScanStockApprovalRequest.DoesNotExist:
        return False

    lines = _approval_lines(request, event_name)
    stats = lines.aggregate(quantity=Sum('requested_quantity'))
    content = _approval_markdown(
        request, event_name, line_count=lines.count(), quantity=stats['quantity'] or 0
    )
    return _post_markdown(content, mention=event_name == 'submitted')


def _approval_lines(request, event_name: str):
    """Return lines that belonged to the approval transition being reported."""
    from stock.models import ScanStockEvent

    lines = request.lines.all()
    if event_name == 'submitted':
        return lines.filter(outcome=ScanStockEvent.Outcome.PENDING)
    if event_name == 'approved':
        return lines.filter(outcome=ScanStockEvent.Outcome.SUCCESS)
    terminal_reason = {
        'rejected': 'REQUEST_REJECTED',
        'cancelled': 'REQUEST_CANCELLED',
        'expired': 'REQUEST_EXPIRED',
    }.get(event_name)
    return lines.filter(
        Q(outcome=ScanStockEvent.Outcome.SUCCESS) | Q(terminal_reason=terminal_reason)
    )


def _direct_inbound_markdown(event) -> str:
    """Render one direct-inbound message without trusting scanned text."""
    product = event.part_name_snapshot or event.part_ipn_snapshot or event.barcode
    operator = (
        event.operator_display_name_snapshot
        or event.operator_username_snapshot
        or '系统'
    )
    location = event.destination_location_path_snapshot or '-'
    content = [
        '### 入库完成',
        f'> 商品: <font color="info">{_safe(product)}</font>',
        f'> 商品编码: {_safe(event.part_ipn_snapshot or event.barcode)}',
        f'> 本次入库: **{_quantity(event.applied_quantity)}**',
        f'> 入库后库存: **{_quantity(event.stock_item_quantity_after)}**',
        f'> 目标库位: {_safe(location)}',
        f'> 操作人: {_safe(operator)}',
        f'> 时间: {_local_time(event.completed_at)}',
    ]
    return '\n'.join(content)


def _approval_markdown(request, event_name, *, line_count: int, quantity) -> str:
    """Render one approval transition message."""
    title, color = APPROVAL_EVENT_META[event_name]
    direction = '入库' if request.direction == 'INBOUND' else '出库'
    actor, note = _approval_actor_and_note(request, event_name)
    content = [
        f'### {title}',
        f'> 申请单: <font color="{color}">{_safe(request.reference)}</font>',
        f'> 类型: {direction}',
        f'> 申请人: {_safe(request.requester_display_name_snapshot or request.requester_username_snapshot or "-")}',
        f'> 库位: {_safe(request.location_path_snapshot)}',
        f'> 明细: **{line_count}** 行 / 数量 **{_quantity(quantity)}**',
        f'> 当前操作人: {_safe(actor)}',
    ]
    if note:
        content.append(f'> 备注: {_safe(note)}')
    event_time = (
        request.submitted_at
        if event_name == 'submitted'
        else request.decided_at or request.cancelled_at or request.updated_at
    )
    content.append(f'> 时间: {_local_time(event_time)}')
    if link := _absolute_url(request.get_absolute_url()):
        content.append(f'[打开申请单]({link})')
    return '\n'.join(content)


def _approval_actor_and_note(request, event_name: str) -> tuple[str, str]:
    """Return the actor and optional note for one approval transition."""
    if event_name == 'submitted':
        user = request.requester
        note = ''
    elif event_name in {'approved', 'rejected'}:
        user = request.decided_by
        note = request.decision_note
    elif event_name == 'cancelled':
        user = request.cancelled_by
        note = request.cancel_note
    else:
        return '系统', ''
    if user is None:
        return '-', note
    name = user.get_full_name().strip() or user.get_username()
    return name, note


def _post_markdown(content: str, *, mention: bool = False) -> bool:
    """Post a bounded Markdown message without exposing the webhook in logs."""
    webhook = wecom_webhook_url()
    if not webhook:
        return False
    if mention:
        mentions = [f'<@{user_id}>' for user_id in _mention_user_ids()]
        if mentions:
            content = f'{content}\n{" ".join(mentions)}'
    try:
        response = requests.post(
            webhook,
            json={'msgtype': 'markdown', 'markdown': {'content': content[:4096]}},
            timeout=(3.05, 5),
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            'Enterprise WeChat robot notification failed', error_type=type(exc).__name__
        )
        return False
    if not response.ok or data.get('errcode') != 0:
        logger.warning(
            'Enterprise WeChat robot rejected notification',
            status=response.status_code,
            errcode=data.get('errcode'),
            errmsg=data.get('errmsg'),
        )
        return False
    return True


def _mention_user_ids() -> list[str]:
    """Return validated fixed Enterprise WeChat user IDs from configuration."""
    return [
        value
        for raw in os.getenv(WECOM_MENTION_ENV, '').split(',')
        if (value := raw.strip()) and MENTION_PATTERN.fullmatch(value)
    ]


def _safe(value) -> str:
    """Escape untrusted database and barcode values for robot Markdown."""
    collapsed = re.sub(r'[\r\n\t]+', ' ', str(value or '-')).strip()
    return html.escape(collapsed, quote=True)


def _quantity(value) -> str:
    """Format a Decimal-like quantity without trailing zeroes."""
    if value is None:
        return '-'
    return format(value, 'f').rstrip('0').rstrip('.') or '0'


def _local_time(value) -> str:
    """Format a timestamp in the deployment time zone."""
    if value is None:
        return '-'
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _absolute_url(path: str) -> str:
    """Join a frontend path to the configured InvenTree site URL."""
    base = (getattr(settings, 'SITE_URL', None) or '').rstrip('/')
    if not base:
        return ''
    return f'{base}/{path.lstrip("/")}'
