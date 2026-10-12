import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Group,
  Modal,
  NumberInput,
  Select,
  Stack,
  TextInput
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, queryClient } from '../../App';
import type { WmsContract } from './WmsContractTypes';
import { wmsError, wmsUrl } from './WmsInventoryPanel';

export function WmsContractEditor({
  opened,
  contract,
  onClose,
  onSaved
}: {
  opened: boolean;
  contract: WmsContract | null;
  onClose: () => void;
  onSaved: (contract: WmsContract) => void;
}) {
  const contracts = useQuery<{ results: WmsContract[] }>({
    queryKey: ['wms-contracts'],
    queryFn: async () => (await api.get(wmsUrl('contracts'))).data
  });
  const [editingContract, setEditingContract] = useState<WmsContract | null>(
    null
  );
  const [number, setNumber] = useState('');
  const [date, setDate] = useState('');
  const [months, setMonths] = useState<number | string>(12);
  const [impact, setImpact] = useState<any>(null);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  function loadContract(value: WmsContract | null) {
    setEditingContract(value);
    setNumber(value?.number ?? '');
    setDate(value?.acceptance_date ?? '');
    setMonths(value?.warranty_months ?? 12);
    setImpact(null);
    setReason('');
    setError('');
  }
  useEffect(() => {
    if (opened) loadContract(contract);
  }, [opened, contract]);
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (err) {
      setError(wmsError(err));
    } finally {
      setBusy(false);
    }
  }
  const contractOptions = (contracts.data?.results ?? []).map((c) => ({
    value: String(c.pk),
    label: c.number
  }));
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={editingContract ? t`编辑合同与验收` : t`新建合同`}
    >
      <Stack>
        {error && <Alert color='red'>{error}</Alert>}
        <Select
          label={t`选择已有合同（留空则新建）`}
          clearable
          searchable
          value={editingContract ? String(editingContract.pk) : null}
          data={contractOptions}
          onChange={(value) =>
            loadContract(
              contracts.data?.results.find((c) => String(c.pk) === value) ??
                null
            )
          }
        />
        <TextInput
          label={t`合同号`}
          required
          maxLength={100}
          value={number}
          onChange={(e) => {
            setNumber(e.currentTarget.value);
            setImpact(null);
          }}
        />
        <TextInput
          type='date'
          label={t`验收日期（合同起始时间）`}
          description={t`可在入库后补填或更新，关联设备按此日期统一计算质保。`}
          value={date}
          onChange={(e) => {
            setDate(e.currentTarget.value);
            setImpact(null);
          }}
        />
        <Group>
          <Button
            variant='light'
            onClick={() => {
              setMonths(12);
              setImpact(null);
            }}
          >{t`12个月`}</Button>
          <Button
            variant='light'
            onClick={() => {
              setMonths(24);
              setImpact(null);
            }}
          >{t`24个月`}</Button>
        </Group>
        <NumberInput
          label={t`合同质保月数`}
          required
          min={1}
          allowDecimal={false}
          value={months}
          onChange={(value) => {
            setMonths(value);
            setImpact(null);
          }}
        />
        {editingContract && (
          <TextInput
            label={t`修改原因`}
            required
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value)}
          />
        )}
        {impact && (
          <Alert color='yellow'>
            {t`关联设备明细数`}：{impact.before.acceptance_followers} ·{' '}
            {t`保存后统一更新验收日期和质保到期日。修改合同号需更换关联标签。`}
          </Alert>
        )}
        <Button
          loading={busy}
          disabled={
            !number.trim() ||
            Number(months) < 1 ||
            (!!editingContract && !reason.trim())
          }
          onClick={() =>
            void run(async () => {
              const values = {
                pk: editingContract?.pk,
                number,
                acceptance_date: date || null,
                warranty_months: Number(months),
                reason,
                expected_updated_at: editingContract?.updated_at
              };
              if (editingContract && !impact) {
                setImpact(
                  (
                    await api.post(wmsUrl('contracts'), {
                      ...values,
                      preview: true
                    })
                  ).data
                );
                return;
              }
              const { data } = await api.post<WmsContract>(
                wmsUrl('contracts'),
                values
              );
              await Promise.all([
                queryClient.invalidateQueries({ queryKey: ['wms-contracts'] }),
                queryClient.invalidateQueries({ queryKey: ['wms-receipts'] }),
                queryClient.invalidateQueries({
                  queryKey: ['wms-stock-detail']
                }),
                queryClient.invalidateQueries({ queryKey: ['wms-options'] })
              ]);
              onSaved(data);
              onClose();
            })
          }
        >
          {editingContract && !impact ? t`核对影响数量` : t`确认保存合同`}
        </Button>
      </Stack>
    </Modal>
  );
}
