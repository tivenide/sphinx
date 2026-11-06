// Real Caddy + real FastAPI + SQLite + deterministic model HTTP fixtures.
import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs'
import { readFile, mkdir } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'

const password = (await readFile(new URL('../.secrets/admin_password', import.meta.url), 'utf8')).trim()
const browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] })
const context = await browser.newContext({ ignoreHTTPSErrors: true, permissions: ['microphone'], viewport: { width: 1100, height: 750 }, colorScheme: 'light' })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  // A fresh Compose start returns before TLS, the API and model fixtures are ready.
  await expect.poll(async () => {
    try { return (await context.request.get('https://localhost:6510/api/health/live', { timeout: 2000 })).status() }
    catch { return 0 }
  }, { timeout: 30_000, intervals: [250, 500, 1000] }).toBe(200)
  await page.goto('https://localhost:6510')
  await page.getByLabel('Benutzername', { exact: true }).fill('admin')
  await page.getByLabel('Passwort', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Anmelden', exact: true }).click()
  const speak = page.getByRole('button', { name: 'Sprechen', exact: true })
  await expect(speak).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Modelle laden', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Modelle laden', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Modelle werden geladen …', exact: true })).toBeVisible()
  await expect(speak).toBeEnabled({ timeout: 20_000 })
  expect(await page.locator('input[type=file], textarea, .transcript').count()).toBe(0)
  for (let turn = 0; turn < 2; turn++) {
    await speak.click()
    await expect(page.getByRole('heading', { name: 'Ich höre zu' })).toBeVisible()
    // A bounded fake browser microphone feeds real AudioWorklet frames.
    await page.waitForTimeout(500)
    await page.getByRole('button', { name: 'Aufnahme beenden', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Sphinx spricht' })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('heading', { name: 'Bereit', exact: true })).toBeVisible({ timeout: 15_000 })
  }
  const statistics = JSON.parse(execFileSync('docker', ['compose', '-f', 'tests/compose.yaml', 'exec', '-T', 'backend', 'python', '-c', "import urllib.request; print(urllib.request.urlopen('http://models:8100/test/stats').read().decode())"], { encoding: 'utf8' }))
  expect(statistics.asr).toBe(2)
  expect(statistics.tts).toBe(2)
  expect(statistics.loads).toBe(1)
  expect(statistics.history_messages).toEqual([0, 2])
  await mkdir(new URL('../data/verification/', import.meta.url), { recursive: true })
  await page.screenshot({ path: new URL('../data/verification/voice-desktop.png', import.meta.url).pathname })
  await page.setViewportSize({ width: 320, height: 650 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: new URL('../data/verification/voice-mobile.png', import.meta.url).pathname })
  await page.getByRole('region', { name: 'Wissensbasis', exact: true }).getByRole('button', { name: 'Dokumente', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Dokumente', exact: true })).toBeVisible()
  // This stack deliberately has no knowledge DB; the UI must not claim it is empty.
  await expect(page.getByRole('alert')).toContainText('Der aktuelle Dokumentbestand kann nicht geprüft werden.')
  await expect(page.getByText('Noch keine Uploadversuche erfasst.', { exact: true })).toBeVisible()
  await expect(page.getByText('Noch keine Dokumente in der Wissensbasis.', { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByText('admin', { exact: true }).click()
  await page.getByRole('button', { name: 'Abmelden', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Anmelden', exact: true })).toBeVisible()
  expect(errors).toEqual([])
  console.log('PASS: HTTPS login, model load/readiness, authenticated WSS microphone, ASR/LLM/TTS HTTP adapters, real PCM playback, hidden context after playback, document API with unavailable DB, logout and 320px.')
} finally {
  await context.close()
  await browser.close()
}
