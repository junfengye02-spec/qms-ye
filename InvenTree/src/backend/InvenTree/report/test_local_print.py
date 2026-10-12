"""Security, idempotency and PDF regression tests for local printing."""

import base64
import copy
import hashlib
import io
import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import include, path
from django.utils import timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from pypdf import PdfReader, PdfWriter
from rest_framework.test import APIClient

from common.models import DataOutput
from common.settings import set_global_setting
from part.models import Part
from plugin.registry import registry
from report import local_print as service
from report.models import LocalPrintJob
from stock.models import StockItem

BASE = '/api/label/local/'


@override_settings(LOCAL_PRINT_ENABLED=True)
class LocalPrintTests(TestCase):
    """Use real RSA signatures and database transitions, mock only task scheduling."""

    @classmethod
    def setUpClass(cls):
        """Create an ephemeral test-only signing certificate outside the source tree."""
        super().setUpClass()
        cls.temp = tempfile.TemporaryDirectory()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, 'Local print test only')]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(cls.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(timezone.now() - timedelta(days=1))
            .not_valid_after(timezone.now() + timedelta(days=1))
            .sign(cls.key, hashes.SHA256())
        )
        root = Path(cls.temp.name)
        (root / 'certificate.pem').write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
        )
        (root / 'private.pem').write_bytes(
            cls.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    @classmethod
    def tearDownClass(cls):
        """Discard generated credentials after testing."""
        cls.temp.cleanup()
        super().tearDownClass()

    def setUp(self):
        """Set up minimal authorized records and built-in business templates."""
        root = Path(self.temp.name)
        self.enterContext(
            override_settings(
                LOCAL_PRINT_CERTIFICATE=str(root / 'certificate.pem'),
                LOCAL_PRINT_PRIVATE_KEY=str(root / 'private.pem'),
            )
        )
        service.credentials()
        apps.get_app_config('report').create_default_labels()
        set_global_setting('LABEL_ENABLE', True)
        self.user = get_user_model().objects.create(
            username='local-printer', is_superuser=True, is_staff=True
        )
        self.other = get_user_model().objects.create(
            username='other-printer', is_superuser=True
        )
        self.part = Part.objects.create(
            name='Test local print', IPN='LOCAL-TEST-001', component=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.values = {
            'request_key': str(uuid.uuid4()),
            'model_type': 'part',
            'items': [self.part.pk],
            'copies': 1,
            'queue': '标签打印机',
        }

    def create(self, values=None):
        """Schedule once but control rendering explicitly inside each test."""
        with patch('report.local_print.enqueue_job') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    BASE + 'jobs/', values or self.values, format='json'
                )
        self.assertEqual(response.status_code, 200, response.data)
        return LocalPrintJob.objects.get(pk=response.data['id']), enqueue.call_count

    def ready(self):
        """Attach a small generated PDF to a real DataOutput."""
        job, _ = self.create()
        writer = PdfWriter()
        writer.add_blank_page(width=50 * 72 / 25.4, height=30 * 72 / 25.4)
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
        job.output.mark_complete(output=ContentFile(data, name='local-test.pdf'))
        job.file_sha256 = hashlib.sha256(data).hexdigest()
        job.state = 'ready'
        job.save()
        return job

    def message(self, job=None):
        """Match the QZ 2.2.5 wire payload, including Unicode queue names."""
        if job:
            params = {
                'printer': {'name': job.queue},
                'options': service.print_options(job),
                'data': [
                    {
                        'type': 'pixel',
                        'format': 'pdf',
                        'flavor': 'base64',
                        'data': base64.b64encode(service.read_pdf(job)).decode(),
                    }
                ],
            }
        else:
            params = {}
        return {
            'call': 'print' if job else 'printers.find',
            'params': params,
            'timestamp': int(timezone.now().timestamp() * 1000),
        }

    def sign(self, message, job=None):
        """Submit full JSON for the restricted signer."""
        body = {
            'message': json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        }
        if job:
            body['job'] = str(job.pk)
        return self.client.post(BASE + 'sign/', body, format='json')

    def test_create_idempotency_and_parameter_conflict(self):
        """Transport retries return the original job/output without rerendering."""
        job, calls = self.create()
        retry, retry_calls = self.create()
        self.assertEqual((job.pk, calls, retry_calls), (retry.pk, 1, 0))
        self.assertEqual(DataOutput.objects.filter(pk=job.output_id).count(), 1)
        response = self.client.post(
            BASE + 'jobs/', {**self.values, 'copies': 2}, format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LocalPrintJob.objects.count(), 1)

    def test_cross_account_and_login_required(self):
        """Even administrators cannot retrieve or sign another operator's job."""
        job = self.ready()
        self.client.force_authenticate(self.other)
        for endpoint in (f'jobs/{job.pk}/', f'jobs/{job.pk}/pdf/'):
            self.assertEqual(self.client.get(BASE + endpoint).status_code, 404)
        self.assertEqual(self.sign(self.message(job), job).status_code, 404)
        self.assertEqual(self.client.get(BASE + 'jobs/').data, [])
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(BASE + 'certificate/').status_code, (401, 403))

    def test_legacy_output_endpoint_cannot_bypass_task_permissions(self):
        """The staff-wide legacy endpoint must not reveal local print PDF URLs."""
        job = self.ready()
        self.assertEqual(
            self.client.get(f'/api/data-output/{job.output_id}/').status_code, 404
        )
        self.assertEqual(self.client.get(BASE + f'jobs/{job.pk}/pdf/').status_code, 200)

    def test_model_permission_and_revocation(self):
        """Current model permission is required at creation and signing time."""
        job = self.ready()
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.sign(self.message(job), job).status_code, 403)
        response = self.client.post(
            BASE + 'jobs/',
            {**self.values, 'request_key': str(uuid.uuid4())},
            format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_session_requests_require_csrf(self):
        """A third-party page cannot create tasks using a logged-in browser cookie."""
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(BASE + 'jobs/', self.values, format='json').status_code, 403
        )

    def test_invalid_items_copies_and_ipn(self):
        """No duplicates, absent objects, blank IPN or excessive page expansion."""
        for changes in (
            {'items': [self.part.pk, self.part.pk]},
            {'items': [99999999]},
            {'copies': 501},
            {'copies': 0},
            {'copies': True},
            {'model_type': 'stocklocation'},
        ):
            with self.subTest(changes=changes):
                response = self.client.post(
                    BASE + 'jobs/', {**self.values, **changes}, format='json'
                )
                self.assertEqual(response.status_code, 400)
        self.part.IPN = ''
        self.part.save()
        self.assertEqual(
            self.client.post(BASE + 'jobs/', self.values, format='json').status_code,
            400,
        )
        self.assertEqual(LocalPrintJob.objects.count(), 0)

    def test_per_item_counts_validation_idempotency_and_reprint(self):
        """Reject malformed counts and preserve each count on retries and reprints."""
        other = Part.objects.create(name='Second label', IPN='SECOND-LABEL')
        values = {
            **self.values,
            'items': [self.part.pk, other.pk],
            'item_copies': {str(self.part.pk): 2, str(other.pk): 5},
        }
        for changes in (
            {'item_copies': {}},
            {'item_copies': {str(self.part.pk): 2}},
            {'item_copies': {**values['item_copies'], '99999': 1}},
            {'item_copies': {str(self.part.pk): 0, str(other.pk): 5}},
            {'item_copies': {str(self.part.pk): True, str(other.pk): 5}},
            {'item_copies': {str(self.part.pk): 2.5, str(other.pk): 5}},
            {'item_copies': {str(self.part.pk): 500, str(other.pk): 1}},
            {'copies': 2},
        ):
            with self.subTest(changes=changes):
                response = self.client.post(
                    BASE + 'jobs/', {**values, **changes}, format='json'
                )
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(LocalPrintJob.objects.count(), 0)
        job, calls = self.create(values)
        retry, retry_calls = self.create(values)
        self.assertEqual((calls, retry_calls), (1, 0))
        self.assertEqual(job.pk, retry.pk)
        self.assertEqual(self.client.get(BASE + f'jobs/{job.pk}/').data['total'], 7)
        changed = {**values, 'item_copies': {str(self.part.pk): 3, str(other.pk): 5}}
        self.assertEqual(
            self.client.post(BASE + 'jobs/', changed, format='json').status_code, 400
        )
        job.state = 'failed'
        job.save(update_fields=['state'])
        changed['request_key'] = str(uuid.uuid4())
        self.assertEqual(
            self.client.post(
                BASE + f'jobs/{job.pk}/reprint/', changed, format='json'
            ).status_code,
            400,
        )
        with patch('report.local_print.enqueue_job'):
            response = self.client.post(
                BASE + f'jobs/{job.pk}/reprint/',
                {**values, 'request_key': str(uuid.uuid4())},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['item_copies'], values['item_copies'])
        self.assertEqual(response.data['total'], 7)

    def test_sign_enumeration_and_crypto_contract(self):
        """QZ verifies an RSA SHA512 signature over the message's SHA256 hex digest."""
        message = self.message()
        response = self.sign(message)
        self.assertEqual(response.status_code, 200, response.data)
        digest = (
            hashlib.sha256(
                json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()
            )
            .hexdigest()
            .encode()
        )
        self.key.public_key().verify(
            base64.b64decode(response.data['signature']),
            digest,
            padding.PKCS1v15(),
            hashes.SHA512(),
        )

    def test_single_claim_and_explicit_reprint(self):
        """A second tab cannot sign again; only a linked new task can print again."""
        job = self.ready()
        self.assertEqual(self.sign(self.message(job), job).status_code, 200)
        self.assertEqual(self.sign(self.message(job), job).status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.state, 'submitting')
        result = self.client.post(
            BASE + f'jobs/{job.pk}/result/', {'state': 'submitted'}, format='json'
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['state'], 'submitted')
        self.client.post(
            BASE + f'jobs/{job.pk}/result/', {'state': 'uncertain'}, format='json'
        )
        job.refresh_from_db()
        self.assertEqual(job.state, 'submitted')
        with patch('report.local_print.enqueue_job'):
            response = self.client.post(
                BASE + f'jobs/{job.pk}/reprint/',
                {**self.values, 'request_key': str(uuid.uuid4())},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(str(response.data['original']), str(job.pk))
        self.assertNotEqual(response.data['id'], str(job.pk))

    def test_restrict_operations_and_all_print_parameters(self):
        """No arbitrary QZ functionality or substituted file/queue/paper is signed."""
        job = self.ready()
        for call in (
            'file.read',
            'serial.openPort',
            'socket.open',
            'printers.clearQueue',
            'printers.getDefault',
        ):
            self.assertEqual(
                self.sign({**self.message(), 'call': call}).status_code, 403
            )
        original = self.message(job)
        mutations = [
            ('printer', {'host': 'attacker', 'port': 9100}),
            ('printer', {'name': 'another printer'}),
            ('options', {**service.print_options(job), 'copies': 3}),
            ('options', {**service.print_options(job), 'scaleContent': True}),
            ('options', {**service.print_options(job), 'forceRaw': True}),
            (
                'data',
                [
                    {
                        'type': 'pixel',
                        'format': 'pdf',
                        'flavor': 'file',
                        'data': 'https://example.com/file.pdf',
                    }
                ],
            ),
            (
                'data',
                [
                    {
                        'type': 'pixel',
                        'format': 'pdf',
                        'flavor': 'base64',
                        'data': 'ZmFrZQ==',
                    }
                ],
            ),
        ]
        for key, value in mutations:
            message = copy.deepcopy(original)
            message['params'][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.sign(message, job).status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.state, 'ready')

    def test_expired_signatures_jobs_and_ambiguous_json(self):
        """Expiration and malformed envelopes fail closed without consuming a job."""
        job = self.ready()
        message = self.message(job)
        message['timestamp'] -= 31000
        self.assertEqual(self.sign(message, job).status_code, 400)
        self.assertEqual(
            self.client.post(
                BASE + 'sign/',
                {'message': '{"call":"print","call":"printers.find"}'},
                format='json',
            ).status_code,
            400,
        )
        LocalPrintJob.objects.filter(pk=job.pk).update(
            expires=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.sign(self.message(job), job).status_code, 400)

    def test_unknown_results_never_become_ready(self):
        """A crashed browser retains an auditable claim and requires explicit reprint."""
        job = self.ready()
        self.assertEqual(self.sign(self.message(job), job).status_code, 200)
        LocalPrintJob.objects.filter(pk=job.pk).update(
            claimed=timezone.now() - timedelta(seconds=61)
        )
        response = self.client.get(BASE + f'jobs/{job.pk}/')
        self.assertEqual(response.data['state'], 'uncertain')
        self.assertEqual(self.sign(self.message(job), job).status_code, 400)
        self.assertEqual(
            self.client.post(
                BASE + f'jobs/{job.pk}/result/', {'state': 'ready'}, format='json'
            ).status_code,
            400,
        )

    def test_pdf_digest_and_feature_rollback(self):
        """Tampering fails; disabling stops sends but preserves authenticated downloads."""
        job = self.ready()
        with override_settings(LOCAL_PRINT_ENABLED=False):
            self.assertEqual(self.sign(self.message(job), job).status_code, 403)
            self.assertEqual(
                self.client.get(BASE + f'jobs/{job.pk}/pdf/').status_code, 200
            )
            self.assertFalse(self.client.get(BASE + 'config/').data['enabled'])
        LocalPrintJob.objects.filter(pk=job.pk).update(file_sha256='0' * 64)
        self.assertEqual(self.client.get(BASE + f'jobs/{job.pk}/pdf/').status_code, 400)

    def test_generation_failure_preserves_job_without_sending(self):
        """Renderer exceptions remain auditable even when DataOutput is deleted."""
        job, _ = self.create()

        def fail(*args, **kwargs):
            job.output.delete()
            raise ValueError('Simulated rendering failure')

        with (
            patch('report.models.LabelTemplate.print', side_effect=fail),
            self.assertLogs('report.local_print', level='ERROR'),
        ):
            service.generate_job(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.assertIsNone(job.output_id)
        self.assertIsNone(job.claimed)

    def test_no_signature_without_valid_credentials(self):
        """Missing keys do not silently fall back to unsigned demo calls."""
        with override_settings(LOCAL_PRINT_PRIVATE_KEY='/does-not-exist'):
            self.assertEqual(self.sign(self.message()).status_code, 400)
            self.assertEqual(self.client.get(BASE + 'certificate/').status_code, 400)

    def test_real_renderer_expands_copies_once(self):
        """Exercise the production renderer for 1, 3 and 6 pages plus stock labels."""
        # Standalone test runners do not load the built-in barcode plugins at startup.
        registry.reload_plugins(full_reload=True, force_reload=True, collect=True)
        parts = [self.part] + [
            Part.objects.create(name=f'Batch {n}', IPN=f'BATCH-{n}') for n in (2, 3)
        ]
        stock = StockItem.objects.create(part=self.part, quantity=1)
        cases = [
            ('part', [self.part.pk], 1, 1),
            ('part', [self.part.pk], 3, 3),
            ('part', [p.pk for p in parts], 2, 6),
            ('stockitem', [stock.pk], 1, 1),
        ]
        for model_type, items, copies, pages in cases:
            with self.subTest(model=model_type, pages=pages):
                job, _ = self.create(
                    {
                        **self.values,
                        'request_key': str(uuid.uuid4()),
                        'model_type': model_type,
                        'items': items,
                        'copies': copies,
                    }
                )
                service.generate_job(str(job.pk))
                job.refresh_from_db()
                self.assertEqual(job.state, 'ready', job.detail)
                self.assertEqual(
                    len(PdfReader(io.BytesIO(service.read_pdf(job))).pages), pages
                )
                self.assertEqual(service.print_options(job)['copies'], 1)

    def test_local_api_schema(self):
        """Validate the added API surface without modifying a running database."""
        schema = SchemaGenerator(
            patterns=[path('api/label/local/', include('report.local_print_api'))]
        ).get_schema(request=None, public=True)
        validate_schema(schema)
        self.assertEqual(len(schema['paths']), 8)
        self.assertIn('/api/label/local/sign/', schema['paths'])
