import { expect, test } from '@playwright/test';

test('public landing page is concise and legacy demo hash stays on the landing page', async ({ page }) => {
  for (const route of ['/#/', '/#/demo']) {
    await page.goto(route);
    await expect(page.getByRole('heading', { name: /Know what.s on every shelf/ })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'From shelf photo to reviewed count' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'A simple, reviewable process' })).toBeVisible();
    await expect(page.getByRole('link', { name: /Open workspace/ })).toHaveAttribute('href', '#/app/scan');
    await expect(page.getByText(/demo data|sample detections|explore demo|interactive demo/i)).toHaveCount(0);
  }
});
