import { describe, expect, it } from 'vitest'
import { FRAME_SAMPLES, PcmFramer } from './pcm'

function collect(rate: number, samples: Float32Array, partition: number): Int16Array {
  const framer = new PcmFramer(rate)
  const chunks: ArrayBuffer[] = []
  for (let i = 0; i < samples.length; i += partition) chunks.push(...framer.feed(samples.subarray(i, i + partition)))
  chunks.push(...framer.flush())
  expect(chunks.every(chunk => chunk.byteLength <= FRAME_SAMPLES * 2 && chunk.byteLength % 2 === 0)).toBe(true)
  return new Int16Array(new Uint8Array(chunks.flatMap(chunk => Array.from(new Uint8Array(chunk)))).buffer)
}

describe('PCM capture', () => {
  it('keeps the same timing and samples across arbitrary 44.1 kHz chunk boundaries', () => {
    const samples = Float32Array.from({ length: 44100 * 3 }, (_, i) => Math.sin(i / 30) * .5)
    const expected = collect(44100, samples, samples.length)
    expect(expected.length).toBe(16000 * 3)
    for (const partition of [128, 2048, 1323, 1]) expect(collect(44100, samples, partition)).toEqual(expected)
  })
  it('flushes the last short frame exactly once and clamps PCM instead of wrapping', () => {
    const framer = new PcmFramer(16000)
    expect(framer.feed(new Float32Array([-2, 2, -.5, .5]))).toEqual([])
    expect(Array.from(new Int16Array(framer.flush()[0]))).toEqual([-32768, 32767, -16384, 16384])
    expect(framer.flush()).toEqual([])
  })
  it('retains a final fractional sample at 24 kHz', () => {
    expect(Array.from(collect(24000, new Float32Array([.5, .5, .5, .5]), 1))).toEqual([16384, 16384, 16384])
  })
  it('rejects corrupt capture data and unsupported sample rates', () => {
    expect(() => new PcmFramer(8000)).toThrow()
    expect(() => new PcmFramer(24000).feed(new Float32Array([NaN]))).toThrow()
  })
})
