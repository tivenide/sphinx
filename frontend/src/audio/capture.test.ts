import { afterEach, expect, it, vi } from 'vitest'
import { MicrophoneCapture } from './capture'

afterEach(() => vi.unstubAllGlobals())

it('releases a microphone permission result that arrives after cancellation', async () => {
  let grant!: (stream: MediaStream) => void
  const stop = vi.fn()
  const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
  vi.stubGlobal('window', { isSecureContext: true })
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: () => new Promise<MediaStream>(resolve => { grant = resolve }) } })
  const capture = new MicrophoneCapture({} as AudioContext)
  const controller = new AbortController()
  const pending = capture.start(controller.signal, vi.fn(), vi.fn())
  controller.abort()
  grant(stream)
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  expect(stop).toHaveBeenCalledOnce()
})
