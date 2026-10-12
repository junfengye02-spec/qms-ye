import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { ModelType } from '@lib/enums/ModelType';
import { apiUrl } from '@lib/functions/Api';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Group,
  Modal,
  Stack,
  Tabs,
  Text,
  Title
} from '@mantine/core';
import {
  IconArrowDown,
  IconArrowUp,
  IconChecks,
  IconClipboardList,
  IconHistory,
  IconPackage,
  IconQrcode,
  IconUser
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, queryClient } from '../../App';
import PageTitle from '../../components/nav/PageTitle';
import { wmsAccess } from '../../functions/wms';
import { useGlobalSettingsState } from '../../states/SettingsStates';
import { useUserState } from '../../states/UserState';
import { StockTrackingTable } from '../../tables/stock/StockTrackingTable';
import { ScanApprovalRequestListPanel } from './ScanApprovalRequestList';
import { ScanStockEventTable } from './ScanStockEventTable';
import type {
  ScanMode,
  ScanStockApprovalRequest,
  ScanStockResponse
} from './ScanStockTypes';
import { ScanWorkPanel } from './ScanWorkPanel';
import { WmsCompleteScan } from './WmsCompleteScan';
import type { WmsReceipt } from './WmsContractTypes';
import { wmsError, wmsUrl } from './WmsInventoryPanel';

export default function ScanStockPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useUserState();
  const globalSettings = useGlobalSettingsState();
  const approvalEnabled = globalSettings.isSet('SCAN_APPROVAL_ENABLED');
  const { canApprove } = wmsAccess(user.user, approvalEnabled);
  const mode: ScanMode = location.pathname.includes('/outbound')
    ? 'outbound'
    : 'inbound';
  const [nextMode, setNextMode] = useState<ScanMode | null>(null);
  const [currentDraft, setCurrentDraft] =
    useState<ScanStockApprovalRequest | null>(null);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const canViewStock = user.hasViewPermission(ModelType.stockitem);
  const canScanStock =
    canViewStock && user.hasChangePermission(ModelType.stockitem);
  const receiptId = mode === 'inbound' ? location.state?.wmsReceiptId : null;
  const receiptQuery = useQuery<{ results: WmsReceipt[] }>({
    queryKey: ['wms-receipts', receiptId],
    enabled: !!receiptId && canScanStock && approvalEnabled,
    queryFn: async () =>
      (await api.get(wmsUrl('receipts'), { params: { pk: receiptId } })).data
  });
  const receipt = receiptQuery.data?.results.find((r) => r.pk === receiptId);

  const switchMode = (target: ScanMode) => {
    if (target !== mode) {
      setNextMode(target);
    }
  };

  const confirmModeSwitch = () => {
    if (!nextMode) return;
    navigate(`/stock/scan/${nextMode}/`);
    setNextMode(null);
  };

  const cancelAndSwitchMode = async () => {
    if (!approvalEnabled || !currentDraft) {
      confirmModeSwitch();
      return;
    }
    setSwitching(true);
    setSwitchError(null);
    try {
      await api.post(
        apiUrl(ApiEndpoints.stock_scan_request_cancel, undefined, {
          id: currentDraft.pk
        }),
        { note: t`Scan direction changed by requester.` }
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['scan-stock-requests'] }),
        queryClient.invalidateQueries({ queryKey: ['scan-stock-active-draft'] })
      ]);
      setCurrentDraft(null);
      confirmModeSwitch();
    } catch (error: any) {
      const data: Partial<ScanStockResponse> =
        error?.response?.data ?? error?.data ?? {};
      setSwitchError(
        data.error_message || t`The current request could not be cancelled.`
      );
    } finally {
      setSwitching(false);
    }
  };

  return (
    <Stack gap='lg'>
      <PageTitle
        title={
          mode === 'inbound' ? t`Scan stock inbound` : t`Scan stock outbound`
        }
      />

      <Group justify='space-between' align='flex-start'>
        <div>
          <Title order={1}>{t`Scan stock`}</Title>
          <Text c='dimmed'>
            {t`Use one workspace for inbound, outbound, current stock, and audit records.`}
          </Text>
        </div>
        <Group gap='xs'>
          <Button
            color='blue'
            variant={mode === 'inbound' ? 'filled' : 'light'}
            leftSection={<IconArrowDown size={18} />}
            onClick={() => switchMode('inbound')}
          >
            {t`Inbound`}
          </Button>
          <Button
            color='orange'
            variant={mode === 'outbound' ? 'filled' : 'light'}
            leftSection={<IconArrowUp size={18} />}
            onClick={() => switchMode('outbound')}
          >
            {t`Outbound`}
          </Button>
        </Group>
      </Group>

      <Tabs
        defaultValue={
          canApprove &&
          (!canScanStock ||
            new URLSearchParams(location.search).get('tab') === 'approval')
            ? 'approval'
            : canScanStock
              ? 'work'
              : canViewStock
                ? 'stock'
                : approvalEnabled
                  ? 'approval'
                  : undefined
        }
        keepMounted={false}
      >
        <Tabs.List>
          {canScanStock && (
            <Tabs.Tab value='work' leftSection={<IconQrcode size={16} />}>
              {t`Scan work`}
            </Tabs.Tab>
          )}
          {canViewStock && (
            <Tabs.Tab value='stock' leftSection={<IconPackage size={16} />}>
              {t`Current stock`}
            </Tabs.Tab>
          )}
          {approvalEnabled && canScanStock && (
            <Tabs.Tab value='mine' leftSection={<IconUser size={16} />}>
              {t`My requests`}
            </Tabs.Tab>
          )}
          {canApprove && (
            <Tabs.Tab value='approval' leftSection={<IconChecks size={16} />}>
              {t`Pending my approval`}
            </Tabs.Tab>
          )}
          {canApprove && (
            <Tabs.Tab
              value='approval-history'
              leftSection={<IconHistory size={16} />}
            >
              {t`审批记录`}
            </Tabs.Tab>
          )}
          {canViewStock && (
            <Tabs.Tab value='records' leftSection={<IconHistory size={16} />}>
              {t`Operation records`}
            </Tabs.Tab>
          )}
        </Tabs.List>

        {canScanStock && (
          <Tabs.Panel value='work' pt='lg'>
            {approvalEnabled ? (
              receiptId ? (
                <Stack>
                  <Group justify='space-between'>
                    <Text>
                      {t`入库单`} #{receiptId}
                      {receipt ? ` · ${receipt.project}` : ''}
                    </Text>
                    <Button
                      variant='subtle'
                      disabled={!!currentDraft?.active_line_count}
                      onClick={() =>
                        navigate('/stock/scan/inbound/', {
                          replace: true,
                          state: null
                        })
                      }
                    >{t`切换为普通扫码`}</Button>
                  </Group>
                  {receiptQuery.isError ? (
                    <Alert color='red'>{wmsError(receiptQuery.error)}</Alert>
                  ) : receiptQuery.isPending ? (
                    <Text>{t`正在加载入库单…`}</Text>
                  ) : !receipt || receipt.cancelled ? (
                    <Alert color='red'>{t`入库单不存在或已取消。`}</Alert>
                  ) : (
                    <WmsCompleteScan
                      key={receipt.pk}
                      mode='inbound'
                      receipt={receipt}
                      onDraftChange={setCurrentDraft}
                    />
                  )}
                </Stack>
              ) : (
                <ScanWorkPanel
                  key={mode}
                  mode={mode}
                  onDraftChange={setCurrentDraft}
                />
              )
            ) : (
              <Alert color='yellow'>{t`请管理员启用扫码审批后使用完整标签业务。`}</Alert>
            )}
          </Tabs.Panel>
        )}

        {canViewStock && (
          <Tabs.Panel value='stock' pt='lg'>
            <Button
              variant='light'
              onClick={() => navigate('/stock/location/index/stock-items')}
            >{t`前往库存页面`}</Button>
          </Tabs.Panel>
        )}

        {approvalEnabled && canScanStock && (
          <Tabs.Panel value='mine' pt='lg'>
            <ScanApprovalRequestListPanel scope='mine' mode={mode} />
          </Tabs.Panel>
        )}

        {canApprove && (
          <Tabs.Panel value='approval' pt='lg'>
            <ScanApprovalRequestListPanel scope='approval' mode={mode} />
          </Tabs.Panel>
        )}
        {canApprove && (
          <Tabs.Panel value='approval-history' pt='lg'>
            <ScanApprovalRequestListPanel scope='history' mode={mode} />
          </Tabs.Panel>
        )}

        {canViewStock && (
          <Tabs.Panel value='records' pt='lg'>
            <Tabs defaultValue='posted' keepMounted={false} variant='outline'>
              <Tabs.List>
                <Tabs.Tab value='posted'>{t`期间入出库记录`}</Tabs.Tab>
                <Tabs.Tab
                  value='tracking'
                  leftSection={<IconClipboardList size={16} />}
                >
                  {t`Stock tracking`}
                </Tabs.Tab>
                <Tabs.Tab value='scans' leftSection={<IconQrcode size={16} />}>
                  {t`Scan records`}
                </Tabs.Tab>
              </Tabs.List>
              <Tabs.Panel value='posted' pt='md'>
                <Button
                  variant='light'
                  onClick={() =>
                    navigate('/stock/location/index/stock-items', {
                      state: { wmsTool: 'records' }
                    })
                  }
                >{t`查询期间入出库记录`}</Button>
              </Tabs.Panel>
              <Tabs.Panel value='tracking' pt='md'>
                <StockTrackingTable scope='global' />
              </Tabs.Panel>
              <Tabs.Panel value='scans' pt='md'>
                <ScanStockEventTable mode={mode} />
              </Tabs.Panel>
            </Tabs>
          </Tabs.Panel>
        )}
      </Tabs>

      <Modal
        opened={nextMode !== null}
        onClose={() => setNextMode(null)}
        title={t`Confirm mode change`}
        centered
      >
        <Stack>
          {switchError && <Alert color='red'>{switchError}</Alert>}
          <Alert color={nextMode === 'outbound' ? 'orange' : 'blue'}>
            {approvalEnabled && currentDraft
              ? t`The current draft is tied to one direction. Keep it for later and start a new request, or cancel it and release any outbound reservation.`
              : t`Changing mode clears the selected location, pending choices, and session results. Confirm the new stock direction before scanning again.`}
          </Alert>
          <Group justify='flex-end'>
            <Button
              variant='default'
              disabled={switching}
              onClick={() => {
                setNextMode(null);
                setSwitchError(null);
              }}
            >
              {t`Cancel`}
            </Button>
            <Button
              color={nextMode === 'outbound' ? 'orange' : 'blue'}
              variant={approvalEnabled && currentDraft ? 'light' : 'filled'}
              disabled={switching}
              onClick={confirmModeSwitch}
            >
              {approvalEnabled && currentDraft
                ? t`Keep request and switch`
                : nextMode === 'outbound'
                  ? t`Switch to outbound`
                  : t`Switch to inbound`}
            </Button>
            {approvalEnabled && currentDraft && (
              <Button
                color='red'
                loading={switching}
                onClick={() => void cancelAndSwitchMode()}
              >
                {t`Cancel request and switch`}
              </Button>
            )}
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
