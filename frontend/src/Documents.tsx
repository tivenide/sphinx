import { useEffect, useState } from 'react'
import { api } from './api'

const PAGE_SIZE = 25
const dateFormat = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' })

interface StoredDocument {
  document_id: string
  filename: string
  chunks: number
  expected_chunks: number
  created_at: string
  status: 'stored' | 'incomplete'
}

interface UploadAttempt {
  id: number
  filename: string
  status: 'processing' | 'ingested' | 'already_exists' | 'failed' | 'unconfirmed'
  detail: string | null
  started_at: string
  finished_at: string | null
  document_id: string | null
  chunks: number | null
  stored_document_id: string | null
}

interface Overview {
  documents_available: boolean
  documents: { items: StoredDocument[]; total: number | null; offset: number }
  attempts: { items: UploadAttempt[]; total: number; offset: number }
}

const attemptStatus: Record<UploadAttempt['status'], string> = {
  processing: 'Wird verarbeitet', ingested: 'Erfolgreich verarbeitet', already_exists: 'War bereits vorhanden',
  failed: 'Fehlgeschlagen', unconfirmed: 'Nicht bestätigt',
}

function Timestamp({ value }: { value: string }) {
  return <time dateTime={value}>{dateFormat.format(new Date(value))}</time>
}

function Pagination({ label, offset, total, count, onChange }: {
  label: string; offset: number; total: number; count: number; onChange: (offset: number) => void
}) {
  if (total === 0 && offset === 0) return null
  return <nav className="document-pagination" aria-label={`${label}: Seiten`}>
    <span className="field-help">{count ? `${offset + 1}–${offset + count}` : '0'} von {total}</span>
    <div className="actions">
      <button className="quiet" aria-label={`${label}: Vorherige Seite`} disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}>Zurück</button>
      <button className="quiet" aria-label={`${label}: Nächste Seite`} disabled={offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)}>Weiter</button>
    </div>
  </nav>
}

export function Documents({ onUpload }: { onUpload: () => void }) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<string | null>(null)
  const [documentsOffset, setDocumentsOffset] = useState(0)
  const [attemptsOffset, setAttemptsOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setOverview(null)
    setUpdatedAt(null)
    const query = new URLSearchParams({ documents_offset: String(documentsOffset), attempts_offset: String(attemptsOffset), limit: String(PAGE_SIZE) })
    api<Overview>(`/ingestion/documents?${query}`, { signal: controller.signal })
      .then(value => {
        if (!controller.signal.aborted) { setOverview(value); setUpdatedAt(new Date().toISOString()) }
      })
      .catch(error => {
        if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Die Dokumentübersicht konnte nicht geladen werden.')
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [documentsOffset, attemptsOffset, refresh])

  return <section aria-labelledby="documents-heading">
    <div className="heading">
      <div><h1 id="documents-heading">Dokumente</h1><p className="subtitle">Bestand der Wissensbasis und Ergebnisse deiner Uploads.</p></div>
      <div className="actions"><button className="secondary" onClick={() => setRefresh(value => value + 1)} disabled={loading}>{loading ? 'Laden …' : 'Aktualisieren'}</button><button className="quiet" onClick={onUpload}>PDFs hochladen</button></div>
    </div>
    {updatedAt && <p className="document-updated field-help">Zuletzt abgerufen: <Timestamp value={updatedAt} /></p>}
    {loading && <p className="notice" role="status">Dokumentübersicht wird geladen …</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {overview && <>
      <section className="document-section" aria-labelledby="stored-heading">
        <h2 id="stored-heading">In der Wissensbasis</h2>
        <p className="field-help">Direkt aus der Wissensdatenbank gelesen. Die Anzahl zeigt vorhandene und erwartete Textabschnitte.</p>
        {!overview.documents_available ? <p className="error" role="alert">Die Wissensdatenbank ist nicht erreichbar. Der aktuelle Dokumentbestand kann nicht geprüft werden.</p> : <>
          {overview.documents.items.length === 0 ? <p className="notice">{overview.documents.total === 0 ? 'Noch keine Dokumente in der Wissensbasis.' : 'Auf dieser Seite sind keine Dokumente vorhanden.'}</p> : <ul className="document-list" aria-label="Gespeicherte Dokumente">
            {overview.documents.items.map(document => <li className="document-row" key={document.document_id}>
              <div className="document-row-heading"><strong>{document.filename}</strong><span className="document-status" data-status={document.status}>{document.status === 'stored' ? 'Vollständig gespeichert' : 'Unvollständig'}</span></div>
              <p className="field-help">{document.chunks} von {document.expected_chunks} Abschnitten · Gespeichert: <Timestamp value={document.created_at} /></p>
            </li>)}
          </ul>}
          {overview.documents.total !== null && <Pagination label="Dokumente" offset={overview.documents.offset} total={overview.documents.total} count={overview.documents.items.length} onChange={setDocumentsOffset} />}
        </>}
      </section>
      <section className="document-section" aria-labelledby="attempts-heading">
        <h2 id="attempts-heading">Uploadverlauf in Sphinx</h2>
        <p className="field-help">Erfasst Uploads seit Einführung dieser Ansicht. Frühere Fehlversuche und Fehler bei direkten n8n-Uploads sind nicht enthalten.</p>
        {overview.attempts.items.length === 0 ? <p className="notice">{overview.attempts.total === 0 ? 'Noch keine Uploadversuche erfasst.' : 'Auf dieser Seite sind keine Uploadversuche vorhanden.'}</p> : <ul className="document-list" aria-label="Uploadversuche">
          {overview.attempts.items.map(attempt => <li className="document-row" key={attempt.id}>
            <div className="document-row-heading"><strong>{attempt.filename}</strong><span className="document-status" data-status={attempt.status}>{attemptStatus[attempt.status]}</span></div>
            <p className="field-help">Gestartet: <Timestamp value={attempt.started_at} />{attempt.finished_at && <> · Letzter Stand: <Timestamp value={attempt.finished_at} /></>}</p>
            {attempt.chunks !== null && ['ingested', 'already_exists'].includes(attempt.status) && <p className="field-help">Beim Abschluss bestätigt: {attempt.chunks} {attempt.chunks === 1 ? 'Abschnitt' : 'Abschnitte'}.</p>}
            {attempt.detail && <p className="document-detail">{attempt.detail}</p>}
            {attempt.status === 'unconfirmed' && <p className="document-detail">{attempt.stored_document_id ? 'Die Datei ist inzwischen in der Wissensbasis vorhanden. Ob dieser Uploadversuch erfolgreich war, ist nicht bestätigt.' : 'Für diesen Uploadversuch liegt keine bestätigte Speicherung vor.'}</p>}
            {attempt.status === 'failed' && attempt.stored_document_id && <p className="document-detail">Die Datei ist inzwischen in der Wissensbasis vorhanden. Dieser Uploadversuch ist fehlgeschlagen.</p>}
            {overview.documents_available && !attempt.stored_document_id && ['ingested', 'already_exists'].includes(attempt.status) && <p className="document-detail">Die Speicherung wurde damals bestätigt; die Datei ist aktuell nicht in der Wissensbasis nachweisbar.</p>}
          </li>)}
        </ul>}
        <Pagination label="Uploadverlauf" offset={overview.attempts.offset} total={overview.attempts.total} count={overview.attempts.items.length} onChange={setAttemptsOffset} />
      </section>
    </>}
  </section>
}
