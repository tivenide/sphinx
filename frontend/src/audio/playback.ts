// Web Audio scheduling for a single spoken response.
const LEAD_SECONDS = 0.08
const MAX_AUDIO_SECONDS = 90

export class PcmPlayback {
  private readonly gain: GainNode
  private readonly sources = new Set<AudioBufferSourceNode>()
  private nextStart = 0
  private totalSeconds = 0
  private complete = false
  private stopped = false

  constructor(private readonly context: AudioContext, private readonly onDrained: () => void) {
    this.gain = context.createGain()
    this.gain.connect(context.destination)
  }

  push(encoded: string, sampleRate: number): void {
    if (this.stopped) return
    if (this.complete) throw new Error('Nach Ende der Antwort wurden weitere Audiodaten empfangen.')
    if (this.context.state !== 'running') throw new Error('Die Audioausgabe wurde vom Browser pausiert. Bitte erneut starten.')
    if (![16000, 22050, 24000, 44100, 48000].includes(sampleRate) || encoded.length > 350_000) {
      throw new Error('Die Sprachausgabe hat ein ungültiges Audioformat geliefert.')
    }
    const binary = atob(encoded)
    if (binary.length === 0 || binary.length % 2) throw new Error('Die Sprachausgabe ist unvollständig.')
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
    const view = new DataView(bytes.buffer)
    const samples = binary.length / 2
    this.totalSeconds += samples / sampleRate
    if (this.totalSeconds > MAX_AUDIO_SECONDS) throw new Error('Die Sprachausgabe ist zu lang. Bitte stelle eine kürzere Frage.')
    const buffer = this.context.createBuffer(1, samples, sampleRate)
    const channel = buffer.getChannelData(0)
    for (let i = 0; i < samples; i++) channel[i] = view.getInt16(i * 2, true) / 32768
    const source = this.context.createBufferSource()
    source.buffer = buffer
    source.connect(this.gain)
    const start = Math.max(this.context.currentTime + LEAD_SECONDS, this.nextStart)
    this.nextStart = start + buffer.duration
    this.sources.add(source)
    source.onended = () => {
      this.sources.delete(source)
      source.disconnect()
      this.checkDrained()
    }
    source.start(start)
  }

  finish(): void {
    this.complete = true
    this.checkDrained()
  }

  private checkDrained(): void {
    if (!this.stopped && this.complete && this.sources.size === 0) {
      this.stopped = true
      this.gain.disconnect()
      this.onDrained()
    }
  }

  stop(): void {
    this.stopped = true
    this.gain.gain.cancelScheduledValues(this.context.currentTime)
    this.gain.gain.setValueAtTime(0, this.context.currentTime)
    for (const source of this.sources) {
      source.onended = null
      try { source.stop() } catch { /* A one-shot source may already be stopped. */ }
      source.disconnect()
    }
    this.sources.clear()
    this.gain.disconnect()
  }
}
