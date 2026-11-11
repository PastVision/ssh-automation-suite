import { AlertCircle, LogIn, Terminal } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/lib/auth'

/** Only sign in local accounts. Proxy mode authenticates before the SPA loads. */
export function LoginPage() {
  const { isAuthenticated, isLoading, canPasswordLogin, login } = useAuth()
  const location = useLocation()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (isLoading) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <span className="text-muted-foreground text-sm">Loading…</span>
      </div>
    )
  }

  if (isAuthenticated) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
    } catch (caught) {
      // The backend deliberately does not distinguish an unknown account from a
      // wrong password, and neither should this message.
      setError(
        caught instanceof Error && 'detail' in caught
          ? ((caught as { detail: string | null }).detail ?? 'Sign-in failed.')
          : 'Sign-in failed.',
      )
      setPassword('')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-background flex min-h-svh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <span className="bg-primary text-primary-foreground flex size-11 items-center justify-center rounded-lg">
            <Terminal aria-hidden="true" className="size-5" />
          </span>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">SSH Automation Suite</h1>
            <p className="text-muted-foreground text-sm">
              Sign in to run and inspect workflows.
            </p>
          </div>
        </div>

        {canPasswordLogin ? (
          <form onSubmit={handleSubmit} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <label htmlFor="username" className="text-sm font-medium">
                Username
              </label>
              <input
                id="username"
                name="username"
                type="text"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
              />
            </div>

            {error ? (
              <p
                role="alert"
                className="text-destructive flex items-start gap-2 text-sm"
              >
                <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                <span>{error}</span>
              </p>
            ) : null}

            <Button
              type="submit"
              className="w-full"
              disabled={submitting || !username || !password}
            >
              <LogIn aria-hidden="true" className="size-4" />
              {submitting ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>
        ) : (
          <div className="text-muted-foreground rounded-lg border p-4 text-sm">
            <p className="text-foreground font-medium">Authentication is handled upstream.</p>
            <p className="mt-1">
              This deployment uses a trusted proxy. Sign in there, then reload this page.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
