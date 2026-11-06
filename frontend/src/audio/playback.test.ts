import { describe, expect, it, vi } from 'vitest'
import { PcmPlayback } from './playback'

function fixture() {
  const sources: Array<{ onended: (() => void) | null; stop: ReturnType<typeof vi.fn>; start: ReturnType<typeof vi.fn> }> = []
  const gain = { connect: vi.fn(), disconnect: vi.fn(), gain: { cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn() } }
  const context = {
    currentTime: 1,
    state: 'running',
    createGain: () => gain,
    createBuffer: (_channels: number, samples: number, rate: number) => ({ duration: samples / rate, getChannelData: () => new Float32Array(samples) }),
    createBufferSource: () => {
      const source = { onended: null as (() => void) | null, start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn() }
      sources.push(source)
      return source
    },
  }
  const drained = vi.fn()
  return { player: new PcmPlayback(context as unknown as AudioContext, drained), sources, drained }
}

describe('PCM playback', () => {
  it('acknowledges playback only after all scheduled PCM has actually ended', () => {
    const { player, sources, drained } = fixture()
    player.push(btoa('\0'.repeat(4800)), 24000)
    player.push(btoa('\0'.repeat(4800)), 24000)
    expect(sources[1].start.mock.calls[0][0]).toBeCloseTo(sources[0].start.mock.calls[0][0] + .1)
    player.finish()
    expect(drained).not.toHaveBeenCalled()
    sources[0].onended?.()
    expect(drained).not.toHaveBeenCalled()
    sources[1].onended?.()
    expect(drained).toHaveBeenCalledOnce()
  })
  it('stops queued audio and never acknowledges a cancelled answer', () => {
    const { player, sources, drained } = fixture()
    player.push(btoa('\0'.repeat(200)), 24000)
    player.stop()
    expect(sources[0].stop).toHaveBeenCalledOnce()
    expect(sources[0].onended).toBeNull()
    player.finish()
    player.push(btoa('\0'.repeat(200)), 24000)
    expect(sources).toHaveLength(1)
    expect(drained).not.toHaveBeenCalled()
  })
  it('rejects odd PCM and unexpected sample rates before scheduling audio', () => {
    const { player, sources } = fixture()
    expect(() => player.push(btoa('\0'), 24000)).toThrow()
    expect(() => player.push(btoa('\0\0'), 1)).toThrow()
    expect(sources).toHaveLength(0)
  })
})
