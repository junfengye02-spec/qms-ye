import { type Page, expect, test } from '@playwright/test';

test('independent device counts reject invalid totals and survive response loss and reload', async ({
  page
}) => {
  const { requests, jobs } = await setup(page, false, true, true);
  const first = page.getByRole('textbox', {
    name: '打印数量 · 设备01',
    exact: true
  });
  const second = page.getByRole('textbox', {
    name: '打印数量 · 设备02',
    exact: true
  });
  const pdf = page.getByRole('button', { name: '下载 PDF', exact: true });
  await first.fill('0');
  await expect(pdf).toBeDisabled();
  await first.fill('500');
  await expect(pdf).toBeDisabled();
  await first.fill('2');
  await second.fill('5');
  await expect(page.getByText('总张数：8', { exact: true })).toBeVisible();
  await connect(page);
  await page.getByRole('button', { name: '打印', exact: true }).click();
  await expect(
    page.getByRole('button', { name: '查询上次请求' })
  ).toBeVisible();
  await expect(first).toBeDisabled();
  await page.reload();
  await page.getByRole('button', { name: '查询上次请求' }).click();
  await expect(
    page.getByText('PDF 已就绪，等待提交', { exact: true })
  ).toBeVisible();
  await expect(first).toHaveValue('2');
  await expect(second).toHaveValue('5');
  await expect(page.getByText('总张数：8', { exact: true })).toBeVisible();
  const creates = requests.filter(
    (request) => request.path.endsWith('/jobs/') && request.body
  );
  expect(creates).toHaveLength(2);
  expect(creates[0].body.item_copies).toEqual({ '1': 2, '2': 5, '3': 1 });
  expect(creates[1].body).toEqual(creates[0].body);
  expect(jobs).toHaveLength(1);
  expect(
    requests.some(
      (request) => request.path.endsWith('/sign/') && request.body.job
    )
  ).toBe(false);
});

test('PDF download without randomUUID retries a lost response with the original key', async ({
  page
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined });
    Object.defineProperty(window, 'isSecureContext', { value: false });
  });
  const { requests, jobs } = await setup(page, false, true);
  await page.getByRole('button', { name: '下载 PDF', exact: true }).click();
  await expect(
    page.getByRole('button', { name: '查询上次请求' })
  ).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '查询上次请求' }).click();
  await expect(
    page.getByText('PDF 已就绪，等待提交', { exact: true })
  ).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 PDF', exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(/^labels-.*\.pdf$/);
  const creates = requests.filter(
    (request) => request.path.endsWith('/jobs/') && request.body
  );
  expect(creates).toHaveLength(2);
  expect(creates[0].body.request_key).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  );
  expect(creates[1].body.request_key).toBe(creates[0].body.request_key);
  expect(jobs).toHaveLength(1);
  expect(requests.some((request) => request.path.endsWith('/sign/'))).toBe(
    false
  );
});

test('lost creation response is recovered with the same key and never auto-sends', async ({
  page
}) => {
  const { requests, jobs } = await setup(page, false, true);
  await connect(page);
  await page.getByRole('button', { name: '打印', exact: true }).click();
  await expect(
    page.getByRole('button', { name: '查询上次请求' })
  ).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '查询上次请求' }).click();
  await expect(
    page.getByText('PDF 已就绪，等待提交', { exact: true })
  ).toBeVisible();
  const creates = requests.filter(
    (request) => request.path.endsWith('/jobs/') && request.body
  );
  expect(creates).toHaveLength(2);
  expect(creates[0].body.request_key).toBe(creates[1].body.request_key);
  expect(jobs).toHaveLength(1);
  expect(
    requests.some(
      (request) => request.path.endsWith('/sign/') && request.body.job
    )
  ).toBe(false);
});

async function setup(
  page: Page,
  failPrint = false,
  loseCreateResponse = false,
  perItem = false
) {
  const jobs: any[] = [];
  const requests: any[] = [];
  await page.addInitScript(
    ({ failPrint }) => {
      const OriginalSocket = window.WebSocket;
      (window as any).__printWire = [];
      class Socket {
        static OPEN = 1;
        static CLOSED = 3;
        readyState = 1;
        onopen?: (event: unknown) => void;
        onmessage?: (event: unknown) => void;
        onclose?: (event: unknown) => void;
        constructor() {
          setTimeout(() => this.onopen?.({}), 0);
        }
        send(raw: string) {
          if (raw === 'ping') return;
          const message = JSON.parse(raw);
          (window as any).__printWire.push(message);
          setTimeout(
            () =>
              this.onmessage?.({
                data: JSON.stringify({
                  uid: message.uid,
                  error:
                    failPrint && message.call === 'print'
                      ? 'Connection closed before response'
                      : undefined,
                  result:
                    message.call === 'getVersion'
                      ? '2.2.5'
                      : message.call === 'printers.find'
                        ? ['标签打印机', 'Office printer']
                        : null
                })
              }),
            0
          );
        }
        close() {
          this.readyState = 3;
          this.onclose?.({});
        }
      }
      window.WebSocket = new Proxy(OriginalSocket, {
        construct(target, args) {
          return String(args[0]).startsWith('wss://localhost')
            ? new Socket()
            : Reflect.construct(target, args);
        }
      });
    },
    { failPrint }
  );
  await page.route('**/api/label/local/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body =
      request.method() === 'POST' ? request.postDataJSON() : undefined;
    requests.push({ path, body });
    let response: any = {};
    const job = jobs.find((entry) => path.includes(entry.id));
    if (path.endsWith('/certificate/')) {
      return route.fulfill({
        contentType: 'text/plain',
        body: 'TEST CERTIFICATE'
      });
    }
    if (path.endsWith('/sign/')) {
      if (body.job)
        jobs.find((entry) => entry.id === body.job).state = 'submitting';
      response = { signature: 'TEST SIGNATURE' };
    } else if (path.endsWith('/pdf/')) {
      response = {
        data: btoa('%PDF-1.4\n%%EOF'),
        options: {
          units: 'mm',
          size: { width: 50, height: 30 },
          scaleContent: false,
          copies: 1
        }
      };
    } else if (path.endsWith('/result/')) {
      job.state = body.state;
      response = job;
    } else if (request.method() === 'POST') {
      response = jobs.find(
        (entry) => entry.request_key === body.request_key
      ) ?? {
        ...body,
        id: crypto.randomUUID(),
        state: 'ready',
        total: body.item_copies
          ? Object.values(body.item_copies).reduce(
              (sum: number, count) => sum + Number(count),
              0
            )
          : body.items.length * body.copies,
        detail: '',
        file_sha256: 'digest'
      };
      if (!jobs.includes(response)) jobs.unshift(response);
      if (loseCreateResponse) {
        loseCreateResponse = false;
        return route.abort('failed');
      }
    } else if (path.endsWith('/jobs/')) response = jobs;
    else response = job;
    return route.fulfill({ json: response });
  });
  await page.goto(
    `/tests/fixtures/local-print/index.html${perItem ? '?perItem=1' : ''}`
  );
  await expect(page.getByRole('dialog')).toBeVisible();
  return { jobs, requests };
}

async function connect(page: Page) {
  await page.getByRole('button', { name: '重新检测', exact: true }).click();
  await expect(page.getByRole('button', { name: '刷新打印机' })).toBeVisible();
  await page.getByRole('combobox', { name: '本机打印机' }).click();
  await page.getByRole('option', { name: '标签打印机', exact: true }).click();
}

test('batch printing uses one submission and never opens a PDF popup', async ({
  page
}) => {
  const { requests } = await setup(page);
  let popups = 0;
  page.on('popup', () => popups++);
  await expect(
    page.getByRole('button', { name: '打印', exact: true })
  ).toBeDisabled();
  await connect(page);
  await page.getByRole('textbox', { name: '每件份数' }).fill('2');
  await expect(page.getByText('总张数：6', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '打印', exact: true }).click();
  await expect(
    page.getByText('已提交到本机打印队列', { exact: true })
  ).toBeVisible();
  expect(
    requests.filter(
      (request) => request.path.endsWith('/jobs/') && request.body
    )
  ).toHaveLength(1);
  const wire = await page.evaluate(() =>
    (window as any).__printWire.filter(
      (message: any) => message.call === 'print'
    )
  );
  expect(wire).toHaveLength(1);
  expect(wire[0].params.options.copies).toBe(1);
  expect(wire[0].params.data[0].flavor).toBe('base64');
  expect(wire[0].signAlgorithm).toBe('SHA512');
  expect(popups).toBe(0);
  await page.screenshot({
    path: 'test-results/local-print-submitted.png',
    fullPage: true
  });
});

test('PDF fallback works without a component and installation status is honest', async ({
  page
}) => {
  const { requests } = await setup(page);
  await page.getByRole('button', { name: '安装打印组件' }).click();
  await expect(
    page.getByText('尚未配置经过验证的安装包，请联系管理员。当前可下载 PDF。')
  ).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 PDF', exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(/^labels-.*\.pdf$/);
  expect(requests.some((request) => request.path.endsWith('/sign/'))).toBe(
    false
  );
});

test('unknown submission requires explicit confirmation and a linked reprint', async ({
  page
}) => {
  const { requests } = await setup(page, true);
  await connect(page);
  await page.getByRole('button', { name: '打印', exact: true }).click();
  await expect(
    page.getByText('结果待确认，请检查 Windows 打印队列后决定是否补打', {
      exact: true
    })
  ).toBeVisible();
  const reprint = page.getByRole('button', { name: '创建补打任务' });
  await expect(reprint).toBeDisabled();
  await page.getByRole('checkbox').check();
  await reprint.click();
  await expect
    .poll(
      () =>
        requests.filter((request) => request.path.endsWith('/reprint/')).length
    )
    .toBe(1);
});
