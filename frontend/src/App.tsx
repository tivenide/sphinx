import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, ApiError, json, type User } from './api'
import { VoiceSession, type VoiceState } from './audio/voice'
import { ModelControls } from './ModelControls'
import { PdfUpload } from './PdfUpload'
import { Documents } from './Documents'

const errorText = (error: unknown) => error instanceof Error ? error.message : 'Die Aktion konnte nicht ausgeführt werden.'

function MicIcon({ stop = false }: { stop?: boolean }) {
  return stop ? <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" /></svg> :
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v3m-3 0h6" /></svg>
}

function Notice({ children, success = false }: { children: ReactNode; success?: boolean }) {
  return children ? <p className={success ? 'notice' : 'error'} role={success ? 'status' : 'alert'}>{children}</p> : null
}

function Login({ onLogin, reason }: { onLogin: (user: User) => void; reason: string | null }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const values = new FormData(form)
    setBusy(true)
    setError(null)
    try {
      const user = await api<User>('/auth/login', json('POST', { username: values.get('username'), password: values.get('password') }))
      form.reset()
      onLogin(user)
    } catch (error) { setError(errorText(error)) } finally { setBusy(false) }
  }
  return <section className="login" aria-labelledby="login-heading">
    <h1 id="login-heading">Willkommen zurück.</h1>
    <p className="subtitle">Melde dich an, um mit Sphinx zu sprechen.</p>
    <form onSubmit={submit} className="form">
      <label>Benutzername<input name="username" autoComplete="username" maxLength={32} required autoFocus /></label>
      <label>Passwort<input name="password" type="password" autoComplete="current-password" maxLength={128} required /></label>
      <button className="primary" disabled={busy}>{busy ? 'Anmelden …' : 'Anmelden'}</button>
    </form>
    <Notice>{error || reason}</Notice>
  </section>
}

function Voice({ admin, onUpload, onDocuments }: { admin: boolean; onUpload: () => void; onDocuments: () => void }) {
  const [state, setState] = useState<VoiceState>({ phase: 'idle', error: null })
  const [available, setAvailable] = useState<boolean | null>(null)
  const [checking, setChecking] = useState(false)
  const session = useRef<VoiceSession | null>(null)
  const healthRequest = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const check = useCallback(async () => {
    if (healthRequest.current) return
    const controller = new AbortController()
    healthRequest.current = controller
    setChecking(true)
    try {
      const health = await api<{ ready: boolean }>('/health/ready', { signal: controller.signal })
      if (mounted.current && !controller.signal.aborted) setAvailable(health.ready)
    } catch { if (mounted.current && !controller.signal.aborted) setAvailable(false) }
    finally {
      if (healthRequest.current === controller) healthRequest.current = null
      if (mounted.current && !controller.signal.aborted) setChecking(false)
    }
  }, [])
  useEffect(() => {
    mounted.current = true
    void check()
    const timer = window.setInterval(() => void check(), 2500)
    const hide = () => session.current?.cancel()
    window.addEventListener('pagehide', hide)
    return () => {
      mounted.current = false
      window.clearInterval(timer)
      healthRequest.current?.abort()
      healthRequest.current = null
      session.current?.cancel(false)
      window.removeEventListener('pagehide', hide)
    }
  }, [check])
  const activate = () => {
    if (state.phase === 'recording') { void session.current?.finish(); return }
    if (state.phase !== 'idle') { session.current?.cancel(); return }
    session.current?.cancel(false)
    const turn = new VoiceSession(next => { if (mounted.current) setState(next) })
    session.current = turn
    void turn.start()
  }
  const titles = { idle: 'Bereit', starting: 'Mikrofon wird geöffnet', recording: 'Ich höre zu', finishing: 'Aufnahme wird beendet', thinking: 'Antwort wird vorbereitet', speaking: 'Sphinx spricht' }
  const labels = { idle: 'Sprechen', starting: 'Abbrechen', recording: 'Aufnahme beenden', finishing: 'Abbrechen', thinking: 'Abbrechen', speaking: 'Ausgabe stoppen' }
  const help = { idle: 'Klicken, sprechen, erneut klicken.', starting: 'Erlaube den Mikrofonzugriff in deinem Browser.', recording: 'Erneut klicken, wenn du fertig bist. Maximal 30 Sekunden.', finishing: 'Einen Moment bitte.', thinking: 'Du kannst die Anfrage jederzeit abbrechen.', speaking: 'Du kannst die Antwort jederzeit stoppen.' }
  const unavailable = state.phase === 'idle' && available === false
  return <section className="voice" aria-label="Sprachsteuerung">
    <h1 aria-live="polite">{unavailable ? 'Sprachdienst nicht verfügbar' : titles[state.phase]}</h1>
    <button className="primary voice-button" data-recording={state.phase === 'recording'} onClick={activate} disabled={state.phase === 'idle' && available !== true}>
      <MicIcon stop={state.phase !== 'idle'} />{labels[state.phase]}
    </button>
    <p className="help">{unavailable ? 'Bitte versuche es später erneut.' : available === null ? 'Verbindung wird geprüft …' : help[state.phase]}</p>
    {unavailable && <button className="quiet" onClick={() => void check()} disabled={checking}>{checking ? 'Prüfen …' : 'Erneut prüfen'}</button>}
    <Notice>{state.error}</Notice>
    {admin && <ModelControls onReady={check} />}
    {admin && <section className="knowledge-controls" aria-label="Wissensbasis">
      <div><h2>Wissensbasis</h2><p className="field-help">Dokumente hinzufügen und Verarbeitung prüfen.</p></div>
      <div className="actions"><button className="secondary" onClick={onDocuments}>Dokumente</button><button className="quiet" onClick={onUpload}>PDFs hochladen</button></div>
    </section>}
  </section>
}

function ChangePassword({ onChanged }: { onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    if (data.get('new_password') !== data.get('confirm_password')) { setError('Die neuen Passwörter stimmen nicht überein.'); return }
    setBusy(true)
    setError(null)
    try {
      await api('/auth/password', json('PUT', { current_password: data.get('current_password'), new_password: data.get('new_password') }))
      form.reset()
      onChanged()
    } catch (error) { setError(errorText(error)) } finally { setBusy(false) }
  }
  return <section className="password-page" aria-labelledby="password-heading">
    <h1 id="password-heading">Passwort ändern</h1>
    <p className="subtitle">Danach meldest du dich mit deinem neuen Passwort an.</p>
    <form className="form" onSubmit={submit}>
      <label>Aktuelles Passwort<input type="password" name="current_password" autoComplete="current-password" maxLength={128} required /></label>
      <label>Neues Passwort<input type="password" name="new_password" autoComplete="new-password" minLength={12} maxLength={128} required aria-describedby="password-help" /></label>
      <label>Neues Passwort wiederholen<input type="password" name="confirm_password" autoComplete="new-password" minLength={12} maxLength={128} required /></label>
      <p className="field-help" id="password-help">Mindestens 12 Zeichen.</p>
      <button className="primary" disabled={busy}>{busy ? 'Speichern …' : 'Passwort ändern'}</button>
    </form>
    <Notice>{error}</Notice>
  </section>
}

function Users({ current }: { current: User }) {
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [resetUser, setResetUser] = useState<User | null>(null)
  useEffect(() => {
    let active = true
    api<User[]>('/users').then(value => { if (active) setUsers(value) }).catch(error => { if (active) setError(errorText(error)) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  const change = async (user: User, values: Partial<Pick<User, 'role' | 'active'>>) => {
    setBusy(true); setError(null); setNotice(null)
    try {
      const updated = await api<User>(`/users/${user.id}`, json('PATCH', values))
      setUsers(items => items.map(item => item.id === user.id ? updated : item))
      setNotice('Zugang aktualisiert.')
    } catch (error) { setError(errorText(error)) } finally { setBusy(false) }
  }
  const add = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    setBusy(true); setError(null); setNotice(null)
    try {
      const user = await api<User>('/users', json('POST', { username: data.get('username'), password: data.get('password'), role: data.get('role') }))
      setUsers(items => [...items, user])
      form.reset(); setAdding(false); setNotice(`Zugang für ${user.username} angelegt.`)
    } catch (error) { setError(errorText(error)) } finally { setBusy(false) }
  }
  const reset = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!resetUser) return
    const form = event.currentTarget
    const data = new FormData(form)
    setBusy(true); setError(null); setNotice(null)
    try {
      await api(`/users/${resetUser.id}/password`, json('PUT', { password: data.get('password') }))
      form.reset(); setResetUser(null); setNotice('Passwort geändert. Bestehende Sitzungen wurden beendet.')
    } catch (error) { setError(errorText(error)) } finally { setBusy(false) }
  }
  return <section aria-labelledby="users-heading">
    <div className="heading"><div><h1 id="users-heading">Nutzer</h1><p className="subtitle">Zugänge für dein Team.</p></div><button className="secondary" onClick={() => { setAdding(!adding); setResetUser(null); setError(null) }} disabled={busy}>{adding ? 'Schließen' : 'Nutzer anlegen'}</button></div>
    <Notice>{error}</Notice><Notice success>{notice}</Notice>
    {adding && <form className="form user-form" onSubmit={add}>
      <label>Benutzername<input name="username" autoComplete="off" minLength={3} maxLength={32} pattern={'[a-zA-Z0-9][a-zA-Z0-9._\\-]{2,31}'} required autoFocus aria-describedby="username-help" /></label>
      <p className="field-help" id="username-help">3–32 Zeichen: Buchstaben, Zahlen, Punkt, _ oder -.</p>
      <label>Passwort<input name="password" type="password" autoComplete="new-password" minLength={12} maxLength={128} required /></label>
      <label>Rolle<select name="role" defaultValue="user"><option value="user">Nutzer</option><option value="admin">Admin</option></select></label>
      <p className="field-help">Passwort mit mindestens 12 Zeichen.</p>
      <button className="primary" disabled={busy}>{busy ? 'Anlegen …' : 'Zugang anlegen'}</button>
    </form>}
    {resetUser && <form className="form user-form" onSubmit={reset}>
      <h2>Passwort für {resetUser.username}</h2>
      <label>Neues Passwort<input type="password" name="password" autoComplete="new-password" minLength={12} maxLength={128} required autoFocus /></label>
      <div className="actions"><button className="primary" disabled={busy}>Passwort setzen</button><button type="button" className="quiet" onClick={() => setResetUser(null)} disabled={busy}>Abbrechen</button></div>
    </form>}
    {loading ? <p className="help" role="status">Nutzer werden geladen …</p> : <ul className="user-list">
      {users.map(user => <li className="user-row" key={user.id}>
        <div className="user-info"><strong>{user.username}</strong><span>{user.active ? 'Aktiv' : 'Deaktiviert'}{user.id === current.id ? ' · Dein Konto' : ''}</span></div>
        <div className="user-actions">
          <label className="role-label"><span className="sr-only">Rolle für {user.username}</span><select value={user.role} onChange={event => void change(user, { role: event.target.value as User['role'] })} disabled={busy || user.id === current.id}><option value="user">Nutzer</option><option value="admin">Admin</option></select></label>
          {user.id !== current.id && <><button className="quiet" onClick={() => void change(user, { active: !user.active })} disabled={busy}>{user.active ? 'Deaktivieren' : 'Aktivieren'}</button><button className="quiet" onClick={() => { setResetUser(user); setAdding(false); setError(null) }} disabled={busy}>Passwort setzen</button></>}
        </div>
      </li>)}
    </ul>}
  </section>
}

export function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [page, setPage] = useState<'voice' | 'users' | 'password' | 'upload' | 'documents'>('voice')
  const [reason, setReason] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loggingOut, setLoggingOut] = useState(false)
  const menu = useRef<HTMLDetailsElement>(null)
  const navigate = (next: typeof page) => {
    if (menu.current) menu.current.open = false
    setError(null); setPage(next)
  }
  useEffect(() => {
    let active = true
    api<User>('/auth/me').then(value => { if (active) setUser(value) }).catch(error => {
      if (!active) return
      setUser(null)
      if (!(error instanceof ApiError && error.status === 401)) setReason(errorText(error))
    })
    const expired = () => { setUser(null); setPage('voice'); setReason('Deine Sitzung ist abgelaufen. Bitte melde dich erneut an.') }
    window.addEventListener('session-expired', expired)
    return () => { active = false; window.removeEventListener('session-expired', expired) }
  }, [])
  const logout = async () => {
    // Unmounting Voice immediately releases microphone, websocket and playback.
    navigate('password'); setLoggingOut(true)
    try { await api('/auth/logout', json('POST')); setUser(null); setReason(null); setPage('voice') }
    catch (error) { setError(errorText(error)); setPage('voice') }
    finally { setLoggingOut(false) }
  }
  const changeComplete = () => { setUser(null); setPage('voice'); setReason('Passwort geändert. Bitte melde dich erneut an.') }
  return <div className="app-shell">
    <header className="header"><span className="brand">Sphinx</span>
      {user && <details className="account" ref={menu}>
        <summary aria-label={`Kontomenü für ${user.username}`}><span className="account-name">{user.username}</span><span aria-hidden="true">⌄</span></summary>
        <div className="account-menu">
          {page !== 'voice' && <button onClick={() => navigate('voice')}>Zur Sprachsteuerung</button>}
          {user.role === 'admin' && <button onClick={() => navigate('users')}>Nutzer verwalten</button>}
          {user.role === 'admin' && <button onClick={() => navigate('documents')}>Dokumente</button>}
          {user.role === 'admin' && <button onClick={() => navigate('upload')}>PDFs hochladen</button>}
          <button onClick={() => navigate('password')}>Passwort ändern</button>
          <button onClick={() => void logout()} disabled={loggingOut}>Abmelden</button>
        </div>
      </details>}
    </header>
    <main>
      {user === undefined ? <p className="loading" role="status">Sphinx wird geöffnet …</p> : user === null ? <Login reason={reason} onLogin={value => { setUser(value); setPage('voice'); setReason(null) }} /> : <>
        {page !== 'voice' && !loggingOut && <button className="quiet back" onClick={() => navigate('voice')}>← Zur Sprachsteuerung</button>}
        <Notice>{error}</Notice>
        {loggingOut ? <p role="status">Abmelden …</p> : page === 'voice' ? <Voice admin={user.role === 'admin'} onUpload={() => navigate('upload')} onDocuments={() => navigate('documents')} /> : page === 'documents' && user.role === 'admin' ? <Documents onUpload={() => navigate('upload')} /> : page === 'upload' && user.role === 'admin' ? <PdfUpload onDocuments={() => navigate('documents')} /> : page === 'users' && user.role === 'admin' ? <Users current={user} /> : <ChangePassword onChanged={changeComplete} />}
      </>}
    </main>
  </div>
}
