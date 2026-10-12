import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Badge,
  Button,
  Card,
  Divider,
  Group,
  Loader,
  Modal,
  ScrollArea,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Textarea,
  Timeline,
  Title
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import {
  IconAlertCircle,
  IconBan,
  IconCheck,
  IconClock,
  IconSend,
  IconTrash,
  IconX
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { api, queryClient } from '../../App';
import { useUserState } from '../../states/UserState';
import type {
  ScanApprovalActionResponse,
  ScanApprovalStatus,
  ScanStockApprovalRequest,
  ScanStockEvent
} from './ScanStockTypes';

type DecisionAction = 'approve' | 'reject' | 'cancel';

function displayTime(value?: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function approvalStatusLabel(status: ScanApprovalStatus): string {
  switch (status) {
    case 'DRAFT':
      return t`Draft`;
    case 'PENDING_APPROVAL':
      return t`Pending approval`;
    case 'APPLYING':
      return t`Applying`;
    case 'COMPLETED':
      return t`Completed`;
    case 'REJECTED':
      return t`Rejected`;
    case 'CANCELLED':
      return t`Cancelled`;
    case 'EXPIRED':
      return t`Expired`;
  }
}

export function approvalStatusColor(status: ScanApprovalStatus): string {
  switch (status) {
    case 'COMPLETED':
      return 'green';
    case 'PENDING_APPROVAL':
    case 'APPLYING':
      return 'yellow';
    case 'REJECTED':
    case 'CANCELLED':
    case 'EXPIRED':
      return 'red';
    default:
      return 'blue';
  }
}

export function approvalOwnerLabel(request: ScanStockApprovalRequest): string {
  const snapshot = request.approval_owner_snapshot;
  if (typeof snapshot === 'string') return snapshot;
  return snapshot?.name || t`Not assigned`;
}

export function approvalLocationLabel(
  request: ScanStockApprovalRequest
): string {
  return request.location_path_snapshot || `#${request.location}`;
}

export function approvalRequestLines(
  request?: ScanStockApprovalRequest
): ScanStockEvent[] {
  return request?.lines ?? [];
}

function actionErrorData(error: any): Partial<ScanApprovalActionResponse> {
  return error?.response?.data ?? error?.data ?? {};
}

function actionEndpoint(action: DecisionAction, requestId: number): string {
  const endpoint =
    action === 'approve'
      ? ApiEndpoints.stock_scan_request_approve
      : action === 'reject'
        ? ApiEndpoints.stock_scan_request_reject
        : ApiEndpoints.stock_scan_request_cancel;
  return apiUrl(endpoint, undefined, { id: requestId });
}

function TimelineEntry({
  label,
  value,
  color = 'blue'
}: Readonly<{ label: string; value?: string | null; color?: string }>) {
  if (!value) return null;
  return (
    <Timeline.Item title={label} color={color} bullet={<IconClock size={13} />}>
      <Text size='sm' c='dimmed'>
        {displayTime(value)}
      </Text>
    </Timeline.Item>
  );
}

export function ScanApprovalRequestDetails({
  requestId,
  initialRequest,
  onChanged,
  hideSubmit = false
}: Readonly<{
  requestId: number;
  initialRequest?: ScanStockApprovalRequest;
  onChanged?: (request: ScanStockApprovalRequest) => void;
  hideSubmit?: boolean;
}>) {
  const currentUserId = useUserState((state) => state.userId());
  const [processing, setProcessing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [decisionAction, setDecisionAction] = useState<DecisionAction | null>(
    null
  );
  const [decisionNote, setDecisionNote] = useState('');

  const requestQuery = useQuery<ScanStockApprovalRequest>({
    queryKey: ['scan-stock-request', requestId],
    queryFn: async () => {
      const response = await api.get<ScanStockApprovalRequest>(
        apiUrl(ApiEndpoints.stock_scan_request_detail, undefined, {
          id: requestId
        })
      );
      return response.data;
    },
    initialData: initialRequest
  });

  const request = requestQuery.data;
  const lines = approvalRequestLines(request);

  const refreshRelatedData = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['scan-stock-requests'] }),
      queryClient.invalidateQueries({ queryKey: ['scan-stock-events'] }),
      queryClient.invalidateQueries({ queryKey: ['scan-stock-active-draft'] }),
      queryClient.invalidateQueries({ queryKey: ['scan-stock-request'] }),
      queryClient.invalidateQueries({ queryKey: ['table-data'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-project-options'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-options'] })
    ]);
  };

  const submitRequest = async () => {
    if (!request) return;
    setProcessing(true);
    setActionError(null);
    try {
      const response = await api.post<ScanStockApprovalRequest>(
        apiUrl(ApiEndpoints.stock_scan_request_submit, undefined, {
          id: request.pk
        })
      );
      queryClient.setQueryData(
        ['scan-stock-request', request.pk],
        response.data
      );
      await refreshRelatedData();
      onChanged?.(response.data);
      notifications.show({
        color: 'green',
        message: t`Approval request submitted.`
      });
    } catch (error) {
      const data = actionErrorData(error);
      setActionError(
        data.error_message || t`The request could not be submitted.`
      );
      await requestQuery.refetch();
    } finally {
      setProcessing(false);
    }
  };

  const performDecision = async () => {
    if (!request || !decisionAction) return;
    if (
      (decisionAction === 'reject' ||
        (decisionAction === 'cancel' &&
          request.status === 'PENDING_APPROVAL')) &&
      !decisionNote.trim()
    ) {
      setActionError(t`A reason is required.`);
      return;
    }

    setProcessing(true);
    setActionError(null);
    try {
      const payload = { note: decisionNote.trim() };
      const response = await api.post<ScanStockApprovalRequest>(
        actionEndpoint(decisionAction, request.pk),
        payload
      );
      queryClient.setQueryData(
        ['scan-stock-request', request.pk],
        response.data
      );
      await refreshRelatedData();
      onChanged?.(response.data);
      setDecisionAction(null);
      setDecisionNote('');
      notifications.show({
        color: 'green',
        message:
          decisionAction === 'approve'
            ? t`Approval request completed.`
            : decisionAction === 'reject'
              ? t`Approval request rejected.`
              : t`Approval request cancelled.`
      });
    } catch (error) {
      const data = actionErrorData(error);
      const conflict = (error as any)?.response?.status === 409;
      setActionError(
        data.error_message ||
          (conflict
            ? t`The request changed on the server. Its latest status has been loaded.`
            : t`The request action failed.`)
      );
      await requestQuery.refetch();
      await refreshRelatedData();
      if (conflict) {
        setDecisionAction(null);
        setDecisionNote('');
      }
    } finally {
      setProcessing(false);
    }
  };

  const removeLine = async (line: ScanStockEvent) => {
    if (!request) return;
    setProcessing(true);
    setActionError(null);
    try {
      const response = await api.delete<ScanStockApprovalRequest>(
        apiUrl(ApiEndpoints.stock_scan_request_lines, undefined, {
          id: request.pk,
          event_pk: line.pk
        })
      );
      queryClient.setQueryData(
        ['scan-stock-request', request.pk],
        response.data
      );
      await refreshRelatedData();
      onChanged?.(response.data);
    } catch (error) {
      const data = actionErrorData(error);
      setActionError(
        data.error_message || t`The request line could not be removed.`
      );
      await requestQuery.refetch();
    } finally {
      setProcessing(false);
    }
  };

  if (requestQuery.isError) {
    return (
      <Alert color='red'>{t`The approval request could not be loaded.`}</Alert>
    );
  }

  if (requestQuery.isLoading || !request) {
    return (
      <Group justify='center' py='xl'>
        <Loader />
        <Text c='dimmed'>{t`Loading approval request…`}</Text>
      </Group>
    );
  }

  const isOwnRequest = request.requester === currentUserId;
  const canSubmit =
    request.status === 'DRAFT' && isOwnRequest && request.active_line_count > 0;
  const canCancel =
    ['DRAFT', 'PENDING_APPROVAL'].includes(request.status) &&
    request.can_cancel;
  const canApprove =
    request.status === 'PENDING_APPROVAL' &&
    !isOwnRequest &&
    request.can_approve;
  const canReject =
    request.status === 'PENDING_APPROVAL' &&
    !isOwnRequest &&
    request.can_approve;

  return (
    <Stack gap='lg'>
      <Group justify='space-between' align='flex-start'>
        <div>
          <Group gap='xs'>
            <Title order={2}>{request.reference}</Title>
            <Badge color={approvalStatusColor(request.status)}>
              {approvalStatusLabel(request.status)}
            </Badge>
          </Group>
          <Text c='dimmed'>{approvalLocationLabel(request)}</Text>
        </div>
        <Group gap='xs'>
          {canSubmit && !hideSubmit && (
            <Button
              leftSection={<IconSend size={16} />}
              loading={processing}
              onClick={() => void submitRequest()}
            >
              {t`Submit for approval`}
            </Button>
          )}
          {canApprove && (
            <Button
              color='green'
              leftSection={<IconCheck size={16} />}
              onClick={() => {
                setActionError(null);
                setDecisionAction('approve');
              }}
            >
              {t`Approve`}
            </Button>
          )}
          {canReject && (
            <Button
              color='red'
              variant='light'
              leftSection={<IconX size={16} />}
              onClick={() => {
                setActionError(null);
                setDecisionAction('reject');
              }}
            >
              {t`Reject`}
            </Button>
          )}
          {canCancel && (
            <Button
              color='red'
              variant='subtle'
              leftSection={<IconBan size={16} />}
              onClick={() => {
                setActionError(null);
                setDecisionAction('cancel');
              }}
            >
              {t`Cancel request`}
            </Button>
          )}
        </Group>
      </Group>

      {actionError && decisionAction === null && (
        <Alert color='red' icon={<IconAlertCircle size={18} />}>
          {actionError}
        </Alert>
      )}
      {request.last_approval_error && (
        <Alert color='orange' title={t`Last approval error`}>
          {request.last_approval_error}
        </Alert>
      )}

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
        <Card withBorder>
          <Text size='xs' c='dimmed'>
            {t`Direction`}
          </Text>
          <Text fw={600}>
            {request.direction === 'INBOUND' ? t`Inbound` : t`Outbound`}
          </Text>
        </Card>
        <Card withBorder>
          <Text size='xs' c='dimmed'>
            {t`Requester`}
          </Text>
          <Text fw={600}>
            {request.requester_display_name_snapshot ||
              request.requester_username_snapshot}
          </Text>
        </Card>
        <Card withBorder>
          <Text size='xs' c='dimmed'>
            {t`Approval route`}
          </Text>
          <Text fw={600}>{approvalOwnerLabel(request)}</Text>
        </Card>
        <Card withBorder>
          <Text size='xs' c='dimmed'>
            {request.direction === 'OUTBOUND'
              ? t`Frozen quantity`
              : t`Pending inbound quantity`}
          </Text>
          <Text fw={600}>
            {request.direction === 'OUTBOUND'
              ? (request.scan_frozen ?? request.total_quantity)
              : (request.pending_inbound ?? request.total_quantity)}
          </Text>
          <Text size='xs' c='dimmed'>
            {request.active_line_count} {t`lines`}
          </Text>
        </Card>
      </SimpleGrid>

      <Divider label={t`Request timeline`} />
      <Timeline active={4} bulletSize={24} lineWidth={2}>
        <TimelineEntry label={t`Created`} value={request.created_at} />
        <TimelineEntry label={t`Submitted`} value={request.submitted_at} />
        <TimelineEntry
          label={t`Decided`}
          value={request.decided_at}
          color={request.status === 'COMPLETED' ? 'green' : 'red'}
        />
        <TimelineEntry
          label={t`Cancelled`}
          value={request.cancelled_at}
          color='red'
        />
        <TimelineEntry
          label={t`Expires`}
          value={request.expires_at}
          color='orange'
        />
      </Timeline>

      {(request.decision_note || request.cancel_note) && (
        <Alert variant='light'>
          {request.decision_note || request.cancel_note}
        </Alert>
      )}

      <Divider label={t`Request lines`} />
      {lines.length === 0 ? (
        <Alert variant='light'>{t`This request has no active lines.`}</Alert>
      ) : (
        <ScrollArea>
          <Table striped highlightOnHover miw={820}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t`Line`}</Table.Th>
                <Table.Th>{t`Product`}</Table.Th>
                <Table.Th>{t`Stock item`}</Table.Th>
                <Table.Th>{t`项目`}</Table.Th>
                <Table.Th>{t`单位`}</Table.Th>
                <Table.Th>{t`用途`}</Table.Th>
                <Table.Th>{t`备注`}</Table.Th>
                <Table.Th>{t`Quantity`}</Table.Th>
                <Table.Th>{t`Request state`}</Table.Th>
                <Table.Th>{t`Reason`}</Table.Th>
                <Table.Th>{t`Actions`}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {lines.map((line) => (
                <Table.Tr key={line.pk}>
                  <Table.Td>{line.request_line_number ?? '-'}</Table.Td>
                  <Table.Td>
                    <Text fw={600}>{line.part_name_snapshot || '-'}</Text>
                    <Text size='xs' c='dimmed'>
                      {line.part_ipn_snapshot}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {line.stock_item_pk_snapshot
                      ? `#${line.stock_item_pk_snapshot}`
                      : '-'}
                  </Table.Td>
                  <Table.Td>
                    {line.batch_snapshot || '-'}
                    <Text size='sm'>
                      {t`合同`}：
                      {line.contract_number_snapshot || t`历史未填写`}
                    </Text>
                    {line.source_detail && (
                      <Text size='xs'>
                        {t`验收`}：{line.acceptance_date_snapshot || t`待验收`}{' '}
                        · {line.warranty_months_snapshot} {t`个月`} ·{' '}
                        {line.warranty_end_snapshot || '—'}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{line.units_snapshot}</Table.Td>
                  <Table.Td>{line.purpose}</Table.Td>
                  <Table.Td>{line.remark}</Table.Td>
                  <Table.Td>{line.requested_quantity}</Table.Td>
                  <Table.Td>
                    <Badge
                      color={
                        line.outcome === 'SUCCESS'
                          ? 'green'
                          : line.outcome === 'PENDING'
                            ? 'yellow'
                            : 'red'
                      }
                    >
                      {line.outcome === 'PENDING'
                        ? request.direction === 'OUTBOUND'
                          ? t`Frozen`
                          : t`Pending inbound`
                        : line.outcome === 'SUCCESS'
                          ? t`Applied`
                          : t`Terminal`}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{line.terminal_reason || '-'}</Table.Td>
                  <Table.Td>
                    {request.status === 'DRAFT' &&
                    isOwnRequest &&
                    line.outcome === 'PENDING' ? (
                      <Button
                        size='compact-xs'
                        color='red'
                        variant='subtle'
                        loading={processing}
                        leftSection={<IconTrash size={14} />}
                        onClick={() => void removeLine(line)}
                      >
                        {t`Remove`}
                      </Button>
                    ) : (
                      '-'
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      )}

      <Modal
        opened={decisionAction !== null}
        onClose={() => {
          if (!processing) {
            setDecisionAction(null);
            setDecisionNote('');
          }
        }}
        title={
          decisionAction === 'approve'
            ? t`Approve request`
            : decisionAction === 'reject'
              ? t`Reject request`
              : t`Cancel request`
        }
        centered
        closeOnClickOutside={!processing}
      >
        <Stack>
          {actionError && (
            <Alert color='red' icon={<IconAlertCircle size={18} />}>
              {actionError}
            </Alert>
          )}
          <Alert
            color={decisionAction === 'approve' ? 'green' : 'red'}
            icon={<IconAlertCircle size={18} />}
          >
            {decisionAction === 'approve'
              ? t`All request lines will be applied in one transaction.`
              : t`Outbound reservations will be released and this request cannot be restored.`}
          </Alert>
          <Textarea
            required={
              decisionAction === 'reject' ||
              (decisionAction === 'cancel' &&
                request.status === 'PENDING_APPROVAL')
            }
            label={decisionAction === 'approve' ? t`Decision note` : t`Reason`}
            value={decisionNote}
            onChange={(event) => {
              setDecisionNote(event.currentTarget.value);
              setActionError(null);
            }}
            minRows={3}
          />
          <Group justify='flex-end'>
            <Button
              variant='default'
              disabled={processing}
              onClick={() => {
                setDecisionAction(null);
                setDecisionNote('');
              }}
            >
              {t`Back`}
            </Button>
            <Button
              color={decisionAction === 'approve' ? 'green' : 'red'}
              loading={processing}
              onClick={() => void performDecision()}
            >
              {t`Confirm`}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
