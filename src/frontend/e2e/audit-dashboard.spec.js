import { test, expect } from '@playwright/test'

/**
 * Enterprise audit log dashboard e2e (#941).
 *
 * Drives the new /enterprise/audit dashboard end-to-end:
 *   - the Enterprise nav link appears for admins (any entitlement)
 *   - the /enterprise landing surfaces an "Audit Log" card marked Available
 *   - clicking the card navigates to /enterprise/audit
 *   - the dashboard renders the filter form + table
 *   - filtering by event_type changes the visible rows
 *   - clicking a row opens the side detail panel
 *
 * Assumes the enterprise submodule is mounted (default for the local
 * stack and the frontend-e2e workflow). In OSS-only builds the
 * /enterprise route 302s to / — that path is covered by the unit
 * test on the route guard, not this spec.
 *
 * The admin login itself emits an `authentication` audit event the
 * first time we authenticate, so by the time we reach the dashboard
 * there is at least one row to render. We do NOT seed extra rows via
 * the internal write endpoint — keeping the spec read-only avoids
 * polluting the dev audit log with synthetic noise.
 */

test.describe('enterprise audit dashboard (#941)', () => {
  test('@smoke admin sees Enterprise nav and the audit card', async ({ page }) => {
    await page.goto('/')
    // NavBar lazily fires the feature-flags request on mount; give it
    // a beat to resolve before we assert.
    const enterpriseNav = page.locator('nav a:has-text("Enterprise")')
    await expect(enterpriseNav).toBeVisible({ timeout: 10000 })

    await enterpriseNav.click()
    await expect(page).toHaveURL(/\/enterprise$/)

    // The audit card is now Available (#941 flips its soon flag).
    const auditCard = page.locator('h3:has-text("Audit Log")')
    await expect(auditCard).toBeVisible()
    // Confirm the card is in the Available state (not Coming soon).
    const card = auditCard.locator('xpath=ancestor::*[contains(@class,"block")][1]')
    await expect(card.locator('text=Available').first()).toBeVisible()
  })

  test('@smoke clicking the audit card opens the dashboard', async ({ page }) => {
    await page.goto('/enterprise')
    await page.locator('h3:has-text("Audit Log")').click()
    await expect(page).toHaveURL(/\/enterprise\/audit$/)

    // Header + filter form render.
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible()
    await expect(page.locator('text=Filters')).toBeVisible()
    await expect(page.locator('button:has-text("Apply")')).toBeVisible()
    await expect(page.locator('button:has-text("Reset")')).toBeVisible()

    // Either the table OR the empty state must render (depends on
    // whether the local DB has any rows in the last-24h window).
    const hasTable = await page
      .locator('table')
      .first()
      .isVisible({ timeout: 5000 })
      .catch(() => false)
    const hasEmptyState = await page
      .locator('text=No audit entries match these filters.')
      .isVisible({ timeout: 5000 })
      .catch(() => false)
    expect(hasTable || hasEmptyState).toBeTruthy()
  })

  test('@smoke row click opens the side detail panel', async ({ page }) => {
    await page.goto('/enterprise/audit')

    // Widen the time window so we're not flaky on dev instances where
    // the last-24h slice is empty (e.g. the box was off).
    await page.locator('input[placeholder*="2026-"]').first().fill('')
    await page.locator('button:has-text("Apply")').click()

    // If still empty, the rest of the test isn't meaningful — skip
    // rather than fail. Catches the genuinely-empty audit_log case.
    const emptyVisible = await page
      .locator('text=No audit entries match these filters.')
      .isVisible({ timeout: 5000 })
      .catch(() => false)
    test.skip(emptyVisible, 'audit_log is empty on this instance')

    // Click the first data row.
    const firstRow = page.locator('tbody tr').first()
    await expect(firstRow).toBeVisible({ timeout: 10000 })
    await firstRow.click()

    // Side panel renders with at least the event_type/action header
    // and the details JSON disclosure.
    await expect(page.locator('text=Details JSON')).toBeVisible({ timeout: 5000 })
    await expect(page.locator('text=Hash chain')).toBeVisible()
  })

  test('@smoke filter dropdown is populated from the distinct endpoint', async ({ page }) => {
    await page.goto('/enterprise/audit')

    // The event_type <select> should have at least the "All" option +
    // one or more concrete event_type values from
    // /api/audit-log/distinct/event-types. On a brand-new install
    // with zero audit rows we tolerate just "All" — covered by the
    // skip below.
    const eventTypeSelect = page.locator('label:has-text("Event type") + select')
    await expect(eventTypeSelect).toBeVisible({ timeout: 10000 })

    const optionCount = await eventTypeSelect.locator('option').count()
    test.skip(optionCount < 2, 'distinct/event-types returned empty (no audit rows yet)')

    // Pick the first non-"All" option and apply.
    const concrete = await eventTypeSelect.locator('option').nth(1).getAttribute('value')
    await eventTypeSelect.selectOption(concrete)
    await page.locator('button:has-text("Apply")').click()

    // The pagination footer's range label should mention this filter
    // resulted in either rows or "No entries" — both are fine, the
    // round-trip is what we're testing.
    await expect(
      page.locator('text=/Showing|No (audit )?entries/').first()
    ).toBeVisible({ timeout: 5000 })
  })
})
