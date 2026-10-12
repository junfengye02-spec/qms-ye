"""Real database races for duplicate creation and printing from two tabs."""

import base64
import hashlib
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from cryptography.hazmat.primitives.asymmetric import rsa
from pypdf import PdfWriter
from rest_framework.exceptions import ValidationError

from common.models import DataOutput
from common.settings import set_global_setting
from part.models import Part
from report import local_print as service
from report.models import LocalPrintJob


@skipUnlessDBFeature('has_select_for_update')
@override_settings(LOCAL_PRINT_ENABLED=True)
class LocalPrintConcurrencyTests(TransactionTestCase):
    """Separate connections must produce one job and at most one print signature."""

    # Keep post-flush ContentType IDs compatible with the project's fixed-ID
    # fixtures when the PostgreSQL test database is reused with --keepdb.
    reset_sequences = True

    def setUp(self):
        """Create committed records visible to both test connections."""
        apps.get_app_config('report').create_default_labels()
        set_global_setting('LABEL_ENABLE', True)
        self.user = get_user_model().objects.create(
            username='concurrent-printer', is_superuser=True
        )
        part = Part.objects.create(name='Concurrent label', IPN='CONCURRENT-001')
        self.values = {
            'request_key': uuid.uuid4(),
            'model_type': 'part',
            'items': [part.pk],
            'copies': 1,
            'queue': 'Local test queue',
        }

    def race(self, function):
        """Start two independent DB transactions at the same barrier."""
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(worker) for _ in range(2)]
            return [result.result(timeout=20) for result in results]

    def test_create_and_claim_races(self):
        """Both tabs recover one task, but only one can authorize its device send."""
        with patch('report.local_print.enqueue_job') as enqueue:
            ids = self.race(lambda: service.create_job(self.user, self.values).pk)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(LocalPrintJob.objects.count(), 1)
        self.assertEqual(DataOutput.objects.count(), 1)
        job = LocalPrintJob.objects.get(pk=ids[0])
        writer = PdfWriter()
        writer.add_blank_page(width=50 * 72 / 25.4, height=30 * 72 / 25.4)
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
        job.output.mark_complete(output=ContentFile(data, name='race.pdf'))
        job.state = 'ready'
        job.file_sha256 = hashlib.sha256(data).hexdigest()
        job.save()
        message = json.dumps(
            {
                'call': 'print',
                'params': {
                    'printer': {'name': job.queue},
                    'options': service.print_options(job),
                    'data': [
                        {
                            'type': 'pixel',
                            'format': 'pdf',
                            'flavor': 'base64',
                            'data': base64.b64encode(data).decode(),
                        }
                    ],
                },
                'timestamp': int(timezone.now().timestamp() * 1000),
            }
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        def sign():
            try:
                service.sign_message(self.user, message, job)
                return 'signed'
            except ValidationError:
                return 'rejected'

        with patch(
            'report.local_print.credentials', return_value=('test certificate', key)
        ):
            results = self.race(sign)
        self.assertCountEqual(results, ['signed', 'rejected'])
        job.refresh_from_db()
        self.assertEqual(job.state, 'submitting')
