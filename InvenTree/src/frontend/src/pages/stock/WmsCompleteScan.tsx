import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Checkbox,
  Group,
  Modal,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, queryClient } from '../../App';
import BarcodeCameraInput from '../../components/barcodes/BarcodeCameraInput';
import { createUuid } from '../../functions/uuid';
import { ScanApprovalRequestDetails } from './ScanApprovalRequestDetails';
import type {
  ScanMode,
  ScanStockApprovalRequest,
  ScanStockResponse
} from './ScanStockTypes';
import type { WmsReceipt, WmsReceiptDetail } from './WmsContractTypes';
import { wmsError, wmsUrl } from './WmsInventoryPanel';

export function WmsCompleteScan({
  mode,
  receipt,
  onDraftChange,
  onChanged
}: {
  mode: ScanMode;
  receipt?: WmsReceipt;
  onDraftChange?: (draft: ScanStockApprovalRequest | null) => void;
  onChanged?: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const running = useRef(false);
  const retry = useRef<Record<string, unknown> | null>(null);
  const [barcode, setBarcode] = useState('');
  const [resolved, setResolved] = useState<WmsReceiptDetail | null>(null);
  const [location, setLocation] = useState<string | null>(
    receipt ? String(receipt.location) : null
  );
  const [quantity, setQuantity] = useState<number | string>(1);
  const [confirmed, setConfirmed] = useState(false);
  const [purpose, setPurpose] = useState('');
  const [remark, setRemark] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [draft, setDraft] = useState<ScanStockApprovalRequest | null>(null);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [camera, setCamera] = useState(false);
  const locations = useQuery<{
    results: Array<{ pk: number; pathstring: string }>;
  }>({
    queryKey: ['scan-stock-locations'],
    queryFn: async () =>
      (
        await api.get(apiUrl(ApiEndpoints.stock_location_list), {
          params: { limit: 500, structural: false }
        })
      ).data
  });
  const active = useQuery<ScanStockApprovalRequest | null>({
    queryKey: ['wms-complete-active-draft', mode],
    queryFn: async () => {
      const response = await api.get(apiUrl(ApiEndpoints.stock_scan_requests), {
        params: {
          mine: true,
          status: 'DRAFT',
          direction: mode.toUpperCase(),
          limit: 1
        }
      });
      const first = response.data.results[0];
      return first
        ? (
            await api.get(
              apiUrl(ApiEndpoints.stock_scan_request_detail, undefined, {
                id: first.pk
              })
            )
          ).data
        : null;
    }
  });
  useEffect(() => {
    if (active.isSuccess) setDraft(active.data);
  }, [active.data, active.isSuccess]);
  useEffect(() => {
    onDraftChange?.(draft);
  }, [draft, onDraftChange]);
  useEffect(() => {
    setResolved(null);
    setBarcode('');
    retry.current = null;
    if (receipt) setLocation(String(receipt.location));
    input.current?.focus();
  }, [receipt?.pk]);
  useEffect(() => {
    if (!busy) {
      const frame = requestAnimationFrame(() => input.current?.focus());
      return () => cancelAnimationFrame(frame);
    }
  }, [busy]);
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['scan-stock-requests'] }),
      queryClient.invalidateQueries({ queryKey: ['scan-stock-active-draft'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-receipts'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-complete-active-draft'] })
    ]);
    onChanged?.();
  };
  async function run(action: () => Promise<void>) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await action();
    } catch (err) {
      setError(
        err instanceof Error && !(err as any).response
          ? err.message
          : wmsError(err)
      );
    } finally {
      running.current = false;
      setBusy(false);
      input.current?.focus();
    }
  }
  async function locate(value: string) {
    await run(async () => {
      setResolved(null);
      retry.current = null;
      const { data } = await api.post<WmsReceiptDetail>(
        wmsUrl('labels/resolve'),
        {
          barcode: value.trim(),
          receipt: receipt?.pk,
          location: location ? Number(location) : undefined
        }
      );
      setResolved(data);
      setBarcode(value.trim());
      setConfirmed(false);
      if (mode === 'inbound') setQuantity(Number(data.prepared_quantity));
      const duplicate = draft?.lines?.find(
        (line) => line.source_detail === data.pk && line.outcome === 'PENDING'
      );
      if (duplicate)
        setMessage(
          t`同一码已加入，已定位原申请行：` + duplicate.request_line_number
        );
    });
  }
  async function add() {
    if (!resolved || !location) return;
    await run(async () => {
      const current = (
        await api.post<WmsReceiptDetail>(wmsUrl('labels/resolve'), {
          barcode: resolved.barcode,
          receipt: receipt?.pk,
          location: Number(location)
        })
      ).data;
      if (mode === 'inbound' && current.state !== 'READY' && !retry.current)
        throw new Error(t`该明细已核对或已入库，请查看原申请。`);
      let request = draft;
      if (request && request.location !== Number(location))
        throw new Error(t`当前草稿属于其他库位，请先提交或取消该草稿。`);
      if (!request) {
        request = (
          await api.post<ScanStockApprovalRequest>(
            apiUrl(ApiEndpoints.stock_scan_requests),
            { direction: mode.toUpperCase(), location: Number(location) }
          )
        ).data;
        setDraft(request);
      }
      const payload = retry.current ?? {
        barcode: current.barcode,
        quantity,
        client_event_id: createUuid(),
        approval_request_id: request.pk,
        receipt_id: current.receipt,
        quantity_confirmed: confirmed,
        purpose,
        remark,
        [mode === 'inbound' ? 'destination_location' : 'source_location']:
          Number(location)
      };
      retry.current = payload;
      const { data } = await api.post<ScanStockResponse>(
        apiUrl(
          mode === 'inbound'
            ? ApiEndpoints.stock_scan_inbound_v2
            : ApiEndpoints.stock_scan_outbound
        ),
        payload
      );
      if (!data.success) {
        if (data.terminal) retry.current = null;
        throw new Error(data.error_message || t`扫码未成功，请刷新后重试。`);
      }
      const updatedRequest = (
        await api.get<ScanStockApprovalRequest>(
          apiUrl(ApiEndpoints.stock_scan_request_detail, undefined, {
            id: request.pk
          })
        )
      ).data;
      queryClient.setQueryData(
        ['scan-stock-request', request.pk],
        updatedRequest
      );
      setDraft(updatedRequest);
      retry.current = null;
      setResolved(null);
      setBarcode('');
      setConfirmed(false);
      setMessage(
        mode === 'outbound'
          ? t`已加入草稿并冻结申请数量。`
          : t`已核对并加入草稿，审批通过后增加库存。`
      );
      await refresh();
    });
  }
  const unchecked =
    receipt?.details.filter(
      (d) =>
        d.state === 'READY' &&
        !draft?.lines?.some(
          (line) => line.source_detail === d.pk && line.outcome === 'PENDING'
        )
    ) ?? [];
  return (
    <Stack>
      <Alert>
        {mode === 'inbound'
          ? t`扫描实物完整标签，核对实际数量后加入草稿。重复扫码只定位明细，不累加数量。`
          : t`扫描完整标签自动加载项目、合同及当前库位库存。加入草稿即冻结，审批通过后扣减。`}
      </Alert>
      {error && <Alert color='red'>{error}</Alert>}
      {message && <Alert color='green'>{message}</Alert>}
      <Select
        label={t`实际库位`}
        value={location}
        disabled={busy || (mode === 'inbound' && !!receipt)}
        searchable
        data={(locations.data?.results ?? []).map((l) => ({
          value: String(l.pk),
          label: l.pathstring
        }))}
        onChange={(value) => {
          setLocation(value);
          setResolved(null);
        }}
      />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void locate(barcode);
        }}
      >
        <Group align='end'>
          <TextInput
            ref={input}
            style={{ flex: 1 }}
            label={t`实物完整二维码`}
            value={barcode}
            disabled={busy}
            onChange={(event) => {
              setBarcode(event.currentTarget.value);
              setResolved(null);
              retry.current = null;
            }}
          />
          <Button
            type='submit'
            loading={busy}
            disabled={!barcode.trim() || !location}
          >{t`识别标签`}</Button>
          {/* 摄像头入口暂时停用，保留代码供后续恢复。
          <Button
            variant='light'
            onClick={() => setCamera(true)}
          >{t`相机扫码`}</Button>
          */}
        </Group>
      </form>
      <Modal
        opened={camera}
        onClose={() => setCamera(false)}
        title={t`相机扫码`}
      >
        {camera && (
          <BarcodeCameraInput
            onScan={(value) => {
              setCamera(false);
              void locate(value);
            }}
          />
        )}
      </Modal>
      {resolved && (
        <Stack gap='xs'>
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing='sm'>
            <TextInput label={t`设备ID`} readOnly value={resolved.equipment} />
            <TextInput label={t`设备名称`} readOnly value={resolved.name} />
            <TextInput label={t`项目`} readOnly value={resolved.project} />
            <TextInput
              label={t`合同号`}
              readOnly
              value={resolved.contract_number}
            />
          </SimpleGrid>
          <Text size='sm'>
            {t`验收日期`}：{resolved.acceptance_date || t`待验收`} ·{' '}
            {t`质保月数`}：{resolved.warranty_months} ·{' '}
            {t`到期日（当天仍在保）`}：{resolved.warranty_end || '—'}
          </Text>
          {mode === 'outbound' &&
            (resolved.stock ?? []).map((item) => (
              <Text key={item.pk}>
                {item.location_name} · {t`库存`} {item.quantity} · {t`冻结`}{' '}
                {item.frozen} · {t`占用`} {item.allocated} · {t`可用`}{' '}
                {item.available}
              </Text>
            ))}
          <NumberInput
            label={t`实际申请数量`}
            min={0.00001}
            decimalScale={5}
            value={quantity}
            disabled={busy || !!retry.current}
            onChange={setQuantity}
          />
          {mode === 'inbound' &&
            Number(quantity) !== Number(resolved.prepared_quantity) && (
              <Checkbox
                label={t`已核对实际到货数量；未入库余量后续另建明细，不自动入库。`}
                checked={confirmed}
                onChange={(e) => setConfirmed(e.currentTarget.checked)}
              />
            )}
          {mode === 'outbound' && (
            <TextInput
              label={t`用途`}
              required
              value={purpose}
              onChange={(e) => setPurpose(e.currentTarget.value)}
            />
          )}
          <TextInput
            label={t`备注`}
            value={remark}
            onChange={(e) => setRemark(e.currentTarget.value)}
          />
          <Button
            loading={busy}
            disabled={
              Number(quantity) <= 0 ||
              (mode === 'outbound' && !purpose.trim()) ||
              (mode === 'inbound' &&
                Number(quantity) !== Number(resolved.prepared_quantity) &&
                !confirmed)
            }
            onClick={() => void add()}
          >
            {retry.current ? t`重试原请求` : t`核对并加入草稿`}
          </Button>
        </Stack>
      )}
      {draft && (
        <>
          <Button
            onClick={() => setSubmitOpen(true)}
            disabled={
              busy || !draft.lines?.some((line) => line.outcome === 'PENDING')
            }
          >{t`提交已核对明细审批`}</Button>
          <ScanApprovalRequestDetails
            requestId={draft.pk}
            initialRequest={draft}
            hideSubmit
            onChanged={(request) => {
              setDraft(request.status === 'DRAFT' ? request : null);
              void refresh();
            }}
          />
        </>
      )}
      <Modal
        opened={submitOpen}
        onClose={() => setSubmitOpen(false)}
        title={t`确认提交范围`}
      >
        <Stack>
          <Text>{t`只提交草稿中的已核对明细。`}</Text>
          {unchecked.length > 0 && (
            <Alert color='yellow'>
              {t`以下明细尚未核对，不会提交；可保留在本记录后续处理：`}
              {unchecked.map((d) => `${d.equipment} ${d.name}`).join('、')}
            </Alert>
          )}
          <Button
            loading={busy}
            onClick={() =>
              void run(async () => {
                if (!draft) return;
                await api.post(
                  apiUrl(ApiEndpoints.stock_scan_request_submit, undefined, {
                    id: draft.pk
                  }),
                  {}
                );
                setDraft(null);
                setSubmitOpen(false);
                await refresh();
                setMessage(t`已提交审批。`);
              })
            }
          >{t`确认提交`}</Button>
        </Stack>
      </Modal>
    </Stack>
  );
}
