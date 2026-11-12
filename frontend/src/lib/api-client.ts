/**
 * Thin typed fetch wrapper.
 *
 * Requests are relative (`/api/...`) so the Vite dev proxy handles them locally
 * and a reverse proxy handles them in production -- the frontend never needs to
 * know the backend origin. `VITE_API_BASE_URL` exists only for split-origin
 * deployments.
 *
 * Two things are centralised here so no caller can forget them:
 *
 * - `credentials: 'include'` -- the session lives in an HttpOnly cookie.
 * - the `X-CSRF-Token` header on state-changing requests -- the token is supplied
 *   by the auth provider once a session exists, and is never read from a cookie.
 */

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

/** Set by the auth provider whenever the session's CSRF token changes. */
let csrfToken: string | null = null

/**
 * Called on any 401. The auth provider uses it to drop local state so a session
 * revoked elsewhere (another tab, an admin, an absolute timeout) surfaces as a
 * redirect to the login page instead of a screen full of failed requests.
 */
let unauthorizedHandler: (() => void) | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }

  /** A human-readable message from the backend's `detail`, when it is a string. */
  get detail(): string | null {
    const body = this.body
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      if (detail && typeof detail === 'object' && 'message' in detail) {
        const message = (detail as { message: unknown }).message
        if (typeof message === 'string') return message
      }
    }
    return null
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { timeoutMs = 15_000, headers, ...rest } = init
  const method = (rest.method ?? 'GET').toUpperCase()

  const requestHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(headers as Record<string, string> | undefined),
  }

  // Only send the token where the backend looks for it. Sending it on GETs would
  // widen the surface for no benefit.
  if (!SAFE_METHODS.has(method) && csrfToken) {
    requestHeaders['X-CSRF-Token'] = csrfToken
  }
  if (rest.body !== undefined && !('Content-Type' in requestHeaders)) {
    requestHeaders['Content-Type'] = 'application/json'
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...rest,
      headers: requestHeaders,
      credentials: 'include',
      signal: controller.signal,
    })

    const text = await response.text()
    const body: unknown = text ? safeJsonParse(text) : null

    if (!response.ok) {
      if (response.status === 401) {
        unauthorizedHandler?.()
      }
      throw new ApiError(
        `Request to ${path} failed with ${response.status}`,
        response.status,
        body,
      )
    }

    return body as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(`Request to ${path} timed out`, 0, null)
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

/** Convenience for the common JSON-payload case. */
export function jsonInit(value: unknown, method = 'POST'): RequestInit {
  return { method, body: JSON.stringify(value) }
}

/**
 * Absolute WebSocket URL for a backend path.
 *
 * `WebSocket` cannot resolve a relative URL, so the origin has to be built here.
 * It follows the same rule as `apiFetch`: relative to the current origin unless
 * `VITE_API_BASE_URL` points somewhere else, in which case the scheme is swapped
 * to `ws`/`wss`. Cookies travel with the upgrade automatically.
 */
export function wsUrl(path: string): string {
  if (API_BASE_URL) {
    return `${API_BASE_URL.replace(/^http/, 'ws')}${path}`
  }
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}${path}`
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}
