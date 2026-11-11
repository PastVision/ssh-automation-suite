/**
 * Session state for the whole app.
 *
 * The server is the only authority on whether a session is valid, so the client
 * never decides that on its own: it asks `/api/auth/status` and trusts the answer.
 * Modelling that as a query (rather than mirrored `useState`) means the status is
 * cached, deduplicated and refreshed without hand-written effects.
 *
 * A 401 from any request feeds `setUnauthorizedHandler`, which invalidates this
 * query. The router then sees an anonymous session and redirects -- so a session
 * revoked in another tab or expired server-side cannot leave a broken screen.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from 'react'

import {
  ApiError,
  apiFetch,
  jsonInit,
  setCsrfToken,
  setUnauthorizedHandler,
} from '@/lib/api-client'
import type { AuthStatus, LoginResponse, SessionInfo, User } from '@/lib/types'

const AUTH_KEY = ['auth', 'status'] as const

const ANONYMOUS: AuthStatus = {
  authenticated: false,
  auth_mode: 'local',
  user: null,
  csrf_token: null,
  session: null,
}

interface AuthContextValue {
  user: User | null
  session: SessionInfo | null
  authMode: AuthStatus['auth_mode'] | null
  isAuthenticated: boolean
  /** True until the first status answer arrives, so the shell can hold off. */
  isLoading: boolean
  canPasswordLogin: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const status = useQuery({
    queryKey: AUTH_KEY,
    queryFn: async (): Promise<AuthStatus> => {
      try {
        return await apiFetch<AuthStatus>('/api/auth/status')
      } catch {
        // An unreachable backend is not an authenticated session. Treating it as
        // anonymous is the safe direction: the login screen appears instead of the
        // app shell pretending to work.
        return ANONYMOUS
      }
    },
    staleTime: 60_000,
    retry: false,
  })

  const current = status.data ?? null
  const csrf = current?.authenticated ? current.csrf_token : null

  // Push the token into the request layer. It lives outside React (a module-level
  // variable), so this is a plain assignment rather than a state update.
  useEffect(() => {
    setCsrfToken(csrf ?? null)
  }, [csrf])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      // The session is gone server-side; drop the cached answer and let the guard
      // redirect. `queryClient.clear()` also removes any data fetched as the
      // previous identity.
      queryClient.setQueryData<AuthStatus>(AUTH_KEY, ANONYMOUS)
      setCsrfToken(null)
    })
    return () => setUnauthorizedHandler(null)
  }, [queryClient])

  const login = useCallback(
    async (username: string, password: string) => {
      const result = await apiFetch<LoginResponse>(
        '/api/auth/login',
        jsonInit({ username, password }),
      )
      queryClient.clear()
      queryClient.setQueryData<AuthStatus>(AUTH_KEY, {
        authenticated: true,
        auth_mode: result.auth_mode,
        user: result.user,
        csrf_token: result.csrf_token,
        session: result.session,
      })
      setCsrfToken(result.csrf_token)
    },
    [queryClient],
  )

  const logout = useCallback(async () => {
    try {
      await apiFetch<null>('/api/auth/logout', { method: 'POST' })
    } catch (error) {
      // A 401 means the session was already gone, which is the desired end state.
      if (!(error instanceof ApiError && error.status === 401)) {
        throw error
      }
    } finally {
      queryClient.clear()
      queryClient.setQueryData<AuthStatus>(AUTH_KEY, ANONYMOUS)
      setCsrfToken(null)
    }
  }, [queryClient])

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: AUTH_KEY })
  }, [queryClient])

  const value = useMemo<AuthContextValue>(
    () => ({
      user: current?.user ?? null,
      session: current?.session ?? null,
      authMode: current?.auth_mode ?? null,
      isAuthenticated: current?.authenticated ?? false,
      isLoading: status.isPending,
      canPasswordLogin: current?.auth_mode === 'local',
      login,
      logout,
      refresh,
    }),
    [current, status.isPending, login, logout, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return context
}
