"""Normalize and enforce company-wide unique non-empty Part IPNs."""

import hashlib

from django.db import migrations, models


def normalize_and_check_ipns(apps, schema_editor):
    """Trim legacy IPNs and fail clearly if case-insensitive duplicates exist."""
    part = apps.get_model('part', 'Part')
    database = schema_editor.connection.alias
    owners = {}
    updates = []

    for item in part.objects.using(database).only('pk', 'IPN', 'ipn_hash').iterator():
        normalized = item.IPN.strip() if isinstance(item.IPN, str) else None
        normalized = normalized or None
        ipn_hash = None

        if normalized:
            ipn_hash = hashlib.sha256(normalized.casefold().encode()).hexdigest()
            previous_pk = owners.setdefault(ipn_hash, item.pk)

            if previous_pk != item.pk:
                raise RuntimeError(
                    'Cannot enforce unique Part IPNs: the normalized IPN '
                    f'{normalized!r} is shared by Part IDs {previous_pk} and {item.pk}'
                )

        if normalized != item.IPN or item.ipn_hash != ipn_hash:
            item.IPN = normalized
            item.ipn_hash = ipn_hash
            updates.append(item)

    if updates:
        part.objects.using(database).bulk_update(
            updates, ['IPN', 'ipn_hash'], batch_size=1000
        )


class Migration(migrations.Migration):
    """Make non-empty IPNs unique without restricting blank legacy parts."""

    dependencies = [('part', '0153_part_barcode_hash_unique')]

    operations = [
        migrations.AddField(
            model_name='part',
            name='ipn_hash',
            field=models.CharField(
                blank=True,
                editable=False,
                help_text='Canonical hash used to enforce unique IPN values',
                max_length=64,
                null=True,
                verbose_name='IPN Hash',
            ),
        ),
        migrations.RunPython(
            normalize_and_check_ipns,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='part',
            name='ipn_hash',
            field=models.CharField(
                blank=True,
                editable=False,
                help_text='Canonical hash used to enforce unique IPN values',
                max_length=64,
                null=True,
                unique=True,
                verbose_name='IPN Hash',
            ),
        ),
    ]
