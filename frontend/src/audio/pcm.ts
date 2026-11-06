// Keep fractional resampling state across capture frames; never round each frame separately.
export const SAMPLE_RATE = 16_000
export const FRAME_SAMPLES = 512
const EPSILON = 1e-9

export class PcmFramer {
  private weight = 0
  private sum = 0
  private frame = new ArrayBuffer(FRAME_SAMPLES * 2)
  private offset = 0
  private readonly ratio: number

  constructor(inputSampleRate: number) {
    if (!Number.isFinite(inputSampleRate) || inputSampleRate < SAMPLE_RATE) {
      throw new Error('Die Samplerate deines Browsers wird nicht unterstützt.')
    }
    this.ratio = inputSampleRate / SAMPLE_RATE
  }

  private append(sample: number): ArrayBuffer | null {
    const clamped = Math.max(-1, Math.min(1, sample))
    // Explicit little endian also keeps the wire format independent of the client architecture.
    new DataView(this.frame).setInt16(this.offset * 2, Math.round(clamped < 0 ? clamped * 32768 : clamped * 32767), true)
    if (++this.offset < FRAME_SAMPLES) return null
    const frame = this.frame
    this.frame = new ArrayBuffer(FRAME_SAMPLES * 2)
    this.offset = 0
    return frame
  }

  feed(samples: Float32Array): ArrayBuffer[] {
    const frames: ArrayBuffer[] = []
    for (const sample of samples) {
      if (!Number.isFinite(sample)) throw new Error('Das Mikrofon hat ungültige Audiodaten geliefert.')
      let remaining = 1
      while (remaining > EPSILON) {
        const consumed = Math.min(remaining, this.ratio - this.weight)
        this.sum += sample * consumed
        this.weight += consumed
        remaining -= consumed
        if (this.weight + EPSILON >= this.ratio) {
          const frame = this.append(this.sum / this.ratio)
          if (frame) frames.push(frame)
          this.sum = 0
          this.weight = 0
        }
      }
    }
    return frames
  }

  // Flush the partial frame at the end of each utterance.
  flush(): ArrayBuffer[] {
    const frames: ArrayBuffer[] = []
    if (this.weight > EPSILON) {
      const frame = this.append(this.sum / this.weight)
      if (frame) frames.push(frame)
    }
    if (this.offset > 0) frames.push(this.frame.slice(0, this.offset * 2))
    this.weight = 0
    this.sum = 0
    this.offset = 0
    this.frame = new ArrayBuffer(FRAME_SAMPLES * 2)
    return frames
  }
}
