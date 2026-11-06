import { expect, test, type Page } from '@playwright/test'

async function session(page: Page, role: 'admin' | 'user' = 'admin') {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') return route.fulfill({ json: { id: 1, username: 'alex', role, active: true } })
    if (path === '/api/health/ready') return route.fulfill({ status: 503, json: { ready: false } })
    if (path === '/api/models') return route.fulfill({ json: { state: 'unavailable', can_load: false, detail: null, models: [] } })
    return route.fulfill({ status: 404, json: { detail: 'Nicht gefunden.' } })
  })
}

async function openUpload(page: Page) {
  await page.goto('/')
  await page.getByRole('region', { name: 'Wissensbasis', exact: true }).getByRole('button', { name: 'PDFs hochladen', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'PDFs hochladen', exact: true })).toBeVisible()
}

const pdf = (name: string) => ({ name, mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7\nUI upload fixture\n%%EOF') })

test('admin uploads PDFs as multipart, sees partial failures and only retries failed files', async ({ page }) => {
  await session(page)
  const requests: string[] = []
  let releaseFirst!: () => void
  const firstResponse = new Promise<void>(resolve => { releaseFirst = resolve })
  await page.route('**/api/ingestion/documents', async route => {
    const request = route.request()
    expect(request.method()).toBe('POST')
    expect(request.headers()['content-type']).toMatch(/^multipart\/form-data; boundary=/)
    const body = request.postDataBuffer()!.toString('utf8')
    expect(body).toContain('name="file"; filename=')
    expect(body).toContain('%PDF-1.7')
    requests.push(body)
    if (requests.length === 1) await firstResponse
    if (requests.length === 2) return route.fulfill({ status: 422, json: { detail: 'Die PDF ist passwortgeschützt.' } })
    return route.fulfill({ status: 201, json: { status: 'ingested', stored: true, document_id: `document-${requests.length}`, chunks: 1 } })
  })

  await openUpload(page)
  const files = page.locator('input[type="file"]')
  const list = page.getByRole('list', { name: 'Ausgewählte PDFs' })
  const chooser = page.waitForEvent('filechooser')
  await page.getByRole('button', { name: 'PDFs auswählen', exact: true }).click()
  await (await chooser).setFiles([pdf('Handbuch.pdf'), pdf('Abläufe.pdf')])
  await expect(page.getByRole('status')).toHaveText('2 PDFs bereit zum Hochladen.')
  await page.getByRole('button', { name: 'Upload starten', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Verarbeiten …', exact: true })).toBeDisabled()
  await expect(page.getByRole('status')).toHaveText('PDFs werden hochgeladen und verarbeitet. Bitte lasse diese Seite geöffnet.')
  await expect(files).toBeDisabled()
  await expect(list.getByRole('button').first()).toBeDisabled()
  releaseFirst()
  await expect(list.getByRole('listitem').filter({ hasText: 'Handbuch.pdf' })).toContainText('In der Wissensbasis gespeichert')
  await expect(list.getByRole('listitem').filter({ hasText: 'Abläufe.pdf' })).toContainText('Upload fehlgeschlagen')
  await expect(page.getByRole('alert')).toHaveText('Die PDF ist passwortgeschützt.')
  await expect(page.getByRole('status')).toHaveText('1 von 2 PDFs sind in der Wissensbasis gespeichert.')
  await page.getByRole('button', { name: 'Upload starten', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('2 von 2 PDFs sind in der Wissensbasis gespeichert.')
  await expect(page.getByRole('button', { name: 'Upload starten', exact: true })).toBeDisabled()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(requests).toHaveLength(3)
  expect(requests[0]).toContain('filename="Handbuch.pdf"')
  expect(requests[1]).toContain('filename="Abläufe.pdf"')
  expect(requests[2]).toContain('filename="Abläufe.pdf"')
  await page.screenshot({ path: 'test-results/pdf-upload-desktop.png' })
})

test('invalid files stay local, can be removed, and long filenames fit a mobile screen', async ({ page }) => {
  await session(page)
  let uploadRequests = 0
  await page.route('**/api/ingestion/documents', async route => {
    uploadRequests++
    await route.fulfill({ status: 200, json: { status: 'already_exists', stored: true, document_id: 'existing-document', chunks: 1 } })
  })
  await page.setViewportSize({ width: 320, height: 700 })
  await openUpload(page)
  const files = page.getByLabel('PDFs auswählen', { exact: true })
  await files.setInputFiles([
    { name: 'Notizen.txt', mimeType: 'text/plain', buffer: Buffer.from('Not a PDF') },
    { name: 'Leer.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(0) },
  ])
  await files.evaluate((element: HTMLInputElement) => {
    const selection = new DataTransfer()
    selection.items.add(new File([new Uint8Array(20 * 1024 * 1024 + 1)], 'Zu-groß.pdf', { type: 'application/pdf' }))
    element.files = selection.files
    element.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await expect(page.getByText('Bitte eine PDF-Datei auswählen.', { exact: true })).toBeVisible()
  await expect(page.getByText('Diese Datei ist leer.', { exact: true })).toBeVisible()
  await expect(page.getByText('Diese Datei ist größer als 20 MB.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Upload starten', exact: true })).toBeDisabled()
  expect(uploadRequests).toBe(0)
  for (const name of ['Notizen.txt', 'Leer.pdf', 'Zu-groß.pdf']) {
    await page.getByRole('button', { name: `${name} aus der Liste entfernen`, exact: true }).click()
  }
  await expect(page.getByRole('status')).toHaveText('Noch keine PDFs ausgewählt.')
  const name = `${'Sehr-langes-Dokument-'.repeat(8)}.PDF`
  await files.setInputFiles({ ...pdf(name), mimeType: '' })
  await expect(page.getByRole('button', { name: 'Upload starten', exact: true })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/pdf-upload-mobile.png' })
  await page.getByRole('button', { name: 'Upload starten', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('1 von 1 PDF ist in der Wissensbasis gespeichert.')
  await expect(page.getByRole('listitem')).toContainText('Bereits in der Wissensbasis vorhanden')
  expect(uploadRequests).toBe(1)
})

for (const failure of [
  { status: 404, message: 'Der PDF-Upload ist noch nicht verfügbar. Bitte versuche es später erneut.' },
  { status: 504, message: 'Die Verarbeitung dauert zu lange. Bitte erneut versuchen.' },
  { status: 202, message: 'Die Speicherung konnte nicht bestätigt werden. Bitte erneut versuchen.' },
]) test(`HTTP ${failure.status} without confirmed storage stops the queue; an expired session returns to login`, async ({ page }) => {
  await session(page)
  let uploadRequests = 0
  await page.route('**/api/ingestion/documents', async route => {
    uploadRequests++
    await route.fulfill({ status: uploadRequests === 1 ? failure.status : 401, json: { detail: failure.message } })
  })
  await page.goto('/')
  await page.getByLabel('Kontomenü für alex').click()
  await page.locator('.account-menu').getByRole('button', { name: 'PDFs hochladen', exact: true }).click()
  await page.getByLabel('PDFs auswählen', { exact: true }).setInputFiles([pdf('Erstes.pdf'), pdf('Zweites.pdf')])
  await page.getByRole('button', { name: 'Upload starten', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(failure.message)
  await expect(page.locator('[data-state="uploaded"], [data-state="existing"]')).toHaveCount(0)
  await expect(page.getByRole('listitem').filter({ hasText: 'Zweites.pdf' })).toContainText('Bereit zum Hochladen')
  expect(uploadRequests).toBe(1)
  await page.getByRole('button', { name: 'Upload starten', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Willkommen zurück.', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('Deine Sitzung ist abgelaufen. Bitte melde dich erneut an.')
  expect(uploadRequests).toBe(2)
})

test('ordinary users have no PDF upload entry or ingestion requests', async ({ page }) => {
  await session(page, 'user')
  const requests: string[] = []
  page.on('request', request => {
    if (request.url().includes('/api/ingestion')) requests.push(request.url())
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Sprachdienst nicht verfügbar', exact: true })).toBeVisible()
  await page.getByLabel('Kontomenü für alex').click()
  await expect(page.getByRole('button', { name: 'PDFs hochladen', exact: true })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Wissensbasis', exact: true })).toHaveCount(0)
  await expect(page.locator('input[type="file"]')).toHaveCount(0)
  expect(requests).toEqual([])
})
