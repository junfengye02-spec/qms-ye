"""Report template model definitions."""

import io
import os
import sys
import uuid
from datetime import date, datetime
from typing import Optional, TypedDict, cast

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.template import Context, Template
from django.template.exceptions import TemplateDoesNotExist, TemplateSyntaxError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

import structlog
from pypdf import PdfWriter

import InvenTree.exceptions
import InvenTree.helpers
import InvenTree.models
import InvenTree.ready
import report.helpers
import report.validators
from common.models import DataOutput, RenderChoices, UpdatedUserMixin
from common.settings import get_global_setting
from InvenTree.helpers_model import get_base_url
from InvenTree.models import MetadataMixin
from plugin import InvenTreePlugin, PluginMixinEnum
from plugin.registry import registry

try:
    from weasyprint import HTML

    from report.fetcher import InvenTreeURLFetcher
except OSError as err:  # pragma: no cover
    print(f'OSError: {err}')
    print("Unable to import 'weasyprint' module.")
    print('You may require some further system packages to be installed.')
    sys.exit(1)


logger = structlog.getLogger('inventree')

PRODUCT_IPN_LABEL_FILENAME = 'product_ipn_qr.html'
PRODUCT_IPN_LABEL_NAME = 'Product IPN QR Code Label'
PRODUCT_IPN_LABEL_DESCRIPTION = 'Product QR label by IPN (not a stock item code)'
MAX_LABELS_PER_PRINT = 500


def log_report_error(*args, **kwargs):
    """Log an error message when a report fails to render."""
    try:
        do_log = get_global_setting('REPORT_LOG_ERRORS', backup_value=True)
    except Exception:
        do_log = True

    if do_log:
        InvenTree.exceptions.log_error(*args, **kwargs)


def rename_template(instance, filename):
    """Function to rename a report template once uploaded.

    - Retains the original uploaded filename
    - Checks for duplicate filenames across instance class
    """
    path = instance.get_upload_path(filename)

    # Throw error if any other model instances reference this path
    instance.check_existing_file(path, raise_error=True)

    # Delete file with this name if it already exists
    if default_storage.exists(path):
        logger.info(f'Deleting existing template file: {path}')
        default_storage.delete(path)

    return path


class TemplateUploadMixin:
    """Mixin class for providing template path management functions.

    - Provides generic method for determining the upload path for a template
    - Provides generic method for checking for duplicate filenames

    Classes which inherit this mixin can guarantee that uploaded templates are unique,
    and that the same filename will be retained when uploaded.
    """

    # Directory in which to store uploaded templates
    SUBDIR = ''

    # Name of the template field
    TEMPLATE_FIELD = 'template'

    def __str__(self) -> str:
        """String representation of a TemplateUploadMixin instance."""
        return str(os.path.basename(self.template_name))

    @property
    def template_name(self):
        """Return the filename of the template associated with this model class."""
        template = getattr(self, self.TEMPLATE_FIELD).name
        template = template.replace('/', os.path.sep)
        template = template.replace('\\', os.path.sep)

        template = settings.MEDIA_ROOT.joinpath(template)

        return str(template)

    @property
    def extension(self):
        """Return the filename extension of the associated template file."""
        return os.path.splitext(self.template.name)[1].lower()

    def get_upload_path(self, filename):
        """Generate an upload path for the given filename."""
        fn = os.path.basename(filename)
        return os.path.join('report', self.SUBDIR, fn)

    def check_existing_file(self, path, raise_error=False):
        """Check if a file already exists with the given filename."""
        filters = {self.TEMPLATE_FIELD: self.get_upload_path(path)}

        exists = self.__class__.objects.filter(**filters).exclude(pk=self.pk).exists()

        if exists and raise_error:
            raise ValidationError(
                {self.TEMPLATE_FIELD: _('Template file with this name already exists')}
            )

        return exists

    def validate_unique(self, exclude=None):
        """Validate that this template is unique."""
        proposed_path = self.get_upload_path(self.template_name)
        self.check_existing_file(proposed_path, raise_error=True)
        return super().validate_unique(exclude)


class BaseContextExtension(TypedDict):
    """Base context extension.

    Attributes:
        base_url: The base URL for the InvenTree instance
        date: Current date, represented as a Python datetime.date object
        datetime: Current datetime, represented as a Python datetime object
        template: The report template instance which is being rendered against
        template_description: Description of the report template
        template_name: Name of the report template
        template_revision: Revision of the report template
        user: User who is creating the report (if available)
    """

    base_url: str
    date: date
    datetime: datetime
    template: 'ReportTemplateBase'
    template_description: str
    template_name: str
    template_revision: int
    user: Optional[AbstractUser]


class LabelContextExtension(TypedDict):
    """Label report context extension.

    Attributes:
        width: The width of the label (in mm)
        height: The height of the label (in mm)
        page_style: The CSS @page style for the label template. This is used to be inserted at the top of the style block for a given label
    """

    width: float
    height: float
    page_style: Optional[str]


class ReportContextExtension(TypedDict):
    """Report context extension.

    Attributes:
        page_size: The page size of the report
        landscape: Boolean value, True if the report is in landscape mode
        merge: Boolean value, True if the a single report is generated against multiple items
    """

    page_size: str
    landscape: bool
    merge: bool


class ReportTemplateBase(
    MetadataMixin, UpdatedUserMixin, InvenTree.models.InvenTreeModel
):
    """Base class for reports, labels."""

    class ModelChoices(RenderChoices):
        """Model choices for report templates."""

        choice_fnc = report.helpers.report_model_options

    class Meta:
        """Metaclass options."""

        abstract = True
        unique_together = ('name', 'model_type')

    def save(self, *args, **kwargs):
        """Perform additional actions when the report is saved."""
        if kwargs.pop('increment_revision', True):
            # Increment revision number
            self.revision += 1

        super().save()

    name = models.CharField(
        blank=False,
        max_length=100,
        verbose_name=_('Name'),
        help_text=_('Template name'),
    )

    description = models.CharField(
        max_length=250,
        verbose_name=_('Description'),
        help_text=_('Template description'),
    )

    revision = models.PositiveIntegerField(
        default=1,
        verbose_name=_('Revision'),
        help_text=_('Revision number (auto-increments)'),
        editable=False,
    )

    attach_to_model = models.BooleanField(
        default=False,
        verbose_name=_('Attach to Model on Print'),
        help_text=_(
            'Save report output as an attachment against linked model instance when printing'
        ),
    )

    def generate_filename(self, context, **kwargs) -> str:
        """Generate a filename for this report."""
        template_string = Template(self.filename_pattern)
        return template_string.render(Context(context))

    def render_as_string(
        self, instance: models.Model, context: Optional[dict] = None, **kwargs
    ) -> str:
        """Render the report to a HTML string.

        Arguments:
            instance: The model instance to render against
            context: Django template language contexts (optional)

        Returns:
            str: HTML string
        """
        if context is None:
            context = self.get_context(instance, **kwargs)

        return render_to_string(self.template_name, context)

    def render(
        self, instance: models.Model, context: Optional[dict] = None, **kwargs
    ) -> bytes:
        """Render the template to a PDF file.

        Arguments:
            instance: The model instance to render against
            context: Django template language contexts (optional)
            user: The user to associate with the generated report

        Returns:
            bytes: PDF data
        """
        html = self.render_as_string(instance, context=context, **kwargs)
        pdf = HTML(string=html, url_fetcher=InvenTreeURLFetcher()).write_pdf(
            pdf_forms=True
        )

        return pdf

    filename_pattern = models.CharField(
        default='output.pdf',
        verbose_name=_('Filename Pattern'),
        help_text=_('Pattern for generating filenames'),
        max_length=100,
    )

    enabled = models.BooleanField(
        default=True, verbose_name=_('Enabled'), help_text=_('Template is enabled')
    )

    model_type = models.CharField(
        max_length=100,
        validators=[report.validators.validate_report_model_type],
        verbose_name=_('Model Type'),
        help_text=_('Target model type for template'),
    )

    def clean(self):
        """Clean model instance, and ensure validity."""
        super().clean()

        model = self.get_model()
        filters = self.filters

        if model and filters:
            report.validators.validate_filters(filters, model=model)

    def get_model(self):
        """Return the database model class associated with this report template."""
        return report.helpers.report_model_from_name(self.model_type)

    filters = models.CharField(
        blank=True,
        max_length=250,
        verbose_name=_('Filters'),
        help_text=_('Template query filters (comma-separated list of key=value pairs)'),
        validators=[report.validators.validate_filters],
    )

    def get_filters(self):
        """Return a filter dict which can be applied to the target model."""
        return report.validators.validate_filters(self.filters, model=self.get_model())

    def base_context(self, **kwargs) -> BaseContextExtension:
        """Return base context data (available to all templates)."""
        return {
            'base_url': get_base_url(),
            'date': InvenTree.helpers.current_date(),
            'datetime': InvenTree.helpers.current_time(),
            'template': self,
            'template_description': self.description,
            'template_name': self.name,
            'template_revision': self.revision,
            'user': kwargs.get('user'),
        }

    def get_context(self, instance: models.Model, **kwargs):
        """Supply context data to the generic template for rendering.

        Arguments:
            instance: The model instance we are printing against
            user: The user to associate with the generated report
        """
        # Provide base context information to all templates
        base_context = self.base_context(**kwargs)

        # Add in an context information provided by the model instance itself
        context = {**base_context, **instance.report_context()}

        return context


class ReportTemplate(TemplateUploadMixin, ReportTemplateBase):
    """Class representing the ReportTemplate database model."""

    SUBDIR = 'report'
    TEMPLATE_FIELD = 'template'

    @staticmethod
    def get_api_url():
        """Return the API endpoint for the ReportTemplate model."""
        return reverse('api-report-template-list')

    def __init__(self, *args, **kwargs):
        """Initialize the particular report instance."""
        super().__init__(*args, **kwargs)

        self._meta.get_field(
            'page_size'
        ).choices = report.helpers.report_page_size_options()

    template = models.FileField(
        upload_to=rename_template,
        verbose_name=_('Template'),
        help_text=_('Template file'),
        validators=[FileExtensionValidator(allowed_extensions=['html', 'htm'])],
    )

    page_size = models.CharField(
        max_length=20,
        default=report.helpers.report_page_size_default,
        verbose_name=_('Page Size'),
        help_text=_('Page size for PDF reports'),
    )

    landscape = models.BooleanField(
        default=False,
        verbose_name=_('Landscape'),
        help_text=_('Render report in landscape orientation'),
    )

    merge = models.BooleanField(
        default=False,
        verbose_name=_('Merge'),
        help_text=_('Render a single report against selected items'),
    )

    def get_report_size(self) -> str:
        """Return the printable page size for this report."""
        try:
            page_size_default = cast(
                str, get_global_setting('REPORT_DEFAULT_PAGE_SIZE', 'A4', create=False)
            )
        except Exception:
            page_size_default = 'A4'

        page_size = self.page_size or page_size_default

        if self.landscape:
            page_size = page_size + ' landscape'

        return page_size

    def get_report_context(self):
        """Return report template context."""
        report_context: ReportContextExtension = {
            'page_size': self.get_report_size(),
            'landscape': self.landscape,
            'merge': self.merge,
        }

        return report_context

    def get_context(self, instance: models.Model, **kwargs):
        """Supply context data to the report template for rendering.

        Arguments:
            instance: The model instance we are printing against
        """
        base_context = super().get_context(instance, **kwargs)
        report_context: ReportContextExtension = self.get_report_context()

        context = {**base_context, **report_context}

        # Pass the context through to the plugin registry for any additional information
        context = self.get_plugin_context(instance, context, **kwargs)
        return context

    def get_plugin_context(self, instance: models.Model, context: dict, **kwargs):
        """Get the context for the plugin.

        Arguments:
            instance: The model instance we are printing against
            context: The context dictionary to add to
            user: The user to associate with the generated report
        """
        user = kwargs.get('user')

        for plugin in registry.with_mixin(PluginMixinEnum.REPORT):
            try:
                plugin.add_report_context(self, instance, user, context)
            except Exception:
                InvenTree.exceptions.log_error('add_report_context', plugin=plugin.slug)

        return context

    def handle_attachment(self, instance, report, report_name, user, debug_mode):
        """Attach the generated report to the model instance (if required)."""
        if self.attach_to_model and not debug_mode:
            instance.create_attachment(
                attachment=ContentFile(report, report_name),
                comment=_(f'Report generated from template {self.name}'),
                upload_user=user,
            )

    def notify_plugins(self, instance, report, user):
        """Provide generated report to any interested plugins.

        Arguments:
            instance: The model instance we are printing against
            report: The generated report object
            user: The user to associate with the generated report
        """
        report_plugins = registry.with_mixin(PluginMixinEnum.REPORT)

        for plugin in report_plugins:
            try:
                plugin.report_callback(self, instance, report, user)
            except Exception:
                InvenTree.exceptions.log_error('report_callback', plugin=plugin.slug)

    def print(
        self,
        items: list,
        output: Optional[DataOutput] = None,
        user: Optional[AbstractUser] = None,
        **kwargs,
    ) -> DataOutput:
        """Print reports for a list of items against this template.

        Arguments:
            items: A list of items to print reports for (model instance)
            output: The DataOutput object to use
            user: The user to associate with the generated report

        Returns:
            output: The DataOutput object representing the generated report(s)

        Raises:
            ValidationError: If there is an error during report printing

        Notes:
            Currently, all items are rendered separately into PDF files,
            and then combined into a single PDF file.

            Further work is required to allow the following extended features:
            - Render a single PDF file with the collated items (optional per template)
            - Render a raw file (do not convert to PDF) - allows for other file types
        """
        logger.info("Printing %s reports against template '%s'", len(items), self.name)

        # Extract user information from the provided context
        user = user or getattr(output, 'user', None)

        outputs = []

        debug_mode = get_global_setting('REPORT_DEBUG_MODE', False)

        # Start with a default report name
        report_name: Optional[str] = None

        # If a DataOutput object is not provided, create a new one
        if not output:
            output = DataOutput.objects.create(
                total=len(items),
                user=user,
                progress=0,
                complete=False,
                output_type=DataOutput.DataOutputTypes.REPORT,
                template_name=self.name,
                output=None,
            )

        if output.progress != 0:
            output.progress = 0
            output.save()

        try:
            if self.merge:
                base_context = super().base_context(user=user)
                report_context = self.get_report_context()
                item_contexts = []
                for instance in items:
                    instance_context = instance.report_context()
                    instance_context = self.get_plugin_context(
                        instance, instance_context, user=user
                    )
                    item_contexts.append(instance_context)

                contexts = {
                    **base_context,
                    **report_context,
                    'instances': item_contexts,
                }

                if report_name is None:
                    report_name = self.generate_filename(contexts)

                try:
                    if debug_mode:
                        report = self.render_as_string(
                            instance, user=user, context=contexts
                        )
                    else:
                        report = self.render(instance, user=user, context=contexts)
                except TemplateDoesNotExist as e:
                    t_name = str(e) or self.template
                    msg = f'Template file {t_name} does not exist'
                    output.mark_failure(error=msg)
                    raise ValidationError(msg)
                except TemplateSyntaxError as e:
                    msg = _('Template syntax error')
                    output.mark_failure(str(e) or msg)
                    raise ValidationError(f'{msg}: {e!s}')
                except ValidationError as e:
                    output.mark_failure(','.join(e.messages))
                    raise e
                except Exception as e:
                    msg = _('Error rendering report')
                    output.mark_failure(msg)
                    raise ValidationError(f'{msg}: {e!s}')

                outputs.append(report)
                self.handle_attachment(instance, report, report_name, user, debug_mode)
                self.notify_plugins(instance, report, user)

                # Update the progress of the report generation
                output.progress += 1
                output.save()
            else:
                for instance in items:
                    context = self.get_context(instance, user=user)

                    if report_name is None:
                        report_name = self.generate_filename(context)

                    # Render the report output
                    try:
                        if debug_mode:
                            report = self.render_as_string(
                                instance, user=user, context=context
                            )
                        else:
                            report = self.render(instance, user=user, context=None)
                    except TemplateDoesNotExist as e:
                        t_name = str(e) or self.template
                        msg = f'Template file {t_name} does not exist'
                        output.mark_failure(error=msg)
                        raise ValidationError(msg)
                    except TemplateSyntaxError as e:
                        msg = _('Template syntax error')
                        output.mark_failure(error=str(e) or msg)
                        raise ValidationError(f'{msg}: {e!s}')
                    except ValidationError as e:
                        output.mark_failure(', '.join(e.messages))
                        raise e
                    except Exception as e:
                        msg = _('Error rendering report')
                        output.mark_failure(error=msg)
                        raise ValidationError(f'{msg}: {e!s}')

                    outputs.append(report)

                    self.handle_attachment(
                        instance, report, report_name, user, debug_mode
                    )

                    self.notify_plugins(instance, report, user)

                    # Update the progress of the report generation
                    output.progress += 1
                    output.save()

        except Exception as exc:
            # Something went wrong during the report generation process
            log_report_error('ReportTemplate.print')

            # If the error occurred in a worker thread, we do not want to raise an error,
            # as this would cause the worker to retry the task indefinitely
            if InvenTree.ready.isInWorkerThread():
                return

            # Raise a ValidationError with the error message
            # This will be caught by the caller and displayed to the user
            raise ValidationError(
                {'error': _('Error generating report'), 'detail': str(exc)}
            )

        if not report_name:
            report_name = ''  # pragma: no cover

        if not report_name.endswith('.pdf'):
            report_name += '.pdf'

        # Combine all the generated reports into a single PDF file
        if debug_mode:
            data = '\n'.join(outputs)
            report_name = report_name.replace('.pdf', '.html')
        else:
            # Merge the outputs back together into a single PDF file
            pdf_writer = PdfWriter()

            try:
                for report in outputs:
                    # Construct file object with raw PDF data
                    report_file = io.BytesIO(report)
                    pdf_writer.append(report_file)

                # Generate raw output
                pdf_file = io.BytesIO()
                pdf_writer.write(pdf_file)
                data = pdf_file.getvalue()
                pdf_file.close()
            except Exception:
                log_report_error('ReportTemplate.print')
                msg = _('Error merging report outputs')
                output.mark_failure(error=msg)

                # If the error occurred in a worker thread, we do not want to raise an error,
                # as this would cause the worker to retry the task indefinitely
                if InvenTree.ready.isInWorkerThread():
                    return

                raise ValidationError(msg)

        # Save the generated report to the database
        generated_file = ContentFile(data, report_name)

        output.mark_complete(output=generated_file)

        return output


class LabelTemplate(TemplateUploadMixin, ReportTemplateBase):
    """Class representing the LabelTemplate database model."""

    SUBDIR = 'label'
    TEMPLATE_FIELD = 'template'

    @staticmethod
    def get_api_url():
        """Return the API endpoint for the LabelTemplate model."""
        return reverse('api-label-template-list')

    template = models.FileField(
        upload_to=rename_template,
        verbose_name=_('Template'),
        help_text=_('Template file'),
        validators=[FileExtensionValidator(allowed_extensions=['html', 'htm'])],
    )

    width = models.FloatField(
        default=50,
        verbose_name=_('Width [mm]'),
        help_text=_('Label width, specified in mm'),
        validators=[MinValueValidator(2)],
    )

    height = models.FloatField(
        default=20,
        verbose_name=_('Height [mm]'),
        help_text=_('Label height, specified in mm'),
        validators=[MinValueValidator(2)],
    )

    def generate_page_style(self, **kwargs):
        """Generate @page style for the label template.

        This is inserted at the top of the style block for a given label
        """
        width = kwargs.get('width', self.width)
        height = kwargs.get('height', self.height)
        margin = kwargs.get('margin', 0)

        return f"""
        @page {{
            size: {width}mm {height}mm;
            margin: {margin}mm;
        }}
        """

    @property
    def is_product_ipn_label(self) -> bool:
        """Return whether this is the built-in product IPN label template."""
        filename = os.path.basename(self.template.name)

        return self.name == PRODUCT_IPN_LABEL_NAME or filename.startswith(
            os.path.splitext(PRODUCT_IPN_LABEL_FILENAME)[0]
        )

    def validate_print_request(self, items: list, copies: int = 1) -> None:
        """Validate label items and copy limits before creating a print job."""
        if isinstance(copies, bool) or not isinstance(copies, int):
            raise ValidationError({'copies': _('Copies must be a whole number')})

        if copies < 1 or copies > MAX_LABELS_PER_PRINT:
            raise ValidationError(
                {
                    'copies': _('Copies must be between 1 and %(maximum)s')
                    % {'maximum': MAX_LABELS_PER_PRINT}
                }
            )

        if len(items) * copies > MAX_LABELS_PER_PRINT:
            raise ValidationError(
                {
                    'copies': _(
                        'A single print request is limited to %(maximum)s labels'
                    )
                    % {'maximum': MAX_LABELS_PER_PRINT}
                }
            )

        if not self.is_product_ipn_label:
            return

        missing_ipn = []

        for item in items:
            part = getattr(item, 'part', item)
            ipn = str(getattr(part, 'IPN', '') or '').strip()

            if not ipn:
                missing_ipn.append(f'{item._meta.verbose_name} #{item.pk} ({item})')

        if missing_ipn:
            raise ValidationError(
                {
                    'items': _(
                        'Cannot print the Product IPN QR Code label because these objects have no IPN: %(items)s'
                    )
                    % {'items': ', '.join(missing_ipn)}
                }
            )

    def get_context(self, instance: models.Model, *args, **kwargs):
        """Supply context data to the label template for rendering.

        Arguments:
            instance: The model instance we are printing against
        """
        user = kwargs.get('user')

        base_context = super().get_context(instance, **kwargs)

        label_context: LabelContextExtension = {
            'width': self.width,
            'height': self.height,
            'page_style': None,
        }

        context = {**base_context, **label_context}

        if self.is_product_ipn_label:
            part = getattr(instance, 'part', instance)
            context['product_ipn'] = str(getattr(part, 'IPN', '') or '').strip()
            context['product_revision'] = str(
                getattr(part, 'revision', '') or ''
            ).strip()

        if kwargs.pop('insert_page_style', True):
            context['page_style'] = self.generate_page_style()

        # Pass the context through to any registered plugins
        plugins = registry.with_mixin(PluginMixinEnum.REPORT)

        for plugin in plugins:
            # Let each plugin add its own context data
            try:
                plugin.add_label_context(self, instance, user, context)
            except Exception:
                InvenTree.exceptions.log_error('add_label_context', plugin=plugin.slug)

        return context

    def print(
        self,
        items: list,
        plugin: InvenTreePlugin,
        output: Optional[DataOutput] = None,
        options: Optional[dict] = None,
        user: Optional[AbstractUser] = None,
        copies: int = 1,
        **kwargs,
    ) -> DataOutput:
        """Print labels for a list of items against this template.

        Arguments:
            items: A list of items to print labels for (model instance)
            plugin: The plugin to use for label rendering
            output: The DataOutput object to use
            options: Additional options for the label printing plugin (optional)
            user: The user to associate with the generated labels
            copies: Number of copies to print for each selected item

        Returns:
            output: The DataOutput object representing the generated label(s)

        Raises:
            ValidationError: If there is an error during label printing
        """
        items = list(items)

        try:
            self.validate_print_request(items, copies=copies)
        except ValidationError:
            if output:
                output.delete()
            raise

        items = [item for item in items for _copy in range(copies)]

        logger.info(
            f"Printing {len(items)} labels against template '{self.name}' using plugin '{plugin.slug}'"
        )

        user = user or getattr(output, 'user', None)

        if not output:
            output = DataOutput.objects.create(
                user=user,
                total=len(items),
                progress=0,
                complete=False,
                output_type=DataOutput.DataOutputTypes.LABEL,
                template_name=self.name,
                plugin=plugin.slug,
                output=None,
            )

        if options is None:
            options = {}

        try:
            if hasattr(plugin, 'before_printing'):
                plugin.before_printing()

            plugin.print_labels(
                self, output, items, None, user=user, printing_options=options
            )

            if hasattr(plugin, 'after_printing'):
                plugin.after_printing()
        except ValidationError as e:
            output.delete()
            raise e
        except Exception as e:
            output.delete()
            InvenTree.exceptions.log_error('print_labels', plugin=plugin.slug)
            raise ValidationError([_('Error printing labels'), str(e)])

        output.refresh_from_db()

        # Return the output object
        return output


class LocalPrintJob(models.Model):
    """Audit a browser-to-local-queue attempt independently of PDF generation."""

    class State(models.TextChoices):
        """Submission is not evidence of physical printing."""

        GENERATING = 'generating'
        READY = 'ready'
        SUBMITTING = 'submitting'
        SUBMITTED = 'submitted'
        FAILED = 'failed'
        UNCERTAIN = 'uncertain'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    request_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    item_versions = models.JSONField(default=dict, blank=True)
    model_type = models.CharField(max_length=20)
    items = models.JSONField()
    copies = models.PositiveIntegerField()
    item_copies = models.JSONField(default=dict, blank=True)
    queue = models.CharField(max_length=255)
    template = models.ForeignKey(LabelTemplate, null=True, on_delete=models.SET_NULL)
    template_name = models.CharField(max_length=100)
    output = models.ForeignKey(DataOutput, null=True, on_delete=models.SET_NULL)
    file_sha256 = models.CharField(max_length=64, blank=True)
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.GENERATING
    )
    detail = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    expires = models.DateTimeField()
    claimed = models.DateTimeField(null=True)
    submitted = models.DateTimeField(null=True)
    original = models.ForeignKey('self', null=True, on_delete=models.SET_NULL)

    def copies_for(self, item_id):
        """Return the durable copy count for one selected object."""
        return self.item_copies.get(str(item_id), self.copies)

    @property
    def total_labels(self):
        """Count all pages, including tasks created before per-item quantities."""
        return sum(self.copies_for(item_id) for item_id in self.items)

    class Meta:
        """Deduplicate independently for each authenticated operator."""

        constraints = [
            models.UniqueConstraint(
                fields=['user', 'request_key'], name='local_print_user_request'
            )
        ]


class ReportSnippet(TemplateUploadMixin, models.Model):
    """Report template 'snippet' which can be used to make templates that can then be included in other reports.

    Useful for 'common' template actions, sub-templates, etc
    """

    SUBDIR = 'snippets'
    TEMPLATE_FIELD = 'snippet'

    snippet = models.FileField(
        upload_to=rename_template,
        verbose_name=_('Snippet'),
        help_text=_('Report snippet file'),
        validators=[FileExtensionValidator(allowed_extensions=['html', 'htm'])],
    )

    description = models.CharField(
        max_length=250,
        verbose_name=_('Description'),
        help_text=_('Snippet file description'),
    )


class ReportAsset(TemplateUploadMixin, models.Model):
    """Asset file for use in report templates.

    For example, an image to use in a header file.
    Uploaded asset files appear in MEDIA_ROOT/report/assets,
    and can be loaded in a template using the {% report_asset <filename> %} tag.
    """

    SUBDIR = 'assets'
    TEMPLATE_FIELD = 'asset'

    # Asset file
    asset = models.FileField(
        upload_to=rename_template,
        verbose_name=_('Asset'),
        help_text=_('Report asset file'),
    )

    # Asset description (user facing string, not used internally)
    description = models.CharField(
        max_length=250,
        verbose_name=_('Description'),
        help_text=_('Asset file description'),
    )
