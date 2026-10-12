"""Tests for Enterprise WeChat scan stock notifications."""

import os
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from stock import notifications


class WeComNotificationTest(SimpleTestCase):
    """Exercise robot configuration, payloads, and untrusted text handling."""

    webhook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-not-a-secret'

    @mock.patch.dict(
        os.environ,
        {
            notifications.WECOM_WEBHOOK_ENV: webhook,
            notifications.WECOM_MENTION_ENV: 'approver.one,bad user',
        },
    )
    @mock.patch('stock.notifications.requests.post')
    def test_post_markdown_mentions_valid_configured_users(self, post):
        """A successful response posts Markdown and filters invalid user IDs."""
        post.return_value.ok = True
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'errcode': 0, 'errmsg': 'ok'}

        self.assertTrue(notifications._post_markdown('待审批', mention=True))

        post.assert_called_once_with(
            self.webhook,
            json={
                'msgtype': 'markdown',
                'markdown': {'content': '待审批\n<@approver.one>'},
            },
            timeout=(3.05, 5),
        )

    @mock.patch.dict(
        os.environ, {notifications.WECOM_WEBHOOK_ENV: 'http://127.0.0.1/internal'}
    )
    @mock.patch('stock.notifications.requests.post')
    def test_invalid_webhook_is_never_called(self, post):
        """Only the official HTTPS webhook endpoint can receive notifications."""
        self.assertFalse(notifications._post_markdown('test'))
        post.assert_not_called()

    @mock.patch.dict(os.environ, {notifications.WECOM_WEBHOOK_ENV: webhook})
    @mock.patch('stock.notifications.logger')
    @mock.patch('stock.notifications.requests.post')
    def test_network_failure_does_not_log_webhook_secret(self, post, logger):
        """Request exceptions can contain the URL, which must stay out of logs."""
        post.side_effect = notifications.requests.ConnectionError(
            f'Failed to connect to {self.webhook}'
        )

        self.assertFalse(notifications._post_markdown('test'))

        logger.warning.assert_called_once_with(
            'Enterprise WeChat robot notification failed', error_type='ConnectionError'
        )
        self.assertNotIn(self.webhook, str(logger.mock_calls))

    def test_direct_inbound_message_escapes_scanned_mentions(self):
        """Barcode text cannot inject a group mention or a new Markdown line."""
        event = SimpleNamespace(
            part_name_snapshot='<@all>\n危险商品',
            part_ipn_snapshot='',
            barcode='<@all>',
            operator_display_name_snapshot='仓管员',
            operator_username_snapshot='',
            destination_location_path_snapshot='成品仓',
            applied_quantity=Decimal('1.00000'),
            stock_item_quantity_after=Decimal('8.00000'),
            completed_at=datetime(2026, 9, 2, 8, 30),
        )

        content = notifications._direct_inbound_markdown(event)

        self.assertNotIn('<@all>', content)
        self.assertIn('&lt;@all&gt; 危险商品', content)
        self.assertIn('本次入库: **1**', content)

    @override_settings(SITE_URL='http://10.112.138.98:8088')
    def test_approval_message_contains_summary_and_link(self):
        """Approval notifications contain the actionable request summary."""
        requester = SimpleNamespace(
            get_full_name=lambda: '申请人', get_username=lambda: 'requester'
        )
        request = SimpleNamespace(
            direction='INBOUND',
            reference='SAR-0001',
            requester=requester,
            requester_display_name_snapshot='申请人',
            requester_username_snapshot='requester',
            location_path_snapshot='成品仓 / 待入库',
            submitted_at=datetime(2026, 9, 2, 9, 0),
            decided_at=None,
            cancelled_at=None,
            updated_at=datetime(2026, 9, 2, 9, 0),
            decision_note='',
            cancel_note='',
            get_absolute_url=lambda: '/web/stock/scan/requests/1',
        )

        content = notifications._approval_markdown(
            request, 'submitted', line_count=3, quantity=Decimal('4.00000')
        )

        self.assertIn('库存申请待审批', content)
        self.assertIn('3** 行 / 数量 **4', content)
        self.assertIn(
            '[打开申请单](http://10.112.138.98:8088/web/stock/scan/requests/1)', content
        )

    @mock.patch.dict(os.environ, {notifications.WECOM_WEBHOOK_ENV: webhook})
    @mock.patch('InvenTree.tasks.offload_task')
    def test_approval_queue_uses_background_notification_group(self, offload):
        """Committed approval events are delegated to the background worker."""
        offload.return_value = 'task-id'

        result = notifications.queue_approval_notification(7, 'approved')

        self.assertEqual(result, 'task-id')
        offload.assert_called_once_with(
            'stock.notifications.send_approval_notification',
            7,
            'approved',
            group='notification',
            check_duplicates=False,
        )
