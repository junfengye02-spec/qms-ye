import { type Page, expect, test } from '@playwright/test';

test.beforeEach(async ({ page }, testInfo) => {
  if (testInfo.project.name === 'lan-http') {
    await page.addInitScript(() => {
      Object.defineProperty(Crypto.prototype, 'randomUUID', {
        value: undefined
      });
      Object.defineProperty(window, 'isSecureContext', { value: false });
    });
  }
});

const barcode =
  'WMS1:{"type":"receipt-detail","protocol":1,"id":"47f10c0f-0777-4f85-95dd-ce0a81592994","version":1,"equipment":"设备01","project":"项目A","contract":"合同A"}';
const uuidV4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const first = {
  pk: 1,
  receipt: 1,
  receipt_location: 1,
  part: 1,
  equipment: '设备01',
  name: '设备一',
  project: '项目A',
  contract: 1,
  contract_number: '合同A',
  detail_uid: '47f10c0f-0777-4f85-95dd-ce0a81592994',
  prepared_quantity: '10',
  acceptance_date: '2026-10-01',
  warranty_months: 12,
  warranty_end: '2027-10-01',
  label_version: 1,
  barcode,
  state: 'READY',
  active_request: null,
  actual_quantity: null
};

async function setup(page: Page, saved = false, loseScanResponse = false) {
  page.on('pageerror', (error) => console.error(error.message));
  let receipts: any[] = saved
    ? [
        {
          pk: 1,
          project: '项目A',
          contract: 1,
          location: 1,
          cancelled: false,
          details: [
            { ...first },
            { ...first, pk: 2, equipment: '设备02', name: '设备二' }
          ]
        }
      ]
    : [];
  let request: any = null;
  let failedOnce = false;
  let currentDetail = { ...first };
  const contractWrites: any[] = [];
  const scans: any[] = [];
  const preparations: any[] = [];
  const draftCreations: any[] = [];
  let submissions = 0;
  const projects = [
    { pk: 1, code: '项目A', description: '', active: true },
    { pk: 2, code: '未保存项目', description: '', active: true }
  ];
  const projectWrites: any[] = [];
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    const data = method === 'POST' ? route.request().postDataJSON() : {};
    let body: any = {};
    let status = 200;
    if (path.includes('/wms/projects/')) {
      if (method === 'POST') {
        projectWrites.push(data);
        if (projects.some((item) => item.code === data.code.trim())) {
          status = 400;
          body = { code: ['项目号已存在，请选择已有项目'] };
        } else {
          body = {
            pk: projects.length + 1,
            code: data.code.trim(),
            description: data.description,
            active: true
          };
          projects.push(body);
        }
      } else body = { results: projects };
    } else if (path.includes('/wms/contracts/')) {
      const contract = {
        pk: 1,
        number: '合同A',
        acceptance_date: first.acceptance_date,
        warranty_months: first.warranty_months,
        updated_at: '2026-10-01T00:00:00Z',
        acceptance_followers: 2,
        warranty_followers: 2
      };
      if (method === 'POST') {
        contractWrites.push(data);
        if (data.preview) body = { before: contract, after: data };
        else {
          currentDetail = {
            ...currentDetail,
            acceptance_date: data.acceptance_date,
            warranty_months: data.warranty_months,
            warranty_end: '2027-10-08'
          };
          body = {
            ...contract,
            acceptance_date: data.acceptance_date,
            warranty_months: data.warranty_months
          };
        }
      } else body = { results: [contract] };
    } else if (path.includes('/wms/details/1/')) body = currentDetail;
    else if (path.includes('/stock/location/'))
      body = { results: [{ pk: 1, pathstring: '仓库 / A' }] };
    else if (path.includes('/wms/parts/'))
      body = {
        results: [
          { pk: 1, IPN: '设备01', name: '设备一' },
          { pk: 2, IPN: '设备02', name: '设备二' }
        ]
      };
    else if (path.includes('/wms/options/'))
      body = {
        projects: ['项目A'],
        scanned_part: url.searchParams.get('barcode')
          ? { pk: 1, IPN: '设备01', name: '设备一' }
          : null,
        stock_status: {
          state: 'AVAILABLE',
          scope: url.searchParams.get('batch') ? 'project' : 'all_projects',
          quantity: '10',
          available: '10',
          frozen: '0',
          allocated: '0',
          units: '台'
        }
      };
    else if (path.includes('/local/config/'))
      body = { enabled: false, qz_version: '2.2.5', max_labels: 500 };
    else if (path.includes('/wms/receipts/')) {
      if (method === 'POST') {
        expect(data.request_key).toMatch(uuidV4);
        preparations.push(data);
        receipts = [
          {
            pk: 1,
            project: data.project,
            contract: data.contract,
            location: data.location,
            cancelled: false,
            details: [
              {
                ...first,
                prepared_quantity: String(data.details[0].prepared_quantity)
              }
            ]
          }
        ];
        body = receipts[0];
        status = 201;
      } else body = { results: receipts };
    } else if (path.includes('/wms/labels/resolve/')) {
      body = {
        ...(receipts[0]?.details[0] ?? first),
        stock: [
          {
            pk: 1,
            location: 1,
            location_name: '仓库 / A',
            quantity: '10',
            frozen: '0',
            allocated: '0',
            available: '10'
          }
        ]
      };
    } else if (path.includes('/scan/requests/') && path.endsWith('/submit/')) {
      submissions++;
      request.status = 'PENDING_APPROVAL';
      body = { request };
    } else if (path === '/api/stock/scan/requests/') {
      if (method === 'POST') {
        draftCreations.push(data);
        request = {
          pk: 1,
          reference: 'SA-TEST',
          direction: data.direction,
          status: 'DRAFT',
          location: 1,
          location_path_snapshot: '仓库 / A',
          requester: 1,
          requester_username_snapshot: 'operator',
          approval_owner_snapshot: { name: '审批人' },
          expires_at: '2026-12-01T00:00:00Z',
          active_line_count: 0,
          line_count: 0,
          lines: []
        };
        body = request;
        status = 201;
      } else body = { results: request?.status === 'DRAFT' ? [request] : [] };
    } else if (path === '/api/stock/scan/requests/1/') body = request;
    else if (
      path.includes('/scan/inbound/') ||
      path.includes('/scan/outbound/')
    ) {
      expect(data.client_event_id).toMatch(uuidV4);
      scans.push(data);
      if (!request.lines.length)
        request.lines = [
          {
            pk: 1,
            source_detail: 1,
            request_line_number: 1,
            outcome: 'PENDING',
            part_name_snapshot: '设备一',
            part_ipn_snapshot: '设备01',
            batch_snapshot: '项目A',
            contract_number_snapshot: '合同A',
            units_snapshot: '台',
            requested_quantity: String(data.quantity),
            approval_request: 1,
            operator_user_id_snapshot: 1,
            terminal_reason: ''
          }
        ];
      if (receipts.length) receipts[0].details[0].state = 'IN_REQUEST';
      if (loseScanResponse && !failedOnce) {
        failedOnce = true;
        await route.abort();
        return;
      }
      body = {
        success: true,
        terminal: true,
        replayed: scans.length > 1,
        event: request.lines[0]
      };
      status = 201;
    }
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body)
    });
  });
  await page.goto('/tests/fixtures/contract-warranty/index.html');
  return {
    scans,
    preparations,
    draftCreations,
    contractWrites,
    projectWrites,
    submissions: () => submissions
  };
}

async function choose(page: Page, name: string, value: string) {
  await page.getByRole('combobox', { name, exact: true }).click();
  await page.getByRole('option', { name: value, exact: true }).click();
}

test('each selected device keeps its own copy count and the preview shows the company', async ({
  page
}) => {
  await setup(page, true);
  const jobs: any[] = [];
  await page.route('**/api/label/local/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/config/'))
      return route.fulfill({
        json: {
          enabled: true,
          qz_version: '2.2.5',
          max_labels: 500,
          download_url: ''
        }
      });
    if (route.request().method() === 'POST') {
      const data = route.request().postDataJSON();
      jobs.push(data);
      return route.fulfill({
        json: {
          ...data,
          id: '7e4688a5-ddcc-47e4-89e6-58865d5d1cb0',
          state: 'failed',
          total: 7,
          detail: ''
        }
      });
    }
    return route.fulfill({ json: [] });
  });
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await choose(page, '已保存的入库单', '#1 · 项目A');
  await page
    .getByRole('textbox', { name: '打印数量 · 设备01', exact: true })
    .fill('2');
  await page
    .getByRole('textbox', { name: '打印数量 · 设备02', exact: true })
    .fill('5');
  for (const box of await page
    .getByRole('checkbox', { name: '选择打印明细', exact: true })
    .all())
    await box.check();
  await page
    .getByRole('button', { name: '标签预览', exact: true })
    .first()
    .click();
  const preview = page.getByRole('dialog', {
    name: '完整标签预览',
    exact: true
  });
  await expect(
    preview.getByText('广州南沙智慧城市大数据有限公司', { exact: true })
  ).toBeVisible();
  await expect(preview.getByText(/标签版本/)).toHaveCount(0);
  await preview.getByRole('banner').getByRole('button').click();
  await page
    .getByRole('button', { name: '打印所选完整标签', exact: true })
    .click();
  const print = page.getByRole('dialog', { name: '本机标签打印', exact: true });
  await expect(
    print.getByRole('textbox', {
      name: '打印数量 · 设备01 · 设备一',
      exact: true
    })
  ).toHaveValue('2');
  await expect(
    print.getByRole('textbox', {
      name: '打印数量 · 设备02 · 设备二',
      exact: true
    })
  ).toHaveValue('5');
  await expect(print.getByText('总张数：7', { exact: true })).toBeVisible();
  await print.getByRole('button', { name: '下载 PDF', exact: true }).click();
  await expect.poll(() => jobs.length).toBe(1);
  expect(jobs[0]).toMatchObject({
    items: [1, 2],
    copies: 1,
    item_copies: { '1': 2, '2': 5 }
  });
});

for (const direction of ['inbound', 'outbound'] as const) {
  test(`${direction} compact QR resolves the same review fields`, async ({
    page
  }) => {
    const state = await setup(page, true);
    if (direction === 'outbound')
      await page.getByRole('button', { name: '切换测试出入库' }).click();
    await page
      .getByRole('textbox', {
        name: '扫码（二维码／设备ID／库存项码）',
        exact: true
      })
      .fill('WMS1:47F10C0F07774F8595DDCE0A81592994:1');
    await expect(
      page.getByRole('textbox', { name: '项目', exact: true })
    ).toHaveValue('项目A');
    await expect(
      page.getByRole('textbox', { name: '合同号', exact: true })
    ).toHaveValue('合同A');
    if (direction === 'outbound')
      await page
        .getByRole('textbox', { name: '用途', exact: true })
        .fill('安装');
    await page
      .getByRole('button', {
        name: direction === 'inbound' ? 'Add pending inbound' : '加入待出库',
        exact: true
      })
      .click();
    await expect.poll(() => state.scans.length).toBe(1);
    expect(state.scans[0].source_detail).toBeUndefined();
    expect(state.scans[0].barcode).toBe(
      'WMS1:47F10C0F07774F8595DDCE0A81592994:1'
    );
  });
}

test('count import refreshes the shared project archive after confirmed commit', async ({
  page
}) => {
  await setup(page);
  let committed = false;
  let commits = 0;
  await page.route('**/wms/projects/', async (route) => {
    await route.fulfill({
      json: {
        results: [
          { pk: 1, code: '项目A', description: '', active: true },
          ...(committed
            ? [{ pk: 2, code: '导入项目', description: '', active: true }]
            : [])
        ]
      }
    });
  });
  await page.route('**/wms/imports/', async (route) => {
    if (
      route.request().headers()['content-type']?.includes('application/json')
    ) {
      committed = true;
      commits += 1;
      await route.fulfill({
        json: { task: 42, completed_at: '2026-10-09T06:00:00Z', results: [] }
      });
    } else {
      await route.fulfill({
        status: 201,
        json: {
          task: 42,
          can_commit: true,
          results: [
            {
              row: 2,
              IPN: '设备01',
              batch: '导入项目',
              location: '仓库 / A',
              action: '新增库存',
              before: '0',
              quantity: '2',
              delta: '2'
            }
          ]
        }
      });
    }
  });
  await page.getByRole('button', { name: '切换测试导入', exact: true }).click();
  await page
    .getByRole('combobox', { name: '导入后项目档案', exact: true })
    .click();
  await expect(
    page.getByRole('option', { name: '项目A', exact: true })
  ).toBeVisible();
  await page
    .getByRole('combobox', { name: '导入后项目档案', exact: true })
    .press('Escape');
  await page.locator('input[type="file"]').setInputFiles({
    name: 'count.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(
      '设备ID,项目,地点完整路径,盘点数量\n设备01,导入项目,仓库 / A,2\n'
    )
  });
  const commit = page.getByRole('button', {
    name: '确认整批提交',
    exact: true
  });
  await expect(commit).toBeDisabled();
  expect(commits).toBe(0);
  await page
    .getByRole('checkbox', {
      name: '已核实资料和当前实盘数量，确认按此预览整批落账',
      exact: true
    })
    .check();
  await commit.click();
  await expect(page.getByText('整批导入成功', { exact: false })).toBeVisible();
  expect(commits).toBe(1);
  await page
    .getByRole('combobox', { name: '导入后项目档案', exact: true })
    .click();
  await expect(
    page.getByRole('option', { name: '导入项目', exact: true })
  ).toBeVisible();
});

test('an erroneous import preview stays blocked after the operator confirms', async ({
  page
}) => {
  await setup(page);
  let commits = 0;
  await page.route('**/wms/imports/', async (route) => {
    if (route.request().headers()['content-type']?.includes('application/json'))
      commits += 1;
    await route.fulfill({
      status: 201,
      json: {
        task: 43,
        can_commit: false,
        results: [
          {
            row: 2,
            IPN: '设备01',
            error: '文件内重复设备或设备＋项目＋实际地点'
          }
        ]
      }
    });
  });
  await page.getByRole('button', { name: '切换测试导入', exact: true }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: 'invalid.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('设备ID\n设备01\n设备01\n')
  });
  await expect(
    page.getByRole('cell', {
      name: '文件内重复设备或设备＋项目＋实际地点',
      exact: true
    })
  ).toBeVisible();
  await page
    .getByRole('checkbox', {
      name: '已核实资料和当前实盘数量，确认按此预览整批落账',
      exact: true
    })
    .check();
  await expect(
    page.getByRole('button', { name: '确认整批提交', exact: true })
  ).toBeDisabled();
  expect(commits).toBe(0);
});

test('project creation rejects duplicate numbers and selects a new unique archive', async ({
  page
}) => {
  const state = await setup(page);
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '新建项目', exact: true });
  await modal
    .getByRole('textbox', { name: '项目号', exact: true })
    .fill(' 项目A ');
  await modal.getByRole('button', { name: '保存项目', exact: true }).click();
  await expect(modal.getByText('项目号已存在，请选择已有项目')).toBeVisible();
  await modal
    .getByRole('textbox', { name: '项目号', exact: true })
    .fill('00123');
  await modal.getByRole('button', { name: '保存项目', exact: true }).click();
  await expect(modal).toBeHidden();
  await expect(
    page.getByRole('combobox', { name: '项目', exact: true })
  ).toHaveValue('00123');
  await choose(page, 'Destination location', '仓库 / A');
  await page
    .getByRole('textbox', {
      name: '扫码（二维码／设备ID／库存项码）',
      exact: true
    })
    .fill('设备01');
  await page
    .getByRole('button', { name: 'Add pending inbound', exact: true })
    .click();
  await expect.poll(() => state.scans.length).toBe(1);
  expect(state.scans[0]).toMatchObject({ batch: '00123', project_id: 3 });
  expect(state.projectWrites).toHaveLength(2);
});

test('preparation selects registered projects and cannot save a typed unknown name', async ({
  page
}) => {
  const state = await setup(page);
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await page
    .getByRole('combobox', { name: '项目号', exact: true })
    .fill('不存在的项目');
  await choose(page, '合同号', '合同A');
  await choose(page, '入库库位', '仓库 / A');
  await choose(page, '选择已有设备', '设备01 · 设备一');
  await page.getByRole('button', { name: '添加设备明细', exact: true }).click();
  await expect(
    page.getByRole('button', { name: '保存并准备打印', exact: true })
  ).toBeDisabled();
  expect(state.preparations).toHaveLength(0);
  await page
    .getByRole('combobox', { name: '项目号', exact: true })
    .fill('项目A');
  await page.getByRole('option', { name: '项目A', exact: true }).click();
  await page
    .getByRole('button', { name: '保存并准备打印', exact: true })
    .click();
  await expect.poll(() => state.preparations.length).toBe(1);
  expect(state.preparations[0].project).toBe('项目A');
});

test('new inbound form clears all previous unsaved values without saving a receipt', async ({
  page
}) => {
  const state = await setup(page);
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  const newForm = page.getByRole('button', { name: '新建入库单', exact: true });
  await newForm.click();
  await choose(page, '项目号', '未保存项目');
  await choose(page, '合同号', '合同A');
  await choose(page, '入库库位', '仓库 / A');
  await choose(page, '选择已有设备', '设备01 · 设备一');
  await page.getByRole('button', { name: '添加设备明细', exact: true }).click();
  await newForm.click();
  await expect(
    page.getByRole('combobox', { name: '项目号', exact: true })
  ).toHaveValue('');
  await expect(
    page.getByRole('combobox', { name: '合同号', exact: true })
  ).toHaveValue('');
  await expect(
    page.getByRole('combobox', { name: '入库库位', exact: true })
  ).toHaveValue('');
  await expect(
    page.getByRole('button', { name: '移除', exact: true })
  ).toHaveCount(0);
  expect(state.preparations).toHaveLength(0);
  expect(state.scans).toHaveLength(0);
});

test('prepare existing equipment with inherited defaults, then preview before stock exists', async ({
  page
}) => {
  const state = await setup(page);
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await choose(page, '项目号', '项目A');
  await choose(page, '合同号', '合同A');
  await expect(
    page.getByText('验收日期（合同起始时间）：2026-10-01')
  ).toBeVisible();
  await choose(page, '入库库位', '仓库 / A');
  await choose(page, '选择已有设备', '设备01 · 设备一');
  await page.getByRole('textbox', { name: '准备数量', exact: true }).fill('10');
  await page.getByRole('button', { name: '添加设备明细' }).click();
  await page.getByRole('button', { name: '保存并准备打印' }).click();
  await expect(page.getByText('待核对', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '标签预览' }).click();
  await expect(page.getByAltText('完整二维码预览')).toBeVisible();
  expect(state.preparations[0].details[0]).toMatchObject({
    part: 1
  });
  expect(state.preparations[0].details[0]).not.toHaveProperty(
    'acceptance_date_override'
  );
  expect(state.preparations[0].details[0]).not.toHaveProperty(
    'warranty_months_override'
  );
  expect(Number(state.preparations[0].details[0].prepared_quantity)).toBe(10);
  expect(state.scans).toHaveLength(0);
});

test('partial actual quantity requires confirmation, repeated scan does not add, submit lists unchecked lines', async ({
  page
}) => {
  const state = await setup(page, true);
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await choose(page, '已保存的入库单', '#1 · 项目A');
  await page.getByRole('button', { name: '转到实物扫码核对' }).click();
  const scanner = page.getByRole('textbox', { name: '实物完整二维码' });
  await scanner.fill(barcode);
  await scanner.press('Enter');
  await expect(page.getByRole('textbox', { name: '实际申请数量' })).toHaveValue(
    '10'
  );
  await page.getByRole('textbox', { name: '实际申请数量' }).fill('8');
  await expect(
    page.getByRole('button', { name: '核对并加入草稿' })
  ).toBeDisabled();
  await page.getByRole('checkbox', { name: /已核对实际到货数量/ }).check();
  await page.getByRole('button', { name: '核对并加入草稿' }).click();
  await expect(scanner).toBeFocused();
  await expect(
    page
      .getByRole('row')
      .filter({ hasText: '设备01' })
      .getByRole('cell', { name: '8', exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Submit for approval', exact: true })
  ).toHaveCount(0);
  expect(state.scans).toHaveLength(1);
  expect(state.scans[0]).toMatchObject({
    quantity_confirmed: true,
    receipt_id: 1
  });
  expect(state.scans[0].batch).toBeUndefined();
  await scanner.fill(barcode);
  await scanner.press('Enter');
  await expect(page.getByText(/同一码已加入/)).toBeVisible();
  expect(state.scans).toHaveLength(1);
  await page.getByRole('button', { name: '提交已核对明细审批' }).click();
  await expect(page.getByText(/设备02 设备二/)).toBeVisible();
  await page.getByRole('button', { name: '确认提交', exact: true }).click();
  await expect.poll(state.submissions).toBe(1);
});

test('lost scan response retries the same event, even after server admitted the detail', async ({
  page
}) => {
  const state = await setup(page, true, true);
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await choose(page, '已保存的入库单', '#1 · 项目A');
  await page.getByRole('button', { name: '转到实物扫码核对' }).click();
  await page.getByRole('textbox', { name: '实物完整二维码' }).fill(barcode);
  await page.getByRole('button', { name: '识别标签' }).click();
  await page.getByRole('button', { name: '核对并加入草稿' }).click();
  await page.getByRole('button', { name: '重试原请求' }).click();
  await expect.poll(() => state.scans.length).toBe(2);
  expect(state.scans[0].client_event_id).toBe(state.scans[1].client_event_id);
});

test('outbound derives project and contract from resolved label without manual entry', async ({
  page
}) => {
  const state = await setup(page);
  await page.getByRole('button', { name: '切换测试出入库' }).click();
  await choose(page, 'Source location', '仓库 / A');
  await page
    .getByRole('textbox', { name: '扫码（二维码／设备ID／库存项码）' })
    .fill(barcode);
  await expect(
    page.getByRole('textbox', { name: '项目', exact: true })
  ).toHaveValue('项目A');
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('合同A');
  await page.getByRole('textbox', { name: 'Quantity per scan' }).fill('3');
  await page.getByRole('textbox', { name: '用途', exact: true }).fill('安装');
  await page.getByRole('button', { name: '加入待出库', exact: true }).click();
  await expect(
    page.getByRole('row').filter({ hasText: '设备01' }).last()
  ).toBeVisible();
  expect(state.scans[0]).toMatchObject({ source_location: 1, purpose: '安装' });
  expect(state.scans[0].batch).toBe('项目A');
});

test('default entry keeps direct equipment scans without preparation or override controls', async ({
  page
}) => {
  const state = await setup(page);
  await expect(
    page.getByRole('button', { name: '新建入库单', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('combobox', { name: '已保存的入库单', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: '新建合同', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('textbox', { name: '项目号', exact: true })
  ).toHaveCount(0);
  await choose(page, 'Destination location', '仓库 / A');
  await page
    .getByRole('textbox', { name: '扫码（二维码／设备ID／库存项码）' })
    .fill('设备01');
  await choose(page, '项目', '项目A');
  await page
    .getByRole('button', { name: 'Add pending inbound', exact: true })
    .click();
  await expect(
    page.getByRole('row').filter({ hasText: '设备01' }).last()
  ).toBeVisible();
  expect(state.preparations).toHaveLength(0);
  expect(state.scans[0]).toMatchObject({
    barcode: '设备01',
    destination_location: 1,
    batch: '项目A'
  });
  await page.getByRole('button', { name: '切换测试填单打印' }).click();
  await expect(
    page.getByRole('checkbox', { name: '此明细单独设置验收或质保' })
  ).toHaveCount(0);
});

test('direct full-label inbound loads its own receipt and confirms actual quantity', async ({
  page
}) => {
  const state = await setup(page, true);
  const scanner = page.getByRole('textbox', {
    name: '扫码（二维码／设备ID／库存项码）'
  });
  await scanner.fill(barcode);
  await expect(
    page.getByRole('textbox', { name: '项目', exact: true })
  ).toHaveValue('项目A');
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('合同A');
  await expect(
    page.getByRole('combobox', { name: 'Destination location', exact: true })
  ).toHaveValue('仓库 / A');
  await page.getByRole('textbox', { name: 'Quantity per scan' }).fill('8');
  await page
    .getByRole('button', { name: 'Add pending inbound', exact: true })
    .click();
  await expect(
    page.getByText('实际数量与准备数量不同，请明确核对确认。')
  ).toBeVisible();
  expect(state.scans).toHaveLength(0);
  await page
    .getByRole('checkbox', { name: '已核对实际数量与准备数量的差异' })
    .check();
  await page
    .getByRole('button', { name: 'Add pending inbound', exact: true })
    .click();
  await expect(
    page.getByRole('row').filter({ hasText: '设备01' }).last()
  ).toBeVisible();
  expect(state.scans[0]).toMatchObject({
    receipt_id: 1,
    quantity: 8,
    quantity_confirmed: true
  });
  await scanner.fill(barcode);
  await expect(
    page.getByRole('textbox', { name: '项目', exact: true })
  ).toHaveValue('项目A');
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('合同A');
  await page
    .getByRole('button', { name: 'Add pending inbound', exact: true })
    .click();
  await expect(page.getByText(/同一码已加入/)).toBeVisible();
  expect(state.scans).toHaveLength(1);
});

test('stock backend updates contract start date after inbound and refreshes warranty display', async ({
  page
}) => {
  const state = await setup(page, true);
  await page.getByRole('button', { name: '切换测试库存维护' }).click();
  await expect(
    page.getByText('验收日期（合同起始时间）：2026-10-01')
  ).toBeVisible();
  await page.getByRole('button', { name: '更新合同验收日期及质保' }).click();
  await page
    .getByLabel('验收日期（合同起始时间）', { exact: true })
    .fill('2026-10-08');
  await page
    .getByRole('textbox', { name: '修改原因', exact: true })
    .fill('入库后更新合同起始时间');
  await page.getByRole('button', { name: '核对影响数量' }).click();
  await expect(page.getByText(/关联设备明细数：2/)).toBeVisible();
  await page.getByRole('button', { name: '确认保存合同' }).click();
  await expect(
    page.getByText('验收日期（合同起始时间）：2026-10-08')
  ).toBeVisible();
  await expect(page.getByText(/质保到期日：2027-10-08/)).toBeVisible();
  expect(state.contractWrites).toHaveLength(2);
  expect(state.contractWrites[1]).toMatchObject({
    pk: 1,
    acceptance_date: '2026-10-08',
    warranty_months: 12,
    reason: '入库后更新合同起始时间'
  });
});

for (const direction of ['inbound', 'outbound'] as const) {
  test(`${direction} full label fills review fields and Enter waits for operator confirmation`, async ({
    page
  }) => {
    const state = await setup(page, true);
    if (direction === 'outbound')
      await page.getByRole('button', { name: '切换测试出入库' }).click();
    const scanner = page.getByRole('textbox', {
      name: '扫码（二维码／设备ID／库存项码）',
      exact: true
    });
    await scanner.fill(barcode);
    for (const [name, value] of [
      ['设备ID', '设备01'],
      ['设备名称', '设备一'],
      ['项目', '项目A'],
      ['合同号', '合同A']
    ]) {
      const field = page.getByRole('textbox', { name, exact: true });
      await expect(field).toHaveValue(value);
      await expect(field).toHaveAttribute('readonly', '');
    }
    await expect(
      page.getByRole('combobox', {
        name:
          direction === 'inbound' ? 'Destination location' : 'Source location',
        exact: true
      })
    ).toHaveValue('仓库 / A');
    if (direction === 'outbound')
      await page
        .getByRole('textbox', { name: '用途', exact: true })
        .fill('安装');
    await scanner.press('Enter');
    await page.waitForLoadState('networkidle');
    await expect(scanner).toHaveValue(barcode);
    expect(state.scans).toHaveLength(0);
    expect(state.draftCreations).toHaveLength(0);
    await page
      .getByRole('button', {
        name: direction === 'inbound' ? 'Add pending inbound' : '加入待出库',
        exact: true
      })
      .click();
    await expect.poll(() => state.scans.length).toBe(1);
    expect(state.scans[0].batch).toBe('项目A');
  });
}

test('equipment ID previews device identity and never fabricates a contract', async ({
  page
}) => {
  const state = await setup(page);
  await choose(page, 'Destination location', '仓库 / A');
  const scanner = page.getByRole('textbox', {
    name: '扫码（二维码／设备ID／库存项码）',
    exact: true
  });
  await scanner.fill('设备01');
  await expect(
    page.getByRole('textbox', { name: '设备名称', exact: true })
  ).toHaveValue('设备一');
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('');
  await choose(page, '项目', '项目A');
  await scanner.press('Enter');
  await page.waitForLoadState('networkidle');
  expect(state.draftCreations).toHaveLength(0);
  expect(state.scans).toHaveLength(0);
  await page.getByRole('button', { name: '切换测试出入库' }).click();
  await choose(page, 'Source location', '仓库 / A');
  await scanner.fill('设备01');
  await expect(
    page.getByRole('combobox', { name: '项目', exact: true })
  ).toHaveValue('项目A');
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('');
});

test('invalid replacement label clears the previous review fields', async ({
  page
}) => {
  const state = await setup(page, true);
  const scanner = page.getByRole('textbox', {
    name: '扫码（二维码／设备ID／库存项码）',
    exact: true
  });
  await scanner.fill(barcode);
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('合同A');
  await page.route('**/api/stock/wms/labels/resolve/', (route) =>
    route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '标签内容无效' })
    })
  );
  await scanner.fill('WMS1:invalid');
  await expect(page.getByText('标签内容无效', { exact: true })).toBeVisible();
  for (const name of ['设备ID', '设备名称', '项目', '合同号'])
    await expect(page.getByRole('textbox', { name, exact: true })).toHaveValue(
      ''
    );
  await expect(
    page.getByRole('button', { name: 'Add pending inbound', exact: true })
  ).toBeDisabled();
  expect(state.scans).toHaveLength(0);
});

test('outbound label with multiple actual locations keeps location selection explicit', async ({
  page
}) => {
  await setup(page, true);
  await page.route('**/api/stock/wms/labels/resolve/', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        ...first,
        stock: [
          { pk: 1, location: 1, quantity: '5', available: '5' },
          { pk: 2, location: 2, quantity: '5', available: '5' }
        ]
      })
    })
  );
  await page.getByRole('button', { name: '切换测试出入库' }).click();
  await page
    .getByRole('textbox', {
      name: '扫码（二维码／设备ID／库存项码）',
      exact: true
    })
    .fill(barcode);
  await expect(
    page.getByRole('textbox', { name: '合同号', exact: true })
  ).toHaveValue('合同A');
  await expect(
    page.getByRole('combobox', { name: 'Source location', exact: true })
  ).toHaveValue('');
  await expect(
    page.getByRole('button', { name: '加入待出库', exact: true })
  ).toBeDisabled();
});
