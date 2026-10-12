import { ModelType } from '@lib/enums/ModelType';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Checkbox,
  FileButton,
  Group,
  Loader,
  Modal,
  Pagination,
  ScrollArea,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput
} from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../App';
import { useUserState } from '../../states/UserState';

import type { ScanStockStatus } from './ScanStockTypes';

export const wmsUrl = (resource: string) => `/api/stock/wms/${resource}/`;
export type WmsOptions = {
  locations: { value: string; label: string }[];
  categories: { value: string; label: string }[];
  projects: string[];
  scanned_part?: { pk: number; IPN: string; name: string } | null;
  stock_status?: ScanStockStatus;
};
type Row = Record<string, any>;
type Column = [string, string];
export const equipmentColumns = (): Column[] => [
  ['IPN', t`设备ID`],
  ['name', t`设备名称`],
  ['brand', t`品牌`],
  ['category', t`分类`],
  ['model_number', t`型号`],
  ['units', t`单位`],
  ['specification', t`规格`],
  ['notes', t`设备备注`]
];
function displayTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(new Date(value));
}
export function cellText(key: string, value: any): string {
  if (value === null || value === undefined || value === '') return '—';
  if (key === 'active') return value ? t`启用` : t`停用`;
  if (['completed_at', 'changed_at'].includes(key)) return displayTime(value);
  if (Array.isArray(value)) return value.join('；') || '—';
  if (typeof value === 'object') return JSON.stringify(value);
  if (['quantity', 'frozen', 'available', 'delta', 'before'].includes(key))
    return String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
  return String(value);
}
export function wmsError(error: any): string {
  const data = error?.response?.data ?? error?.data;
  const messages: Record<string, string> = {
    INSUFFICIENT_UNALLOCATED_STOCK: t`可用库存不足，未扣减库存。请核对项目、库位和出库数量。`,
    IDEMPOTENCY_CONFLICT: t`本次操作编号已用于其他请求，请重新扫码。`,
    LOCATION_REQUIRED: t`请选择库存地点。`,
    PART_NOT_FOUND: t`未找到设备，请核对设备ID。`
  };
  if (data?.error_code) {
    return (
      messages[data.error_code] ?? t`操作未完成，请核对设备、项目和库位后重试。`
    );
  }
  // Only render error messages, never the response payload or event snapshots.
  const detail = data?.detail ?? data?.error_message ?? data;
  if (typeof detail === 'string' && !/^[\s]*[<{[]/.test(detail)) return detail;
  if (Array.isArray(detail) && detail.every((item) => typeof item === 'string'))
    return detail.join('；');
  return t`操作失败，请核对填写内容后重试`;
}
export async function wmsDownload(resource: string, params: Row) {
  const response = await api.get(wmsUrl(resource), {
    params,
    responseType: 'blob'
  });
  const url = URL.createObjectURL(response.data);
  const link = document.createElement('a');
  link.href = url;
  link.download = `wms-${resource}-${params.kind ?? 'export'}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}
function DataRows({
  rows,
  columns,
  edit
}: {
  rows: Row[];
  columns: Column[];
  edit?: (row: Row) => void;
}) {
  return (
    <ScrollArea>
      <Table striped highlightOnHover withTableBorder miw={900}>
        <Table.Thead>
          <Table.Tr>
            {columns.map(([key, label]) => (
              <Table.Th key={key}>{label}</Table.Th>
            ))}
            {edit && <Table.Th>{t`操作`}</Table.Th>}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row, index) => (
            <Table.Tr key={`${row.pk ?? row.row}-${index}`}>
              {columns.map(([key]) => (
                <Table.Td
                  key={key}
                  style={{ maxWidth: 300, overflowWrap: 'anywhere' }}
                >
                  {['before', 'after'].includes(key) &&
                  row[key] &&
                  typeof row[key] === 'object' ? (
                    <Stack gap={4}>
                      {equipmentColumns()
                        .filter(([field]) => Object.hasOwn(row[key], field))
                        .map(([field, label]) => (
                          <Text key={field} size='sm'>
                            {label}：{cellText(field, row[key][field])}
                          </Text>
                        ))}
                    </Stack>
                  ) : (
                    cellText(key, row[key])
                  )}
                </Table.Td>
              ))}
              {edit && (
                <Table.Td>
                  <Button
                    size='xs'
                    variant='light'
                    onClick={() => edit(row)}
                  >{t`编辑`}</Button>
                </Table.Td>
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {rows.length === 0 && (
        <Text p='lg' c='dimmed'>{t`没有符合条件的记录`}</Text>
      )}
    </ScrollArea>
  );
}
export function chinaDay(value = new Date()) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).format(value);
}
export function WmsDataPanel({
  resource
}: {
  resource: 'inventory' | 'parts' | 'records';
}) {
  const user = useUserState();
  const [filters, setFilters] = useState<Row>({
    view: 'summary',
    include_children: 'true',
    operation: 'INBOUND',
    start: `${chinaDay().slice(0, 7)}-01`,
    end: chinaDay()
  });
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const change = (key: string, value: any) => {
    setFilters({ ...filters, [key]: value });
    setPage(1);
  };
  const options = useQuery<WmsOptions>({
    queryKey: ['wms-options'],
    queryFn: async () => (await api.get(wmsUrl('options'))).data
  });
  const query = useQuery<{ count: number; results: Row[]; statistics: Row }>({
    queryKey: ['wms', resource, filters, page],
    queryFn: async () =>
      (
        await api.get(wmsUrl(resource), {
          params: { ...filters, offset: (page - 1) * 50 }
        })
      ).data
  });
  let columns = equipmentColumns();
  if (resource === 'inventory')
    columns = [
      ...columns,
      ...(filters.view === 'detail'
        ? ([
            ['batch', t`项目`],
            ['location', t`实际地点`]
          ] as Column[])
        : []),
      ['quantity', t`库存数量`],
      ['frozen', t`冻结数量`],
      ['available', t`可用数量`],
      ...((filters.view === 'summary'
        ? [['project_count', t`项目数量`]]
        : [
            ['changed_at', t`最后库存变动时间`],
            ['stock_notes', t`库存备注`]
          ]) as Column[])
    ];
  if (resource === 'parts') columns = [...columns, ['active', t`启用`]];
  if (resource === 'records')
    columns = [
      ['IPN', t`设备ID`],
      ['name', t`设备名称`],
      ['units', t`单位`],
      ['quantity', t`数量`],
      ['batch', t`项目`],
      ['location', t`实际地点`],
      ['purpose', t`用途`],
      ['operator', t`发起人`],
      ['completed_at', t`正式时间`],
      ['remark', t`备注`],
      ['approval_status', t`审批状态`],
      ['approver', t`审批人`]
    ];
  if (resource === 'records' && filters.operation === 'INBOUND')
    columns = columns.filter(([key]) => key !== 'purpose');
  const range = (preset: string) => {
    const today = new Date(`${chinaDay()}T12:00:00+08:00`);
    let start = chinaDay(today);
    let end = start;
    if (preset === 'yesterday') {
      today.setUTCDate(today.getUTCDate() - 1);
      start = end = chinaDay(today);
    }
    if (preset === 'week') {
      today.setUTCDate(today.getUTCDate() - ((today.getUTCDay() + 6) % 7));
      start = chinaDay(today);
    }
    if (preset === 'month') start = `${end.slice(0, 7)}-01`;
    if (preset === 'lastMonth') {
      today.setUTCDate(0);
      end = chinaDay(today);
      start = `${end.slice(0, 7)}-01`;
    }
    setFilters({ ...filters, start, end });
    setPage(1);
  };
  const save = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post(wmsUrl('parts'), editing);
      setEditing(null);
      await query.refetch();
    } catch (e) {
      setError(wmsError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack>
      {resource === 'inventory' && (
        <>
          <SegmentedControl
            value={filters.view}
            onChange={(value) => change('view', value)}
            data={[
              { value: 'summary', label: t`库存汇总` },
              { value: 'detail', label: t`库存明细` }
            ]}
          />
          <Alert color='blue'>
            {filters.view === 'summary'
              ? t`数量为当前筛选范围内的设备合计；含下级时包含一级直属库存及下级库存。`
              : t`每行对应设备＋项目＋实际存货地点，数量均为直属库存，不包含汇总行。`}
          </Alert>
        </>
      )}
      {resource === 'records' && (
        <>
          <SegmentedControl
            value={filters.operation}
            onChange={(value) => change('operation', value)}
            data={[
              { value: 'INBOUND', label: t`入库记录` },
              { value: 'OUTBOUND', label: t`出库记录` }
            ]}
          />
          <Alert>{t`仅统计成功落账的扫码明细，按中国时区正式时间查询，包含结束日全天。盘点调整不计入。`}</Alert>
          <Group>
            {[
              ['today', t`今天`],
              ['yesterday', t`昨天`],
              ['week', t`本周`],
              ['month', t`本月`],
              ['lastMonth', t`上月`]
            ].map(([key, label]) => (
              <Button
                key={key}
                variant='light'
                size='xs'
                onClick={() => range(key)}
              >
                {label}
              </Button>
            ))}
            <TextInput
              type='date'
              label={t`开始日期`}
              value={filters.start}
              onChange={(e) => change('start', e.currentTarget.value)}
            />
            <TextInput
              type='date'
              label={t`结束日期`}
              value={filters.end}
              onChange={(e) => change('end', e.currentTarget.value)}
            />
          </Group>
        </>
      )}
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        <TextInput
          label={t`设备ID／名称／关键字`}
          value={filters.search ?? ''}
          onChange={(e) => change('search', e.currentTarget.value)}
        />
        <TextInput
          label={t`品牌`}
          value={filters.brand ?? ''}
          onChange={(e) => change('brand', e.currentTarget.value)}
        />
        <TextInput
          label={t`型号`}
          value={filters.model_number ?? ''}
          onChange={(e) => change('model_number', e.currentTarget.value)}
        />
        <Select
          clearable
          searchable
          label={t`分类（含下级）`}
          data={options.data?.categories ?? []}
          value={filters.category ?? null}
          onChange={(value) => change('category', value)}
        />
        {resource !== 'parts' && (
          <>
            <TextInput
              label={t`项目（精确匹配）`}
              value={filters.batch ?? ''}
              onChange={(e) => change('batch', e.currentTarget.value)}
            />
            <Select
              clearable
              searchable
              label={t`实际地点`}
              data={options.data?.locations ?? []}
              value={filters.location ?? null}
              onChange={(value) => change('location', value)}
            />
          </>
        )}
        {resource === 'records' && (
          <>
            <TextInput
              label={t`发起人`}
              value={filters.operator ?? ''}
              onChange={(e) => change('operator', e.currentTarget.value)}
            />
            {filters.operation === 'OUTBOUND' && (
              <TextInput
                label={t`用途`}
                value={filters.purpose ?? ''}
                onChange={(e) => change('purpose', e.currentTarget.value)}
              />
            )}
          </>
        )}
      </SimpleGrid>
      {resource === 'inventory' && (
        <Group>
          <SegmentedControl
            value={filters.include_children}
            onChange={(value) => change('include_children', value)}
            data={[
              { value: 'true', label: t`含下级汇总` },
              { value: 'false', label: t`仅本地点` }
            ]}
          />
          <Checkbox
            label={t`包含零库存`}
            checked={filters.include_zero === 'true'}
            onChange={(e) =>
              change('include_zero', String(e.currentTarget.checked))
            }
          />
          {[
            ['has_stock', t`有无库存`],
            ['has_frozen', t`有无冻结`]
          ].map(([key, label]) => (
            <Select
              key={key}
              label={label}
              clearable
              value={filters[key] ?? null}
              onChange={(value) => change(key, value)}
              data={[
                { value: 'true', label: t`有` },
                { value: 'false', label: t`无` }
              ]}
            />
          ))}
        </Group>
      )}
      {(error || query.isError) && (
        <Alert color='red'>{error || wmsError(query.error)}</Alert>
      )}
      <Group justify='space-between'>
        <Text>
          {t`匹配记录`}：{query.data?.count ?? 0}
        </Text>
        <Group>
          <Button
            variant='default'
            onClick={() => void query.refetch()}
          >{t`刷新`}</Button>
          <Button
            variant='light'
            onClick={() =>
              void wmsDownload(resource, { ...filters, export: 'csv' }).catch(
                (e) => setError(wmsError(e))
              )
            }
          >{t`导出全部匹配记录`}</Button>
          {resource === 'parts' && user.hasAddPermission(ModelType.part) && (
            <Button
              onClick={() =>
                setEditing({
                  IPN: '',
                  name: '',
                  units: '',
                  category: '',
                  active: true
                })
              }
            >{t`新增设备`}</Button>
          )}
        </Group>
      </Group>
      {resource === 'records' && query.data && (
        <Alert color='green'>
          {t`成功记录数`}：{query.data.statistics.record_count}；{t`设备种类数`}
          ：{query.data.statistics.part_count}；{t`数量小计`}：
          {Object.entries(query.data.statistics.units ?? {})
            .map(([unit, quantity]) => `${quantity}${unit || t`未填写单位`}`)
            .join('、') || '0'}
        </Alert>
      )}
      {resource === 'records' && query.data && (
        <details>
          <summary>{t`按设备查看数量小计`}</summary>
          <Text size='sm'>
            {Object.entries(query.data.statistics.parts ?? {})
              .map(
                ([ipn, quantity]) =>
                  `${ipn}: ${quantity} ${query.data?.statistics.part_units?.[ipn] ?? ''}`
              )
              .join('；')}
          </Text>
        </details>
      )}
      {query.isFetching && <Loader size='sm' />}
      <DataRows
        rows={query.data?.results ?? []}
        columns={columns}
        edit={
          resource === 'parts' && user.hasChangePermission(ModelType.part)
            ? setEditing
            : undefined
        }
      />
      <Pagination
        total={Math.max(1, Math.ceil((query.data?.count ?? 0) / 50))}
        value={page}
        onChange={setPage}
      />
      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title={t`设备资料`}
        size='lg'
      >
        <Stack>
          {error && <Alert color='red'>{error}</Alert>}
          {equipmentColumns()
            .filter(([key]) => key !== 'category')
            .map(([key, label]) => (
              <TextInput
                key={key}
                required={['name', 'units'].includes(key)}
                label={label}
                value={editing?.[key] ?? ''}
                onChange={(e) =>
                  setEditing({ ...editing, [key]: e.currentTarget.value })
                }
              />
            ))}
          <Select
            required
            searchable
            label={t`设备分类完整路径`}
            data={(options.data?.categories ?? []).map((x) => ({
              value: x.label,
              label: x.label
            }))}
            value={editing?.category ?? null}
            onChange={(value) => setEditing({ ...editing, category: value })}
          />
          <Checkbox
            label={t`启用设备（取消勾选即停用，保留历史记录）`}
            checked={editing?.active ?? true}
            onChange={(e) =>
              setEditing({ ...editing, active: e.currentTarget.checked })
            }
          />
          <Button loading={busy} onClick={() => void save()}>{t`保存`}</Button>
        </Stack>
      </Modal>
    </Stack>
  );
}
export function WmsImportPanel({
  initialKind,
  onCommitted
}: {
  initialKind?: string;
  onCommitted?: () => void;
} = {}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState(initialKind ?? 'count');
  const [preview, setPreview] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const upload = async (file: File | null) => {
    if (!file) return;
    setError('');
    setPreview(null);
    setBusy(true);
    setConfirmed(false);
    const form = new FormData();
    form.append('kind', kind);
    form.append('file', file);
    try {
      setPreview((await api.post(wmsUrl('imports'), form)).data);
    } catch (e) {
      setError(wmsError(e));
    } finally {
      setBusy(false);
    }
  };
  const commit = async () => {
    setError('');
    setBusy(true);
    try {
      setPreview(
        (await api.post(wmsUrl('imports'), { task: preview?.task })).data
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['wms-projects'] }),
        queryClient.invalidateQueries({ queryKey: ['wms-project-options'] }),
        queryClient.invalidateQueries({ queryKey: ['wms-options'] })
      ]);
      onCommitted?.();
    } catch (e) {
      setError(wmsError(e));
    } finally {
      setBusy(false);
    }
  };
  const columns: Column[] = [
    ['row', t`行号`],
    ['IPN', t`设备ID`],
    ...(kind === 'count'
      ? ([
          ['batch', t`项目`],
          ['location', t`实际地点`]
        ] as Column[])
      : []),
    ['action', t`操作`],
    ['before', kind === 'count' ? t`当前数量` : t`原设备资料`],
    ...((kind === 'count'
      ? [
          ['quantity', t`盘点数量`],
          ['delta', t`调整差额`],
          ['frozen', t`冻结数量`],
          ['count_date', t`盘点日期`],
          ['remark', t`盘点备注`]
        ]
      : [['after', t`更新资料`]]) as Column[]),
    ['error', t`错误`]
  ];
  return (
    <Stack>
      {!initialKind && (
        <SegmentedControl
          disabled={busy}
          value={kind}
          onChange={(value) => {
            setKind(value);
            setPreview(null);
            setConfirmed(false);
          }}
          data={[
            { value: 'count', label: t`日常盘点导入` },
            { value: 'parts', label: t`设备资料导入` }
          ]}
        />
      )}
      <Alert color='blue'>
        {kind === 'count'
          ? t`库存明细导出与本模板使用相同的列名和顺序，可直接导入。盘点数量填写当前实盘总数，不是增量；仅调整表内指定项目和实际地点。上传前请核实最新出入库变化。`
          : t`设备ID用于匹配已有设备；新增设备可留空，系统会自动生成内部ID。空白可选资料保留旧值；有错误时整批不能提交。`}
      </Alert>
      <Group>
        <Button
          variant='light'
          onClick={() =>
            void wmsDownload('imports', { kind }).catch((e) =>
              setError(wmsError(e))
            )
          }
        >{t`下载专用模板`}</Button>
        <FileButton onChange={(file) => void upload(file)} accept='.xlsx,.csv'>
          {(props) => (
            <Button {...props} loading={busy}>{t`上传Excel／CSV并预览`}</Button>
          )}
        </FileButton>
      </Group>
      {error && <Alert color='red'>{error}</Alert>}
      {preview && (
        <>
          <Text>
            {t`导入任务`} #{preview.task}
          </Text>
          <DataRows rows={preview.results ?? []} columns={columns} />
          {preview.completed_at ? (
            <Alert color='green'>
              {t`整批导入成功`} · {displayTime(preview.completed_at)}
            </Alert>
          ) : (
            <>
              <Checkbox
                checked={confirmed}
                onChange={(e) => setConfirmed(e.currentTarget.checked)}
                label={
                  kind === 'count'
                    ? t`已核实资料和当前实盘数量，确认按此预览整批落账`
                    : t`已核实设备资料，确认按此预览整批保存`
                }
              />
              <Button
                loading={busy}
                disabled={!preview.can_commit || !confirmed}
                onClick={() => void commit()}
              >{t`确认整批提交`}</Button>
            </>
          )}
        </>
      )}
    </Stack>
  );
}
export function WmsInventoryPanel() {
  return (
    <Tabs defaultValue='inventory' keepMounted={false}>
      <Tabs.List>
        <Tabs.Tab value='inventory'>{t`当前库存`}</Tabs.Tab>
        <Tabs.Tab value='parts'>{t`设备资料`}</Tabs.Tab>
        <Tabs.Tab value='imports'>{t`资料与盘点导入`}</Tabs.Tab>
      </Tabs.List>
      <Tabs.Panel value='inventory' pt='md'>
        <WmsDataPanel resource='inventory' />
      </Tabs.Panel>
      <Tabs.Panel value='parts' pt='md'>
        <WmsDataPanel resource='parts' />
      </Tabs.Panel>
      <Tabs.Panel value='imports' pt='md'>
        <WmsImportPanel />
      </Tabs.Panel>
    </Tabs>
  );
}
