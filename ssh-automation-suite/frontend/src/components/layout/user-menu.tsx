import { LogOut, ShieldCheck, UserRound } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useAuth } from '@/lib/auth'
import { displayNameOf, initialsOf } from '@/lib/display'

/**
 * Signed-in identity, with a sign-out confirmation.
 *
 * Signing out is confirmed rather than immediate: it is destructive to the
 * current view (unsaved edits are lost, locks are released) and it is adjacent to
 * a button users click casually.
 */
export function UserMenu() {
  const { user, authMode, logout } = useAuth()
  const [confirming, setConfirming] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSignOut() {
    setPending(true)
    setError(null)
    try {
      await logout()
    } catch {
      setError('Could not sign out. Check your connection and try again.')
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <div className="flex items-center gap-2.5 px-5 py-3">
        <span
          aria-hidden="true"
          className="bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold"
        >
          {initialsOf(user)}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{displayNameOf(user)}</p>
          <p className="text-muted-foreground truncate text-xs">
            {user ? `@${user.username}` : 'Not signed in'}
            {authMode === 'proxy' ? ' · proxy' : ''}
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Sign out"
          title="Sign out"
          onClick={() => setConfirming(true)}
        >
          <LogOut aria-hidden="true" className="size-4" />
        </Button>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Sign out?</DialogTitle>
            <DialogDescription>
              Any unsaved edits will be lost and edit locks you hold will be
              released so others can continue.
            </DialogDescription>
          </DialogHeader>
          {error ? <p className="text-destructive text-sm">{error}</p> : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(false)} disabled={pending}>
              Cancel
            </Button>
            <Button onClick={handleSignOut} disabled={pending}>
              {pending ? 'Signing out…' : 'Sign out'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Compact account panel used at the top of the settings page. */
export function AccountCard() {
  const { user, authMode } = useAuth()
  if (!user) return null

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border p-4">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="bg-muted text-muted-foreground flex size-10 items-center justify-center rounded-full text-sm font-semibold"
        >
          {initialsOf(user)}
        </span>
        <div>
          <p className="font-medium">{displayNameOf(user)}</p>
          <p className="text-muted-foreground text-sm">@{user.username}</p>
        </div>
      </div>
      <div className="text-muted-foreground flex items-center gap-4 text-xs">
        <span className="inline-flex items-center gap-1.5">
          <ShieldCheck aria-hidden="true" className="size-3.5" />
          {authMode === 'proxy' ? 'Trusted proxy' : 'Password account'}
        </span>
        <Link
          to="/settings"
          className="hover:text-foreground inline-flex items-center gap-1.5 underline-offset-4 hover:underline"
        >
          <UserRound aria-hidden="true" className="size-3.5" />
          SSH settings
        </Link>
      </div>
    </div>
  )
}
