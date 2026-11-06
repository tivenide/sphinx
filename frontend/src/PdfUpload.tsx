import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { api, ApiError } from './api'

const UPLOAD_PATH = '/ingestion/documents'
const MAX_PDF_BYTES = 20 * 1024 * 1024

type UploadState = 'ready' | 'invalid' | 'uploading' | 'uploaded' | 'existing' | 'error'
interface UploadResult {
  stored: true
  status: 'ingested' | 'already_exists'
  document_id: string
  chunks: number
}
interface UploadItem {
  id: number
  file: File
  state: UploadState
  detail: string | null
}

const stateText: Record<UploadState, string> = {
  ready: 'Bereit zum Hochladen', invalid: 'Nicht hochladbar', uploading: 'Wird verarbeitet …',
  uploaded: 'In der Wissensbasis gespeichert', existing: 'Bereits in der Wissensbasis vorhanden', error: 'Upload fehlgeschlagen',
}

function fileError(file: File): string | null {
  if (!file.name.toLowerCase().endsWith('.pdf')) return 'Bitte eine PDF-Datei auswählen.'
  if (file.size === 0) return 'Diese Datei ist leer.'
  if (file.size > MAX_PDF_BYTES) return 'Diese Datei ist größer als 20 MB.'
  return null
}

function fileSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toLocaleString('de-DE', { maximumFractionDigits: 1 })} MB`
}

function uploadError(error: unknown): string {
  if (error instanceof ApiError) {
    if ([404, 405, 501].includes(error.status)) return 'Der PDF-Upload ist noch nicht verfügbar. Bitte versuche es später erneut.'
    if (error.status === 413) return 'Die Datei überschreitet das Upload-Limit des Servers.'
    if (error.status === 403) return 'Du hast keine Berechtigung, PDFs hochzuladen.'
  }
  return error instanceof Error ? error.message : 'Die PDF konnte nicht hochgeladen werden.'
}

export function PdfUpload({ onDocuments }: { onDocuments: () => void }) {
  const [items, setItems] = useState<UploadItem[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nextId = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const chooseButton = useRef<HTMLButtonElement>(null)

  useEffect(() => () => { controller.current?.abort() }, [])

  const select = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files || []).map(file => {
      const detail = fileError(file)
      return { id: nextId.current++, file, state: detail ? 'invalid' as const : 'ready' as const, detail }
    })
    setItems(current => [...current, ...selected.filter(item => !current.some(existing =>
      existing.file.name === item.file.name && existing.file.size === item.file.size && existing.file.lastModified === item.file.lastModified,
    ))])
    setError(null)
    event.target.value = ''
  }

  const upload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (controller.current) return
    const queued = items.filter(item => item.state === 'ready' || item.state === 'error')
    if (!queued.length) return
    const pending = new AbortController()
    controller.current = pending
    setBusy(true)
    setError(null)
    const update = (id: number, state: UploadState, detail: string | null = null) => {
      if (!pending.signal.aborted) setItems(current => current.map(item => item.id === id ? { ...item, state, detail } : item))
    }
    try {
      for (const item of queued) {
        if (pending.signal.aborted) break
        update(item.id, 'uploading')
        const data = new FormData()
        data.append('file', item.file)
        try {
          const result = await api<UploadResult>(UPLOAD_PATH, { method: 'POST', body: data, signal: pending.signal })
          if (result?.stored !== true || !['ingested', 'already_exists'].includes(result.status)) {
            throw new ApiError('Die Speicherung konnte nicht bestätigt werden. Bitte erneut versuchen.', 503)
          }
          update(item.id, result.status === 'already_exists' ? 'existing' : 'uploaded')
        } catch (error) {
          if (pending.signal.aborted) break
          const detail = uploadError(error)
          update(item.id, 'error', detail)
          setError(detail)
          // Stop on connectivity, authentication or a missing endpoint; retain the queue.
          if (error instanceof ApiError && [0, 401, 403, 404, 405, 429, 501, 503, 504].includes(error.status)) break
        }
      }
    } finally {
      if (controller.current === pending) controller.current = null
      if (!pending.signal.aborted) setBusy(false)
    }
  }

  const uploaded = items.filter(item => item.state === 'uploaded' || item.state === 'existing').length
  const pending = items.filter(item => item.state === 'ready' || item.state === 'error').length

  return <section className="pdf-upload" aria-labelledby="pdf-heading">
    <div className="heading"><h1 id="pdf-heading">PDFs hochladen</h1><button className="quiet" onClick={onDocuments} disabled={busy}>Dokumente ansehen</button></div>
    <p className="subtitle">Ergänze die Wissensbasis von Sphinx mit deinen Dokumenten.</p>
    <form className="form" onSubmit={upload}>
      <div className="pdf-picker">
        <h2>Dokumente auswählen</h2>
        <p className="field-help" id="pdf-help">Mehrere PDFs möglich, jeweils bis zu 20 MB. Nach dem Upload werden die Dokumente für die Wissensbasis verarbeitet.</p>
        <input ref={input} name="files" type="file" accept=".pdf,application/pdf" multiple hidden onChange={select} disabled={busy} aria-label="PDFs auswählen" />
        <button ref={chooseButton} className="secondary" type="button" disabled={busy} onClick={() => input.current?.click()} aria-describedby="pdf-help">PDFs auswählen</button>
      </div>
      {items.length > 0 && <ul className="pdf-list" aria-label="Ausgewählte PDFs">
        {items.map(item => <li className="pdf-row" key={item.id} data-state={item.state}>
          <div className="pdf-info">
            <strong>{item.file.name}</strong>
            <span className="field-help">{fileSize(item.file.size)} · {stateText[item.state]}</span>
            {item.detail && <p className="pdf-error">{item.detail}</p>}
          </div>
          <button className="quiet" type="button" disabled={busy} aria-label={`${item.file.name} aus der Liste entfernen`} onClick={() => {
            setItems(current => current.filter(existing => existing.id !== item.id))
            setError(null)
            chooseButton.current?.focus()
          }}>Entfernen</button>
        </li>)}
      </ul>}
      <p className="field-help" role="status">{busy ? 'PDFs werden hochgeladen und verarbeitet. Bitte lasse diese Seite geöffnet.' : uploaded > 0
        ? `${uploaded} von ${items.length} ${items.length === 1 ? 'PDF ist' : 'PDFs sind'} in der Wissensbasis gespeichert.`
        : items.some(item => item.state === 'invalid') ? 'Bitte entferne ungültige Dateien oder wähle andere PDFs aus.'
        : pending > 0 ? `${pending} ${pending === 1 ? 'PDF bereit' : 'PDFs bereit'} zum Hochladen.` : 'Noch keine PDFs ausgewählt.'}</p>
      <div className="actions">
        <button className="primary" disabled={busy || pending === 0}>{busy ? 'Verarbeiten …' : 'Upload starten'}</button>
      </div>
    </form>
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
