"""Business date arithmetic and the versioned, database-verified WMS label protocol."""

import calendar
import json
import re
import uuid
from datetime import date
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone

PREFIX = 'WMS1:'
MAX_CODE_LENGTH = 2000


def warranty_end(accepted, months):
    """Add calendar months, clamping the day to the target month's last day."""
    if accepted is None or months is None:
        return None
    index = accepted.year * 12 + accepted.month - 1 + months
    year, month = divmod(index, 12)
    if year > 9999:
        raise ValidationError('Warranty end date is outside the supported date range')
    return date(
        year, month + 1, min(accepted.day, calendar.monthrange(year, month + 1)[1])
    )


def label_payload(detail):
    """Encode readable identifiers in JSON without ambiguous field delimiters."""
    return {
        'type': 'receipt-detail',
        'protocol': 1,
        'id': str(detail.uid),
        'version': detail.label_version,
        'equipment': detail.part.IPN or detail.part.barcode,
        'project': detail.project,
        'contract': detail.contract.number,
    }


def encode_label(detail):
    """Keep QR data short and alphanumeric; business fields come from the server."""
    return f'{PREFIX}{detail.uid.hex.upper()}:{detail.label_version}'


def parse_label(value):
    """Validate protocol shape before looking up a server-owned receipt detail."""
    if (
        not isinstance(value, str)
        or not value.startswith(PREFIX)
        or len(value) > MAX_CODE_LENGTH
    ):
        raise ValidationError('Invalid complete label protocol')
    try:
        raw = value[len(PREFIX) :]
        if not raw.startswith('{'):
            match = re.fullmatch(r'([0-9A-F]{32}):([1-9][0-9]{0,9})', raw)
            if not match:
                raise ValueError
            return {'id': str(uuid.UUID(hex=match[1])), 'version': int(match[2])}
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {
            'type',
            'protocol',
            'id',
            'version',
            'equipment',
            'project',
            'contract',
        }:
            raise ValueError
        if (
            data['type'] != 'receipt-detail'
            or type(data['protocol']) is not int
            or data['protocol'] != 1
        ):
            raise ValueError
        if type(data['version']) is not int or data['version'] < 1:
            raise ValueError
        if not isinstance(data['id'], str):
            raise ValueError
        uuid.UUID(data['id'])
        for field in ('equipment', 'project', 'contract'):
            if (
                not isinstance(data[field], str)
                or not data[field].strip()
                or len(data[field]) > 100
            ):
                raise ValueError
        return data
    except (ValueError, TypeError, KeyError) as error:
        raise ValidationError('Invalid complete label fields') from error


def label_matches(detail, data):
    """Verify every encoded field, including fields on already printed JSON labels."""
    current = label_payload(detail)
    return all(current[key] == value for key, value in data.items())


def resolve_label(value, *, lock=False):
    """Reject cancelled, stale or tampered codes using current database values."""
    from stock.models import WmsReceiptDetail

    data = parse_label(value)
    query = WmsReceiptDetail.objects
    if lock:
        query = query.select_for_update()
    try:
        detail = query.get(uid=data['id'])
    except WmsReceiptDetail.DoesNotExist as error:
        raise ValidationError('Receipt detail does not exist') from error
    if detail.receipt.cancelled:
        raise ValidationError('Receipt was cancelled; this label cannot be used')
    if not label_matches(detail, data):
        raise ValidationError(
            'Label is outdated or inconsistent; refresh and replace the label'
        )
    return detail


def warranty_values(detail):
    """Return current effective information, including explicit inheritance flags."""
    if detail is None:
        return {
            'source_detail': None,
            'detail_uid': None,
            'contract_number': '',
            'acceptance_date': None,
            'warranty_months': None,
            'warranty_end': None,
            'warranty_status': 'UNFILLED',
        }
    accepted = detail.effective_acceptance_date
    months = detail.effective_warranty_months
    end = warranty_end(accepted, months)
    business_date = timezone.now().astimezone(ZoneInfo('Asia/Shanghai')).date()

    return {
        'source_detail': detail.pk,
        'detail_uid': str(detail.uid),
        'contract_number': detail.contract.number,
        'acceptance_date': accepted.isoformat() if accepted else None,
        'warranty_months': months,
        'warranty_end': end.isoformat() if end else None,
        'warranty_status': 'AWAITING_ACCEPTANCE'
        if not accepted
        else ('IN_WARRANTY' if end >= business_date else 'EXPIRED'),
    }
