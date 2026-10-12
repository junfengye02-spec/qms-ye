import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  Paper,
  ScrollArea,
  Select,
  Table,
  Text
} from '@mantine/core';
import { IconRefresh } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { api } from '../../App';
import {
  ScanApprovalRequestDetails,
  approvalLocationLabel,
  approvalOwnerLabel,
  approvalStatusColor,
  approvalStatusLabel
} from './ScanApprovalRequestDetails';
import type {
  ScanApprovalStatus,
  ScanMode,
  ScanStockApprovalRequest,
  ScanStockApprovalRequestList
} from './ScanStockTypes';

type RequestScope = 'mine' | 'approval' | 'history';

function displayTime(value?: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ScanApprovalRequestListPanel({
  scope,
  mode
}: Readonly<{ scope: RequestScope; mode?: ScanMode }>) {
  const [status, setStatus] = useState<ScanApprovalStatus | null>(
    scope === 'approval' ? 'PENDING_APPROVAL' : null
  );
  const [selected, setSelected] = useState<ScanStockApprovalRequest | null>(
    null
  );

  const params = {
    mine: scope === 'mine' || undefined,
    pending_for_me: scope === 'approval' || undefined,
    reviewed_by_me: scope === 'history' || undefined,
    status: status || undefined,
    direction: mode?.toUpperCase(),
    limit: 100
  };
  const requests = useQuery<ScanStockApprovalRequestList>({
    queryKey: ['scan-stock-requests', params],
    queryFn: async () => {
      const response = await api.get<ScanStockApprovalRequestList>(
        apiUrl(ApiEndpoints.stock_scan_requests),
        { params }
      );
      return response.data;
    }
  });

  return (
    <>
      <Paper withBorder p='md' mb='md'>
        <Group justify='space-between'>
          <Select
            clearable={scope !== 'approval'}
            disabled={scope === 'approval'}
            label={t`Request status`}
            placeholder={t`All statuses`}
            data={[
              { value: 'DRAFT', label: t`Draft` },
              { value: 'PENDING_APPROVAL', label: t`Pending approval` },
              { value: 'COMPLETED', label: t`Completed` },
              { value: 'REJECTED', label: t`Rejected` },
              { value: 'CANCELLED', label: t`Cancelled` },
              { value: 'EXPIRED', label: t`Expired` }
            ]}
            value={status}
            onChange={(value) => setStatus(value as ScanApprovalStatus | null)}
          />
          <Button
            variant='subtle'
            loading={requests.isFetching}
            leftSection={<IconRefresh size={16} />}
            onClick={() => void requests.refetch()}
          >
            {t`Refresh`}
          </Button>
        </Group>
      </Paper>

      {requests.isLoading ? (
        <Group justify='center' py='xl'>
          <Loader />
          <Text c='dimmed'>{t`Loading approval requests…`}</Text>
        </Group>
      ) : requests.isError ? (
        <Alert color='red'>{t`Approval requests could not be loaded.`}</Alert>
      ) : requests.data?.results.length === 0 ? (
        <Alert variant='light'>
          {scope === 'approval'
            ? t`There are no requests waiting for your approval.`
            : t`No approval requests found.`}
        </Alert>
      ) : (
        <ScrollArea>
          <Table striped highlightOnHover miw={1000}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t`Request`}</Table.Th>
                <Table.Th>{t`Status`}</Table.Th>
                <Table.Th>{t`Direction`}</Table.Th>
                <Table.Th>{t`Location`}</Table.Th>
                <Table.Th>{t`项目 / 合同`}</Table.Th>
                <Table.Th>{t`验收 / 质保月数 / 到期`}</Table.Th>
                <Table.Th>{t`Requester`}</Table.Th>
                <Table.Th>{t`Approval route`}</Table.Th>
                <Table.Th>{t`Lines`}</Table.Th>
                <Table.Th>{t`Quantity`}</Table.Th>
                <Table.Th>{t`Expires`}</Table.Th>
                <Table.Th>{t`Details`}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {requests.data?.results.map((request) => (
                <Table.Tr key={request.pk}>
                  <Table.Td>
                    <Text fw={600}>{request.reference}</Text>
                    <Text size='xs' c='dimmed'>
                      {displayTime(request.created_at)}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={approvalStatusColor(request.status)}>
                      {approvalStatusLabel(request.status)}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    {request.direction === 'INBOUND' ? t`Inbound` : t`Outbound`}
                  </Table.Td>
                  <Table.Td>{approvalLocationLabel(request)}</Table.Td>
                  <Table.Td>
                    <Text>{request.business_summary?.project || '—'}</Text>
                    <Text size='sm'>
                      {request.business_summary?.contract || t`未填写`}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size='sm'>
                      {request.business_summary?.acceptance_date || '—'} /{' '}
                      {request.business_summary?.warranty_months || '—'} /{' '}
                      {request.business_summary?.warranty_end || '—'}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {request.requester_display_name_snapshot ||
                      request.requester_username_snapshot}
                  </Table.Td>
                  <Table.Td>{approvalOwnerLabel(request)}</Table.Td>
                  <Table.Td>{request.active_line_count}</Table.Td>
                  <Table.Td>{request.total_quantity}</Table.Td>
                  <Table.Td>{displayTime(request.expires_at)}</Table.Td>
                  <Table.Td>
                    <Button
                      size='compact-xs'
                      variant='subtle'
                      onClick={() => setSelected(request)}
                    >
                      {t`Open`}
                    </Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      )}

      <Modal
        opened={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.reference ?? t`Approval request`}
        size='90%'
      >
        {selected && (
          <ScanApprovalRequestDetails
            requestId={selected.pk}
            initialRequest={selected}
            onChanged={(request) => {
              setSelected(request);
              void requests.refetch();
            }}
          />
        )}
      </Modal>
    </>
  );
}
