"""Resolve local product labels without deployment-specific database IDs."""

from django.core.exceptions import ValidationError

from report.models import PRODUCT_IPN_LABEL_NAME, LabelTemplate


def resolve_local_print_template(model_type, *, require_enabled=True):
    """Reject missing, ambiguous, disabled or incorrectly sized business labels."""
    if model_type == 'wmsreceiptdetail':
        candidates = list(
            LabelTemplate.objects.filter(
                model_type=model_type, name='WMS Complete Receipt Label'
            )
        )
        if len(candidates) != 1:
            raise ValidationError(
                'Complete receipt label template is missing or ambiguous'
            )
        template = candidates[0]
        if (require_enabled and not template.enabled) or (
            template.width,
            template.height,
        ) != (50, 30):
            raise ValidationError('Complete label must be enabled and 50 x 30 mm')
        if not template.template or not template.template.storage.exists(
            template.template.name
        ):
            raise ValidationError('Complete receipt template file is missing')
        return template
    if model_type not in ('part', 'stockitem'):
        raise ValidationError('Local product labels support part and stockitem only')

    candidates = [
        template
        for template in LabelTemplate.objects.filter(model_type=model_type)
        if template.is_product_ipn_label
    ]
    if len(candidates) != 1:
        raise ValidationError(
            f'{model_type}: expected exactly one Product IPN label; found {len(candidates)}'
        )
    template = candidates[0]
    if template.name != PRODUCT_IPN_LABEL_NAME:
        raise ValidationError(f'{model_type}: Product IPN label name was changed')
    if require_enabled and not template.enabled:
        raise ValidationError(f'{model_type}: Product IPN label is disabled')
    if (template.width, template.height) != (50, 30):
        raise ValidationError(f'{model_type}: Product IPN label must be 50 x 30 mm')
    if not template.template or not template.template.storage.exists(
        template.template.name
    ):
        raise ValidationError(f'{model_type}: Product IPN template file is missing')
    return template
