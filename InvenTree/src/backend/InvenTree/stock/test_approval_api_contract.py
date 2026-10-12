"""API metadata and authorization contract tests for scan approvals."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from oauth2_provider.models import AccessToken, Application
from rest_framework import status
from rest_framework.test import APIClient

from InvenTree.unit_test import InvenTreeAPITestCase, addUserPermission
from users.oauth2_scopes import DEFAULT_READ, get_granular_scope


class ScanStockApprovalAPIContractTest(InvenTreeAPITestCase):
    """Verify approval endpoint serializers, roles and OAuth scopes."""

    roles = ['stock.view', 'stock.add', 'stock.change']

    def test_native_stock_writes_cannot_bypass_approval(self):
        """Ordinary warehouse accounts must use the approval workflow."""
        from common.settings import get_global_setting, set_global_setting
        from part.models import Part
        from stock.models import StockItem

        previous = get_global_setting('SCAN_APPROVAL_ENABLED', False)
        set_global_setting('SCAN_APPROVAL_ENABLED', True)
        try:
            self.user.is_staff = False
            self.user.is_superuser = False
            self.user.save()
            item = StockItem.objects.create(
                part=Part.objects.create(name='Permission test device'), quantity=5
            )
            for path in (
                '/api/stock/', '/api/stock/add/', '/api/stock/remove/',
                '/api/stock/count/', '/api/stock/transfer/', '/api/stock/wms/imports/',
            ):
                with self.subTest(path=path):
                    self.post(path, {}, expected_code=403)
            self.patch(f'/api/stock/{item.pk}/', {'quantity': 99}, expected_code=403)
            item.refresh_from_db()
            self.assertEqual(item.quantity, 5)
            self.user.is_staff = True
            self.user.save()
            self.post('/api/stock/add/', {}, expected_code=400)
        finally:
            set_global_setting('SCAN_APPROVAL_ENABLED', previous)

    @staticmethod
    def approval_urls():
        """Return one URL for each approval endpoint shape."""
        return {
            'list': reverse('api-stock-scan-request-list'),
            'detail': reverse('api-stock-scan-request-detail', kwargs={'pk': 1}),
            'line': reverse(
                'api-stock-scan-request-line-detail',
                kwargs={'pk': 1, 'event_pk': 1},
            ),
            'submit': reverse('api-stock-scan-request-submit', kwargs={'pk': 1}),
            'cancel': reverse('api-stock-scan-request-cancel', kwargs={'pk': 1}),
            'approve': reverse('api-stock-scan-request-approve', kwargs={'pk': 1}),
            'reject': reverse('api-stock-scan-request-reject', kwargs={'pk': 1}),
        }

    def test_approval_options_expose_method_specific_contracts(self):
        """OPTIONS is stable for list, detail, line and lifecycle endpoints."""
        urls = self.approval_urls()

        list_options = self.options(urls['list'], expected_code=status.HTTP_200_OK)
        self.assertIn('reference', list_options.data['actions']['GET'])
        self.assertNotIn('lines', list_options.data['actions']['GET'])
        self.assertEqual(
            set(list_options.data['actions']['POST']), {'direction', 'location'}
        )

        detail_options = self.options(
            urls['detail'], expected_code=status.HTTP_200_OK
        )
        self.assertIn('lines', detail_options.data['actions']['GET'])

        self.options(urls['line'], expected_code=status.HTTP_200_OK)
        for action in ('submit', 'cancel', 'approve', 'reject'):
            response = self.options(urls[action], expected_code=status.HTTP_200_OK)
            self.assertEqual(set(response.data['actions']['POST']), {'note'})

    def test_scan_options_separate_get_output_from_post_input(self):
        """Scan metadata documents event reads separately from scan writes."""
        response = self.options(
            reverse('api-stock-scan-inbound-v2'), expected_code=status.HTTP_200_OK
        )

        self.assertIn('scanned_at', response.data['actions']['GET'])
        self.assertNotIn('create_part', response.data['actions']['GET'])
        self.assertIn('create_part', response.data['actions']['POST'])
        self.assertNotIn('scanned_at', response.data['actions']['POST'])

    def test_standard_roles_still_gate_request_creation(self):
        """A view-only login cannot create an approval request."""
        self.clearRoles()
        self.assignRole('stock.view')

        self.post(
            reverse('api-stock-scan-request-list'),
            {'direction': 'INBOUND', 'location': 1},
            expected_code=status.HTTP_403_FORBIDDEN,
        )

    def test_approval_only_user_can_read_metadata_and_reach_actions(self):
        """Custom approvers are not forced to hold the stock change role."""
        approver = User.objects.create_user(username='contract-approver')
        addUserPermission(
            approver, 'stock', 'scanstockapprovalrequest', 'approve'
        )
        self.client.force_authenticate(user=approver)
        urls = self.approval_urls()

        self.options(urls['list'], expected_code=status.HTTP_200_OK)
        self.options(urls['detail'], expected_code=status.HTTP_200_OK)
        self.options(urls['approve'], expected_code=status.HTTP_200_OK)
        self.options(urls['reject'], expected_code=status.HTTP_200_OK)

    def test_low_scope_oauth_tokens_are_rejected(self):
        """A valid login role cannot compensate for a weak OAuth token scope."""
        self.logout()
        client = APIClient()
        client.logout()
        client.cookies.clear()
        urls = self.approval_urls()

        for index, (url, method, scope) in enumerate((
            (urls['list'], client.get, DEFAULT_READ),
            (urls['list'], client.post, get_granular_scope('view', 'stock')),
            (urls['line'], client.delete, get_granular_scope('view', 'stock')),
            (urls['submit'], client.post, get_granular_scope('view', 'stock')),
        )):
            raw_token = f'approval-contract-{index}-{method.__name__}-{scope}'
            application = Application.objects.create(
                user=self.user,
                client_type=Application.CLIENT_CONFIDENTIAL,
                authorization_grant_type=Application.GRANT_PASSWORD,
                name=raw_token,
            )
            AccessToken.objects.create(
                user=self.user,
                application=application,
                token=raw_token,
                expires=timezone.now() + timedelta(hours=1),
                scope=scope,
            )
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw_token}')

            response = method(url, {}, format='json')

            with self.subTest(index=index, method=method.__name__, scope=scope):
                request = response.renderer_context['request']
                self.assertEqual(
                    response.status_code,
                    status.HTTP_403_FORBIDDEN,
                    (
                        f'authenticator={type(request.successful_authenticator)!r}; '
                        f'auth={request.auth!r}'
                    ),
                )
