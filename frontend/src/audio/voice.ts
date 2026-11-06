import { MicrophoneCapture } from './capture'
import { FRAME_SAMPLES, PcmFramer, SAMPLE_RATE } from './pcm'
import { PcmPlayback } from './playback'

export type VoicePhase = 'idle' | 'starting' | 'recording' | 'finishing' | 'thinking' | 'speaking'
export interface VoiceState { phase: VoicePhase; error: string | null }
const SOCKET_BUFFER_LIMIT = 32 * 1024 // Bounded browser WebSocket send queue.

function audioError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') return 'Bitte erlaube den Mikrofonzugriff in deinem Browser.'
    if (error.name === 'NotFoundError') return 'Es wurde kein Mikrofon gefunden.'
    if (error.name === 'NotReadableError') return 'Das Mikrofon ist gerade nicht verfügbar.'
  }
  return error instanceof Error ? error.message : 'Die Sprachverbindung wurde unterbrochen. Bitte erneut versuchen.'
}

/** A single live microphone turn. No upload, transcript UI or parallel input pipeline. */
export class VoiceSession {
  private context: AudioContext | null = null
  private capture: MicrophoneCapture | null = null
  private framer: PcmFramer | null = null
  private playback: PcmPlayback | null = null
  private socket: WebSocket | null = null
  private abort = new AbortController()
  private closed = false
  private phase: VoicePhase = 'starting'
  private maxTimer: ReturnType<typeof setTimeout> | null = null
  private watchdog: ReturnType<typeof setTimeout> | null = null
  private receivedDone = false
  private audioReceived = false
  private sentBytes = 0
  private maxBytes = 30 * SAMPLE_RATE * 2
  private readyReject: ((reason: Error) => void) | null = null

  constructor(private readonly update: (state: VoiceState) => void) {}

  private state(phase: VoicePhase): void {
    if (this.closed) return
    this.phase = phase
    this.update({ phase, error: null })
  }

  // Call directly in the click handler: resume() must retain the browser's user activation.
  async start(): Promise<void> {
    try {
      this.state('starting')
      const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Context) throw new Error('Dieser Browser unterstützt die benötigte Audioausgabe nicht.')
      this.context = new Context({ sampleRate: 24000 })
      const resumed = this.context.resume()
      this.context.onstatechange = () => {
        if (!this.closed && this.phase !== 'starting' && this.context?.state !== 'running') {
          this.fail(new Error('Die Audioausgabe wurde vom Browser pausiert. Bitte erneut starten.'))
        }
      }
      this.capture = new MicrophoneCapture(this.context)
      this.framer = new PcmFramer(this.context.sampleRate)
      this.playback = new PcmPlayback(this.context, () => this.played())
      const url = new URL('/api/voice', window.location.href)
      url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      this.socket = new WebSocket(url)
      const ready = new Promise<void>((resolve, reject) => {
        this.readyReject = reject
        this.socket!.onmessage = event => {
          if (this.closed) return
          try {
            const message = JSON.parse(event.data)
            if (message.type === 'ready') {
              if (this.readyReject === null || message.sample_rate !== SAMPLE_RATE || message.frame_samples !== FRAME_SAMPLES || !Number.isFinite(message.max_seconds) || message.max_seconds <= 0 || message.max_seconds > 30) {
                throw new Error('Das Audioformat des Servers wird nicht unterstützt.')
              }
              this.maxBytes = Math.floor(message.max_seconds * SAMPLE_RATE) * 2
              this.readyReject = null
              if (this.watchdog) clearTimeout(this.watchdog)
              resolve()
            } else this.receive(message)
          } catch (error) { this.fail(error) }
        }
        this.socket!.onerror = () => this.fail(new Error('Die Sprachverbindung konnte nicht hergestellt werden.'))
        this.socket!.onclose = event => {
          if (this.closed) return
          if (event.code === 4401) window.dispatchEvent(new Event('session-expired'))
          this.fail(new Error('Die Sprachverbindung wurde beendet. Bitte erneut versuchen.'))
        }
        this.watchdog = setTimeout(() => this.fail(new Error('Der Sprachdienst antwortet nicht. Bitte erneut versuchen.')), 10_000)
      })
      await Promise.all([ready, resumed])
      if (this.closed) return
      if (this.context.state !== 'running') throw new Error('Bitte aktiviere die Audioausgabe im Browser und versuche es erneut.')
      await this.capture.start(this.abort.signal, samples => {
        if (this.closed || (this.phase !== 'recording' && this.phase !== 'finishing')) return
        try { for (const frame of this.framer!.feed(samples)) this.sendFrame(frame) } catch (error) { this.fail(error) }
      }, () => this.fail(new Error('Das Mikrofon wurde getrennt. Bitte erneut verbinden.')))
      if (this.closed) return
      this.state('recording')
      this.maxTimer = setTimeout(() => { void this.finish() }, this.maxBytes / (SAMPLE_RATE * 2) * 1000)
    } catch (error) {
      if (!this.closed) this.fail(error)
    }
  }

  private sendFrame(frame: ArrayBuffer): void {
    if (this.closed) return
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error('Die Sprachverbindung ist unterbrochen.')
    if (this.socket.bufferedAmount + frame.byteLength > SOCKET_BUFFER_LIMIT) throw new Error('Die Verbindung ist zu langsam für die Aufnahme. Bitte erneut versuchen.')
    const remaining = this.maxBytes - this.sentBytes
    if (remaining <= 0) { if (this.phase === 'recording') void this.finish(); return }
    const data = frame.byteLength > remaining ? frame.slice(0, remaining) : frame
    this.socket.send(data)
    this.sentBytes += data.byteLength
    if (this.sentBytes === this.maxBytes && this.phase === 'recording') void this.finish()
  }

  async finish(): Promise<void> {
    if (this.closed || this.phase !== 'recording') return
    this.state('finishing')
    if (this.maxTimer) clearTimeout(this.maxTimer)
    try {
      await this.capture?.finish()
      if (this.closed) return
      for (const frame of this.framer!.flush()) this.sendFrame(frame)
      if (this.sentBytes === 0) throw new Error('Es wurde keine Aufnahme erkannt. Bitte erneut versuchen.')
      this.socket!.send(JSON.stringify({ type: 'finish' }))
      this.state('thinking')
      this.watchdog = setTimeout(() => this.fail(new Error('Die Antwort hat zu lange gedauert. Bitte erneut versuchen.')), 180_000)
    } catch (error) { if (!this.closed) this.fail(error) }
  }

  private receive(message: Record<string, unknown>): void {
    if (message.type === 'error') {
      throw new Error(typeof message.detail === 'string' ? message.detail : 'Die Sprachanfrage konnte nicht verarbeitet werden.')
    }
    if (this.phase === 'recording' || this.phase === 'starting' || this.phase === 'finishing') {
      throw new Error('Der Sprachdienst hat unerwartet geantwortet.')
    }
    if (message.type === 'state') {
      // Keep "speaking" once audio has arrived even while the next sentence is synthesized.
      if (!this.audioReceived) this.state(message.state === 'speaking' ? 'speaking' : 'thinking')
    } else if (message.type === 'audio') {
      if (typeof message.data !== 'string' || typeof message.sample_rate !== 'number') throw new Error('Ungültige Sprachausgabe.')
      this.audioReceived = true
      this.playback!.push(message.data, message.sample_rate)
      this.state('speaking')
    } else if (message.type === 'done') {
      if (!this.audioReceived) throw new Error('Die Antwort enthält keine Sprachausgabe.')
      if (this.receivedDone) throw new Error('Die Antwort wurde mehrfach abgeschlossen.')
      this.receivedDone = true
      this.playback!.finish()
    } else throw new Error('Der Sprachdienst hat eine ungültige Nachricht geliefert.')
  }

  private played(): void {
    if (this.closed || !this.receivedDone) return
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: 'played' }))
    this.cleanup()
    this.update({ phase: 'idle', error: null })
  }

  private fail(error: unknown): void {
    if (this.closed) return
    const detail = audioError(error)
    this.cleanup()
    this.update({ phase: 'idle', error: detail })
  }

  cancel(notify = true): void {
    if (this.closed) return
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: 'cancel' }))
    this.cleanup()
    if (notify) this.update({ phase: 'idle', error: null })
  }

  private cleanup(): void {
    if (this.closed) return
    this.closed = true
    if (this.watchdog) clearTimeout(this.watchdog)
    if (this.maxTimer) clearTimeout(this.maxTimer)
    this.readyReject?.(new DOMException('Abgebrochen', 'AbortError'))
    this.readyReject = null
    this.abort.abort()
    this.capture?.stop()
    this.playback?.stop()
    if (this.socket) {
      this.socket.onmessage = null
      this.socket.onclose = null
      this.socket.onerror = null
      this.socket.close()
    }
    if (this.context) {
      this.context.onstatechange = null
      if (this.context.state !== 'closed') void this.context.close().catch(() => {})
    }
  }
}
