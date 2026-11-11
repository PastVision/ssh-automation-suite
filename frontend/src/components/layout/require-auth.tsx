import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '@/lib/auth'

/**
 * Gate the app shell on a server-confirmed session.
 *
 * The redirect target is remembered so a deep link survives the login detour.
 * Rendering a spinner while `/api/auth/status` is in flight avoids a visible
 * flash of the login page on every reload.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <span className="text-muted-foreground text-sm">Checking session…</span>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  return <>{children}</>
}
