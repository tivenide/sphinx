// Stream one browser microphone through an AudioWorklet.
export class MicrophoneCapture {
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private fallback: ScriptProcessorNode | null = null
  private silent: GainNode | null = null
  private finishAck: (() => void) | null = null
  private stopped = false

  constructor(private readonly context: AudioContext) {}

  async start(signal: AbortSignal, onFrame: (samples: Float32Array) => void, onEnded: () => void): Promise<void> {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Für das Mikrofon ist eine sichere HTTPS-Verbindung erforderlich.')
    }
    signal.throwIfAborted()
    const stop = () => this.stop()
    signal.addEventListener('abort', stop, { once: true })
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      })
      if (signal.aborted || this.stopped) {
        for (const track of stream.getTracks()) track.stop()
        throw new DOMException('Aufnahme abgebrochen', 'AbortError')
      }
      this.stream = stream
      this.source = this.context.createMediaStreamSource(stream)
      this.silent = this.context.createGain()
      this.silent.gain.value = 0
      this.silent.connect(this.context.destination)
      if (this.context.audioWorklet) {
        await this.context.audioWorklet.addModule('/audio-capture.js')
        signal.throwIfAborted()
        if (this.stopped) throw new DOMException('Aufnahme abgebrochen', 'AbortError')
        this.worklet = new AudioWorkletNode(this.context, 'sphinx-capture', {
          numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
        })
        this.worklet.port.onmessage = event => {
          if (this.stopped) return
          if (event.data?.type === 'finished') this.finishAck?.()
          else if (event.data?.type === 'samples') onFrame(event.data.samples)
        }
        this.source.connect(this.worklet)
        this.worklet.connect(this.silent)
      } else {
        // The compatibility path streams PCM directly.
        this.fallback = this.context.createScriptProcessor(2048, 1, 1)
        this.fallback.onaudioprocess = event => {
          if (!this.stopped) onFrame(new Float32Array(event.inputBuffer.getChannelData(0)))
        }
        this.source.connect(this.fallback)
        this.fallback.connect(this.silent)
      }
      stream.getAudioTracks()[0]?.addEventListener('ended', () => {
        if (!this.stopped) onEnded()
      }, { once: true })
    } catch (error) {
      this.stop()
      throw error
    } finally {
      // VoiceSession owns the abort signal for all of capture, transport and playback.
      if (this.stopped) signal.removeEventListener('abort', stop)
    }
  }

  async finish(): Promise<void> {
    if (this.stopped) return
    this.source?.disconnect()
    try {
      if (this.worklet) {
        await new Promise<void>((resolve, reject) => {
          const timeout = window.setTimeout(() => reject(new Error('Die Aufnahme konnte nicht vollständig beendet werden.')), 1500)
          this.finishAck = () => { clearTimeout(timeout); resolve() }
          this.worklet!.port.postMessage('finish')
        })
      }
    } finally {
      this.stop()
    }
  }

  stop(): void {
    this.stopped = true
    this.finishAck?.()
    this.finishAck = null
    if (this.worklet) {
      this.worklet.port.onmessage = null
      this.worklet.port.close()
      this.worklet.disconnect()
    }
    if (this.fallback) {
      this.fallback.onaudioprocess = null
      this.fallback.disconnect()
    }
    this.source?.disconnect()
    this.silent?.disconnect()
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
    this.worklet = null
    this.fallback = null
    this.source = null
    this.silent = null
  }
}
