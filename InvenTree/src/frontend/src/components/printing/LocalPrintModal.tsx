import { t } from '@lingui/core/macro';
import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Group,
  Modal,
  NumberInput,
  Select,
  Stack,
  Text
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../contexts/ApiContext';
import {
  LOCAL_PRINT_URL,
  type LocalPrintJob,
  printErrorKind,
  validSavedQueue
} from '../../functions/printing/LocalPrintClient';
import {
  getLocalPrintClient,
  localPrintSession
} from '../../functions/printing/localPrint';
import { createUuid } from '../../functions/uuid';
import { useUserState } from '../../states/UserState';

export type LocalPrintConfig = {
  enabled: boolean;
  qz_version: string;
  download_url: string;
  max_labels: number;
};

function statusText(state: LocalPrintJob['state']) {
  return {
    generating: t`正在生成 PDF`,
    ready: t`PDF 已就绪，等待提交`,
    submitting: t`正在提交，请勿重复打印`,
    submitted: t`已提交到本机打印队列`,
    uncertain: t`结果待确认，请检查 Windows 打印队列后决定是否补打`,
    failed: t`任务失败，可下载已有 PDF 或创建补打任务`
  }[state];
}

function errorText(error: unknown) {
  const response = (error as { response?: { status: number; data: unknown } })
    ?.response;
  if (response && [400, 403, 404].includes(response.status)) {
    const details =
      typeof response.data === 'string'
        ? response.data
        : JSON.stringify(response.data);
    return `${t`服务端拒绝了此操作：`}${details.slice(0, 500)}`;
  }
  const kind = printErrorKind(error);
  const messages: Record<string, string> = {
    session: t`登录已变化，已停止发送。请重新打开打印窗口。`,
    version: t`打印组件版本不支持，请安装管理员指定的版本。`,
    trust: t`证书或信任验证失败，请检查本站信任和服务端签名证书。`,
    browser: t`浏览器阻止了本机连接，请检查本站的本地网络访问权限。`,
    timeout: t`连接或提交超时。请检查组件和系统队列，不要直接重复打印。`,
    queue: t`所选打印机已不可用，请刷新列表并重新选择。`,
    connection: t`操作未完成。请检查打印组件是否运行、登录权限和服务端配置。`
  };
  return messages[kind];
}

export function LocalPrintModal({
  items,
  itemDetails,
  modelType,
  config,
  onClose
}: {
  items: number[];
  itemDetails?: Array<{ id: number; label: string; copies: number | string }>;
  modelType: string;
  config: LocalPrintConfig;
  onClose: () => void;
}) {
  const api = useApi();
  const userId = useUserState((state) => state.user?.pk);
  const storageKey = `wms-local-print:${window.location.origin}:${api.defaults.baseURL ?? ''}`;
  const pendingKey = `${storageKey}:pending:${userId}`;
  const [queues, setQueues] = useState<string[]>([]);
  const [queue, setQueue] = useState<string | null>(null);
  const [copies, setCopies] = useState(1);
  const [itemCopies, setItemCopies] = useState<Record<string, number | string>>(
    () =>
      Object.fromEntries(
        (itemDetails ?? []).map((item) => [String(item.id), item.copies])
      )
  );
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState<LocalPrintJob>();
  const [confirmed, setConfirmed] = useState(false);
  const [install, setInstall] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const working = useRef(false);
  const alive = useRef(true);
  const pending = useRef<any>(undefined);

  useEffect(() => {
    alive.current = true;
    try {
      pending.current = JSON.parse(
        sessionStorage.getItem(pendingKey) || 'null'
      );
    } catch {
      pending.current = undefined;
    }
    setHasPending(!!pending.current);
    return () => {
      alive.current = false;
    };
  }, [pendingKey]);

  const recent = useQuery<LocalPrintJob[]>({
    queryKey: ['local-print-jobs', userId],
    queryFn: async () => (await api.get(`${LOCAL_PRINT_URL}jobs/`)).data,
    refetchInterval: busy ? 2000 : false
  });

  const monitor = useQuery<LocalPrintJob>({
    queryKey: ['local-print-job', userId, job?.id],
    enabled: !!job && ['generating', 'submitting'].includes(job.state),
    queryFn: async () =>
      (await api.get(`${LOCAL_PRINT_URL}jobs/${job?.id}/`)).data,
    refetchInterval: 1500
  });
  useEffect(() => {
    if (monitor.data) setJob(monitor.data);
  }, [monitor.data]);

  async function run(action: (check: () => void) => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError('');
    const session = localPrintSession();
    const check = () => {
      if (!alive.current || !session || session !== localPrintSession())
        throw new Error('Login session changed');
    };
    try {
      check();
      await action(check);
    } catch (failure) {
      if (alive.current) setError(errorText(failure));
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }

  async function detect(check: () => void) {
    setConnected(false);
    setQueues([]);
    const client = await getLocalPrintClient(api);
    await client.connect(config.qz_version);
    const printers = await client.printers();
    check();
    setQueues(printers);
    let saved = queue;
    try {
      saved ??= localStorage.getItem(`${storageKey}:queue`);
    } catch {
      /* Storage may be disabled. */
    }
    const selected = validSavedQueue(saved, printers);
    setQueue(selected);
    if (!selected) {
      try {
        localStorage.removeItem(`${storageKey}:queue`);
      } catch {
        /* Optional preference. */
      }
    }
    setConnected(true);
  }

  async function create(check: () => void, reprint = false, pdfOnly = false) {
    const original = reprint ? job : undefined;
    const counts = original
      ? original.item_copies
      : itemDetails
        ? Object.fromEntries(
            items.map((id) => [String(id), Number(itemCopies[String(id)])])
          )
        : undefined;
    const payload = pending.current?.payload ?? {
      request_key: createUuid(),
      model_type: original?.model_type ?? modelType,
      items: original?.items ?? items,
      copies: original?.copies ?? (itemDetails ? 1 : copies),
      ...(counts && Object.keys(counts).length ? { item_copies: counts } : {}),
      queue: queue || (pdfOnly ? 'PDF download only' : '')
    };
    const url =
      pending.current?.url ??
      (original
        ? `${LOCAL_PRINT_URL}jobs/${original.id}/reprint/`
        : `${LOCAL_PRINT_URL}jobs/`);
    pending.current = { payload, url };
    setHasPending(true);
    // Persist before POST so a lost response can be retried with the same key.
    sessionStorage.setItem(pendingKey, JSON.stringify(pending.current));
    const response = await api.post(url, payload);
    check();
    pending.current = undefined;
    setHasPending(false);
    sessionStorage.removeItem(pendingKey);
    setJob(response.data);
    setConfirmed(false);
    await recent.refetch();
    return response.data as LocalPrintJob;
  }

  async function awaitPdf(current: LocalPrintJob, check: () => void) {
    const deadline = Date.now() + 120000;
    while (current.state === 'generating') {
      if (Date.now() > deadline) throw new Error('PDF generation timeout');
      await new Promise((resolve) => setTimeout(resolve, 750));
      check();
      current = (await api.get(`${LOCAL_PRINT_URL}jobs/${current.id}/`)).data;
      check();
      setJob(current);
    }
    if (current.state === 'failed') throw new Error('PDF generation failed');
    return current;
  }

  async function print(check: () => void, reprint = false) {
    let current = reprint || !job ? await create(check, reprint) : job;
    current = await awaitPdf(current, check);
    check();
    if (current.queue !== queue)
      throw new Error('Task queue differs from selected queue');
    const client = await getLocalPrintClient(api);
    await client.connect(config.qz_version);
    check();
    try {
      setJob(await client.submit(current, check));
    } finally {
      if (alive.current && localPrintSession()) {
        const response = await api
          .get(`${LOCAL_PRINT_URL}jobs/${current.id}/`)
          .catch(() => undefined);
        if (response) setJob(response.data);
        await recent.refetch();
      }
    }
  }

  async function download(check: () => void) {
    const current = await awaitPdf(
      job ?? (await create(check, false, true)),
      check
    );
    const { data } = await api.get(`${LOCAL_PRINT_URL}jobs/${current.id}/pdf/`);
    check();
    const bytes = Uint8Array.from(atob(data.data), (char) =>
      char.charCodeAt(0)
    );
    const url = URL.createObjectURL(
      new Blob([bytes], { type: 'application/pdf' })
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `labels-${current.id}.pdf`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  const terminal =
    job && ['submitted', 'uncertain', 'failed'].includes(job.state);
  const selectedTotal = itemDetails
    ? items.reduce((sum, id) => sum + Number(itemCopies[String(id)]), 0)
    : items.length * copies;
  const invalidCopies =
    !Number.isInteger(selectedTotal) ||
    selectedTotal < 1 ||
    selectedTotal > config.max_labels ||
    (itemDetails &&
      items.some(
        (id) =>
          !Number.isInteger(Number(itemCopies[String(id)])) ||
          Number(itemCopies[String(id)]) < 1
      ));
  const total = job?.total ?? selectedTotal;
  const installerUrl = /^https:\/\//.test(config.download_url)
    ? config.download_url
    : undefined;

  return (
    <Modal
      opened
      onClose={() => {
        if (!busy) onClose();
      }}
      title={t`本机标签打印`}
      closeOnClickOutside={!busy}
      closeOnEscape={!busy}
      withCloseButton={!busy}
      size='lg'
    >
      <Stack>
        <Text fw={600}>
          {modelType === 'wmsreceiptdetail'
            ? t`完整设备标签 · 50 × 30 mm`
            : t`商品 IPN 标签 · 50 × 30 mm`}
        </Text>
        <Text
          size='sm'
          c='dimmed'
        >{t`标签已按份数生成。提交成功只表示进入本机队列，请检查实际出纸。`}</Text>
        {error && <Alert color='red'>{error}</Alert>}
        <Group>
          <Button
            variant='light'
            disabled={busy}
            onClick={() => setInstall(!install)}
          >{t`安装打印组件`}</Button>
          <Button
            variant='default'
            disabled={busy}
            onClick={() => void run(detect)}
          >
            {connected ? t`刷新打印机` : t`重新检测`}
          </Button>
          <Button
            variant='default'
            disabled={busy || hasPending || (!job && !!invalidCopies)}
            onClick={() => void run(download)}
          >{t`下载 PDF`}</Button>
        </Group>
        {hasPending && (
          <Alert color='orange'>
            <Text>{t`上次创建请求的结果尚未确认。先查询该请求，再决定是否提交，避免重复创建。`}</Text>
            <Button
              disabled={busy}
              onClick={() =>
                void run(async (check) => {
                  await create(check);
                })
              }
            >{t`查询上次请求`}</Button>
          </Alert>
        )}
        {install && (
          <Alert title={t`Windows 安装说明`}>
            <Stack gap='xs'>
              <Text size='sm'>{t`先安装打印机厂商驱动，并确认 Windows 测试页能打印。安装 QZ Tray，允许登录自启，完成管理员提供的本站信任设置，再返回此处重新检测。`}</Text>
              <Text size='sm'>
                {t`要求组件版本：`}
                {config.qz_version}
              </Text>
              {installerUrl ? (
                <Anchor
                  href={installerUrl}
                  target='_blank'
                  rel='noopener noreferrer'
                >{t`下载管理员指定安装包`}</Anchor>
              ) : (
                <Text size='sm'>{t`尚未配置经过验证的安装包，请联系管理员。当前可下载 PDF。`}</Text>
              )}
            </Stack>
          </Alert>
        )}
        <Select
          label={t`本机打印机`}
          placeholder={connected ? t`请选择打印机` : t`请先检测打印组件`}
          data={queues}
          value={queue}
          disabled={!connected || busy}
          searchable
          onChange={(value) => {
            setQueue(value);
            try {
              if (value) localStorage.setItem(`${storageKey}:queue`, value);
              else localStorage.removeItem(`${storageKey}:queue`);
            } catch {
              /* Optional preference. */
            }
          }}
        />
        {itemDetails ? (
          <Stack gap='xs'>
            <Text size='sm' fw={600}>{t`每台设备的打印数量`}</Text>
            {(job?.items ?? items).map((id) => (
              <Group key={id} wrap='nowrap'>
                <Text size='sm' style={{ flex: 1, overflowWrap: 'anywhere' }}>
                  {itemDetails.find((item) => item.id === id)?.label ??
                    `#${id}`}
                </Text>
                <NumberInput
                  aria-label={`${t`打印数量`} · ${itemDetails.find((item) => item.id === id)?.label ?? `#${id}`}`}
                  w={95}
                  min={1}
                  max={config.max_labels}
                  allowDecimal={false}
                  value={
                    job
                      ? (job.item_copies?.[String(id)] ?? job.copies)
                      : itemCopies[String(id)]
                  }
                  disabled={busy || !!job || hasPending}
                  onChange={(value) =>
                    setItemCopies((current) => ({
                      ...current,
                      [String(id)]: value
                    }))
                  }
                />
              </Group>
            ))}
          </Stack>
        ) : (
          <NumberInput
            label={t`每件份数`}
            min={1}
            max={Math.max(1, Math.floor(config.max_labels / items.length))}
            allowDecimal={false}
            value={copies}
            disabled={busy || !!job || hasPending}
            onChange={(value) => setCopies(Number(value) || 1)}
          />
        )}
        <Text>
          {t`总张数：`}
          {total}
        </Text>
        {job && (
          <Alert
            color={
              job.state === 'submitted'
                ? 'green'
                : job.state === 'uncertain'
                  ? 'orange'
                  : 'blue'
            }
          >
            <Text>{statusText(job.state)}</Text>
            <Text size='xs'>
              {t`任务：`}
              {job.id}
            </Text>
            <Text size='sm'>
              {t`目标队列：`}
              {job.queue}
            </Text>
          </Alert>
        )}
        {terminal && (
          <Checkbox
            checked={confirmed}
            onChange={(event) => setConfirmed(event.currentTarget.checked)}
            label={t`我已检查系统队列和实际标签，确认需要重新打印这些标签`}
          />
        )}
        <Group>
          {terminal ? (
            <Button
              disabled={
                busy || hasPending || !connected || !queue || !confirmed
              }
              onClick={() => void run((check) => print(check, true))}
            >{t`创建补打任务`}</Button>
          ) : (
            <Button
              loading={busy}
              disabled={
                hasPending ||
                !connected ||
                !queue ||
                total > config.max_labels ||
                (!job && !!invalidCopies) ||
                (!!job && !['ready', 'generating'].includes(job.state)) ||
                (!!job && job.queue !== queue)
              }
              onClick={() => void run((check) => print(check))}
            >
              {job ? t`继续提交` : t`打印`}
            </Button>
          )}
          <Button
            variant='subtle'
            disabled={busy || hasPending}
            onClick={() => {
              setJob(undefined);
              setConfirmed(false);
            }}
          >{t`新建标签任务`}</Button>
        </Group>
        {recent.data && recent.data.length > 0 && (
          <Select
            label={t`最近任务（选择后不会自动发送）`}
            value={job?.id ?? null}
            disabled={busy}
            data={recent.data.map((value) => ({
              value: value.id,
              label: `${statusText(value.state)} · ${value.queue} · ${value.total} · ${value.id.slice(0, 8)}`
            }))}
            onChange={(value) => {
              setJob(recent.data.find((item) => item.id === value));
              setConfirmed(false);
            }}
          />
        )}
      </Stack>
    </Modal>
  );
}
