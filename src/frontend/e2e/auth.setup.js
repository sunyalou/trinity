import { test as setup, expect } from '@playwright/test'

const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD
if (!ADMIN_PASSWORD) {
  throw new Error('ADMIN_PASSWORD env var must be set for e2e tests')
}

setup('authenticate as admin', async ({ page }) => {
  await page.goto('/')

  // Default landing form is email-auth (when EMAIL_AUTH_ENABLED is true). The
  // admin form is reached via a toggle button labelled "Admin Login". When
  // EMAIL_AUTH_ENABLED is false the admin form shows immediately.
  const adminToggle = page.getByRole('button', { name: /admin login/i })
  if (await adminToggle.isVisible({ timeout: 5000 }).catch(() => false)) {
    await adminToggle.click()
  }

  // Wait for the password input to be ready, then fill + submit. Using the
  // form's submit button (rather than a name regex) keeps this resilient to
  // copy changes.
  const passwordInput = page.locator('#password')
  await passwordInput.waitFor({ state: 'visible', timeout: 10000 })
  await passwordInput.fill(ADMIN_PASSWORD)
  await page.locator('form button[type="submit"]').click()

  // Login successful when the password field is gone (form unmounted).
  await expect(passwordInput).toBeHidden({ timeout: 10000 })
  await page.context().storageState({ path: 'e2e/.auth/admin.json' })
})
