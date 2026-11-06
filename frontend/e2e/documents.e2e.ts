import { expect, test, type Page } from '@playwright/test'

const date = '2026-11-03T09:15:00Z'
const document = (index = 1) => ({ document_id: `document-${index}`, filename: `Handbuch-${index}.pdf`, chunks: 3, expected_chunks: 3, created_at: date, status: 'stored' })
const attempt = (index = 1) => ({ id: index, filename: `Handbuch-${index}.pdf`, status: 'ingested', detail: null as string | null, started_at: date, finished_at: date as string | null, document_id: `document-${index}` as string | null, chunks: 3 as number | null, stored_document_id: `document-${index}` as string | null })
const empty = () => ({ documents_available: true, documents: { items: [] as ReturnType<typeof document>[], total: 0 as number | null, offset: 0 }, attempts: { items: [] as ReturnType<typeof attempt>[], total: 0, offset: 0 } })

async function session(page: Page, role: 'admin' | 'user' = 'admin') {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') return route.fulfill({ json: { id: 1, username: 'alex', role, active: true } })
    if (path === '/api/health/ready') return route.fulfill({ status: 503, json: { ready: false } })
    if (path === '/api/models') return route.fulfill({ json: { state: 'unavailable', can_load: false, detail: null, models: [] } })
    return route.fulfill({ status: 404, json: { detail: 'Nicht gefunden.' } })
  })
}

async function openDocuments(page: Page) {
  await page.goto('/')
  await page.getByRole('region', { name: 'Wissensbasis', exact: true }).getByRole('button', { name: 'Dokumente', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Dokumente', exact: true })).toBeVisible()
}

test('shows database evidence, complete and incomplete counts, and actual upload outcomes', async ({ page }) => {
  await session(page)
  const snapshot = empty()
  snapshot.documents.items = [document(), { ...document(2), filename: 'Teilweise.pdf', chunks: 2, expected_chunks: 5, status: 'incomplete' }]
  snapshot.documents.total = 2
  snapshot.attempts.items = [attempt(), { ...attempt(2), filename: 'Geschützt.pdf', status: 'failed', detail: 'Die PDF ist passwortgeschützt.', chunks: null, document_id: null, stored_document_id: null }, { ...attempt(3), filename: 'Duplikat.pdf', status: 'already_exists' }]
  snapshot.attempts.total = 3
  await page.route('**/api/ingestion/documents?**', route => {
    expect(route.request().method()).toBe('GET')
    return route.fulfill({ json: snapshot })
  })
  await openDocuments(page)
  const documents = page.getByRole('list', { name: 'Gespeicherte Dokumente' })
  await expect(documents.getByRole('listitem').filter({ hasText: 'Handbuch-1.pdf' })).toContainText('Vollständig gespeichert')
  await expect(documents.getByRole('listitem').filter({ hasText: 'Handbuch-1.pdf' })).toContainText('3 von 3 Abschnitten')
  await expect(documents.getByRole('listitem').filter({ hasText: 'Teilweise.pdf' })).toContainText('2 von 5 Abschnitten')
  await expect(documents.getByRole('listitem').filter({ hasText: 'Teilweise.pdf' })).toContainText('Unvollständig')
  await expect(documents.locator('time').first()).toHaveAttribute('datetime', date)
  const attempts = page.getByRole('list', { name: 'Uploadversuche' })
  await expect(attempts.getByRole('listitem').filter({ hasText: 'Handbuch-1.pdf' })).toContainText('Erfolgreich verarbeitet')
  await expect(attempts.getByRole('listitem').filter({ hasText: 'Geschützt.pdf' })).toContainText('Fehlgeschlagen')
  await expect(attempts.getByRole('listitem').filter({ hasText: 'Geschützt.pdf' })).toContainText('Die PDF ist passwortgeschützt.')
  await expect(attempts.getByRole('listitem').filter({ hasText: 'Duplikat.pdf' })).toContainText('War bereits vorhanden')
  await expect(page.getByText(/Frühere Fehlversuche und Fehler bei direkten n8n-Uploads/)).toBeVisible()
  await page.screenshot({ path: 'test-results/documents-desktop.png' })
})

test('does not treat an unconfirmed attempt or failed attempt as successful when the file exists', async ({ page }) => {
  await session(page)
  const snapshot = empty()
  snapshot.documents.items = [document()]
  snapshot.documents.total = 1
  snapshot.attempts.items = [
    { ...attempt(), status: 'unconfirmed', detail: 'Keine Antwort des Verarbeitungsdienstes.', chunks: null, document_id: null },
    { ...attempt(2), filename: 'Fehler.pdf', status: 'failed', chunks: null },
    { ...attempt(3), filename: 'Entfernt.pdf', stored_document_id: null },
  ]
  snapshot.attempts.total = 3
  await page.route('**/api/ingestion/documents?**', route => route.fulfill({ json: snapshot }))
  await openDocuments(page)
  const attempts = page.getByRole('list', { name: 'Uploadversuche' })
  const unknown = attempts.getByRole('listitem').filter({ hasText: 'Handbuch-1.pdf' })
  await expect(unknown).toContainText('Nicht bestätigt')
  await expect(unknown).toContainText('Die Datei ist inzwischen in der Wissensbasis vorhanden. Ob dieser Uploadversuch erfolgreich war, ist nicht bestätigt.')
  await expect(unknown).not.toContainText('Erfolgreich verarbeitet')
  await expect(unknown).not.toContainText('Beim Abschluss bestätigt')
  const failed = attempts.getByRole('listitem').filter({ hasText: 'Fehler.pdf' })
  await expect(failed).toContainText('Fehlgeschlagen')
  await expect(failed).not.toContainText('Erfolgreich verarbeitet')
  await expect(attempts.getByRole('listitem').filter({ hasText: 'Entfernt.pdf' })).toContainText('Die Speicherung wurde damals bestätigt; die Datei ist aktuell nicht in der Wissensbasis nachweisbar.')
})

test('a database outage is distinct from empty inventory and upload history remains available', async ({ page }) => {
  await session(page)
  let available = false
  await page.route('**/api/ingestion/documents?**', route => route.fulfill({ json: {
    ...empty(), documents_available: available, documents: { items: [], total: available ? 0 : null, offset: 0 },
    attempts: { items: [{ ...attempt(), status: 'unconfirmed', stored_document_id: null, chunks: null }], total: 1, offset: 0 },
  } }))
  await openDocuments(page)
  await expect(page.getByRole('alert')).toHaveText('Die Wissensdatenbank ist nicht erreichbar. Der aktuelle Dokumentbestand kann nicht geprüft werden.')
  await expect(page.getByText('Noch keine Dokumente in der Wissensbasis.', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('list', { name: 'Uploadversuche' })).toContainText('Nicht bestätigt')
  available = true
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click()
  await expect(page.getByText('Noch keine Dokumente in der Wissensbasis.', { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('refresh replaces stale evidence and updates processing attempts from the API', async ({ page }) => {
  await session(page)
  let phase = 0
  await page.route('**/api/ingestion/documents?**', route => {
    if (phase === 2) return route.fulfill({ status: 503, json: { detail: 'Dokumentübersicht vorübergehend nicht erreichbar.' } })
    return route.fulfill({ json: {
      ...empty(), documents: { items: phase ? [document()] : [], total: phase, offset: 0 },
      attempts: { items: [{ ...attempt(), status: phase ? 'ingested' : 'processing', chunks: phase ? 3 : null, finished_at: phase ? date : null, stored_document_id: phase ? 'document-1' : null }], total: 1, offset: 0 },
    } })
  })
  await openDocuments(page)
  await expect(page.getByRole('list', { name: 'Uploadversuche' })).toContainText('Wird verarbeitet')
  phase = 1
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Uploadversuche' })).toContainText('Erfolgreich verarbeitet')
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' })).toContainText('Vollständig gespeichert')
  phase = 2
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Dokumentübersicht vorübergehend nicht erreichbar.')
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' })).toHaveCount(0)
  await expect(page.getByText(/Zuletzt abgerufen:/)).toHaveCount(0)
  phase = 1
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' })).toBeVisible()
})

test('documents and attempts paginate independently with bounded requests', async ({ page }) => {
  await session(page)
  const queries: string[] = []
  await page.route('**/api/ingestion/documents?**', route => {
    const query = new URL(route.request().url()).searchParams
    queries.push(query.toString())
    expect(query.get('limit')).toBe('25')
    const documentsOffset = Number(query.get('documents_offset'))
    const attemptsOffset = Number(query.get('attempts_offset'))
    return route.fulfill({ json: {
      documents_available: true,
      documents: { items: Array.from({ length: documentsOffset ? 1 : 25 }, (_, index) => document(documentsOffset + index + 1)), total: 26, offset: documentsOffset },
      attempts: { items: Array.from({ length: attemptsOffset ? 1 : 25 }, (_, index) => attempt(attemptsOffset + index + 1)), total: 26, offset: attemptsOffset },
    } })
  })
  await openDocuments(page)
  await page.getByRole('button', { name: 'Dokumente: Nächste Seite', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' }).getByRole('listitem')).toHaveCount(1)
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' })).toContainText('Handbuch-26.pdf')
  await expect(page.getByRole('button', { name: 'Dokumente: Nächste Seite', exact: true })).toBeDisabled()
  await expect(page.getByRole('list', { name: 'Uploadversuche' }).getByRole('listitem')).toHaveCount(25)
  await page.getByRole('button', { name: 'Uploadverlauf: Nächste Seite', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Uploadversuche' }).getByRole('listitem')).toHaveCount(1)
  await page.getByRole('button', { name: 'Dokumente: Vorherige Seite', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' }).getByRole('listitem')).toHaveCount(25)
  await expect(page.getByRole('list', { name: 'Uploadversuche' }).getByRole('listitem')).toHaveCount(1)
  // Development StrictMode can replay the initial effect before its abort arrives.
  expect([...new Set(queries)]).toEqual([
    'documents_offset=0&attempts_offset=0&limit=25',
    'documents_offset=25&attempts_offset=0&limit=25',
    'documents_offset=25&attempts_offset=25&limit=25',
    'documents_offset=0&attempts_offset=25&limit=25',
  ])
})

test('account and upload navigation work on mobile with long filenames', async ({ page }) => {
  await session(page)
  await page.setViewportSize({ width: 320, height: 700 })
  const filename = `${'Sehr-langes-Dokument-'.repeat(8)}.pdf`
  const snapshot = empty()
  snapshot.documents.items = [{ ...document(), filename }]
  snapshot.documents.total = 1
  snapshot.attempts.items = [{ ...attempt(), filename, status: 'unconfirmed', chunks: null }]
  snapshot.attempts.total = 1
  await page.route('**/api/ingestion/documents?**', route => route.fulfill({ json: snapshot }))
  await page.goto('/')
  await page.getByLabel('Kontomenü für alex').click()
  await page.locator('.account-menu').getByRole('button', { name: 'Dokumente', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Gespeicherte Dokumente' })).toContainText(filename)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/documents-mobile.png', fullPage: true })
  await page.getByRole('button', { name: 'PDFs hochladen', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'PDFs hochladen', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Dokumente ansehen', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Uploadversuche' })).toBeVisible()
})

test('ordinary users have no document overview entry and make no ingestion requests', async ({ page }) => {
  await session(page, 'user')
  const requests: string[] = []
  page.on('request', request => { if (request.url().includes('/api/ingestion')) requests.push(request.url()) })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Sprachdienst nicht verfügbar', exact: true })).toBeVisible()
  await page.getByLabel('Kontomenü für alex').click()
  await expect(page.getByRole('button', { name: 'Dokumente', exact: true })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Wissensbasis', exact: true })).toHaveCount(0)
  expect(requests).toEqual([])
})

test('expired sessions during overview refresh return to login', async ({ page }) => {
  await session(page)
  await page.route('**/api/ingestion/documents?**', route => route.fulfill({ status: 401, json: { detail: 'Nicht angemeldet.' } }))
  await page.goto('/')
  await page.getByRole('region', { name: 'Wissensbasis', exact: true }).getByRole('button', { name: 'Dokumente', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Willkommen zurück.', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('Deine Sitzung ist abgelaufen. Bitte melde dich erneut an.')
})
