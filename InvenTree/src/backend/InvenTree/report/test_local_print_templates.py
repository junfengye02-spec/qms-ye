"""Regression coverage for reversible local print template configuration."""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from report.local_print_templates import resolve_local_print_template
from report.models import PRODUCT_IPN_LABEL_NAME, LabelTemplate


class LocalPrintTemplateTests(TestCase):
    """Keep unrelated labels intact and reject ambiguous configuration."""

    def setUp(self):
        """Create the shipped labels using the existing registration path."""
        apps.get_app_config('report').create_default_labels()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'labels.json'

    def run_command(self, **kwargs):
        """Capture command output without noise in the test runner."""
        call_command('configure_local_print_templates', stdout=StringIO(), **kwargs)

    def flags(self):
        """Snapshot enabled flags for exact rollback comparisons."""
        return dict(LabelTemplate.objects.values_list('pk', 'enabled'))

    def test_preview_apply_repeat_and_restore(self):
        """Preview is read-only; repeated apply and rollback preserve other labels."""
        part = resolve_local_print_template('part')
        LabelTemplate.objects.filter(pk=part.pk).update(enabled=False)
        custom = LabelTemplate.objects.create(
            name='My Product IPN shipping label',
            model_type='part',
            template=ContentFile(b'custom', name='custom.html'),
        )
        before = self.flags()
        self.run_command()
        self.assertEqual(before, self.flags())
        self.run_command(apply=True, backup=self.backup)
        self.assertTrue(resolve_local_print_template('part').enabled)
        self.assertTrue(self.flags()[custom.pk])
        for template in LabelTemplate.objects.filter(model_type='stocklocation'):
            self.assertEqual(template.enabled, before[template.pk])
        self.assertFalse(
            LabelTemplate.objects.get(
                name='InvenTree Part Label', model_type='part'
            ).enabled
        )
        after = self.flags()
        self.run_command(apply=True, backup=Path(self.temp.name) / 'second.json')
        self.assertEqual(after, self.flags())
        self.run_command(apply=True, restore=self.backup)
        self.assertEqual(before, self.flags())
        self.run_command(apply=True, restore=self.backup)
        self.assertEqual(before, self.flags())

    def test_missing_or_disabled(self):
        """A runtime resolver never silently selects a disabled or missing label."""
        template = resolve_local_print_template('part')
        LabelTemplate.objects.filter(pk=template.pk).update(enabled=False)
        with self.assertRaisesMessage(ValidationError, 'disabled'):
            resolve_local_print_template('part')
        template.delete()
        with self.assertRaisesMessage(CommandError, 'found 0'):
            self.run_command(apply=True, backup=self.backup)
        self.assertFalse(self.backup.exists())

    def test_ambiguous_template(self):
        """An IPN template copied under a different name requires manual resolution."""
        original = resolve_local_print_template('part')
        LabelTemplate.objects.create(
            name='Copied product label',
            model_type='part',
            template=original.template.name,
        )
        before = self.flags()
        with self.assertRaisesMessage(CommandError, 'found 2'):
            self.run_command(apply=True, backup=self.backup)
        self.assertEqual(before, self.flags())
        self.assertFalse(self.backup.exists())

    def test_wrong_size_and_unsupported_model(self):
        """Do not silently resize labels or print other model types."""
        LabelTemplate.objects.filter(name=PRODUCT_IPN_LABEL_NAME).update(width=60)
        with self.assertRaisesMessage(ValidationError, '50 x 30'):
            resolve_local_print_template('part')
        with self.assertRaisesMessage(ValidationError, 'part and stockitem'):
            resolve_local_print_template('stocklocation')

    def test_backup_required_and_not_overwritten(self):
        """Mutations require a fresh rollback file."""
        before = self.flags()
        with self.assertRaises(CommandError):
            self.run_command(apply=True)
        self.backup.write_text('existing backup', encoding='utf-8')
        with self.assertRaises(CommandError):
            self.run_command(apply=True, backup=self.backup)
        self.assertEqual(self.backup.read_text(), 'existing backup')
        self.assertEqual(before, self.flags())

    def test_restore_rejects_changed_records_atomically(self):
        """A stale backup cannot overwrite later template edits."""
        self.run_command(apply=True, backup=self.backup)
        LabelTemplate.objects.filter(
            model_type='stockitem', name=PRODUCT_IPN_LABEL_NAME
        ).update(height=31)
        before = self.flags()
        with self.assertRaisesMessage(CommandError, 'changed'):
            self.run_command(apply=True, restore=self.backup)
        self.assertEqual(before, self.flags())

    def test_invalid_backup(self):
        """Malformed flags cannot be coerced into database values."""
        self.run_command(apply=True, backup=self.backup)
        payload = json.loads(self.backup.read_text())
        payload['records'][0]['before'] = 'false'
        self.backup.write_text(json.dumps(payload))
        before = self.flags()
        with self.assertRaisesMessage(CommandError, 'booleans'):
            self.run_command(apply=True, restore=self.backup)
        self.assertEqual(before, self.flags())
