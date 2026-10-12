"""Authenticated endpoints for local Windows queue submission."""

import base64

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from common.settings import get_global_setting
from InvenTree.permissions import IsAuthenticatedOrReadScope
from report import local_print as service
from report.models import MAX_LABELS_PER_PRINT, LocalPrintJob


class JobRequest(serializers.Serializer):
    """Only supported business inputs; no template/plugin/file override."""

    request_key = serializers.UUIDField()
    model_type = serializers.ChoiceField(
        choices=['part', 'stockitem', 'wmsreceiptdetail']
    )
    items = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=MAX_LABELS_PER_PRINT,
    )
    copies = serializers.IntegerField(
        min_value=1, max_value=MAX_LABELS_PER_PRINT, default=1
    )
    item_copies = serializers.DictField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_LABELS_PER_PRINT),
        required=False,
        allow_empty=False,
    )
    queue = serializers.CharField(max_length=255, trim_whitespace=False)

    def validate(self, values):
        """Require a count for exactly each selected item and bound the total."""
        counts = values.get('item_copies')
        if counts is not None:
            if set(counts) != {str(pk) for pk in values['items']}:
                raise serializers.ValidationError(
                    {'item_copies': 'Counts must match exactly the selected items'}
                )
            if values['copies'] != 1:
                raise serializers.ValidationError(
                    {'copies': 'Use either uniform copies or per-item counts'}
                )
            if sum(counts.values()) > MAX_LABELS_PER_PRINT:
                raise serializers.ValidationError(
                    {'item_copies': 'A single task is limited to 500 labels'}
                )
        return values

    def validate_queue(self, value):
        """Reject control characters while preserving exact Windows queue names."""
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise serializers.ValidationError('Invalid printer queue name')
        return value


class JobResponse(serializers.ModelSerializer):
    """Expose audit state without public media links or signing secrets."""

    total = serializers.SerializerMethodField()

    def get_total(self, job) -> int:
        """Number of PDF pages, already expanded by the renderer."""
        return job.total_labels

    class Meta:
        """Stable local job response fields."""

        model = LocalPrintJob
        fields = [
            'id',
            'model_type',
            'items',
            'copies',
            'item_copies',
            'queue',
            'template_name',
            'file_sha256',
            'state',
            'detail',
            'created',
            'updated',
            'expires',
            'claimed',
            'submitted',
            'original',
            'total',
        ]


class SignRequest(serializers.Serializer):
    """Full SDK message is validated before its hash is signed."""

    message = serializers.CharField(max_length=12 * 1024 * 1024, trim_whitespace=False)
    job = serializers.UUIDField(required=False)


class ResultRequest(serializers.Serializer):
    """Client observations cannot assert physical output or make a task resendable."""

    state = serializers.ChoiceField(choices=['submitted', 'uncertain'])


class ConfigResponse(serializers.Serializer):
    """Public configuration fields for the authenticated browser."""

    enabled = serializers.BooleanField()
    qz_version = serializers.CharField()
    download_url = serializers.CharField(allow_blank=True)
    max_labels = serializers.IntegerField()


class PdfResponse(serializers.Serializer):
    """Authenticated bytes and server-owned printer options."""

    data = serializers.CharField()
    sha256 = serializers.CharField()
    options = serializers.JSONField()


class SignResponse(serializers.Serializer):
    """Base64 RSA signature, never a private key."""

    signature = serializers.CharField()


@method_decorator(never_cache, name='dispatch')
class LocalPrintView(APIView):
    """All operations require login, including certificate retrieval."""

    permission_classes = [IsAuthenticatedOrReadScope]

    def get_job(self, pk):
        """Filter by owner first, including for administrators."""
        job = get_object_or_404(LocalPrintJob, pk=pk, user=self.request.user)
        service.job_access(self.request.user, job)
        return service.refresh_job(job)


class ConfigView(LocalPrintView):
    """Read configuration without connecting to any desktop component."""

    @extend_schema(responses=ConfigResponse)
    def get(self, request):
        """Expose a feature flag and an administrator-verified installer URL."""
        return Response(
            {
                'enabled': settings.LOCAL_PRINT_ENABLED
                and bool(get_global_setting('LABEL_ENABLE')),
                'qz_version': settings.LOCAL_PRINT_QZ_VERSION,
                'download_url': settings.LOCAL_PRINT_DOWNLOAD_URL,
                'max_labels': MAX_LABELS_PER_PRINT,
            }
        )


class CertificateView(LocalPrintView):
    """Expose only the public certificate, never a signing key."""

    @extend_schema(responses=str)
    def get(self, request):
        """Fail closed if server secrets are missing, mismatched or expired."""
        service.require_enabled()
        certificate, _ = service.credentials()
        return HttpResponse(certificate, content_type='text/plain')


class JobsView(LocalPrintView):
    """Create deduplicated tasks and recover recent submission attempts."""

    @extend_schema(responses=JobResponse(many=True))
    def get(self, request):
        """Keep audit records queryable after the feature is disabled."""
        jobs = LocalPrintJob.objects.filter(user=request.user).order_by('-created')[:20]
        return Response(
            JobResponse([service.refresh_job(job) for job in jobs], many=True).data
        )

    @extend_schema(request=JobRequest, responses=JobResponse)
    def post(self, request):
        """A repeated key never generates another PDF or sends another print."""
        serializer = JobRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = service.create_job(request.user, serializer.validated_data)
        return Response(JobResponse(service.refresh_job(job)).data)


class JobView(LocalPrintView):
    """Poll generation/submission state independently of ordinary report output."""

    @extend_schema(responses=JobResponse)
    def get(self, request, pk):
        """Return the caller's task with conservative timeout handling."""
        return Response(JobResponse(self.get_job(pk)).data)


class PdfView(LocalPrintView):
    """The logged-in browser, not QZ, downloads the protected PDF."""

    @extend_schema(responses=PdfResponse)
    def get(self, request, pk):
        """Supply exact server-owned bytes and fixed QZ options."""
        job = self.get_job(pk)
        return Response(
            {
                'data': base64.b64encode(service.read_pdf(job)).decode(),
                'sha256': job.file_sha256,
                'options': service.print_options(job),
            }
        )


class SignView(LocalPrintView):
    """Only explicit printer enumeration and job-bound PDF operations are signed."""

    @extend_schema(request=SignRequest, responses=SignResponse)
    def post(self, request):
        """Issue at most one print signature for a task, across browser tabs."""
        serializer = SignRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        job = self.get_job(values['job']) if values.get('job') else None
        signature = service.sign_message(request.user, values['message'], job)
        return Response({'signature': signature})


class ResultView(LocalPrintView):
    """Record a client observation without trusting it as hardware evidence."""

    @extend_schema(request=ResultRequest, responses=JobResponse)
    def post(self, request, pk):
        """Allow late acknowledgments but never regress a confirmed submission."""
        job = self.get_job(pk)
        serializer = ResultRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        state = serializer.validated_data['state']
        if job.state not in ('submitting', 'uncertain', 'submitted'):
            raise ValidationError('Task has not been claimed for submission')
        values = {'state': state, 'detail': '', 'updated': timezone.now()}
        if state == 'submitted':
            values['submitted'] = timezone.now()
        LocalPrintJob.objects.filter(
            pk=job.pk, state__in=['submitting', 'uncertain']
        ).update(**values)
        job.refresh_from_db()
        return Response(JobResponse(job).data)


class ReprintView(LocalPrintView):
    """Explicit reprints create linked jobs with fresh idempotency keys."""

    @extend_schema(request=JobRequest, responses=JobResponse)
    def post(self, request, pk):
        """Keep the original objects and copies; permit an explicitly chosen queue."""
        original = self.get_job(pk)
        if original.state not in ('submitted', 'uncertain', 'failed'):
            raise ValidationError('Original task is still active')
        serializer = JobRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if any(
            values[field] != getattr(original, field)
            for field in ('model_type', 'items', 'copies')
        ):
            raise ValidationError('Reprint must retain original items and copies')
        if values.get('item_copies', {}) != original.item_copies:
            raise ValidationError('Reprint must retain original per-item copies')
        job = service.create_job(request.user, values, original=original)
        return Response(JobResponse(service.refresh_job(job)).data)


urlpatterns = [
    path('config/', ConfigView.as_view()),
    path('certificate/', CertificateView.as_view()),
    path('sign/', SignView.as_view()),
    path('jobs/', JobsView.as_view()),
    path('jobs/<uuid:pk>/', JobView.as_view()),
    path('jobs/<uuid:pk>/pdf/', PdfView.as_view()),
    path('jobs/<uuid:pk>/result/', ResultView.as_view()),
    path('jobs/<uuid:pk>/reprint/', ReprintView.as_view()),
]
