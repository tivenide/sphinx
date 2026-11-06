// AudioWorklet frame capture with an explicit drain for single utterances.
class SphinxCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buffer = new Float32Array(Math.round(sampleRate * 0.03))
    this.filled = 0
    this.ended = false
    this.port.onmessage = event => {
      if (event.data !== 'finish') return
      this.ended = true
      if (this.filled) this.emit(this.buffer.slice(0, this.filled))
      this.filled = 0
      this.port.postMessage({ type: 'finished' })
    }
  }
  emit(samples) { this.port.postMessage({ type: 'samples', samples }, [samples.buffer]) }
  process(inputs) {
    if (this.ended) return false
    const samples = inputs[0] && inputs[0][0]
    if (!samples) return true
    for (const sample of samples) {
      this.buffer[this.filled++] = sample
      if (this.filled === this.buffer.length) {
        this.emit(this.buffer.slice())
        this.filled = 0
      }
    }
    return true
  }
}
registerProcessor('sphinx-capture', SphinxCapture)
