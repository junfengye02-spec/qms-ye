import { ModelType } from '@lib/enums/ModelType';
import useTable from '@lib/hooks/UseTable';
import type { TableFilter } from '@lib/types/Filters';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Checkbox,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../App';
import { InvenTreeTable } from '../../components/tables/InvenTreeTable';
import { useUserState } from '../../states/UserState';
import {
  type WmsOptions,
  cellText,
  chinaDay,
  equipmentColumns,
  wmsDownload,
  wmsError,
  wmsUrl
} from './WmsInventoryPanel';

export function WmsSubtotal({ data }: { data: any }) {
  return (
    <Text size='sm' c='dimmed' aria-live='polite'>
      {t`设备种类数`}：{data?.part_count ?? '—'} · {t`数量小计`}：
      {data
        ? Object.entries(data.units ?? {})
            .map(
              ([unit, quantity]) => `${cellText('quantity', quantity)} ${unit}`
            )
            .join('、') || '0'
        : '—'}
    </Text>
  );
}

export function WmsNativeResource({
  resource,
  scope = {}
}: {
  resource: 'inventory' | 'records' | 'parts';
  scope?: Record<string, any>;
}) {
  const table = useTable(`wms-native-${resource}`);
  const user = useUserState();
  const [data, setData] = useState<any>();
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState('INBOUND');
  const [start, setStart] = useState(`${chinaDay().slice(0, 7)}-01`);
  const [end, setEnd] = useState(chinaDay());
  const options = useQuery<WmsOptions>({
    queryKey: ['wms-options'],
    queryFn: async () => (await api.get(wmsUrl('options'))).data
  });
  const fields = equipmentColumns();
  const columns =
    resource === 'records'
      ? [
          ['IPN', t`设备ID`],
          ['name', t`设备名称`],
          ['units', t`单位`],
          ['quantity', t`数量`],
          ['batch', t`项目`],
          ['contract_number', t`合同号`],
          ['acceptance_date', t`验收日期`],
          ['warranty_months', t`质保月数`],
          ['warranty_end', t`质保到期日`],
          ['location', t`实际地点`],
          ['completed_at', t`正式时间`],
          ['operator', t`发起人`],
          ['purpose', t`用途`],
          ['remark', t`备注`],
          ['approval_status', t`审批状态`],
          ['approver', t`审批人`]
        ]
      : [
          ...fields,
          ...(resource === 'inventory'
            ? [
                ['quantity', t`库存数量`],
                ['frozen', t`冻结数量`],
                ['available', t`可用数量`],
                ['project_count', t`项目数量`],
                ['contract_number', t`合同号`],
                ['acceptance_date', t`验收日期`],
                ['warranty_months', t`质保月数`],
                ['warranty_end', t`质保到期日`]
              ]
            : [['active', t`启用`]])
        ];
  const filters: TableFilter[] = [
    ...(resource !== 'parts'
      ? [{ name: 'contract', label: t`合同号`, type: 'text' as const }]
      : []),
    { name: 'brand', label: t`品牌`, type: 'text' },
    { name: 'model_number', label: t`型号`, type: 'text' },
    {
      name: 'category',
      label: t`分类（含下级）`,
      type: 'choice',
      choices: options.data?.categories ?? []
    },
    ...(resource === 'records'
      ? [
          {
            name: 'batch',
            label: t`项目（精确匹配）`,
            type: 'text' as const
          },
          {
            name: 'location',
            label: t`实际地点`,
            type: 'choice' as const,
            choices: options.data?.locations ?? []
          },
          { name: 'operator', label: t`发起人`, type: 'text' as const },
          { name: 'purpose', label: t`用途`, type: 'text' as const }
        ]
      : [])
  ];
  const save = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post(wmsUrl('parts'), editing);
      setEditing(null);
      table.refreshTable();
    } catch (e) {
      setError(wmsError(e));
    } finally {
      setBusy(false);
    }
  };
  const preset = (value: string | null) => {
    const today = new Date(`${chinaDay()}T12:00:00+08:00`);
    let from = chinaDay(today);
    let to = from;
    if (value === 'yesterday') {
      today.setUTCDate(today.getUTCDate() - 1);
      from = to = chinaDay(today);
    }
    if (value === 'week') {
      today.setUTCDate(today.getUTCDate() - ((today.getUTCDay() + 6) % 7));
      from = chinaDay(today);
    }
    if (value === 'month') from = `${to.slice(0, 7)}-01`;
    if (value === 'lastMonth') {
      today.setUTCDate(0);
      to = chinaDay(today);
      from = `${to.slice(0, 7)}-01`;
    }
    setStart(from);
    setEnd(to);
    table.setPage(1);
  };
  return (
    <Stack gap='sm'>
      {resource === 'records' && (
        <>
          <Group align='end'>
            <Select
              label={t`方向`}
              value={operation}
              onChange={(v) => {
                setOperation(v ?? 'INBOUND');
                table.setPage(1);
              }}
              data={[
                { value: 'INBOUND', label: t`入库记录` },
                { value: 'OUTBOUND', label: t`出库记录` }
              ]}
            />
            <Select
              label={t`日期范围`}
              placeholder={t`自定义`}
              onChange={preset}
              data={[
                { value: 'today', label: t`今天` },
                { value: 'yesterday', label: t`昨天` },
                { value: 'week', label: t`本周` },
                { value: 'month', label: t`本月` },
                { value: 'lastMonth', label: t`上月` }
              ]}
            />
            <TextInput
              type='date'
              label={t`开始日期`}
              value={start}
              onChange={(e) => {
                setStart(e.currentTarget.value);
                table.setPage(1);
              }}
            />
            <TextInput
              type='date'
              label={t`结束日期`}
              value={end}
              onChange={(e) => {
                setEnd(e.currentTarget.value);
                table.setPage(1);
              }}
            />
          </Group>
          <Text
            size='xs'
            c='dimmed'
          >{t`仅统计成功落账的扫码明细，按中国时区正式时间查询，包含结束日全天。盘点调整不计入。`}</Text>
        </>
      )}
      {error && <Alert color='red'>{error}</Alert>}
      {resource !== 'parts' && (
        <Group gap='xs'>
          {resource === 'records' && (
            <Text size='sm'>
              {t`成功记录数`}：{data?.statistics?.record_count ?? '—'} ·
            </Text>
          )}
          <WmsSubtotal data={data?.statistics} />
        </Group>
      )}
      <InvenTreeTable
        url={resource === 'inventory' ? '/api/stock/' : wmsUrl(resource)}
        tableState={table}
        columns={columns.map(([accessor, title]) => ({
          accessor,
          title,
          defaultVisible: ![
            'brand',
            'model_number',
            'specification',
            'notes',
            'purpose',
            'remark',
            'approval_status',
            'approver'
          ].includes(accessor),
          render: (row: any) => cellText(accessor, row[accessor])
        }))}
        props={{
          params:
            resource === 'inventory'
              ? { ...scope, view: 'summary', wms: 'true' }
              : resource === 'records'
                ? { operation, start, end }
                : {},
          enableDownload: true,
          wrapToolbar: true,
          useUrlFilters: false,
          enableSelection: false,
          detailAction: false,
          tableFilters: resource === 'inventory' ? [] : filters,
          enableFilters: resource !== 'inventory',
          enableSearch: resource !== 'inventory',
          onDataLoaded: setData,
          onDownload: (params) => {
            void (
              resource === 'inventory'
                ? nativeInventoryDownload(params)
                : wmsDownload(resource, { ...params, export: 'csv' })
            ).catch((e) => setError(wmsError(e)));
          },
          tableActions:
            resource === 'parts' && user.hasAddPermission(ModelType.part)
              ? [
                  <Button
                    key='new'
                    size='xs'
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
                ]
              : [],
          rowActions:
            resource === 'parts' && user.hasChangePermission(ModelType.part)
              ? (row) => [{ title: t`编辑`, onClick: () => setEditing(row) }]
              : undefined
        }}
      />
      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title={t`设备资料`}
        size='lg'
      >
        <Stack>
          {error && <Alert color='red'>{error}</Alert>}
          {fields
            .filter(([key]) => key !== 'category')
            .map(([key, label]) => (
              <TextInput
                key={key}
                required={['IPN', 'name', 'units'].includes(key)}
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
export async function nativeInventoryDownload(params: Record<string, any>) {
  const response = await api.get('/api/stock/', {
    params: { ...params, wms: 'true', export: 'csv' },
    responseType: 'blob'
  });
  const url = URL.createObjectURL(response.data);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'inventory.csv';
  link.click();
  URL.revokeObjectURL(url);
}
