"""Add the direct scan inbound stock item marker and audit trail."""

from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import InvenTree.models


class Migration(migrations.Migration):
    """Create the scan inbound persistence model and constraints."""

    dependencies = [
        ('build', '0059_build_tags'),
        ('company', '0080_company_tags'),
        ('order', '0121_add_line_item_discount'),
        ('part', '0152_alter_partpricing_currency'),
        ('stock', '0127_alter_stockitemtestresult_options'),
        (
            'taggit',
            '0006_rename_taggeditem_content_type_object_id_taggit_tagg_content_8fc721_idx',
        ),
        ('users', '0015_alter_userprofile_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScanInboundRecord',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'barcode',
                    models.CharField(
                        help_text='Raw numeric barcode received from the scanner',
                        max_length=500,
                        verbose_name='Barcode',
                    ),
                ),
                (
                    'quantity',
                    models.DecimalField(
                        decimal_places=5,
                        default=Decimal('1'),
                        max_digits=15,
                        validators=[django.core.validators.MinValueValidator(1)],
                        verbose_name='Inbound Quantity',
                    ),
                ),
                (
                    'stock_item_quantity',
                    models.DecimalField(
                        decimal_places=5,
                        help_text='Dedicated scan stock item quantity after this scan',
                        max_digits=15,
                        verbose_name='Stock Item Quantity',
                    ),
                ),
                (
                    'new_total_stock',
                    models.DecimalField(
                        decimal_places=5,
                        help_text='Part total stock after this scan',
                        max_digits=15,
                        verbose_name='New Total Stock',
                    ),
                ),
                (
                    'scanned_at',
                    models.DateTimeField(auto_now_add=True, verbose_name='Scanned At'),
                ),
            ],
            options={
                'verbose_name': 'Scan Inbound Record',
                'verbose_name_plural': 'Scan Inbound Records',
                'ordering': ['-scanned_at', '-pk'],
            },
            bases=(
                InvenTree.models.ContentTypeMixin,
                InvenTree.models.PluginValidationMixin,
                models.Model,
            ),
        ),
        migrations.AddField(
            model_name='stockitem',
            name='is_scan_inbound_item',
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text='Marks the dedicated stock item used to accumulate scan inbound quantity',
                verbose_name='Scan Inbound Stock Item',
            ),
        ),
        migrations.AddConstraint(
            model_name='stockitem',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_scan_inbound_item', True)),
                fields=('part', 'location'),
                name='stock_item_unique_scan_inbound_part_location',
            ),
        ),
        migrations.AddField(
            model_name='scaninboundrecord',
            name='location',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='scan_inbound_records',
                to='stock.stocklocation',
                verbose_name='Stock Location',
            ),
        ),
        migrations.AddField(
            model_name='scaninboundrecord',
            name='part',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='scan_inbound_records',
                to='part.part',
                verbose_name='Part',
            ),
        ),
        migrations.AddField(
            model_name='scaninboundrecord',
            name='stock_item',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='scan_inbound_records',
                to='stock.stockitem',
                verbose_name='Stock Item',
            ),
        ),
        migrations.AddField(
            model_name='scaninboundrecord',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='scan_inbound_records',
                to=settings.AUTH_USER_MODEL,
                verbose_name='User',
            ),
        ),
        migrations.AddIndex(
            model_name='scaninboundrecord',
            index=models.Index(
                fields=['-scanned_at', '-id'], name='scan_inbound_recent_idx'
            ),
        ),
    ]
