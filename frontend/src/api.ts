export interface User {
  id: number
  username: string
  role: 'admin' | 'user'
  active: boolean
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export async function api<T>(path: string, options: RequestInit = {}, acceptedStatuses: readonly number[] = []): Promise<T> {
  let response: Response
  try {
    response = await fetch('/api' + path, {
      ...options,
      credentials: 'same-origin',
      headers: { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    })
  } catch {
    throw new ApiError('Die Verbindung zum Server ist unterbrochen. Bitte erneut versuchen.', 0)
  }
  if (!response.ok && !acceptedStatuses.includes(response.status)) {
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('session-expired'))
    const body = await response.json().catch(() => null)
    throw new ApiError(typeof body?.detail === 'string' ? body.detail : 'Die Anfrage konnte nicht verarbeitet werden.', response.status)
  }
  return response.status === 204 ? undefined as T : response.json()
}

export const json = (method: string, body?: unknown): RequestInit => ({
  method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
})
