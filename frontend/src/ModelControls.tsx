import { useCallback, useEffect, useRef, useState } from 'react'
import { api, json } from './api'

type ModelState = 'not_loaded' | 'loading' | 'ready' | 'error' | 'unavailable'

const LOADING_POLL_MS = 2500
const STATUS_POLL_MS = 10_000

interface ModelStatus {
  state: ModelState
  can_load: boolean
  detail: string | null
  models: { id: 'asr' | 'llm' | 'tts'; label: string; state: ModelState; detail: string | null }[]
}

const stateText: Record<ModelState, string> = {
  not_loaded: 'Nicht geladen', loading: 'Wird geladen …', ready: 'Bereit',
  error: 'Fehler', unavailable: 'Nicht verfügbar',
}

export function ModelControls({ onReady }: { onReady: () => void }) {
  const [status, setStatus] = useState<ModelStatus | null>(null)
  const [requesting, setRequesting] = useState<'check' | 'load' | null>('check')
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const request = useCallback(async (load = false): Promise<void> => {
    if (timer.current !== null) clearTimeout(timer.current)
    controller.current?.abort()
    const pending = new AbortController()
    controller.current = pending
    setRequesting(load ? 'load' : 'check')
    let delay = STATUS_POLL_MS
    try {
      // A blocked load returns its current status with HTTP 409.
      const next = await api<ModelStatus>(load ? '/models/load' : '/models', {
        ...(load ? json('POST') : {}), signal: pending.signal,
      }, load ? [409] : [])
      if (pending.signal.aborted) return
      setStatus(next)
      setError(null)
      if (next.state === 'ready') onReady()
      if (next.state === 'loading') delay = LOADING_POLL_MS
    } catch (error) {
      if (pending.signal.aborted) return
      setStatus(null)
      setError(error instanceof Error ? error.message : 'Der Modellstatus konnte nicht geprüft werden.')
    } finally {
      if (!pending.signal.aborted) {
        setRequesting(null)
        // Observe external starts/stops and recover from failures without overlapping requests.
        timer.current = setTimeout(() => void request(), delay)
      }
    }
  }, [onReady])

  useEffect(() => {
    void request()
    return () => {
      if (timer.current !== null) clearTimeout(timer.current)
      controller.current?.abort()
    }
  }, [request])

  const loading = requesting === 'load' || status?.state === 'loading'
  return <section className="model-controls" aria-label="Modelle">
    <div className="model-heading"><h2>Modelle</h2><p className="model-summary" role="status">{
      loading ? 'Modelle werden geladen …' : error ? 'Status nicht verfügbar' : status?.state === 'ready' ? 'Alle Modelle bereit' :
        status ? stateText[status.state] : 'Status wird geprüft …'
    }</p></div>
    {status && <ul className="model-list">
      {status.models.map(model => <li key={model.id}>
        <div className="model-row"><span>{model.label}</span><span>{stateText[model.state]}</span></div>
        {model.detail && model.state !== 'ready' && model.state !== 'loading' && model.detail !== status.detail && <p className="model-detail">{model.detail}</p>}
      </li>)}
    </ul>}
    {status?.detail && status.state !== 'ready' && status.state !== 'loading' && <p className="model-detail">{status.detail}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="model-actions">
      <button className="secondary" onClick={() => void request(true)} disabled={requesting !== null || loading || !status?.can_load || status.state === 'ready'}>
        {loading ? 'Modelle werden geladen …' : 'Modelle laden'}
      </button>
      <button className="quiet" onClick={() => void request()} disabled={requesting !== null}>Erneut prüfen</button>
    </div>
  </section>
}
