"""Preview, back up and apply the local product label configuration."""

import hashlib
import json
import os
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from report.local_print_templates import resolve_local_print_template
from report.models import LabelTemplate

LEGACY_LABELS = (
    ('part', 'InvenTree Part Label'),
    ('part', 'InvenTree Part Label (Code128)'),
    ('stockitem', 'InvenTree Stock Item Label (QR)'),
)


def template_identity(template):
    """Bind a backup to the exact record and template contents."""
    with template.template.open('rb') as source:
        digest = hashlib.sha256(source.read()).hexdigest()
    return {
        'pk': template.pk,
        'name': template.name,
        'model_type': template.model_type,
        'template': template.template.name,
        'sha256': digest,
        'width': template.width,
        'height': template.height,
    }


class Command(BaseCommand):
    """Change enabled flags only; preserve all template files and custom labels."""

    help = 'Preview local label configuration; --apply requires a new --backup file'

    def add_arguments(self, parser):
        """Register explicit mutation and restoration options."""
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup', type=Path)
        parser.add_argument('--restore', type=Path)

    @transaction.atomic
    def handle(self, *args, **options):
        """Validate every change before applying any of them."""
        if options['restore'] and options['backup']:
            raise CommandError('--restore and --backup cannot be combined')
        if options['apply'] and not (options['backup'] or options['restore']):
            raise CommandError('--apply requires --backup (or --restore)')

        # Serialize configuration changes; run in a controlled maintenance window.
        list(LabelTemplate.objects.select_for_update().values_list('pk', flat=True))
        try:
            if options['restore']:
                changes = self.restore_changes(options['restore'])
            else:
                changes = [
                    (resolve_local_print_template(model, require_enabled=False), True)
                    for model in ('part', 'stockitem')
                ]
                for model, name in LEGACY_LABELS:
                    changes.extend(
                        (template, False)
                        for template in LabelTemplate.objects.filter(
                            model_type=model, name=name
                        )
                    )

            records = [
                {
                    **template_identity(template),
                    'before': template.enabled,
                    'after': enabled,
                }
                for template, enabled in changes
            ]
            for record in records:
                self.stdout.write(
                    f'{record["model_type"]} / {record["name"]}: '
                    f'{record["before"]} -> {record["after"]}'
                )

            if not options['apply']:
                self.stdout.write('Preview only; no changes written')
                return

            if options['backup']:
                # Exclusive creation prevents an idempotent rerun overwriting rollback data.
                with options['backup'].open('x', encoding='utf-8') as backup:
                    json.dump({'version': 1, 'records': records}, backup, indent=2)
                    backup.write('\n')
                    backup.flush()
                    os.fsync(backup.fileno())

            for template, enabled in changes:
                LabelTemplate.objects.filter(pk=template.pk).update(enabled=enabled)
            self.stdout.write(self.style.SUCCESS('Template enabled flags updated'))
        except (ValidationError, OSError, ValueError, KeyError, TypeError) as error:
            raise CommandError(str(error)) from error

    def restore_changes(self, path):
        """Refuse rollback if records changed since the configuration was applied."""
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise CommandError('Unsupported backup format')
        records = payload.get('records')
        if not isinstance(records, list) or not records:
            raise CommandError('Backup records must be a nonempty list')
        changes = []
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise CommandError('Invalid backup record')
            if type(record['before']) is not bool or type(record['after']) is not bool:
                raise CommandError('Backup enabled flags must be booleans')
            template = LabelTemplate.objects.filter(pk=record['pk']).first()
            if template is None or template.pk in seen:
                raise CommandError('Missing or duplicate template in backup')
            seen.add(template.pk)
            identity = template_identity(template)
            if any(record.get(key) != value for key, value in identity.items()):
                raise CommandError(f'Template {template.pk} changed; review manually')
            changes.append((template, record['before']))
        return changes
