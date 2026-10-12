import { expect, test } from '../baseFixtures.js';
import { navigate } from '../helpers.js';
import { doCachedLogin } from '../login.js';
import { setSettingState } from '../settings.js';

function approvalRequest(overrides: Record<string, unknown> = {}) {
  return {
    pk: 77,
    reference: 'SA-20260827-000077',
    direction: 'OUTBOUND',
    status: 'DRAFT',
    location: 1,
    location_path_snapshot: 'Main Warehouse',
    requester: 1,
    requester_username_snapshot: 'admin',
    requester_display_name_snapshot: 'Admin',
    approval_owner_snapshot: { name: 'Warehouse supervisors' },
    submitted_at: null,
    activity_at: '2026-08-27T02:00:00Z',
    expires_at: '2099-08-27T02:30:00Z',
    decided_at: null,
    decision_note: '',
    cancelled_at: null,
    cancel_note: '',
    last_approval_error: '',
    created_at: '2026-08-27T02:00:00Z',
    updated_at: '2026-08-27T02:00:00Z',
    line_count: 0,
    active_line_count: 0,
    total_quantity: '0',
    scan_frozen: '0',
    pending_inbound: '0',
    can_submit: false,
    can_cancel: true,
    can_approve: false,
    can_reject: false,
    lines: [],
    ...overrides
  };
}

function pendingScanEvent(pk: number, barcode: string) {
  return {
    pk,
    client_event_id: `00000000-0000-4000-8000-${String(pk).padStart(12, '0')}`,
    operation: 'OUTBOUND',
    outcome: 'PENDING',
    error_code: '',
    error_message: '',
    barcode,
    barcode_kind: 'STOCK_ITEM',
    requested_quantity: '1',
    applied_quantity: '0',
    part_pk_snapshot: 1,
    part_name_snapshot: `Product ${pk}`,
    part_ipn_snapshot: `IPN-${pk}`,
    stock_item_pk_snapshot: pk,
    batch_snapshot: `BATCH-${pk}`,
    source_location_path_snapshot: 'Main Warehouse',
    destination_location_path_snapshot: '',
    stock_item_quantity_before: null,
    stock_item_quantity_after: null,
    part_total_before: null,
    part_total_after: null,
    operator_user_id_snapshot: 1,
    operator_username_snapshot: 'admin',
    operator_display_name_snapshot: 'Admin',
    device_id: '',
    session_id: '',
    replay_count: 0,
    replayed: false,
    scanned_at: '2026-08-27T02:01:00Z',
    completed_at: null,
    tracking_entries: [],
    approval_request: 77,
    approval_request_reference: 'SA-20260827-000077',
    request_line_number: pk,
    terminal_reason: ''
  };
}

test('Scan Stock - Inbound and outbound routes use English source text', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: false });
  const page = await doCachedLogin(browser, {
    url: 'stock/location/index/'
  });

  await page.waitForURL('**/web/stock/scan/inbound/');
  await expect(page).toHaveTitle(/Scan stock inbound/);
  await expect(
    page.getByRole('heading', { name: 'Scan stock', level: 1 })
  ).toBeVisible();
  await expect(page.getByText('Inbound mode', { exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Scan work' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Current stock' })).toBeVisible();
  await expect(
    page.getByRole('tab', { name: 'Operation records' })
  ).toBeVisible();

  await page.getByRole('button', { name: 'Camera Input' }).click();
  const cameraDialog = page.getByRole('dialog', { name: 'Camera Input' });
  await expect(cameraDialog).toBeVisible();
  await expect(
    cameraDialog.getByText('Start scanning by selecting a camera')
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(cameraDialog).not.toBeVisible();

  await expect(page.locator('body')).not.toContainText('扫码入库');
  await expect(page.locator('body')).not.toContainText('扫码作业');

  await navigate(page, 'stock/scan/outbound/');

  await page.waitForURL('**/web/stock/scan/outbound/');
  await expect(page).toHaveTitle(/Scan stock outbound/);
  await expect(page.getByText('Outbound mode', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Source location')).toHaveValue('');
  await expect(page.locator('body')).not.toContainText('扫码出库');

  await page.getByRole('tab', { name: 'Operation records' }).click();
  await page.getByRole('tab', { name: 'Scan records' }).click();
  await expect(page.getByLabel('Outcome')).toBeVisible();
  await expect(page.getByLabel('Operator ID')).toBeVisible();
  await expect(page.getByLabel('Part ID')).toBeVisible();
  await expect(page.getByLabel('Stock item ID')).toBeVisible();
  await expect(page.getByLabel('Source location ID')).toBeVisible();
  await expect(page.getByLabel('Destination location ID')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Export data' })).toBeVisible();
});

test('Scan Stock - Legacy inbound route replaces browser history', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: false });
  const page = await doCachedLogin(browser, {
    url: 'stock/location/index/'
  });

  await page.waitForURL('**/web/stock/location/index/**');
  await navigate(page, 'stock/scan-inbound/');

  await page.waitForURL('**/web/stock/scan/inbound/');
  await expect(page.getByText('Inbound mode', { exact: true })).toBeVisible();

  await page.goBack();
  await page.waitForURL('**/web/stock/location/index/**');
});

test('Scan Stock - Disabled approval keeps the direct scan payload', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: false });
  const page = await doCachedLogin(browser, {
    url: 'stock/scan/inbound/'
  });
  let requestCalls = 0;
  let scanPayload: Record<string, unknown> | undefined;

  await page.route('**/api/barcode/', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ stocklocation: { pk: 1 } })
    });
  });
  await page.route('**/api/stock/scan/requests/**', async (route) => {
    requestCalls += 1;
    await route.fulfill({
      status: 500,
      body: 'Approval API must not be called'
    });
  });
  await page.route('**/api/stock/scan/inbound/', async (route) => {
    scanPayload = route.request().postDataJSON();
    const event = {
      ...pendingScanEvent(1, 'INV-SI1'),
      operation: 'INBOUND',
      outcome: 'SUCCESS',
      applied_quantity: '1',
      approval_request: null,
      request_line_number: null
    };
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        terminal: true,
        replayed: false,
        event,
        message: 'Stock updated.'
      })
    });
  });

  await navigate(page, 'stock/scan/inbound/');
  await expect(page.getByRole('tab', { name: 'My requests' })).toHaveCount(0);
  await expect(
    page.getByRole('tab', { name: 'Pending my approval' })
  ).toHaveCount(0);

  await page.getByLabel('Location barcode').fill('INV-SL1');
  await page.getByRole('button', { name: 'Set location' }).click();
  await page.getByLabel('Product IPN or stock item code').fill('INV-SI1');
  await page.getByRole('button', { name: 'Add stock' }).click();

  await expect(page.getByText('Stock updated.')).toBeVisible();
  expect(requestCalls).toBe(0);
  expect(scanPayload).toBeDefined();
  expect(scanPayload).not.toHaveProperty('approval_request_id');
});

test('Scan Stock - Unknown scan becomes the read-only product IPN', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: true });
  const page = await doCachedLogin(browser, {
    url: 'stock/scan/inbound/'
  });
  const scannedIpn = 'INTERNAL-SCAN-001';
  const scanPayloads: any[] = [];
  const draft = approvalRequest({
    direction: 'INBOUND',
    pending_inbound: '1',
    total_quantity: '1'
  });

  await page.route('**/api/stock/scan/requests/**', async (route) => {
    const url = new URL(route.request().url());
    if (
      route.request().method() === 'GET' &&
      url.pathname.endsWith('/requests/')
    ) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ count: 0, results: [] })
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(draft)
    });
  });

  await page.route('**/api/barcode/', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ stocklocation: { pk: 1 } })
    });
  });
  await page.route('**/api/stock/scan/inbound/', async (route) => {
    const payload = route.request().postDataJSON();
    scanPayloads.push(payload);

    if (!payload.create_part) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          terminal: false,
          error_code: 'SETUP_REQUIRED',
          error_message: 'Product setup is required'
        })
      });
      return;
    }

    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        terminal: true,
        replayed: false,
        message: 'Stock updated'
      })
    });
  });

  await page.getByLabel('Location barcode').fill('INV-SL1');
  await page.getByRole('button', { name: 'Set location' }).click();
  await page.getByLabel('Product IPN or stock item code').fill(scannedIpn);
  await page.getByRole('button', { name: 'Add pending inbound' }).click();

  const setupDialog = page.getByRole('dialog', {
    name: 'Create product for pending inbound'
  });
  const ipnInput = setupDialog.getByLabel('Internal part number (IPN)');
  await expect(ipnInput).toHaveValue(scannedIpn);
  await expect(ipnInput).toHaveAttribute('readonly', '');
  await expect(
    setupDialog.getByLabel('Internal part number (IPN, optional)')
  ).toHaveCount(0);

  await setupDialog.getByLabel('Product name').fill('Internal scan product');
  await setupDialog
    .getByRole('button', { name: 'Add product setup to request' })
    .click();

  await expect(setupDialog).not.toBeVisible();
  expect(scanPayloads).toHaveLength(2);
  expect(scanPayloads[1].create_part.IPN).toBe(scannedIpn);
  expect(scanPayloads[1].approval_request_id).toBe(77);
});

test('Scan Stock - Consecutive outbound scans reserve lines in one draft', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: true });
  const page = await doCachedLogin(browser, {
    url: 'stock/scan/outbound/'
  });
  const lines: any[] = [];
  const scanPayloads: any[] = [];
  let submitted = false;

  const currentDraft = () =>
    approvalRequest({
      line_count: lines.length,
      active_line_count: lines.length,
      total_quantity: String(lines.length),
      scan_frozen: String(lines.length),
      can_submit: lines.length > 0,
      lines
    });

  await page.route('**/api/barcode/', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ stocklocation: { pk: 1 } })
    });
  });
  await page.route('**/api/stock/scan/requests/**', async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname.endsWith('/submit/')) {
      submitted = true;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(
          approvalRequest({
            ...currentDraft(),
            status: 'PENDING_APPROVAL',
            submitted_at: '2026-08-27T02:05:00Z',
            can_submit: false
          })
        )
      });
      return;
    }
    if (method === 'GET' && url.pathname.endsWith('/requests/')) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          count: submitted ? 0 : 0,
          results: []
        })
      });
      return;
    }
    await route.fulfill({
      status: method === 'POST' ? 201 : 200,
      contentType: 'application/json',
      body: JSON.stringify(currentDraft())
    });
  });
  await page.route('**/api/stock/scan/outbound/', async (route) => {
    const payload = route.request().postDataJSON();
    scanPayloads.push(payload);
    const event = pendingScanEvent(lines.length + 1, payload.barcode);
    lines.push(event);
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        terminal: true,
        replayed: false,
        event,
        message: 'Stock reserved for approval.'
      })
    });
  });

  await page.getByLabel('Location barcode').fill('INV-SL1');
  await page.getByRole('button', { name: 'Set location' }).click();

  const barcode = page.getByLabel('Product IPN or stock item code');
  await barcode.fill('INV-SI1');
  await page.getByRole('button', { name: 'Reserve stock' }).click();
  await expect(page.getByText('Product 1')).toBeVisible();
  await expect(barcode).toHaveValue('');

  await barcode.fill('INV-SI2');
  await barcode.press('Enter');
  await expect(page.getByText('Product 2')).toBeVisible();
  await expect(barcode).toHaveValue('');
  await expect(page.getByText('SA-20260827-000077').first()).toBeVisible();

  expect(scanPayloads).toHaveLength(2);
  expect(scanPayloads[0].approval_request_id).toBe(77);
  expect(scanPayloads[1].approval_request_id).toBe(77);

  await page.getByRole('button', { name: 'Submit for approval' }).click();
  await expect(page.getByText('Approval request submitted.')).toBeVisible();
  expect(submitted).toBe(true);
});

test('Scan Stock - Approver can open and approve a routed request', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: true });
  const page = await doCachedLogin(browser, {
    url: 'stock/scan/outbound/'
  });
  let approved = false;
  const pending = approvalRequest({
    requester: 999,
    requester_username_snapshot: 'operator',
    requester_display_name_snapshot: 'Warehouse Operator',
    status: 'PENDING_APPROVAL',
    line_count: 1,
    active_line_count: 1,
    total_quantity: '1',
    scan_frozen: '1',
    submitted_at: '2026-08-27T02:05:00Z',
    can_submit: false,
    can_cancel: false,
    can_approve: true,
    can_reject: true,
    lines: [pendingScanEvent(1, 'INV-SI1')]
  });

  await page.route('**/api/stock/scan/requests/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/approve/')) {
      approved = true;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          ...pending,
          status: 'COMPLETED',
          decided_at: '2026-08-27T02:06:00Z',
          can_approve: false,
          can_reject: false
        })
      });
      return;
    }
    if (url.pathname.endsWith('/requests/')) {
      const pendingForMe = url.searchParams.get('pending_for_me') === 'true';
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          count: pendingForMe ? 1 : 0,
          results: pendingForMe ? [pending] : []
        })
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(pending)
    });
  });

  await page.getByRole('tab', { name: 'Pending my approval' }).click();
  await expect(page.getByText('Warehouse Operator')).toBeVisible();
  await page.getByRole('button', { name: 'Open' }).click();
  const details = page.getByRole('dialog', {
    name: 'SA-20260827-000077'
  });
  await expect(details.getByRole('button', { name: 'Approve' })).toBeVisible();
  await details.getByRole('button', { name: 'Approve' }).click();
  const confirm = page.getByRole('dialog', { name: 'Approve request' });
  await confirm.getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByText('Approval request completed.')).toBeVisible();
  expect(approved).toBe(true);
});

test('Scan Stock - Confirmed mode change clears scan context', async ({
  browser
}) => {
  await setSettingState({ setting: 'SCAN_APPROVAL_ENABLED', value: false });
  const page = await doCachedLogin(browser, {
    url: 'stock/scan/inbound/'
  });

  const barcodeInput = page.getByLabel('Product IPN or stock item code');
  const destinationInput = page.getByLabel('Destination location');
  const locationBarcodeInput = page.getByLabel('Location barcode');
  const missingLocationMessage =
    'Select a destination location before scanning stock.';

  await barcodeInput.fill('missing-location-context');
  await barcodeInput.press('Enter');
  await expect(page.getByText(missingLocationMessage)).toBeVisible();

  await page.route('**/api/barcode/', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ stocklocation: { pk: 1 } })
    });
  });
  await locationBarcodeInput.fill('{"stocklocation": 1}');
  await page.getByRole('button', { name: 'Set location' }).click();
  await expect(
    page.getByText('Destination location set from barcode.')
  ).toBeVisible();
  await expect(destinationInput).not.toHaveValue('');
  const selectedLocation = await destinationInput.inputValue();

  await barcodeInput.fill('unsubmitted-scan-context');
  await page.getByRole('button', { name: 'Outbound', exact: true }).click();

  const confirmDialog = page.getByRole('dialog', {
    name: 'Confirm mode change'
  });
  await expect(confirmDialog).toBeVisible();
  await expect(page).toHaveURL(/\/web\/stock\/scan\/inbound\/$/);

  await confirmDialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(confirmDialog).not.toBeVisible();
  await expect(destinationInput).toHaveValue(selectedLocation);
  await expect(barcodeInput).toHaveValue('unsubmitted-scan-context');
  await expect(page.getByText(missingLocationMessage)).toBeVisible();

  await page.getByRole('button', { name: 'Outbound', exact: true }).click();
  await page.getByRole('button', { name: 'Switch to outbound' }).click();

  await page.waitForURL('**/web/stock/scan/outbound/');
  await expect(page.getByText('Outbound mode', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Source location')).toHaveValue('');
  await expect(page.getByLabel('Location barcode')).toHaveValue('');
  await expect(page.getByLabel('Product IPN or stock item code')).toHaveValue(
    ''
  );
  await expect(page.getByText(missingLocationMessage)).not.toBeVisible();
});
