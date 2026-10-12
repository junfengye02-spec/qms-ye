import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Checkbox,
  Group,
  NumberInput,
  Select,
  Stack,
  Text,
  TextInput
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from '../../App';
import {
  type LocalPrintConfig,
  LocalPrintModal
} from '../../components/printing/LocalPrintModal';
import { LOCAL_PRINT_URL } from '../../functions/printing/LocalPrintClient';
import { createUuid } from '../../functions/uuid';
import { WmsContractEditor } from './WmsContractEditor';
import type { WmsContract, WmsReceiptDetail } from './WmsContractTypes';
import { wmsError, wmsUrl } from './WmsInventoryPanel';
import { WmsProjectSelect } from './WmsProjectSelect';

export function WmsStockAttribution({
  stock,
  onChanged
}: {
  stock: any;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<WmsReceiptDetail | null>(null);
  const [project, setProject] = useState(stock.batch ?? '');
  const [contract, setContract] = useState<string | null>(null);
  const [quantity, setQuantity] = useState<number | string>(
    Number(stock.quantity)
  );
  const [contractOpen, setContractOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [printing, setPrinting] = useState(false);
  const [requestKey] = useState(() => createUuid());
  const sourceId = detail?.pk ?? stock.source_detail;
  const contracts = useQuery<{ results: WmsContract[] }>({
    queryKey: ['wms-contracts'],
    queryFn: async () => (await api.get(wmsUrl('contracts'))).data
  });
  const config = useQuery<LocalPrintConfig>({
    queryKey: ['local-print-config'],
    queryFn: async () => (await api.get(`${LOCAL_PRINT_URL}config/`)).data
  });
  const sourceDetail = useQuery({
    queryKey: ['wms-stock-detail', sourceId],
    enabled: !!sourceId,
    queryFn: async () => {
      const { data } = await api.get<WmsReceiptDetail>(
        wmsUrl(`details/${sourceId}`)
      );
      return data;
    }
  });
  useEffect(() => {
    const data = sourceDetail.data;
    if (!data) return;
    setDetail(data);
    setProject(data.project);
    setContract(String(data.contract));
  }, [sourceDetail.data]);
  async function save() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const response = detail
        ? await api.post<WmsReceiptDetail>(wmsUrl(`details/${detail.pk}`), {
            project,
            contract: Number(contract),
            reason,
            label_version: detail.label_version
          })
        : await api.post<WmsReceiptDetail>(wmsUrl(`legacy/${stock.pk}`), {
            request_key: requestKey,
            project,
            contract: Number(contract),
            quantity,
            confirmed_stock_quantity: stock.quantity,
            confirmed_single_contract: confirmed,
            reason
          });
      setDetail(response.data);
      setMessage(t`已保存当前归属。项目或合同变化后请更换完整标签。`);
      onChanged();
    } catch (err) {
      setError(wmsError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Stack>
      {error && <Alert color='red'>{error}</Alert>}
      {message && <Alert color='green'>{message}</Alert>}
      <Text>
        {stock.part_detail?.name} · {t`当前库存数量`}：{stock.quantity}
      </Text>
      {!stock.source_detail && !detail && (
        <Alert color='yellow'>{t`请核对本行来源。若包含多个合同，只填写本次确认归属的数量，系统拆分库存后分别补录；补录不增加库存、不生成入库流水。`}</Alert>
      )}
      <WmsProjectSelect
        label={t`项目号`}
        value={project}
        onChange={setProject}
      />
      <Select
        label={t`合同号`}
        searchable
        value={contract}
        data={(contracts.data?.results ?? []).map((c) => ({
          value: String(c.pk),
          label: c.number
        }))}
        onChange={setContract}
      />
      {!detail ? (
        <>
          <NumberInput
            label={t`本次确认归属数量`}
            min={0.00001}
            max={Number(stock.quantity)}
            value={quantity}
            onChange={setQuantity}
          />
          <Checkbox
            label={t`已核对当前整行数量，确认本次数量属于所选合同；其余数量另行核对补录。`}
            checked={confirmed}
            onChange={(e) => setConfirmed(e.currentTarget.checked)}
          />
        </>
      ) : (
        <Stack gap='xs'>
          <Text size='sm'>
            {t`验收日期（合同起始时间）`}：{detail.acceptance_date || t`待验收`}
          </Text>
          <Text size='sm'>
            {t`合同质保月数`}：{detail.warranty_months} · {t`质保到期日`}：
            {detail.warranty_end || t`待验收`}
          </Text>
          <Button
            variant='light'
            onClick={() => setContractOpen(true)}
          >{t`更新合同验收日期及质保`}</Button>
          <Text
            size='xs'
            c='dimmed'
          >{t`入库后可在此补填或修改验收日期，同一合同的关联设备统一更新。`}</Text>
        </Stack>
      )}
      <TextInput
        label={t`修改或补录原因`}
        required
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
      />
      <Group>
        <Button
          loading={busy}
          disabled={
            sourceDetail.isLoading ||
            !!sourceDetail.error ||
            !project.trim() ||
            !contract ||
            !reason.trim() ||
            (!detail && !confirmed)
          }
          onClick={() => void save()}
        >{t`保存归属`}</Button>
        {detail && (
          <Button
            disabled={!config.data?.enabled}
            onClick={() => setPrinting(true)}
          >{t`打印完整标签`}</Button>
        )}
      </Group>
      <WmsContractEditor
        opened={contractOpen}
        contract={
          contracts.data?.results.find((c) => c.pk === detail?.contract) ?? null
        }
        onClose={() => setContractOpen(false)}
        onSaved={() => onChanged()}
      />
      {printing && detail && config.data && (
        <LocalPrintModal
          modelType='wmsreceiptdetail'
          items={[detail.pk]}
          config={config.data}
          onClose={() => setPrinting(false)}
        />
      )}
    </Stack>
  );
}
