"""Persist individual label counts without changing existing uniform print jobs."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add optional per-item counts with an empty default for existing jobs."""

    dependencies = [('report', '0034_contract_warranty')]

    operations = [
        migrations.AddField(
            model_name='localprintjob',
            name='item_copies',
            field=models.JSONField(blank=True, default=dict),
        )
    ]
