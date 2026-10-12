import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Badge,
  Button,
  Collapse,
  Group,
  Loader,
  Modal,
  Paper,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput
} from '@mantine/core';
import { DateInput } from '@mantine/dates';
import {
  IconChevronDown,
  IconChevronRight,
  IconDownload,
  IconFilterOff,
  IconRefresh
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Fragment, useEffect, useMemo, useState } from 'react';

import { api } from '../../App';
import useDataExport from '../../hooks/UseDataExport';
import type { ScanMode, ScanStockEvent } from './ScanStockTypes';

type EventList = {
  count: number;
  results: ScanStockEvent[];
};

type EventFilters = {
  outcome: string | null;
  minDate: string | null;
  maxDate: string | null;
  operator: string;
  part: string;
  stockItem: string;
  sourceLocation: string;
  destinationLocation: string;
};

const EMPTY_FILTERS: EventFilters = {
  outcome: null,
  minDate: null,
  maxDate: null,
  operator: '',
  part: '',
  stockItem: '',
  sourceLocation: '',
  destinationLocation: ''
};

const PAGE_SIZE = 100;

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function totalQuantity(events: ScanStockEvent[]): string {
  const total = events.reduce((sum, event) => {
    const value =
      event.outcome === 'PENDING'
        ? event.requested_quantity
        : event.applied_quantity;
    const quantity = Number(value);
    return Number.isFinite(quantity) ? sum + quantity : sum;
  }, 0);
  return String(total);
}

export function ScanStockEventTable({ mode }: Readonly<{ mode?: ScanMode }>) {
  const [filters, setFilters] = useState<EventFilters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [selectedEvent, setSelectedEvent] = useState<ScanStockEvent | null>(
    null
  );
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const queryParams = useMemo(
    () => ({
      operation: mode?.toUpperCase(),
      outcome: filters.outcome || undefined,
      min_date: filters.minDate || undefined,
      max_date: filters.maxDate || undefined,
      operator: filters.operator.trim() || undefined,
      part: filters.part.trim() || undefined,
      stock_item: filters.stockItem.trim() || undefined,
      source_location: filters.sourceLocation.trim() || undefined,
      destination_location: filters.destinationLocation.trim() || undefined
    }),
    [filters, mode]
  );

  const events = useQuery<EventList>({
    queryKey: ['scan-stock-events', queryParams, offset],
    queryFn: async () => {
      const response = await api.get<EventList>(
        apiUrl(ApiEndpoints.stock_scan_events),
        { params: { limit: PAGE_SIZE, offset, ...queryParams } }
      );
      return response.data;
    }
  });

  useEffect(() => {
    setOffset(0);
  }, [filters, mode]);

  const exportModal = useDataExport({
    url: apiUrl(ApiEndpoints.stock_scan_events),
    enabled: true,
    filters: queryParams
  });

  const updateFilter = <Key extends keyof EventFilters>(
    key: Key,
    value: EventFilters[Key]
  ) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const eventGroups = useMemo(() => {
    const groups = new Map<string, ScanStockEvent[]>();
    for (const event of events.data?.results ?? []) {
      const key = event.approval_request
        ? `${event.approval_request}:${event.stock_item_pk_snapshot ?? `part-${(event.part_pk_snapshot ?? event.part_ipn_snapshot) || event.pk}`}`
        : `event-${event.pk}`;
      groups.set(key, [...(groups.get(key) ?? []), event]);
    }
    return [...groups.entries()];
  }, [events.data?.results]);

  const toggleGroup = (key: string) => {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (events.isLoading) {
    return (
      <Group justify='center' py='xl'>
        <Loader />
        <Text c='dimmed'>{t`Loading scan records…`}</Text>
      </Group>
    );
  }

  if (events.isError) {
    return <Alert color='red'>{t`Scan records could not be loaded.`}</Alert>;
  }

  return (
    <>
      {exportModal.modal}

      <Paper withBorder p='md' mb='md'>
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
          <Select
            clearable
            label={t`Outcome`}
            placeholder={t`All outcomes`}
            data={[
              { value: 'SUCCESS', label: t`Success` },
              { value: 'FAILED', label: t`Failed` },
              { value: 'PENDING', label: t`Pending` }
            ]}
            value={filters.outcome}
            onChange={(value) => updateFilter('outcome', value)}
          />
          <DateInput
            clearable
            label={t`Scanned after`}
            placeholder={t`Select start date`}
            value={filters.minDate}
            onChange={(value) => updateFilter('minDate', value)}
          />
          <DateInput
            clearable
            label={t`Scanned before`}
            placeholder={t`Select end date`}
            value={filters.maxDate}
            onChange={(value) => updateFilter('maxDate', value)}
          />
          <TextInput
            label={t`Operator ID`}
            inputMode='numeric'
            value={filters.operator}
            onChange={(event) =>
              updateFilter('operator', event.currentTarget.value)
            }
          />
          <TextInput
            label={t`Part ID`}
            inputMode='numeric'
            value={filters.part}
            onChange={(event) =>
              updateFilter('part', event.currentTarget.value)
            }
          />
          <TextInput
            label={t`Stock item ID`}
            inputMode='numeric'
            value={filters.stockItem}
            onChange={(event) =>
              updateFilter('stockItem', event.currentTarget.value)
            }
          />
          <TextInput
            label={t`Source location ID`}
            inputMode='numeric'
            value={filters.sourceLocation}
            onChange={(event) =>
              updateFilter('sourceLocation', event.currentTarget.value)
            }
          />
          <TextInput
            label={t`Destination location ID`}
            inputMode='numeric'
            value={filters.destinationLocation}
            onChange={(event) =>
              updateFilter('destinationLocation', event.currentTarget.value)
            }
          />
        </SimpleGrid>
        <Group justify='flex-end' mt='md'>
          <Button
            variant='default'
            size='xs'
            leftSection={<IconFilterOff size={16} />}
            onClick={() => setFilters(EMPTY_FILTERS)}
          >
            {t`Clear filters`}
          </Button>
        </Group>
      </Paper>

      <Group justify='flex-end' mb='sm'>
        <Button
          variant='subtle'
          size='xs'
          leftSection={<IconDownload size={16} />}
          onClick={() => exportModal.open()}
        >
          {t`Export data`}
        </Button>
        <Button
          variant='subtle'
          size='xs'
          loading={events.isFetching}
          leftSection={<IconRefresh size={16} />}
          onClick={() => events.refetch()}
        >
          {t`Refresh`}
        </Button>
      </Group>
      {events.data?.results.length === 0 ? (
        <Alert variant='light'>{t`No scan records found.`}</Alert>
      ) : (
        <ScrollArea>
          <Table striped highlightOnHover miw={1250}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t`Scanned at`}</Table.Th>
                <Table.Th>{t`Operation`}</Table.Th>
                <Table.Th>{t`Outcome`}</Table.Th>
                <Table.Th>{t`Request`}</Table.Th>
                <Table.Th>{t`Operator`}</Table.Th>
                <Table.Th>{t`Part`}</Table.Th>
                <Table.Th>{t`Stock item`}</Table.Th>
                <Table.Th>{t`Quantity change`}</Table.Th>
                <Table.Th>{t`Location`}</Table.Th>
                <Table.Th>{t`Reason`}</Table.Th>
                <Table.Th>{t`Details`}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {eventGroups.map(([groupKey, group]) => {
                const event = group[0];
                const grouped = group.length > 1;
                const expanded = expandedGroups.has(groupKey);
                return (
                  <Fragment key={groupKey}>
                    <Table.Tr
                      tabIndex={0}
                      style={{ cursor: 'pointer' }}
                      onClick={() =>
                        grouped
                          ? toggleGroup(groupKey)
                          : setSelectedEvent(event)
                      }
                      onKeyDown={(keyboardEvent) => {
                        if (keyboardEvent.key === 'Enter') {
                          grouped
                            ? toggleGroup(groupKey)
                            : setSelectedEvent(event);
                        }
                      }}
                    >
                      <Table.Td>{displayTime(event.scanned_at)}</Table.Td>
                      <Table.Td>
                        {event.operation === 'INBOUND'
                          ? t`Inbound`
                          : t`Outbound`}
                      </Table.Td>
                      <Table.Td>
                        <Badge
                          color={
                            event.outcome === 'SUCCESS'
                              ? 'green'
                              : event.outcome === 'PENDING'
                                ? 'yellow'
                                : 'red'
                          }
                        >
                          {event.replayed
                            ? t`Replayed`
                            : event.outcome === 'SUCCESS'
                              ? t`Success`
                              : event.outcome === 'PENDING'
                                ? event.operation === 'OUTBOUND'
                                  ? t`Frozen`
                                  : t`Pending inbound`
                                : t`Failed`}
                        </Badge>
                      </Table.Td>
                      <Table.Td>
                        <Text fw={600}>
                          {event.approval_request
                            ? `#${event.approval_request}`
                            : t`Direct scan`}
                        </Text>
                        {grouped ? (
                          <Badge size='xs' variant='light'>
                            {group.length} {t`scans`}
                          </Badge>
                        ) : event.request_line_number ? (
                          <Text size='xs' c='dimmed'>
                            {t`Line`} {event.request_line_number}
                          </Text>
                        ) : null}
                      </Table.Td>
                      <Table.Td>
                        <Text>
                          {event.operator_display_name_snapshot ||
                            t`Unknown operator`}
                        </Text>
                        <Text size='xs' c='dimmed'>
                          {event.operator_username_snapshot}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Text fw={600}>{event.part_name_snapshot || '-'}</Text>
                        <Text size='xs' c='dimmed'>
                          {event.part_ipn_snapshot}
                          <br />
                          {t`项目`}：{event.batch_snapshot || '—'} · {t`合同`}：
                          {event.contract_number_snapshot || t`历史未填写`}
                          {event.source_detail && (
                            <>
                              <br />
                              {t`验收`}：
                              {event.acceptance_date_snapshot || t`待验收`} ·{' '}
                              {event.warranty_months_snapshot} {t`个月`} ·{' '}
                              {event.warranty_end_snapshot || '—'}
                            </>
                          )}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        {event.stock_item_pk_snapshot
                          ? `#${event.stock_item_pk_snapshot}`
                          : '-'}
                      </Table.Td>
                      <Table.Td>
                        {event.operation === 'OUTBOUND' ? '-' : '+'}
                        {grouped
                          ? totalQuantity(group)
                          : event.outcome === 'PENDING'
                            ? event.requested_quantity
                            : event.applied_quantity}
                        {event.stock_item_quantity_before !== null && (
                          <Text size='xs' c='dimmed'>
                            {event.stock_item_quantity_before} →{' '}
                            {event.stock_item_quantity_after}
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        {event.source_location_path_snapshot ||
                          event.destination_location_path_snapshot ||
                          '-'}
                      </Table.Td>
                      <Table.Td>
                        {event.terminal_reason || event.error_message || '-'}
                      </Table.Td>
                      <Table.Td>
                        <Button
                          size='compact-xs'
                          variant='subtle'
                          leftSection={
                            grouped ? (
                              expanded ? (
                                <IconChevronDown size={14} />
                              ) : (
                                <IconChevronRight size={14} />
                              )
                            ) : undefined
                          }
                          onClick={(clickEvent) => {
                            clickEvent.stopPropagation();
                            grouped
                              ? toggleGroup(groupKey)
                              : setSelectedEvent(event);
                          }}
                        >
                          {grouped
                            ? expanded
                              ? t`Collapse`
                              : t`Expand`
                            : t`View`}
                        </Button>
                      </Table.Td>
                    </Table.Tr>
                    {grouped && (
                      <Table.Tr key={`${groupKey}-details`}>
                        <Table.Td colSpan={11} p={0}>
                          <Collapse expanded={expanded}>
                            <Stack gap='xs' p='sm'>
                              {group.map((line) => (
                                <Group
                                  key={line.pk}
                                  justify='space-between'
                                  wrap='nowrap'
                                >
                                  <Text size='sm'>
                                    {t`Line`} {line.request_line_number ?? '-'}{' '}
                                    · {displayTime(line.scanned_at)} ·{' '}
                                    {line.requested_quantity}
                                  </Text>
                                  <Button
                                    size='compact-xs'
                                    variant='subtle'
                                    onClick={() => setSelectedEvent(line)}
                                  >
                                    {t`View scan`}
                                  </Button>
                                </Group>
                              ))}
                            </Stack>
                          </Collapse>
                        </Table.Td>
                      </Table.Tr>
                    )}
                  </Fragment>
                );
              })}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      )}

      {(events.data?.count ?? 0) > 0 && (
        <Group justify='space-between' mt='sm'>
          <Text size='sm' c='dimmed'>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, events.data?.count ?? 0)}{' '}
            / {events.data?.count ?? 0}
          </Text>
          <Group gap='xs'>
            <Button
              size='xs'
              variant='default'
              disabled={offset === 0}
              onClick={() =>
                setOffset((current) => Math.max(0, current - PAGE_SIZE))
              }
            >
              {t`Previous`}
            </Button>
            <Button
              size='xs'
              variant='default'
              disabled={offset + PAGE_SIZE >= (events.data?.count ?? 0)}
              onClick={() => setOffset((current) => current + PAGE_SIZE)}
            >
              {t`Next`}
            </Button>
          </Group>
        </Group>
      )}

      <Modal
        opened={selectedEvent !== null}
        onClose={() => setSelectedEvent(null)}
        title={t`Scan event details`}
        size='lg'
      >
        {selectedEvent && (
          <Table withRowBorders={false}>
            <Table.Tbody>
              {[
                [t`Event identifier`, selectedEvent.client_event_id],
                [
                  t`Approval request`,
                  selectedEvent.approval_request
                    ? `#${selectedEvent.approval_request}`
                    : '-'
                ],
                [t`Request line`, selectedEvent.request_line_number ?? '-'],
                [t`Barcode`, selectedEvent.barcode],
                [t`Barcode type`, selectedEvent.barcode_kind],
                [t`Error code`, selectedEvent.error_code || '-'],
                [t`Error message`, selectedEvent.error_message || '-'],
                [t`Terminal reason`, selectedEvent.terminal_reason || '-'],
                [t`Requested quantity`, selectedEvent.requested_quantity],
                [t`Applied quantity`, selectedEvent.applied_quantity],
                [
                  t`Stock item quantity`,
                  `${selectedEvent.stock_item_quantity_before ?? '-'} → ${selectedEvent.stock_item_quantity_after ?? '-'}`
                ],
                [
                  t`Part total quantity`,
                  `${selectedEvent.part_total_before ?? '-'} → ${selectedEvent.part_total_after ?? '-'}`
                ],
                [
                  t`Source location`,
                  selectedEvent.source_location_path_snapshot || '-'
                ],
                [
                  t`Destination location`,
                  selectedEvent.destination_location_path_snapshot || '-'
                ],
                [t`Device`, selectedEvent.device_id || '-'],
                [t`Session`, selectedEvent.session_id || '-'],
                [t`Replay count`, selectedEvent.replay_count],
                [
                  t`Tracking entries`,
                  selectedEvent.tracking_entries.length > 0
                    ? selectedEvent.tracking_entries
                        .map((entry) => `#${entry}`)
                        .join(', ')
                    : '-'
                ],
                [t`Scanned at`, displayTime(selectedEvent.scanned_at)],
                [
                  t`Completed at`,
                  selectedEvent.completed_at
                    ? displayTime(selectedEvent.completed_at)
                    : '-'
                ]
              ].map(([label, value]) => (
                <Table.Tr key={String(label)}>
                  <Table.Td w='35%'>
                    <Text fw={600}>{label}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text
                      ff={
                        label === t`Event identifier` ? 'monospace' : undefined
                      }
                    >
                      {value}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Modal>
    </>
  );
}
