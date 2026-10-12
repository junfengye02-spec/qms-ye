import { t } from '@lingui/core/macro';
import { Alert, Badge, ScrollArea, Stack, Table, Text } from '@mantine/core';
import { IconAlertCircle, IconCheck } from '@tabler/icons-react';

import type { ScanSessionResult } from './ScanStockTypes';

function displayTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ScanResultList({
  results
}: Readonly<{ results: ScanSessionResult[] }>) {
  if (results.length === 0) {
    return (
      <Alert variant='light' color='blue'>
        {t`Scan results from this session will appear here.`}
      </Alert>
    );
  }

  return (
    <ScrollArea>
      <Table striped highlightOnHover miw={760}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t`Time`}</Table.Th>
            <Table.Th>{t`Result`}</Table.Th>
            <Table.Th>{t`Barcode`}</Table.Th>
            <Table.Th>{t`Part`}</Table.Th>
            <Table.Th>{t`Quantity`}</Table.Th>
            <Table.Th>{t`Message`}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {results.map((result) => (
            <Table.Tr key={result.id}>
              <Table.Td>{displayTime(result.event?.scanned_at)}</Table.Td>
              <Table.Td>
                <Badge
                  color={result.success ? 'green' : 'red'}
                  leftSection={
                    result.success ? (
                      <IconCheck size={12} />
                    ) : (
                      <IconAlertCircle size={12} />
                    )
                  }
                >
                  {result.replayed
                    ? t`Replayed`
                    : result.success
                      ? result.event?.outcome === 'PENDING'
                        ? result.event.operation === 'OUTBOUND'
                          ? t`Frozen`
                          : t`Pending inbound`
                        : t`Success`
                      : t`Failed`}
                </Badge>
              </Table.Td>
              <Table.Td>
                <Text ff='monospace'>{result.barcode}</Text>
              </Table.Td>
              <Table.Td>
                <Stack gap={0}>
                  <Text fw={600}>
                    {result.event?.part_name_snapshot || '-'}
                  </Text>
                  <Text size='xs' c='dimmed'>
                    {result.event?.part_ipn_snapshot}
                  </Text>
                </Stack>
              </Table.Td>
              <Table.Td>
                {result.event?.operation === 'OUTBOUND' ? '-' : '+'}
                {result.event?.outcome === 'PENDING'
                  ? result.event.requested_quantity
                  : (result.event?.applied_quantity ?? '0')}
              </Table.Td>
              <Table.Td>{result.message}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </ScrollArea>
  );
}
