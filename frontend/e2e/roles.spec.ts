import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const password = 'Calibrex-demo1';
const base = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:5173';

async function login(page: Page, email: string) {
  const signOut = page.getByRole('button', { name: /Sign out/ }).first();
  if (await signOut.isVisible().catch(() => false)) {
    await signOut.click();
    await expect(page.getByLabel('Email')).toBeVisible();
  }
  await page.goto('/#/app');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  const response = page.waitForResponse((r) => r.url().includes('/auth/login') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: /Sign out/ })).toBeVisible();
  await expect(page.locator('.workspace-shell')).toHaveAttribute('data-theme', 'light');
  const loginResponse = await response;
  expect(loginResponse.ok()).toBeTruthy();
  return (await loginResponse.json()).access_token as string;
}

async function verifyThemePreferences(page: Page) {
  const shell = page.locator('.workspace-shell');
  await expect(shell).toHaveAttribute('data-theme', 'light');

  await page.getByRole('button', { name: 'Switch to dark theme' }).click();
  await expect(shell).toHaveAttribute('data-theme', 'dark');
  await page.goto('/#/app/scans');
  await expect(shell).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(shell).toHaveAttribute('data-theme', 'dark');

  await page.getByRole('button', { name: 'Switch to light theme' }).click();
  await expect(shell).toHaveAttribute('data-theme', 'light');
  await page.goto('/#/app/scan');
  await expect(shell).toHaveAttribute('data-theme', 'light');
}

async function settleAndResolve(request: APIRequestContext, token: string, scanId: string) {
  const headers = { Authorization: `Bearer ${token}` };
  let view: any;
  for (let i = 0; i < 45; i++) {
    const response = await request.get(`${base}/api/scans/${scanId}`, { headers });
    expect(response.ok()).toBeTruthy();
    view = await response.json();
    if (!['UPLOADED', 'PROCESSING'].includes(view.status)) break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  expect(['UPLOADED', 'PROCESSING']).not.toContain(view.status);
  return { view, headers };
}

async function uploadScan(page: Page, request: APIRequestContext, token: string) {
  await page.goto('/#/app/scan');
  await expect(page.getByLabel('Shelf / location')).toBeVisible();
  const fileInput = page.locator('input[type=file]').last();
  await fileInput.setInputFiles('e2e/fixtures/shelf.jpg');
  await page.getByRole('button', { name: /Upload 1 photo and count/ }).click();
  await expect(page).toHaveURL(/#\/app\/scans\/[0-9a-f-]+/i);
  const scanId = page.url().split('/').pop()!;
  const { view, headers } = await settleAndResolve(request, token, scanId);
  if (view.totals.unresolved > 0) {
    // Exercise the review panel once, then resolve the remaining deterministic stub detections in a batch.
    await page.reload();
    await expect(page.getByRole('tab', { name: /Review/ })).toBeVisible();
    const next = page.getByRole('button', { name: 'Next uncertain' });
    if (await next.isVisible().catch(() => false)) {
      await next.click();
      const suggestions = page.getByRole('list', { name: 'Suggested products' }).getByRole('button');
      if (await suggestions.count()) await suggestions.first().click();
    }
    const after = await (await request.get(`${base}/api/scans/${scanId}`, { headers })).json();
    const unresolved = after.detections.filter((d: any) => !d.deleted && !d.accepted_unknown && ['CHECK', 'UNKNOWN'].includes(d.state));
    if (unresolved.length) {
      await request.patch(`${base}/api/scans/${scanId}/detections`, {
        headers, data: { ids: unresolved.map((d: any) => d.id), action: 'CONFIRM' },
      });
    }
    await page.reload();
    await expect(page.getByText('Every box is resolved.')).toBeVisible();
  }
  await page.getByRole('button', { name: 'Submit for approval' }).click();
  await page.getByRole('dialog', { name: 'submit' }).getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByText('Submitted. A manager will review it.')).toBeVisible();
  return scanId;
}

test('staff, manager, owner, and auditor workflows', async ({ page, request }) => {
  test.setTimeout(120_000);
  // A legacy browser stored dark as the default; the workspace should upgrade it to light once.
  await page.goto('/#/app');
  await page.evaluate(() => {
    localStorage.removeItem('cx.theme-version');
    localStorage.setItem('cx.theme', 'dark');
  });
  const staffToken = await login(page, 'staff@calibrex.demo');
  await verifyThemePreferences(page);
  const approveScan = await uploadScan(page, request, staffToken);
  const returnScan = await uploadScan(page, request, staffToken);

  const managerToken = await login(page, 'manager@calibrex.demo');
  await page.goto('/#/app/scans/' + approveScan);
  await expect(page.getByRole('button', { name: 'Approve' })).toBeVisible();
  await page.getByRole('button', { name: 'Approve' }).click();
  await expect(page.getByText('Scan approved and locked.')).toBeVisible();

  // Return the second staff submission with a pinned box and a correction note.
  await page.goto(`/#/app/scans/${returnScan}`);
  await expect(page.getByRole('button', { name: 'Return…' })).toBeVisible();
  const submittedView = await (await request.get(`${base}/api/scans/${returnScan}`, { headers: { Authorization: `Bearer ${managerToken}` } })).json();
  const detection = submittedView.detections.find((d: any) => !d.deleted);
  if (detection) {
    const canvas = page.locator('canvas[aria-label^="Shelf image"]');
    const bounds = await canvas.boundingBox();
    if (bounds) await canvas.click({ position: { x: (detection.bbox[0] + detection.bbox[2]) / 2 / submittedView.image.width * bounds.width,
      y: (detection.bbox[1] + detection.bbox[3]) / 2 / submittedView.image.height * bounds.height } });
    const pin = page.getByRole('button', { name: /Pin to return note/ });
    await expect(pin).toBeVisible();
    await pin.click();
    await expect(page.getByRole('button', { name: /Unpin from return/ })).toBeVisible();
  }
  await page.getByRole('button', { name: 'Return…' }).click();
  await page.getByPlaceholder('What should be fixed?').fill('Please verify the front row count.');
  await page.getByRole('dialog', { name: 'return' }).getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByText('Returned to staff.')).toBeVisible();

  await login(page, 'owner@calibrex.demo');
  await page.goto('/#/app/team');
  await page.getByLabel('margin').fill('0.08');
  const invitedEmail = `pw-${Date.now()}@example.com`;
  await page.getByLabel('Invite by email').fill(invitedEmail);
  await page.getByRole('button', { name: 'Send invite' }).click();
  await expect(page.getByText(/Invite created/)).toBeVisible();
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Saved. New scans use these values.')).toBeVisible();
  const inviteText = await page.locator('[role=alert]').filter({ hasText: 'Invite created' }).textContent();
  const invitePath = inviteText?.match(/\/#\/invite\/([^\s]+)/)?.[0];
  expect(invitePath).toBeTruthy();
  await page.goto(invitePath!);
  await expect(page.getByRole('heading', { name: 'Accept your invite' })).toBeVisible();
  await page.getByLabel('Your name').fill('Playwright Invitee');
  await page.getByLabel('Choose a password').fill('Invite-demo123');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page).toHaveURL(/#\/app/);

  const auditorToken = await login(page, 'auditor@calibrex.demo');
  await page.goto(`/#/app/scans/${approveScan}`);
  await expect(page.getByText('This scan is approved and locked (read-only).')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit for approval' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Return…' })).toHaveCount(0);
  const readOnlyView = await (await request.get(`${base}/api/scans/${approveScan}`, { headers: { Authorization: `Bearer ${auditorToken}` } })).json();
  const readOnlyDetection = readOnlyView.detections.find((d: any) => !d.deleted);
  if (readOnlyDetection) {
    const canvas = page.locator('canvas[aria-label^="Shelf image"]');
    const bounds = await canvas.boundingBox();
    if (bounds) await canvas.click({ position: { x: (readOnlyDetection.bbox[0] + readOnlyDetection.bbox[2]) / 2 / readOnlyView.image.width * bounds.width,
      y: (readOnlyDetection.bbox[1] + readOnlyDetection.bbox[3]) / 2 / readOnlyView.image.height * bounds.height } });
    await expect(page.getByText('Read-only.')).toBeVisible();
    await expect(page.getByRole('list', { name: 'Suggested products' })).toHaveCount(0);
  }
});

test('manager sees product learning progress and can test recognition without creating a scan', async ({ page }) => {
  test.setTimeout(60_000);
  await login(page, 'manager@calibrex.demo');
  await page.goto('/#/app/catalog');
  await expect(page.getByRole('heading', { name: 'Catalog & expected stock' })).toBeVisible();
  await page.getByLabel('SKU code').fill(`E2E-${Date.now()}`);
  await page.getByLabel('Product name').fill('Recognition preview product');
  await page.locator('form').getByLabel(/Add photos/).click();
  await page.locator('form input[type=file]').setInputFiles('e2e/fixtures/shelf.jpg');
  await page.getByRole('button', { name: 'Register product' }).click();
  await expect(page.getByText('3. Ready for next scan')).toBeVisible();
  await expect(page.getByText('Server status:')).toBeVisible();
  await expect(page.getByText(/saved in the catalog and ready to recognize/)).toBeVisible();

  const card = page.locator('li').filter({ hasText: 'Recognition preview product' });
  await card.getByRole('button', { name: 'Test recognition' }).click();
  await card.locator('input[type=file]').setInputFiles('e2e/fixtures/shelf.jpg');
  await expect(card.getByText(/Detected item|No product boxes found/)).toBeVisible();
  await expect(card.getByText('This preview does not create a scan.')).toBeVisible();
});
