"""Add an idempotency key to scan inbound audit records."""

import uuid

from django.db import migrations, models


def populate_client_event_ids(apps, schema_editor):
    """Give every existing scan record a distinct client event identifier."""
    scan_record = apps.get_model('stock', 'ScanInboundRecord')

    for record in scan_record.objects.filter(client_event_id__isnull=True).iterator():
        record.client_event_id = uuid.uuid4()
        record.save(update_fields=['client_event_id'])


class Migration(migrations.Migration):
    """Add and backfill the scan inbound idempotency key."""

    dependencies = [
        ('stock', '0128_scaninboundrecord_stockitem_is_scan_inbound_item_and_more')
    ]

    operations = [
        migrations.AddField(
            model_name='scaninboundrecord',
            name='client_event_id',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_client_event_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='scaninboundrecord',
            name='client_event_id',
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
                verbose_name='Client Event ID',
                help_text=(
                    'Unique client-generated identifier used to make scan retries '
                    'idempotent'
                ),
            ),
        ),
    ]
