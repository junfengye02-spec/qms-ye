"""Restricted QZ signing and durable local print submission orchestration."""

import base64
import hashlib
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pypdf import PdfReader
from rest_framework.exceptions import PermissionDenied, ValidationError

from common.models import DataOutput
from common.settings import get_global_setting
from InvenTree.tasks import offload_task
from report.local_print_templates import resolve_local_print_template
from report.models import MAX_LABELS_PER_PRINT, LocalPrintJob
from users.permissions import check_user_permission

MAX_PDF_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


def require_enabled():
    """Disabling the feature also stops signing outstanding jobs."""
    if not settings.LOCAL_PRINT_ENABLED or not get_global_setting('LABEL_ENABLE'):
        raise PermissionDenied('Local printing is disabled')


def validate_items(user, model_type, item_ids, copies, item_copies=None):
    """Reuse label validation and existing model view permissions."""
    try:
        if item_copies:
            if (
                copies != 1
                or set(item_copies) != {str(pk) for pk in item_ids}
                or any(
                    type(count) is not int or count < 1
                    for count in item_copies.values()
                )
                or sum(item_copies.values()) > MAX_LABELS_PER_PRINT
            ):
                raise ValidationError('Invalid per-item print counts')
        template = resolve_local_print_template(model_type)
        model = template.get_model()
        from stock.models import StockItem

        permission_model = StockItem if model_type == 'wmsreceiptdetail' else model
        if not check_user_permission(user, permission_model, 'view') or (
            model_type == 'wmsreceiptdetail'
            and not check_user_permission(user, StockItem, 'change')
        ):
            raise PermissionDenied('You cannot print this model type')
        items = list(
            model.objects.filter(pk__in=item_ids).filter(**template.get_filters())
        )
        if len(items) != len(item_ids) or len(set(item_ids)) != len(item_ids):
            raise ValidationError('Items must exist, be unique and match the template')
        if model_type == 'wmsreceiptdetail':
            for item in items:
                if item.receipt.cancelled:
                    raise ValidationError('Cancelled preparation cannot be printed')
        template.validate_print_request(items, copies=copies)
        return template
    except DjangoValidationError as error:
        raise ValidationError(error.messages) from error


def job_access(user, job):
    """Recheck ownership and current object permissions before every operation."""
    if job.user_id != user.pk:
        raise PermissionDenied('This print job belongs to another user')
    template = validate_items(
        user, job.model_type, job.items, job.copies, job.item_copies
    )
    if job.model_type == 'wmsreceiptdetail':
        from stock.models import WmsReceiptDetail
        from stock.warranty import label_matches, parse_label

        details = list(WmsReceiptDetail.objects.filter(pk__in=job.items))
        if set(job.item_versions) != {str(d.pk) for d in details} or any(
            not label_matches(d, parse_label(job.item_versions[str(d.pk)]))
            for d in details
        ):
            raise ValidationError('Label attribution changed; create a new print task')
    return template


def create_job(user, values, original=None):
    """Return an identical retry or create a new PDF output exactly once."""
    require_enabled()
    template = validate_items(
        user,
        values['model_type'],
        values['items'],
        values['copies'],
        values.get('item_copies'),
    )
    payload = {
        **values,
        'request_key': str(values['request_key']),
        'original': str(original.pk) if original else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    with transaction.atomic():
        job, created = LocalPrintJob.objects.get_or_create(
            user=user,
            request_key=values['request_key'],
            defaults={
                'request_hash': fingerprint,
                'model_type': values['model_type'],
                'items': values['items'],
                'copies': values['copies'],
                'item_copies': values.get('item_copies', {}),
                'queue': values['queue'],
                'template': template,
                'template_name': template.name,
                'expires': timezone.now() + timedelta(minutes=15),
                'original': original,
            },
        )
        if job.request_hash != fingerprint:
            raise ValidationError(
                'This request key was already used for different parameters'
            )
        if created:
            if job.model_type == 'wmsreceiptdetail':
                from stock.contract_api import audit, lock_parts
                from stock.models import WmsReceiptDetail

                details = list(WmsReceiptDetail.objects.filter(pk__in=job.items))
                lock_parts([d.part_id for d in details])
                details = list(WmsReceiptDetail.objects.filter(pk__in=job.items))
                if any(d.receipt.cancelled for d in details):
                    raise ValidationError('Cancelled preparation cannot be printed')
                job.item_versions = {str(d.pk): d.complete_barcode for d in details}
                job.save(update_fields=['item_versions'])
                for detail in details:
                    audit(
                        user,
                        'label_reprint' if original else 'label_print',
                        detail=detail,
                        after={
                            'job': str(job.pk),
                            'copies': job.copies_for(detail.pk),
                            'version': detail.label_version,
                        },
                        reason='Local print replacement'
                        if original
                        else 'Local print request',
                    )
            job.output = DataOutput.objects.create(
                user=user,
                total=job.total_labels,
                output_type=DataOutput.DataOutputTypes.LABEL,
                template_name=template.name,
                plugin='inventreelabel',
            )
            job.save(update_fields=['output'])
            transaction.on_commit(lambda: enqueue_job(str(job.pk)))
    return job


def enqueue_job(job_id):
    """Record broker failures rather than leaving a retry to create another job."""
    try:
        offload_task(generate_job, job_id)
    except Exception:
        logger.exception('Could not schedule local print job %s', job_id)
        LocalPrintJob.objects.filter(pk=job_id, state='generating').update(
            state='failed',
            detail='Could not schedule PDF generation',
            updated=timezone.now(),
        )


def generate_job(job_id):
    """Use the existing label renderer and a fresh PDF plugin instance."""
    from plugin.builtin.labels.inventree_label import InvenTreeLabelPlugin

    job = LocalPrintJob.objects.get(pk=job_id)
    # A queue redelivery must not concurrently render into the same DataOutput.
    with transaction.atomic():
        job = LocalPrintJob.objects.select_for_update().get(pk=job_id)
        if job.state != 'generating' or job.detail == 'rendering':
            return
        job.detail = 'rendering'
        job.save(update_fields=['detail'])
    try:
        template = job_access(job.user, job)
        items = {
            item.pk: item
            for item in template.get_model().objects.filter(pk__in=job.items)
        }
        # Instance isolation avoids sharing the plugin's mutable PDF buffer across jobs.
        plugin = InvenTreeLabelPlugin()
        template.print(
            [items[pk] for pk in job.items for _ in range(job.copies_for(pk))],
            plugin,
            output=job.output,
            user=job.user,
            copies=1,
            options={},
        )
        job.refresh_from_db()
        data = read_pdf(job)
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) != job.total_labels:
            raise ValueError('Unexpected PDF page count')
        for page in reader.pages:
            if (
                abs(float(page.mediabox.width) * 25.4 / 72 - 50) > 0.2
                or abs(float(page.mediabox.height) * 25.4 / 72 - 30) > 0.2
            ):
                raise ValueError('Unexpected PDF page size')
        LocalPrintJob.objects.filter(pk=job.pk, state='generating').update(
            state='ready',
            file_sha256=hashlib.sha256(data).hexdigest(),
            detail='',
            updated=timezone.now(),
        )
    except Exception:
        logger.exception('Could not render local print job %s', job_id)
        LocalPrintJob.objects.filter(pk=job.pk, state='generating').update(
            state='failed',
            detail='PDF generation failed; check template, IPN and worker logs',
            updated=timezone.now(),
        )


def read_pdf(job):
    """Read only the server-owned output; never fetch a client-supplied URL."""
    if (
        not job.output
        or not job.output.complete
        or job.output.errors
        or not job.output.output
    ):
        raise ValidationError('PDF is not available')
    try:
        with job.output.output.open('rb') as source:
            data = source.read(MAX_PDF_BYTES + 1)
    except OSError as error:
        raise ValidationError('PDF has expired or is missing') from error
    if len(data) > MAX_PDF_BYTES or not data.startswith(b'%PDF-'):
        raise ValidationError('Invalid or oversized PDF')
    if job.file_sha256 and hashlib.sha256(data).hexdigest() != job.file_sha256:
        raise ValidationError('PDF digest changed')
    return data


def refresh_job(job):
    """Timeouts never make a signed attempt eligible for another send."""
    now = timezone.now()
    if (
        job.state == 'submitting'
        and job.claimed
        and job.claimed < now - timedelta(seconds=60)
    ):
        LocalPrintJob.objects.filter(pk=job.pk, state='submitting').update(
            state='uncertain',
            detail='Submission result is unknown; inspect the system queue',
            updated=now,
        )
    elif job.state in ('generating', 'ready') and job.expires < now:
        LocalPrintJob.objects.filter(
            pk=job.pk, state__in=['generating', 'ready']
        ).update(state='failed', detail='Task expired before submission', updated=now)
    job.refresh_from_db()
    return job


def print_options(job):
    """Exact QZ 2.2.5 SDK options: fixed paper, actual size, copies already expanded."""
    return {
        'bounds': None,
        'colorType': 'color',
        'copies': 1,
        'density': 0,
        'duplex': False,
        'fallbackDensity': None,
        'interpolation': 'bicubic',
        'jobName': f'WMS-{job.pk}',
        'legacy': False,
        'margins': 0,
        'orientation': None,
        'paperThickness': None,
        'printerTray': None,
        'rasterize': False,
        'rotation': 0,
        'scaleContent': False,
        'size': {'width': 50, 'height': 30},
        'units': 'mm',
        'forceRaw': False,
        'encoding': None,
        'spool': None,
    }


def credentials():
    """Load only configured secret files and check certificate/key consistency."""
    try:
        certificate = Path(settings.LOCAL_PRINT_CERTIFICATE).read_bytes()
        cert = x509.load_pem_x509_certificate(certificate)
        key = serialization.load_pem_private_key(
            Path(settings.LOCAL_PRINT_PRIVATE_KEY).read_bytes(), password=None
        )
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError('RSA key required')
        if key.public_key().public_numbers() != cert.public_key().public_numbers():
            raise ValueError('Certificate mismatch')
        now = datetime.now(UTC)
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise ValueError('Certificate expired')
        return certificate.decode(), key
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValidationError(
            'Local print signing certificate is not configured or valid'
        ) from error


def unique_keys(pairs):
    """Reject duplicate JSON keys instead of signing ambiguous payloads."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def sign_message(user, message, job=None):
    """Sign only enumeration or one task-bound print; signing atomically claims it."""
    require_enabled()
    try:
        payload = json.loads(message, object_pairs_hook=unique_keys)
    except (ValueError, TypeError) as error:
        raise ValidationError('Invalid signing payload') from error
    if not isinstance(payload, dict) or set(payload) != {'call', 'params', 'timestamp'}:
        raise ValidationError('Invalid signing envelope')
    timestamp = payload['timestamp']
    if (
        type(timestamp) is not int
        or abs(timezone.now().timestamp() * 1000 - timestamp) > 30000
    ):
        raise ValidationError('Signing timestamp expired')
    _, key = credentials()
    with transaction.atomic():
        if (
            payload['call'] == 'printers.find'
            and payload['params'] == {}
            and job is None
        ):
            from part.models import Part
            from stock.models import StockItem

            if not any(
                check_user_permission(user, model, 'view')
                for model in (Part, StockItem)
            ):
                raise PermissionDenied('No label printing permission')
        elif payload['call'] == 'print' and job is not None:
            job = LocalPrintJob.objects.select_for_update().get(pk=job.pk)
            job_access(user, job)
            if job.state != 'ready' or job.expires <= timezone.now():
                raise ValidationError(
                    'Task cannot be submitted again; query it or explicitly reprint'
                )
            expected = {
                'printer': {'name': job.queue},
                'options': print_options(job),
                'data': [
                    {
                        'type': 'pixel',
                        'format': 'pdf',
                        'flavor': 'base64',
                        'data': base64.b64encode(read_pdf(job)).decode(),
                    }
                ],
            }
            # JSON comparison also distinguishes true from 1 and rejects extra keys.
            if json.dumps(payload['params'], sort_keys=True) != json.dumps(
                expected, sort_keys=True
            ):
                raise ValidationError(
                    'Print parameters do not match the authorized task'
                )
            claimed = LocalPrintJob.objects.filter(pk=job.pk, state='ready').update(
                state='submitting', claimed=timezone.now(), updated=timezone.now()
            )
            if claimed != 1:
                raise ValidationError('Task already claimed')
        else:
            raise PermissionDenied('This QZ operation is not allowed')
        # QZ signs the lowercase SHA256 hex digest of the exact UTF-8 JSON string.
        digest = hashlib.sha256(message.encode('utf-8')).hexdigest().encode('ascii')
        signature = key.sign(digest, padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode()
