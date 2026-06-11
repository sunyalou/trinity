import { test, expect, request } from '@playwright/test'

/**
 * Agent Detail tab overflow (#1114).
 *
 * The Agent Detail tab bar (AgentDetail.vue) used to rely on `overflow-x-auto`
 * horizontal scrolling once the tab count exceeded the container width. This
 * suite pins the replacement behavior — the reusable `OverflowTabs.vue`
 * "priority+" pattern: as many tabs as fit render inline, the remainder
 * collapse into a right-aligned "More ▾" disclosure, and the split re-measures
 * on resize via ResizeObserver.
 *
 * These tests only render tabs (no Claude dispatch) but need a real agent to
 * exist and navigate to, so they are @interactive. `beforeAll` logs in via the
 * API and resolves a target agent: TABS_TEST_AGENT if set and present,
 * otherwise the first agent the admin can see. The agent does NOT need to be
 * running — the tab strip renders regardless of status.
 *
 * Stable selectors are component-internal contracts defined by OverflowTabs:
 *   - visible More trigger:  [data-overflow-trigger]
 *   - open dropdown panel:   [data-overflow-menu]
 *   - dropdown items:        [data-overflow-menu] [data-menu-item]
 *   - hidden measurement row buttons: [data-measure-tab]
 * (The hidden mirror row's "More" has data-measure-more and NO
 *  data-overflow-trigger, so the trigger selector never matches it.)
 */

let TEST_AGENT = process.env.TABS_TEST_AGENT || ''
let api

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) {
    throw new Error(`Admin login failed: ${loginResp.status()}`)
  }
  const token = (await loginResp.json()).access_token
  const listResp = await api.get('/api/agents', {
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await listResp.json()
  const agents = Array.isArray(body) ? body : body.agents || []
  const names = agents.map((a) => a.name)
  if (!TEST_AGENT || !names.includes(TEST_AGENT)) {
    if (names.length === 0) throw new Error('No agents available to test tab overflow')
    TEST_AGENT = names[0]
  }
})

test.afterAll(async () => {
  if (api) await api.dispose()
})

// Count how many tabs are currently in the overflow set (total measured tabs
// minus inline tabs). Returns 0 when nothing overflows (no More trigger).
async function overflowCount(page) {
  return page.evaluate(() => {
    const firstNav = document.querySelector('nav.-mb-px')
    if (!firstNav) return -1
    const root = firstNav.closest('.relative.border-b')
    const vis = root.querySelector('nav.-mb-px:not([style*="max-content"])')
    const inline = [...vis.querySelectorAll(':scope > button')].filter(
      (b) => !b.hasAttribute('data-overflow-trigger')
    ).length
    const total = root.querySelectorAll('[data-measure-tab]').length
    return total - inline
  })
}

test.describe('Agent Detail tab overflow (#1114)', () => {
  // B1 — at a narrow viewport the bar overflows into a "More" dropdown and the
  // visible nav does NOT horizontally scroll (the regression being fixed).
  test('@interactive narrow viewport collapses overflow into a More dropdown', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)

    const more = page.locator('[data-overflow-trigger]')
    await expect(more).toBeVisible({ timeout: 15000 })
    await expect(more).toContainText('More')

    // The visible nav fits the container — no horizontal scroll.
    const noScroll = await page.evaluate(() => {
      const root = document.querySelector('nav.-mb-px').closest('.relative.border-b')
      const nav = root.querySelector('nav.-mb-px:not([style*="max-content"])')
      return nav.scrollWidth <= root.clientWidth + 2
    })
    expect(noScroll).toBe(true)
  })

  // B2 — opening "More" reveals the overflow items; selecting one activates
  // that tab and closes the menu, and the trigger reflects the active state.
  test('@interactive selecting an overflow item activates that tab', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)

    const more = page.locator('[data-overflow-trigger]')
    await expect(more).toBeVisible({ timeout: 15000 })
    await more.click()

    const menu = page.locator('[data-overflow-menu]')
    await expect(menu).toBeVisible()

    const lastItem = menu.locator('[data-menu-item]').last()
    await lastItem.click()

    // Menu closes on selection…
    await expect(menu).toHaveCount(0)
    // …and the now-selected tab lives in overflow, so the trigger carries the
    // active underline (border-action-primary-500).
    await expect(more).toHaveClass(/border-action-primary-500/)
  })

  // B3 — ResizeObserver reflow: a wider container fits more tabs inline, so the
  // overflow set shrinks as the viewport grows.
  test('@interactive reflows on resize — wider fits more tabs inline', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)
    await expect(page.locator('[data-overflow-trigger]')).toBeVisible({ timeout: 15000 })

    await page.waitForTimeout(300)
    const narrow = await overflowCount(page)
    expect(narrow).toBeGreaterThan(0)

    await page.setViewportSize({ width: 1280, height: 900 })
    await page.waitForTimeout(300)
    const wide = await overflowCount(page)

    expect(wide).toBeLessThan(narrow)
  })

  // B4 — the active tab is reflected whether it lands inline or in the overflow
  // set, and that tracking survives a live reflow. This is the heart of the
  // "deep-link selects the correct tab whether inline or in the overflow menu"
  // acceptance criterion, exercised through the component's real contract
  // (active id in → correct placement out) rather than AgentDetail's fresh-load
  // ?tab= handling, which is a separate, pre-existing concern.
  test('@interactive active tab is reflected across reflow (overflow → inline)', async ({ page }) => {
    await page.setViewportSize({ width: 640, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)

    const more = page.locator('[data-overflow-trigger]')
    await expect(more).toBeVisible({ timeout: 15000 })

    // Select the FIRST overflow item — the tab that returns inline first when
    // the container widens.
    await more.click()
    const firstItem = page.locator('[data-overflow-menu] [data-menu-item]').first()
    const label = (await firstItem.textContent()).trim()
    await firstItem.click()

    // Active tab now lives in overflow → the trigger reflects it.
    await expect(more).toHaveClass(/border-action-primary-500/)

    // Widen → the selected tab moves inline and renders with inline active
    // styling (proving the active id is reflected in BOTH placements).
    await page.setViewportSize({ width: 1280, height: 900 })
    const inlineActive = page.locator(
      'nav.-mb-px:not([style*="max-content"]) > button.border-action-primary-500'
    )
    await expect(inlineActive).toContainText(label, { timeout: 5000 })
  })

  // B5 — keyboard: Escape closes the dropdown and returns focus to the trigger.
  test('@interactive Escape closes the dropdown and returns focus to the trigger', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)

    const more = page.locator('[data-overflow-trigger]')
    await expect(more).toBeVisible({ timeout: 15000 })
    await more.click()
    await expect(page.locator('[data-overflow-menu]')).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(page.locator('[data-overflow-menu]')).toHaveCount(0)
    await expect(more).toBeFocused()
  })

  // B6 — clicking outside the tab strip closes the dropdown (touch/pointer).
  test('@interactive outside click closes the dropdown', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto(`/agents/${TEST_AGENT}`)

    const more = page.locator('[data-overflow-trigger]')
    await expect(more).toBeVisible({ timeout: 15000 })
    await more.click()
    await expect(page.locator('[data-overflow-menu]')).toBeVisible()

    // Click the top-left corner (NavBar region) — outside the tab strip.
    await page.mouse.click(8, 8)
    await expect(page.locator('[data-overflow-menu]')).toHaveCount(0)
  })
})
