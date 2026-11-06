import { expect, test, type Page } from '@playwright/test'

type ModelState = 'not_loaded' | 'loading' | 'ready' | 'error' | 'unavailable'

function modelSnapshot(state: ModelState, canLoad = state === 'not_loaded', detail: string | null = null) {
  return {
    state, can_load: canLoad, detail,
    models: [
      { id: 'asr', label: 'Spracherkennung', state, detail: null },
      { id: 'llm', label: 'Denkmodell', state, detail: null },
      { id: 'tts', label: 'Sprachausgabe', state, detail: null },
    ],
  }
}

async function mockApi(page: Page) {
  let loggedIn = false
  const users = [{ id: 1, username: 'admin', role: 'admin', active: true }, { id: 2, username: 'mila', role: 'user', active: true }]
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    let status = 200
    let body: unknown
    if (path === '/api/auth/me') { status = loggedIn ? 200 : 401; body = loggedIn ? users[0] : { detail: 'Nicht angemeldet.' } }
    else if (path === '/api/auth/login') { loggedIn = true; body = users[0] }
    else if (path === '/api/auth/logout' || path === '/api/auth/password') { loggedIn = false; status = 204 }
    else if (path === '/api/health/ready') body = { ready: true, services: { asr: true, llm: true, tts: true } }
    else if (path === '/api/models' && request.method() === 'GET') body = modelSnapshot('ready')
    else if (path === '/api/users' && request.method() === 'GET') body = users
    else if (path === '/api/users' && request.method() === 'POST') {
      const data = request.postDataJSON()
      const user = { id: users.length + 1, username: data.username, role: data.role, active: true }
      users.push(user); body = user
    } else if (/^\/api\/users\/\d+$/.test(path)) {
      const user = users.find(item => item.id === Number(path.split('/').at(-1)))!
      Object.assign(user, request.postDataJSON()); body = user
    } else if (/^\/api\/users\/\d+\/password$/.test(path)) status = 204
    else { status = 404; body = { detail: 'Nicht gefunden.' } }
    await route.fulfill({ status, contentType: 'application/json', ...(status === 204 ? {} : { body: JSON.stringify(body) }) })
  })
}

async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('Benutzername', { exact: true }).fill('admin')
  await page.getByLabel('Passwort', { exact: true }).fill('only-a-mocked-password')
  await page.getByRole('button', { name: 'Anmelden', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeEnabled()
}

test('live PCM capture, playback confirmation, navigation cleanup and user management', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await mockApi(page)
  let bytes = 0
  let finished = 0
  let played = 0
  let cancelled = 0
  await page.routeWebSocket('**/api/voice', socket => {
    socket.send(JSON.stringify({ type: 'ready', sample_rate: 16000, frame_samples: 512, max_seconds: 30 }))
    socket.onMessage(message => {
      if (typeof message !== 'string') {
        bytes += message.length
        expect(message.length).toBeLessThanOrEqual(1024)
        expect(message.length % 2).toBe(0)
        return
      }
      const data = JSON.parse(message)
      if (data.type === 'finish') {
        finished++
        socket.send(JSON.stringify({ type: 'state', state: 'thinking' }))
        socket.send(JSON.stringify({ type: 'audio', sample_rate: 24000, data: Buffer.alloc(24000).toString('base64') }))
        socket.send(JSON.stringify({ type: 'done' }))
      } else if (data.type === 'played') { played++; socket.close({ code: 1000 }) }
      else if (data.type === 'cancel') { cancelled++; socket.close({ code: 1000 }) }
    })
  })
  await login(page)
  await expect(page.locator('input[type="file"], textarea')).toHaveCount(0)
  await page.getByRole('button', { name: 'Sprechen', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Ich höre zu', exact: true })).toBeVisible()
  await expect.poll(() => bytes).toBeGreaterThan(3000)
  await page.getByRole('button', { name: 'Aufnahme beenden', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Bereit', exact: true })).toBeVisible()
  await expect.poll(() => played).toBe(1)
  expect(finished).toBe(1)
  await page.getByRole('button', { name: 'Sprechen', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Ich höre zu', exact: true })).toBeVisible()
  await page.getByLabel('Kontomenü für admin').click()
  await page.getByRole('button', { name: 'Nutzer verwalten', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Nutzer', exact: true })).toBeVisible()
  await expect.poll(() => cancelled).toBe(1)
  await page.getByRole('button', { name: 'Nutzer anlegen', exact: true }).click()
  await page.getByLabel('Benutzername', { exact: true }).fill('alex')
  await page.getByLabel('Passwort', { exact: true }).fill('another-mocked-password')
  await page.getByRole('button', { name: 'Zugang anlegen', exact: true }).click()
  const alex = page.locator('.user-row').filter({ hasText: 'alex' })
  await alex.getByRole('button', { name: 'Deaktivieren', exact: true }).click()
  await expect(alex.getByText('Deaktiviert', { exact: true })).toBeVisible()
  await alex.getByRole('button', { name: 'Passwort setzen', exact: true }).click()
  await page.getByLabel('Neues Passwort', { exact: true }).fill('another-mocked-password-2')
  await page.locator('form').getByRole('button', { name: 'Passwort setzen', exact: true }).click()
  await page.getByRole('button', { name: '← Zur Sprachsteuerung', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Bereit', exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/voice-desktop.png' })
  await page.setViewportSize({ width: 320, height: 700 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/voice-mobile.png' })
  await page.getByLabel('Kontomenü für admin').click()
  await page.getByRole('button', { name: 'Passwort ändern', exact: true }).click()
  await page.getByLabel('Aktuelles Passwort', { exact: true }).fill('only-a-mocked-password')
  await page.getByLabel('Neues Passwort', { exact: true }).fill('new-mocked-password')
  await page.getByLabel('Neues Passwort wiederholen', { exact: true }).fill('new-mocked-password')
  await page.locator('form').getByRole('button', { name: 'Passwort ändern', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Willkommen zurück.', exact: true })).toBeVisible()
  expect(errors).toEqual([])
})

test('denied microphone permission returns to idle and closes the voice connection', async ({ page }) => {
  await mockApi(page)
  let closed = false
  await page.routeWebSocket('**/api/voice', socket => {
    socket.send(JSON.stringify({ type: 'ready', sample_rate: 16000, frame_samples: 512, max_seconds: 30 }))
    socket.onClose(() => { closed = true })
  })
  await login(page)
  await page.evaluate(() => {
    navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Denied', 'NotAllowedError') }
  })
  await page.getByRole('button', { name: 'Sprechen', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Bitte erlaube den Mikrofonzugriff in deinem Browser.')
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeEnabled()
  await expect.poll(() => closed).toBe(true)
})

test('admin loads models and speaking stays disabled until the services are ready', async ({ page }) => {
  let loadRequests = 0
  let loadingChecks = 0
  let ready = false
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let status = 200
    let body: unknown
    if (path === '/api/auth/me') body = { id: 1, username: 'admin', role: 'admin', active: true }
    else if (path === '/api/health/ready') {
      status = ready ? 200 : 503
      body = { ready, services: { asr: ready, llm: ready, tts: ready } }
    } else if (path === '/api/models/load' && route.request().method() === 'POST') {
      loadRequests++
      status = 202
      body = modelSnapshot('loading')
    } else if (path === '/api/models' && route.request().method() === 'GET') {
      if (loadRequests) {
        loadingChecks++
        ready = loadingChecks >= 2
        body = modelSnapshot(ready ? 'ready' : 'loading')
      } else body = modelSnapshot('not_loaded')
    } else { status = 404; body = { detail: 'Nicht gefunden.' } }
    await route.fulfill({ status, json: body })
  })

  await page.goto('/')
  const models = page.getByRole('region', { name: 'Modelle', exact: true })
  await expect(models.getByRole('button', { name: 'Modelle laden', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeDisabled()
  await models.getByRole('button', { name: 'Modelle laden', exact: true }).click()
  await expect(models.getByRole('button', { name: 'Modelle werden geladen …', exact: true })).toBeDisabled()
  await expect.poll(() => loadingChecks, { timeout: 7000 }).toBeGreaterThanOrEqual(1)
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeEnabled({ timeout: 8000 })
  await expect(models.getByRole('listitem')).toHaveCount(3)
  for (const label of ['Spracherkennung', 'Denkmodell', 'Sprachausgabe']) {
    await expect(models.getByRole('listitem').filter({ hasText: label })).toContainText('Bereit')
  }
  await expect(models.getByRole('button', { name: 'Modelle laden', exact: true })).toBeDisabled()
  expect(loadRequests).toBe(1)
  await page.setViewportSize({ width: 320, height: 700 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('admin can recheck a blocked 409 model snapshot after host preparation', async ({ page }) => {
  const reason = 'Die Spracherkennung ist noch nicht vorbereitet. Das benötigte Image fehlt.'
  let loadRequests = 0
  let prepared = false
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let status = 200
    let body: unknown
    if (path === '/api/auth/me') body = { id: 1, username: 'admin', role: 'admin', active: true }
    else if (path === '/api/health/ready') { status = 503; body = { ready: false, services: { asr: false, llm: false, tts: false } } }
    else if (path === '/api/models' && route.request().method() === 'GET') {
      body = loadRequests && !prepared ? modelSnapshot('unavailable', false, reason) : modelSnapshot('not_loaded')
    }
    else if (path === '/api/models/load' && route.request().method() === 'POST') {
      loadRequests++
      status = 409
      body = modelSnapshot('unavailable', false, reason)
    } else { status = 404; body = { detail: 'Nicht gefunden.' } }
    await route.fulfill({ status, json: body })
  })

  await page.goto('/')
  const models = page.getByRole('region', { name: 'Modelle', exact: true })
  await models.getByRole('button', { name: 'Modelle laden', exact: true }).click()
  await expect(models.getByText(reason, { exact: true })).toBeVisible()
  await expect(models.getByRole('button', { name: 'Modelle laden', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeDisabled()
  await expect(models.getByRole('button', { name: 'Erneut prüfen', exact: true })).toBeEnabled()
  prepared = true
  await models.getByRole('button', { name: 'Erneut prüfen', exact: true }).click()
  await expect(models.getByRole('button', { name: 'Modelle laden', exact: true })).toBeEnabled()
  await expect(models.getByText(reason, { exact: true })).toHaveCount(0)
  await expect(models.getByRole('button', { name: 'Erneut prüfen', exact: true })).toBeEnabled()
  expect(loadRequests).toBe(1)
})

test('admin detects stopped models, reloads them and stops polling after navigation', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  let modelState: ModelState = 'ready'
  let speechReady = true
  let modelChecks = 0
  let loadRequests = 0
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let status = 200
    let body: unknown
    if (path === '/api/auth/me') body = { id: 1, username: 'admin', role: 'admin', active: true }
    else if (path === '/api/health/ready') {
      status = speechReady ? 200 : 503
      body = { ready: speechReady }
    } else if (path === '/api/models' && route.request().method() === 'GET') {
      modelChecks++
      body = modelSnapshot(modelState)
    } else if (path === '/api/models/load' && route.request().method() === 'POST') {
      loadRequests++
      modelState = 'loading'
      status = 202
      body = modelSnapshot(modelState)
    } else { status = 404; body = { detail: 'Nicht gefunden.' } }
    await route.fulfill({ status, json: body })
  })

  await page.goto('/')
  const models = page.getByRole('region', { name: 'Modelle', exact: true })
  const speak = page.getByRole('button', { name: 'Sprechen', exact: true })
  const load = models.getByRole('button', { name: 'Modelle laden', exact: true })
  await expect(models.getByRole('status')).toHaveText('Alle Modelle bereit')
  await expect(speak).toBeEnabled()

  // Docker was stopped outside the app after the initial ready snapshot.
  modelState = 'not_loaded'
  speechReady = false
  await page.clock.runFor(10_000)
  await expect(models.getByRole('status')).toHaveText('Nicht geladen')
  await expect(load).toBeEnabled()
  await expect(speak).toBeDisabled()
  expect(loadRequests).toBe(0)
  await load.click()
  await expect(models.getByRole('button', { name: 'Modelle werden geladen …', exact: true })).toBeDisabled()
  expect(loadRequests).toBe(1)

  modelState = 'ready'
  await page.clock.runFor(2500)
  await expect(models.getByRole('status')).toHaveText('Alle Modelle bereit')
  await expect(load).toBeDisabled()
  // Container status alone must never enable speech before the endpoints are ready.
  await expect(speak).toBeDisabled()
  speechReady = true
  await page.clock.runFor(2500)
  await expect(speak).toBeEnabled()

  await page.getByLabel('Kontomenü für admin').click()
  await page.getByRole('button', { name: 'Passwort ändern', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Passwort ändern', exact: true })).toBeVisible()
  const checksBeforeNavigation = modelChecks
  await page.clock.runFor(30_000)
  expect(modelChecks).toBe(checksBeforeNavigation)
  expect(loadRequests).toBe(1)
})

for (const failure of ['http', 'network'] as const) {
  test(`model refresh clears stale readiness on ${failure} failure and recovers automatically`, async ({ page }) => {
    await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
    await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
    let fail = false
    let modelState: ModelState = 'ready'
    let loadRequests = 0
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      let status = 200
      let body: unknown
      if (path === '/api/auth/me') body = { id: 1, username: 'admin', role: 'admin', active: true }
      else if (path === '/api/health/ready') body = { ready: true }
      else if (path === '/api/models' && route.request().method() === 'GET') {
        if (fail && failure === 'network') { await route.abort(); return }
        status = fail ? 503 : 200
        body = fail ? { detail: 'Die Modellsteuerung ist nicht erreichbar.' } : modelSnapshot(modelState)
      } else if (path === '/api/models/load') {
        loadRequests++
        status = 409
        body = modelSnapshot('error', false)
      } else { status = 404; body = { detail: 'Nicht gefunden.' } }
      await route.fulfill({ status, json: body })
    })

    await page.goto('/')
    const models = page.getByRole('region', { name: 'Modelle', exact: true })
    const refresh = models.getByRole('button', { name: 'Erneut prüfen', exact: true })
    const load = models.getByRole('button', { name: 'Modelle laden', exact: true })
    await expect(models.getByRole('status')).toHaveText('Alle Modelle bereit')
    await expect(refresh).toBeEnabled()
    fail = true
    await refresh.click()
    await expect(models.getByRole('status')).toHaveText('Status nicht verfügbar')
    await expect(models.getByRole('alert')).toBeVisible()
    await expect(models.getByText('Bereit', { exact: true })).toHaveCount(0)
    await expect(load).toBeDisabled()
    await expect(refresh).toBeEnabled()

    fail = false
    modelState = 'error'
    await page.clock.runFor(10_000)
    await expect(models.getByRole('status')).toHaveText('Fehler')
    await expect(models.getByRole('alert')).toHaveCount(0)
    // A running but unhealthy service is not startable (can_load is false).
    await expect(load).toBeDisabled()
    modelState = 'ready'
    await page.clock.runFor(10_000)
    await expect(models.getByRole('status')).toHaveText('Alle Modelle bereit')
    await expect(refresh).toBeEnabled()
    expect(loadRequests).toBe(0)
  })
}

test('ordinary users only poll speech readiness and never request model controls', async ({ page }) => {
  const modelRequests: string[] = []
  let healthChecks = 0
  let ready = false
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let status = 200
    let body: unknown
    if (path === '/api/auth/me') body = { id: 2, username: 'mila', role: 'user', active: true }
    else if (path === '/api/health/ready') {
      healthChecks++
      status = ready ? 200 : 503
      body = { ready, services: { asr: ready, llm: ready, tts: ready } }
    } else if (path.startsWith('/api/models')) {
      modelRequests.push(path)
      status = 403
      body = { detail: 'Nur für Admins.' }
    } else { status = 404; body = { detail: 'Nicht gefunden.' } }
    await route.fulfill({ status, json: body })
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Sprachdienst nicht verfügbar', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeDisabled()
  await expect(page.getByRole('region', { name: 'Modelle', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Modelle laden', exact: true })).toHaveCount(0)
  ready = true
  await expect(page.getByRole('button', { name: 'Sprechen', exact: true })).toBeEnabled({ timeout: 7000 })
  expect(healthChecks).toBeGreaterThanOrEqual(2)
  expect(modelRequests).toEqual([])
})
