import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Group,
  Modal,
  NumberInput,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  ThemeIcon,
  Title
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import {
  IconAlertCircle,
  IconArrowDown,
  IconArrowUp,
  // IconCamera,
  IconCheck,
  IconClock,
  IconMapPin,
  IconQrcode,
  IconSwitchHorizontal
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { type KeyboardEvent, useEffect, useRef, useState } from 'react';

import { api } from '../../App';
import BarcodeCameraInput from '../../components/barcodes/BarcodeCameraInput';
import { createUuid } from '../../functions/uuid';
import { useGlobalSettingsState } from '../../states/SettingsStates';
import {
  ScanApprovalRequestDetails,
  approvalLocationLabel,
  approvalOwnerLabel
} from './ScanApprovalRequestDetails';
import { ScanResultList } from './ScanResultList';
import type {
  ScanMode,
  ScanSessionResult,
  ScanStockApprovalRequest,
  ScanStockApprovalRequestList,
  ScanStockCandidate,
  ScanStockResponse
} from './ScanStockTypes';
import type { WmsReceiptDetail } from './WmsContractTypes';
import { type WmsOptions, wmsError, wmsUrl } from './WmsInventoryPanel';
import { WmsProjectSelect } from './WmsProjectSelect';

type LocationList = {
  results: Array<{ pk: number; name: string; pathstring: string }>;
};

type PendingScan = {
  locationId: string | null;
  barcode: string;
  clientEventId: string;
  quantity: number | string;
  batch: string;
  purpose: string;
  remark: string;
  receiptId?: number;
  quantityConfirmed?: boolean;
  projectId?: number;
};

type BarcodeLocationResponse = {
  stocklocation?: { pk?: number };
};

function errorData(error: any): ScanStockResponse | undefined {
  return error?.response?.data ?? error?.data;
}

function remainingTime(value?: string): string {
  if (!value) return '-';
  const seconds = Math.max(
    0,
    Math.floor((new Date(value).getTime() - Date.now()) / 1000)
  );
  if (seconds <= 0) return t`Expired`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const unit = (value: number, name: 'hour' | 'minute' | 'second') =>
    new Intl.NumberFormat(undefined, {
      style: 'unit',
      unit: name,
      unitDisplay: 'short'
    }).format(value);
  return hours > 0
    ? `${unit(hours, 'hour')} ${unit(minutes, 'minute')}`
    : `${unit(minutes, 'minute')} ${unit(remainder, 'second')}`;
}

export function ScanWorkPanel({
  mode,
  initialLocationId = null,
  onDraftChange
}: Readonly<{
  mode: ScanMode;
  initialLocationId?: string | null;
  onDraftChange?: (draft: ScanStockApprovalRequest | null) => void;
}>) {
  const queryClient = useQueryClient();
  const globalSettings = useGlobalSettingsState();
  const approvalEnabled = globalSettings.isSet('SCAN_APPROVAL_ENABLED');
  const inputRef = useRef<HTMLInputElement>(null);
  const [locationId, setLocationId] = useState<string | null>(null);
  const [locationBarcode, setLocationBarcode] = useState('');
  const [locationProcessing, setLocationProcessing] = useState(false);
  const [locationMessage, setLocationMessage] = useState<string | null>(null);
  const [locationError, setLocationError] = useState<string | null>(null);
  const [barcode, setBarcode] = useState('');
  const completeCode = /^WMS[0-9]+:/.test(barcode.trim());
  const [stableBarcode] = useDebouncedValue(barcode.trim(), 150);
  const [quantityConfirmed, setQuantityConfirmed] = useState(false);
  const completeDetail = useQuery<WmsReceiptDetail>({
    queryKey: ['wms-direct-label', barcode.trim(), locationId],
    enabled: completeCode && stableBarcode === barcode.trim(),
    retry: false,
    queryFn: async ({ signal }) =>
      (
        await api.post(
          wmsUrl('labels/resolve'),
          {
            barcode: barcode.trim(),
            location: locationId ? Number(locationId) : undefined
          },
          { signal }
        )
      ).data
  });
  const [cameraOpened, setCameraOpened] = useState(false);
  const [quantity, setQuantity] = useState<number | string>(1);
  const [projectSelection, setProjectSelection] = useState({
    value: '',
    scope: ''
  });
  const selectionScope = JSON.stringify([mode, locationId, barcode.trim()]);
  const project = completeCode
    ? (completeDetail.data?.project ?? '')
    : mode === 'inbound' || projectSelection.scope === selectionScope
      ? projectSelection.value
      : '';
  const setProject = (value: string) =>
    setProjectSelection({ value, scope: selectionScope });
  const [purpose, setPurpose] = useState('');
  const [projectId, setProjectId] = useState<number | undefined>();
  const [remark, setRemark] = useState('');
  const [setupUnits, setSetupUnits] = useState('');
  const [setupCategory, setSetupCategory] = useState<string | null>(null);
  const [retryPayload, setRetryPayload] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [scanError, setScanError] = useState('');
  const projectOptions = useQuery<WmsOptions>({
    queryKey: [
      'wms-project-options',
      mode,
      mode === 'outbound' ? locationId : null,
      barcode.trim(),
      mode === 'outbound' ? project : ''
    ],
    enabled:
      !completeCode &&
      stableBarcode === barcode.trim() &&
      (mode === 'inbound' || Boolean(locationId && barcode.trim())),
    queryFn: async ({ signal }) =>
      (
        await api.get(wmsUrl('options'), {
          signal,
          params:
            mode === 'outbound'
              ? {
                  location: locationId,
                  barcode: barcode.trim(),
                  batch: project,
                  outbound: 'true'
                }
              : { barcode: barcode.trim() || undefined }
        })
      ).data,
    refetchInterval: 15000
  });
  const stockStatus = projectOptions.data?.stock_status;
  const label =
    completeCode && !completeDetail.isError ? completeDetail.data : undefined;
  const scannedPart =
    !completeCode &&
    !projectOptions.isError &&
    stableBarcode === barcode.trim() &&
    barcode.trim()
      ? projectOptions.data?.scanned_part
      : null;
  const outboundReady = completeCode
    ? !completeDetail.isFetching &&
      !completeDetail.isError &&
      !!completeDetail.data?.stock?.some(
        (s) => s.location === Number(locationId) && Number(s.available) > 0
      )
    : !projectOptions.isFetching &&
      !projectOptions.isError &&
      stockStatus?.state === 'AVAILABLE' &&
      'scope' in stockStatus &&
      stockStatus.scope === 'project';
  const refreshStockOptions = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ['wms-project-options'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-options'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-direct-label'] }),
      queryClient.invalidateQueries({ queryKey: ['wms-receipts'] })
    ]);

  // Use the posted event and submitted location, even after the input has changed.
  // A failed follow-up read must never turn a committed scan into a failed scan.
  const refreshPostedResult = async (
    data: ScanStockResponse,
    submittedLocation: unknown
  ) => {
    await refreshStockOptions();
    const event = data.event;
    if (
      !data.success ||
      event?.operation !== 'OUTBOUND' ||
      event.outcome !== 'SUCCESS' ||
      !event.part_pk_snapshot
    )
      return;
    try {
      const response = await api.get<WmsOptions>(wmsUrl('options'), {
        params: {
          outbound: 'true',
          barcode: `INV-PA${event.part_pk_snapshot}`,
          batch: event.batch_snapshot,
          location: submittedLocation
        }
      });
      if (response.data.stock_status?.state === 'NO_STOCK') {
        setResults((current) =>
          current.map((result) =>
            result.id === event.client_event_id
              ? {
                  ...result,
                  message: data.replayed
                    ? t`原出库请求已确认，当前库存已为0；未重复扣减。`
                    : t`出库成功，当前库存已为0。`
                }
              : result
          )
        );
      }
    } catch {
      setResults((current) =>
        current.map((result) =>
          result.id === event.client_event_id
            ? {
                ...result,
                message: t`出库已成功，剩余库存查询失败，请重试库存查询。`
              }
            : result
        )
      );
    }
  };
  const [processing, setProcessing] = useState(false);
  const processingRef = useRef(false);
  const [results, setResults] = useState<ScanSessionResult[]>([]);
  const [pendingScan, setPendingScan] = useState<PendingScan | null>(null);
  const [candidates, setCandidates] = useState<ScanStockCandidate[]>([]);
  const [setupName, setSetupName] = useState('');
  const [draft, setDraft] = useState<ScanStockApprovalRequest | null>(null);
  const [pendingLocationId, setPendingLocationId] = useState<string | null>(
    null
  );
  const [, setClock] = useState(() => Date.now());

  const locations = useQuery<LocationList>({
    queryKey: ['scan-stock-locations'],
    queryFn: async () => {
      const response = await api.get<LocationList>(
        apiUrl(ApiEndpoints.stock_location_list),
        { params: { limit: 500, structural: false, ordering: 'pathstring' } }
      );
      return response.data;
    }
  });

  const activeDraft = useQuery<ScanStockApprovalRequest | null>({
    queryKey: ['scan-stock-active-draft', mode],
    enabled: approvalEnabled,
    queryFn: async () => {
      const response = await api.get<ScanStockApprovalRequestList>(
        apiUrl(ApiEndpoints.stock_scan_requests),
        {
          params: {
            mine: true,
            status: 'DRAFT',
            direction: mode.toUpperCase(),
            limit: 1
          }
        }
      );
      const summary = response.data.results[0];
      if (!summary) return null;
      const detail = await api.get<ScanStockApprovalRequest>(
        apiUrl(ApiEndpoints.stock_scan_request_detail, undefined, {
          id: summary.pk
        })
      );
      return detail.data;
    }
  });

  useEffect(() => {
    setLocationId(initialLocationId);
    setLocationBarcode('');
    setLocationMessage(null);
    setLocationError(null);
    setBarcode('');
    setProjectSelection({ value: '', scope: '' });
    setPurpose('');
    setRemark('');
    setScanError('');
    setPendingScan(null);
    setCandidates([]);
    setResults([]);
    setDraft(null);
    setPendingLocationId(null);
    inputRef.current?.focus();
  }, [mode, initialLocationId]);

  useEffect(() => {
    if (mode === 'outbound')
      setProjectSelection({ value: '', scope: selectionScope });
    setScanError('');
  }, [selectionScope]);

  useEffect(() => {
    if (!approvalEnabled) {
      setDraft(null);
      setPendingLocationId(null);
    }
  }, [approvalEnabled]);

  useEffect(() => {
    if (!approvalEnabled || !activeDraft.isSuccess) return;
    setDraft(activeDraft.data);
    if (activeDraft.data) {
      setLocationId(String(activeDraft.data.location));
    }
  }, [activeDraft.data, activeDraft.isSuccess, approvalEnabled]);

  useEffect(() => {
    onDraftChange?.(draft);
  }, [draft, onDraftChange]);

  useEffect(() => {
    setQuantityConfirmed(false);
    if (mode === 'inbound' && completeDetail.data) {
      setLocationId(String(completeDetail.data.receipt_location));
      setQuantity(Number(completeDetail.data.prepared_quantity));
    }
  }, [mode, completeDetail.data?.pk]);

  useEffect(() => {
    if (
      mode !== 'outbound' ||
      project ||
      completeCode ||
      projectOptions.isFetching
    )
      return;
    const projects = projectOptions.data?.projects ?? [];
    if (!projectOptions.isError && projects.length === 1)
      setProject(projects[0]);
  }, [
    mode,
    project,
    completeCode,
    projectOptions.data,
    projectOptions.isFetching,
    selectionScope
  ]);

  useEffect(() => {
    if (
      mode !== 'outbound' ||
      locationId ||
      !label ||
      completeDetail.isFetching
    )
      return;
    const actualLocations = Array.from(
      new Set((label.stock ?? []).map((item) => item.location))
    );
    if (actualLocations.length === 1) setLocationId(String(actualLocations[0]));
  }, [mode, locationId, label, completeDetail.isFetching]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const locationOptions = (locations.data?.results ?? []).map((location) => ({
    value: String(location.pk),
    label: location.pathstring || location.name
  }));

  const addResult = (result: ScanSessionResult) => {
    setResults((current) => [result, ...current].slice(0, 50));
  };

  const refreshDraft = async (requestId: number) => {
    const response = await api.get<ScanStockApprovalRequest>(
      apiUrl(ApiEndpoints.stock_scan_request_detail, undefined, {
        id: requestId
      })
    );
    setDraft(response.data.status === 'DRAFT' ? response.data : null);
    queryClient.setQueryData(['scan-stock-request', requestId], response.data);
    return response.data;
  };

  const ensureDraft = async (
    selectedLocation: string
  ): Promise<ScanStockApprovalRequest> => {
    if (
      draft?.status === 'DRAFT' &&
      draft.direction === mode.toUpperCase() &&
      draft.location === Number(selectedLocation)
    ) {
      return draft;
    }
    const response = await api.post<ScanStockApprovalRequest>(
      apiUrl(ApiEndpoints.stock_scan_requests),
      {
        direction: mode.toUpperCase(),
        location: Number(selectedLocation)
      }
    );
    setDraft(response.data);
    return response.data;
  };

  const chooseLocation = (value: string | null) => {
    if (draft && value !== null && Number(value) !== draft.location) {
      setPendingLocationId(value);
      return;
    }
    setLocationId(value);
    setLocationMessage(null);
    setLocationError(null);
  };

  const keepDraftAndChangeLocation = () => {
    setDraft(null);
    setLocationId(pendingLocationId);
    setPendingLocationId(null);
    setLocationMessage(null);
    setLocationError(null);
  };

  const cancelDraftAndChangeLocation = async () => {
    if (!draft) return;
    setProcessing(true);
    try {
      await api.post(
        apiUrl(ApiEndpoints.stock_scan_request_cancel, undefined, {
          id: draft.pk
        }),
        { note: t`Scan location changed by requester.` }
      );
      keepDraftAndChangeLocation();
    } catch (error) {
      const data = errorData(error);
      setLocationError(
        data?.error_message || t`The current request could not be cancelled.`
      );
    } finally {
      setProcessing(false);
    }
  };

  const applyScannedLocation = (location: number) => {
    if (draft && location !== draft.location) {
      setPendingLocationId(String(location));
    } else {
      setLocationId(String(location));
    }
    setLocationBarcode('');
    setLocationError(null);
    setLocationMessage(
      mode === 'inbound'
        ? t`Destination location set from barcode.`
        : t`Source location set from barcode.`
    );
  };

  const scanLocationBarcode = async () => {
    const normalized = locationBarcode.trim();
    if (!normalized) return;

    setLocationProcessing(true);
    setLocationMessage(null);
    setLocationError(null);
    try {
      const response = await api.post<BarcodeLocationResponse>(
        apiUrl(ApiEndpoints.barcode),
        { barcode: normalized }
      );
      const scannedLocation = response.data.stocklocation?.pk;
      if (!scannedLocation) {
        setLocationError(t`The scanned barcode is not a stock location.`);
        return;
      }
      applyScannedLocation(scannedLocation);
    } catch (_error) {
      setLocationError(t`The location barcode could not be resolved.`);
    } finally {
      setLocationProcessing(false);
    }
  };

  const submitScan = async (
    scan: PendingScan,
    extra: Record<string, unknown> = {}
  ) => {
    const selectedLocation = scan.locationId;
    if (!selectedLocation) {
      addResult({
        id: scan.clientEventId,
        barcode: scan.barcode,
        success: false,
        errorCode: 'LOCATION_REQUIRED',
        message:
          mode === 'inbound'
            ? t`Select a destination location before scanning stock.`
            : t`Select a source location before scanning stock.`
      });
      return;
    }

    if (processingRef.current) return;

    processingRef.current = true;
    setProcessing(true);
    try {
      const approvalRequest = approvalEnabled
        ? await ensureDraft(selectedLocation)
        : null;
      const endpoint =
        mode === 'inbound'
          ? ApiEndpoints.stock_scan_inbound_v2
          : ApiEndpoints.stock_scan_outbound;
      const payload = {
        barcode: scan.barcode,
        client_event_id: scan.clientEventId,
        quantity: scan.quantity,
        batch: scan.batch,
        ...(scan.projectId ? { project_id: scan.projectId } : {}),
        purpose: mode === 'outbound' ? scan.purpose : '',
        remark: scan.remark,
        ...(scan.receiptId
          ? {
              receipt_id: scan.receiptId,
              quantity_confirmed: scan.quantityConfirmed
            }
          : {}),
        ...(approvalRequest ? { approval_request_id: approvalRequest.pk } : {}),
        ...(mode === 'inbound'
          ? { destination_location: Number(selectedLocation) }
          : { source_location: Number(selectedLocation) }),
        ...extra
      };
      setRetryPayload(payload);
      const response = await api.post<ScanStockResponse>(
        apiUrl(endpoint),
        payload
      );
      const data = response.data;
      setRetryPayload(null);
      setScanError('');

      if (
        !data.terminal &&
        data.error_code === 'LOCATION_BARCODE_REQUIRES_CONTEXT' &&
        data.location
      ) {
        applyScannedLocation(data.location);
        setPendingScan(null);
        setCandidates([]);
        return;
      }

      if (!data.terminal && data.error_code === 'SETUP_REQUIRED') {
        setPendingScan(scan);
        setSetupName('');
        return;
      }
      if (!data.terminal && data.error_code === 'STOCK_SELECTION_REQUIRED') {
        setPendingScan(scan);
        setCandidates(data.candidates ?? []);
        return;
      }

      addResult({
        id: data.event?.client_event_id ?? scan.clientEventId,
        barcode: scan.barcode,
        event: data.event,
        success: data.success,
        replayed: data.replayed,
        errorCode: data.error_code,
        message: data.replayed
          ? approvalEnabled
            ? t`This scan was already processed; the request quantity was not changed again.`
            : t`This scan was already processed; stock was not changed again.`
          : (!data.success ? wmsError({ data }) : '') ||
            data.message ||
            (approvalEnabled
              ? mode === 'outbound'
                ? t`Stock reserved for approval.`
                : t`Stock added to the pending inbound request.`
              : t`Stock updated.`)
      });
      void refreshPostedResult(data, selectedLocation);
      if (approvalRequest) {
        await refreshDraft(approvalRequest.pk);
      }
      setPendingScan(null);
      setCandidates([]);
    } catch (error: any) {
      if (error?.response?.status >= 400 && error.response.status < 500)
        setRetryPayload(null);
      setScanError(wmsError(error));
      const data = errorData(error);
      addResult({
        id: data?.event?.client_event_id ?? scan.clientEventId,
        barcode: scan.barcode,
        event: data?.event,
        success: false,
        errorCode: data?.error_code,
        message: wmsError(error)
      });
    } finally {
      processingRef.current = false;
      setProcessing(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  };

  const startScanValue = (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;
    if (/^WMS[0-9]+:/.test(normalized) && normalized !== barcode.trim()) {
      setBarcode(normalized);
      setCameraOpened(false);
      return;
    }
    if (completeCode) {
      if (
        !completeDetail.data ||
        completeDetail.isFetching ||
        completeDetail.isError
      )
        return;
      const duplicate = draft?.lines?.find(
        (line) =>
          line.source_detail === completeDetail.data.pk &&
          line.outcome === 'PENDING'
      );
      if (duplicate) {
        setScanError(
          t`同一码已加入，已定位原申请行：` + duplicate.request_line_number
        );
        return;
      }
      if (mode === 'inbound' && completeDetail.data.state !== 'READY') {
        setScanError(t`该明细已核对或已入库，请查看原申请。`);
        return;
      }
      if (
        mode === 'inbound' &&
        Number(quantity) !== Number(completeDetail.data.prepared_quantity) &&
        !quantityConfirmed
      ) {
        setScanError(t`实际数量与准备数量不同，请明确核对确认。`);
        return;
      }
    }
    if (
      mode === 'outbound' &&
      (normalized !== barcode.trim() || !outboundReady)
    )
      return;
    if (!project.trim() || (mode === 'outbound' && !purpose.trim())) {
      setScanError(t`项目必填，出库还须填写用途`);
      return;
    }
    if (retryPayload) {
      setScanError(t`请先安全重试并确认上一笔请求`);
      return;
    }
    const scan = {
      locationId,
      barcode: normalized,
      clientEventId: createUuid(),
      quantity,
      batch: project.trim(),
      projectId: completeCode
        ? (completeDetail.data?.project_id ?? undefined)
        : mode === 'inbound'
          ? projectId
          : undefined,
      purpose: purpose.trim(),
      remark: remark.trim(),
      ...(completeCode && completeDetail.data
        ? {
            receiptId: completeDetail.data.receipt,
            quantityConfirmed
          }
        : {})
    };
    if (mode === 'inbound') setBarcode('');
    void submitScan(scan);
  };

  const startScan = () => startScanValue(barcode);

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault();
    }
  };

  const confirmSetup = () => {
    if (
      !pendingScan ||
      !setupName.trim() ||
      !locationId ||
      !setupCategory ||
      !setupUnits.trim()
    )
      return;
    void submitScan(pendingScan, {
      create_part: {
        name: setupName.trim(),
        IPN: pendingScan.barcode,
        description: '',
        category: Number(setupCategory),
        units: setupUnits.trim(),
        default_location: Number(locationId)
      }
    });
  };

  return (
    <Stack gap='lg'>
      {scanError && <Alert color='red'>{scanError}</Alert>}
      {retryPayload && !processing && (
        <Alert color='orange'>
          <Stack>
            <Text>{t`网络结果未确认，安全重试复用原请求，避免重复入出库。`}</Text>
            <Button
              loading={processing}
              onClick={async () => {
                setProcessing(true);
                try {
                  const response = await api.post<ScanStockResponse>(
                    apiUrl(
                      retryPayload.source_location
                        ? ApiEndpoints.stock_scan_outbound
                        : ApiEndpoints.stock_scan_inbound_v2
                    ),
                    retryPayload
                  );
                  setRetryPayload(null);
                  setScanError('');
                  addResult({
                    id:
                      response.data.event?.client_event_id ??
                      String(retryPayload.client_event_id),
                    barcode: String(retryPayload.barcode),
                    event: response.data.event,
                    success: response.data.success,
                    replayed: response.data.replayed,
                    message: response.data.error_message || t`请求已确认`
                  });
                  void refreshPostedResult(
                    response.data,
                    retryPayload.source_location
                  );
                  if (draft) await refreshDraft(draft.pk);
                } catch (e) {
                  setScanError(wmsError(e));
                } finally {
                  setProcessing(false);
                }
              }}
            >{t`安全重试原请求`}</Button>
          </Stack>
        </Alert>
      )}

      <Alert
        color={mode === 'inbound' ? 'blue' : 'orange'}
        icon={mode === 'inbound' ? <IconArrowDown /> : <IconArrowUp />}
        title={mode === 'inbound' ? t`Inbound mode` : t`Outbound mode`}
      >
        {mode === 'inbound'
          ? approvalEnabled
            ? t`Review the scanned information, then add a pending inbound line. Physical stock changes only after the whole request is approved.`
            : t`Review the scanned information, then confirm to add stock to the destination location.`
          : approvalEnabled
            ? t`Review the scanned information, then add an outbound line to reserve stock. Physical stock changes only after the whole request is approved.`
            : t`Review the scanned information, then confirm to remove unallocated stock from the source location.`}
      </Alert>

      <SimpleGrid cols={{ base: 1, md: 2 }} spacing='lg'>
        <Paper withBorder p='lg' radius='md'>
          <Stack>
            <Group gap='sm'>
              <ThemeIcon
                color={mode === 'inbound' ? 'blue' : 'orange'}
                variant='light'
              >
                <IconQrcode />
              </ThemeIcon>
              <Title order={2} size='h3'>
                {t`Scan stock`}
              </Title>
            </Group>

            <Select
              searchable
              clearable={!draft}
              required
              label={
                mode === 'inbound'
                  ? t`Destination location`
                  : t`Source location`
              }
              placeholder={t`Select and confirm a stock location`}
              data={locationOptions}
              value={locationId}
              onChange={chooseLocation}
              disabled={
                locations.isLoading || (completeCode && mode === 'inbound')
              }
              nothingFoundMessage={t`No stock locations found`}
            />

            <Group align='flex-end' wrap='nowrap'>
              <TextInput
                value={locationBarcode}
                onChange={(event) =>
                  setLocationBarcode(event.currentTarget.value)
                }
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void scanLocationBarcode();
                  }
                }}
                label={t`Location barcode`}
                placeholder={t`Scan an INV-SL or linked location barcode`}
                leftSection={<IconMapPin size={20} />}
                maxLength={500}
                style={{ flex: 1 }}
              />
              <Button
                variant='light'
                loading={locationProcessing}
                disabled={!locationBarcode.trim()}
                leftSection={<IconQrcode size={18} />}
                onClick={() => void scanLocationBarcode()}
              >
                {t`Set location`}
              </Button>
            </Group>

            {locationMessage && (
              <Alert color='green' icon={<IconCheck size={18} />}>
                {locationMessage}
              </Alert>
            )}
            {locationError && (
              <Alert color='red' icon={<IconAlertCircle size={18} />}>
                {locationError}
              </Alert>
            )}

            <TextInput
              ref={inputRef}
              required
              value={barcode}
              onChange={(event) => setBarcode(event.currentTarget.value)}
              onKeyDown={handleKeyDown}
              label={t`扫码（二维码／设备ID／库存项码）`}
              description={t`扫码后自动填充，请核对信息和数量后加入申请。`}
              placeholder={t`扫描标签或输入设备编号`}
              leftSection={<IconQrcode size={20} />}
              maxLength={2000}
              size='lg'
            />

            {(completeCode || scannedPart) && (
              <SimpleGrid cols={{ base: 1, sm: 2 }} spacing='sm'>
                <TextInput
                  label={t`设备ID`}
                  readOnly
                  value={label?.equipment ?? scannedPart?.IPN ?? ''}
                  placeholder={t`扫码后自动填充`}
                />
                <TextInput
                  label={t`设备名称`}
                  readOnly
                  value={label?.name ?? scannedPart?.name ?? ''}
                  placeholder={t`扫码后自动填充`}
                />
              </SimpleGrid>
            )}

            {completeCode ? (
              <TextInput
                label={t`项目`}
                readOnly
                value={label?.project ?? ''}
                placeholder={t`扫码后自动填充`}
              />
            ) : mode === 'inbound' ? (
              <WmsProjectSelect
                required
                label={t`项目`}
                value={project}
                onChange={(value, id) => {
                  setProject(value);
                  setProjectId(id);
                }}
              />
            ) : (
              <Select
                required
                searchable
                label={t`项目`}
                placeholder={t`输入设备码后选择项目`}
                disabled={!barcode.trim() || !locationId}
                value={project || null}
                onChange={(value) => setProject(value ?? '')}
                data={
                  barcode.trim() && locationId
                    ? Array.from(
                        new Set([
                          ...(projectOptions.data?.projects ?? []),
                          ...(project ? [project] : [])
                        ])
                      )
                    : []
                }
              />
            )}
            <TextInput
              label={t`合同号`}
              readOnly
              value={label?.contract_number ?? ''}
              placeholder={
                !completeCode && barcode.trim()
                  ? t`此码未包含合同号`
                  : t`扫描完整二维码后自动填充`
              }
            />

            {completeCode && (
              <Stack gap='xs'>
                {completeDetail.isFetching && <Text>{t`正在识别标签…`}</Text>}
                {completeDetail.error && (
                  <Alert color='red'>{wmsError(completeDetail.error)}</Alert>
                )}
                {label && (
                  <Alert>
                    <Text>
                      {t`验收日期（合同起始时间）`}：
                      {label.acceptance_date || t`待验收`} · {t`质保到期日`}：
                      {label.warranty_end || t`待验收`}
                    </Text>
                    {mode === 'inbound' && (
                      <Text>
                        {t`准备数量`}：{label.prepared_quantity}
                      </Text>
                    )}
                    {mode === 'outbound' && (
                      <Text>
                        {t`当前库存`}：
                        {label.stock?.reduce(
                          (n, s) => n + Number(s.quantity),
                          0
                        ) ?? 0}{' '}
                        · {t`可用数量`}：
                        {label.stock?.reduce(
                          (n, s) => n + Number(s.available),
                          0
                        ) ?? 0}
                      </Text>
                    )}
                  </Alert>
                )}
              </Stack>
            )}
            {mode === 'outbound' && !completeCode && (
              <Alert
                color={
                  projectOptions.isError
                    ? 'red'
                    : outboundReady
                      ? 'blue'
                      : 'orange'
                }
                aria-live='polite'
              >
                <Stack gap='xs'>
                  {!locationId || !barcode.trim() ? (
                    <Text>{t`请选择实际库位并输入设备码查看库存。`}</Text>
                  ) : projectOptions.isFetching ? (
                    <Text>{t`正在查询库存…`}</Text>
                  ) : projectOptions.isError ? (
                    <Text>{t`库存查询失败，请重试。`}</Text>
                  ) : stockStatus && 'quantity' in stockStatus ? (
                    <>
                      <Text>
                        {stockStatus.scope === 'all_projects'
                          ? t`当前库位该设备所有项目合计`
                          : t`当前设备、项目及实际库位`}
                      </Text>
                      <Text>
                        {t`当前库存`}: {stockStatus.quantity}{' '}
                        {stockStatus.units} · {t`可用数量`}:{' '}
                        {stockStatus.available} {stockStatus.units}
                      </Text>
                      <Text size='sm'>
                        {t`冻结数量`}: {stockStatus.frozen} · {t`业务占用数量`}:{' '}
                        {stockStatus.allocated}
                      </Text>
                      {stockStatus.state === 'NO_STOCK' ? (
                        <Text>
                          {stockStatus.scope === 'project'
                            ? t`当前项目在此库位暂无该设备库存，无法出库。`
                            : t`当前库位该设备暂无库存，无法出库。`}
                        </Text>
                      ) : stockStatus.state === 'NO_AVAILABLE_STOCK' ? (
                        <Text>{t`当前无可用库存，库存已冻结或占用。`}</Text>
                      ) : null}
                      {!project && stockStatus.state !== 'NO_STOCK' && (
                        <Text>{t`请选择项目查看可用库存`}</Text>
                      )}
                    </>
                  ) : (
                    <Text>
                      {stockStatus?.error_message ||
                        t`设备码无法识别，请检查设备ID或库存项码。`}
                    </Text>
                  )}
                  {locationId && barcode.trim() && (
                    <Button
                      size='xs'
                      variant='subtle'
                      loading={projectOptions.isFetching}
                      onClick={() => void projectOptions.refetch()}
                    >{t`重新查询库存`}</Button>
                  )}
                </Stack>
              </Alert>
            )}
            {mode === 'outbound' && (
              <TextInput
                required
                label={t`用途`}
                maxLength={250}
                value={purpose}
                onChange={(e) => setPurpose(e.currentTarget.value)}
              />
            )}
            <TextInput
              label={t`本次扫码备注`}
              maxLength={2000}
              value={remark}
              onChange={(e) => setRemark(e.currentTarget.value)}
            />
            <NumberInput
              label={t`Quantity per scan`}
              value={quantity}
              onChange={(value) => {
                setQuantity(value);
                setQuantityConfirmed(false);
              }}
              min={0.00001}
              decimalScale={5}
            />
            {completeCode &&
              mode === 'inbound' &&
              completeDetail.data &&
              Number(quantity) !==
                Number(completeDetail.data.prepared_quantity) && (
                <Checkbox
                  label={t`已核对实际数量与准备数量的差异`}
                  checked={quantityConfirmed}
                  onChange={(e) =>
                    setQuantityConfirmed(e.currentTarget.checked)
                  }
                />
              )}

            <SimpleGrid cols={1} spacing='sm'>
              {/* 摄像头入口暂时停用，恢复时取消注释并恢复 IconCamera 导入。
              {globalSettings.isSet('BARCODE_WEBCAM_SUPPORT', true) && (
                <Button
                  size='lg'
                  variant='light'
                  leftSection={<IconCamera size={20} />}
                  onClick={() => setCameraOpened(true)}
                >
                  {t`Camera Input`}
                </Button>
              )}
              */}
              <Button
                size='lg'
                color={mode === 'inbound' ? 'blue' : 'orange'}
                loading={processing}
                disabled={
                  !barcode.trim() ||
                  !locationId ||
                  !project.trim() ||
                  (completeCode &&
                    (completeDetail.isFetching ||
                      completeDetail.isError ||
                      !completeDetail.data)) ||
                  (mode === 'outbound' &&
                    (!purpose.trim() || !outboundReady)) ||
                  !(Number(quantity) > 0)
                }
                leftSection={<IconCheck size={20} />}
                onClick={startScan}
              >
                {approvalEnabled
                  ? mode === 'inbound'
                    ? t`Add pending inbound`
                    : t`加入待出库`
                  : mode === 'inbound'
                    ? t`Add stock`
                    : t`Remove stock`}
              </Button>
            </SimpleGrid>
          </Stack>
        </Paper>

        <Card withBorder p='lg' radius='md'>
          <Stack>
            <Group justify='space-between'>
              <Title order={2} size='h3'>
                {approvalEnabled && draft
                  ? t`Current request`
                  : t`Session summary`}
              </Title>
              <Group gap='xs'>
                <Badge color='blue'>
                  {approvalEnabled ? t`Accepted` : t`Success`}:{' '}
                  {results.filter((result) => result.success).length}
                </Badge>
                <Badge color='red'>
                  {t`Failed`}:{' '}
                  {results.filter((result) => !result.success).length}
                </Badge>
              </Group>
            </Group>
            {approvalEnabled && draft ? (
              <Stack gap='xs'>
                <Group justify='space-between'>
                  <Text size='sm' c='dimmed'>
                    {t`Request`}
                  </Text>
                  <Text fw={600}>{draft.reference}</Text>
                </Group>
                <Group justify='space-between'>
                  <Text size='sm' c='dimmed'>
                    {t`Location`}
                  </Text>
                  <Text fw={600}>{approvalLocationLabel(draft)}</Text>
                </Group>
                <Group justify='space-between'>
                  <Text size='sm' c='dimmed'>
                    {t`Approval route`}
                  </Text>
                  <Text fw={600}>{approvalOwnerLabel(draft)}</Text>
                </Group>
                <Group justify='space-between'>
                  <Text size='sm' c='dimmed'>
                    {mode === 'outbound'
                      ? t`Frozen quantity`
                      : t`Pending inbound quantity`}
                  </Text>
                  <Text fw={600}>
                    {mode === 'outbound'
                      ? (draft.scan_frozen ?? draft.total_quantity)
                      : (draft.pending_inbound ?? draft.total_quantity)}{' '}
                    ({draft.active_line_count} {t`lines`})
                  </Text>
                </Group>
                <Group justify='space-between'>
                  <Text size='sm' c='dimmed'>
                    {t`Draft time remaining`}
                  </Text>
                  <Badge
                    color={
                      remainingTime(draft.expires_at) === t`Expired`
                        ? 'red'
                        : 'yellow'
                    }
                    leftSection={<IconClock size={12} />}
                  >
                    {remainingTime(draft.expires_at)}
                  </Badge>
                </Group>
              </Stack>
            ) : (
              <Text size='sm' c='dimmed'>
                {approvalEnabled
                  ? activeDraft.isLoading
                    ? t`Restoring your most recent draft…`
                    : t`A draft request is created when you first confirm and add a line.`
                  : t`Changing mode clears the location and current session results.`}
              </Text>
            )}
          </Stack>
        </Card>
      </SimpleGrid>

      <ScanResultList results={results} />

      <Modal
        opened={cameraOpened}
        onClose={() => setCameraOpened(false)}
        title={t`Camera Input`}
        size='lg'
        centered
      >
        <BarcodeCameraInput
          onScan={(value) => {
            setBarcode(value);
            setScanError('');
            setCameraOpened(false);
          }}
        />
      </Modal>

      {approvalEnabled && draft && (
        <Paper withBorder p='lg' radius='md'>
          <ScanApprovalRequestDetails
            requestId={draft.pk}
            initialRequest={draft}
            onChanged={(request) => {
              void refreshStockOptions();
              if (request.status === 'DRAFT') {
                setDraft(request);
              } else {
                setDraft(null);
                setResults([]);
                void activeDraft.refetch();
              }
            }}
          />
        </Paper>
      )}

      <Modal
        opened={Boolean(pendingScan && candidates.length > 0)}
        onClose={() => {
          setPendingScan(null);
          setCandidates([]);
        }}
        title={t`Select a stock item`}
        closeOnClickOutside={false}
      >
        <Stack>
          <Alert icon={<IconAlertCircle />} color='yellow'>
            {t`当前设备、项目及地点存在多个库存项，请明确选择。`}
          </Alert>
          {candidates.map((candidate) => (
            <Card key={candidate.pk} withBorder>
              <Group justify='space-between' align='center'>
                <div>
                  <Text fw={700}>#{candidate.pk}</Text>
                  <Text size='sm'>
                    {t`项目`}: {candidate.batch || t`None`}
                  </Text>
                  <Text size='sm'>
                    {t`Available`}: {candidate.unallocated_quantity}
                  </Text>
                </div>
                <Button
                  onClick={() => {
                    if (pendingScan) {
                      void submitScan(pendingScan, {
                        stock_item_id: candidate.pk
                      });
                    }
                  }}
                >
                  {t`Select`}
                </Button>
              </Group>
            </Card>
          ))}
          {mode === 'inbound' && (
            <Button
              variant='light'
              onClick={() => {
                if (pendingScan) {
                  void submitScan(pendingScan, { create_stock_item: true });
                }
              }}
            >
              {t`Create a new stock item`}
            </Button>
          )}
        </Stack>
      </Modal>

      <Modal
        opened={Boolean(
          mode === 'inbound' && pendingScan && candidates.length === 0
        )}
        onClose={() => setPendingScan(null)}
        title={
          approvalEnabled
            ? t`Create product for pending inbound`
            : t`Create product and add stock`
        }
        closeOnClickOutside={false}
      >
        <Stack>
          <Alert color='blue'>
            {approvalEnabled
              ? t`This IPN is unknown. The setup is stored on the request; product creation and stock addition happen only if the whole request is approved.`
              : t`This IPN is unknown. Product creation, stock creation, and inbound quantity will commit together.`}
          </Alert>
          <TextInput
            readOnly
            label={t`Internal part number (IPN)`}
            description={t`The scanned code becomes the new product IPN and is used for future scans.`}
            value={pendingScan?.barcode ?? ''}
          />
          <TextInput
            required
            label={t`Product name`}
            value={setupName}
            onChange={(event) => setSetupName(event.currentTarget.value)}
          />
          <Select
            required
            searchable
            label={t`设备分类`}
            value={setupCategory}
            onChange={setSetupCategory}
            data={projectOptions.data?.categories ?? []}
          />
          <TextInput
            required
            label={t`单位`}
            value={setupUnits}
            onChange={(e) => setSetupUnits(e.currentTarget.value)}
          />
          <Button
            loading={processing}
            disabled={!setupName.trim() || !setupCategory || !setupUnits.trim()}
            onClick={confirmSetup}
          >
            {approvalEnabled
              ? t`Add product setup to request`
              : t`Create product and add stock`}
          </Button>
        </Stack>
      </Modal>

      <Modal
        opened={pendingLocationId !== null}
        onClose={() => setPendingLocationId(null)}
        title={t`Change request location`}
        centered
        closeOnClickOutside={!processing}
      >
        <Stack>
          <Alert color='orange' icon={<IconSwitchHorizontal size={18} />}>
            {t`The current request is tied to one location. Keep it and start a new request at the selected location, or cancel it and release any outbound reservation.`}
          </Alert>
          <Group justify='flex-end'>
            <Button
              variant='default'
              disabled={processing}
              onClick={() => setPendingLocationId(null)}
            >
              {t`Back`}
            </Button>
            <Button
              variant='light'
              disabled={processing}
              onClick={keepDraftAndChangeLocation}
            >
              {t`Keep request and start new`}
            </Button>
            <Button
              color='red'
              loading={processing}
              onClick={() => void cancelDraftAndChangeLocation()}
            >
              {t`Cancel request and switch`}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
