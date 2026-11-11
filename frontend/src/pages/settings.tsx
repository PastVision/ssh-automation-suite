import { AlertCircle, CheckCircle2, KeyRound, ShieldAlert } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { AccountCard } from '@/components/layout/user-menu'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useClearPassphrase,
  useClearSudoPassword,
  useDeletePrivateKey,
  useProfile,
  useSetPassphrase,
  useSetSudoPassword,
  useUpdateProfile,
  useUploadPrivateKey,
} from '@/hooks/use-profile'
import { ApiError } from '@/lib/api-client'
import type { SshProfile } from '@/lib/types'

function errorMessage(error: unknown): string | null {
  if (error instanceof ApiError) {
    return error.detail ?? `Request failed (${error.status}).`
  }
  if (error instanceof Error) return error.message
  return null
}

function StatusPill({ configured, label }: { configured: boolean; label: string }) {
  return configured ? (
    <Badge variant="outline" className="gap-1">
      <CheckCircle2 aria-hidden="true" className="size-3" />
      {label}
    </Badge>
  ) : (
    <Badge variant="outline" className="text-muted-foreground gap-1">
      Not set
    </Badge>
  )
}

/* -------------------------------------------------------------------------- */
/* Ordinary settings                                                           */
/* -------------------------------------------------------------------------- */

function ConnectionForm({ profile }: { profile: SshProfile }) {
  const update = useUpdateProfile()
  const [username, setUsername] = useState(profile.ssh_username)
  const [port, setPort] = useState(String(profile.default_port))
  // Seeded from the server value. The parent remounts this component whenever the
  // server value changes (`key` at the call site), so there is no effect to re-sync.
  const [message, setMessage] = useState<string | null>(null)

  const dirty =
    username !== profile.ssh_username || port !== String(profile.default_port)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage(null)
    try {
      await update.mutateAsync({
        ssh_username: username,
        default_port: Number(port),
      })
      setMessage('Saved.')
    } catch (error) {
      setMessage(errorMessage(error))
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor="ssh-username" className="text-sm font-medium">
            SSH username
          </label>
          <input
            id="ssh-username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="deploy"
            className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
          />
          <p className="text-muted-foreground text-xs">
            The account used on remote hosts. Shell metacharacters are rejected.
          </p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="ssh-port" className="text-sm font-medium">
            Default port
          </label>
          <input
            id="ssh-port"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(event) => setPort(event.target.value)}
            className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
          />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={!dirty || update.isPending}>
          {update.isPending ? 'Saving…' : 'Save changes'}
        </Button>
        {message ? (
          <span className="text-muted-foreground text-sm" role="status">
            {message}
          </span>
        ) : null}
      </div>
    </form>
  )
}

/* -------------------------------------------------------------------------- */
/* Private key                                                                 */
/* -------------------------------------------------------------------------- */

function PrivateKeySection({ profile }: { profile: SshProfile }) {
  const upload = useUploadPrivateKey()
  const remove = useDeletePrivateKey()
  const [text, setText] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setDone(false)
    try {
      await upload.mutateAsync({
        private_key: text,
        passphrase: passphrase || null,
      })
      // Clear immediately: keeping key material in component state is needless
      // exposure, and the fingerprint below is enough to confirm what was stored.
      setText('')
      setPassphrase('')
      setDone(true)
    } catch (caught) {
      setError(errorMessage(caught))
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Private key</h3>
          <p className="text-muted-foreground text-xs">
            Encrypted before it touches disk. The key itself is never stored in the
            database and never returned by the API.
          </p>
        </div>
        <StatusPill
          configured={profile.secrets.private_key_configured}
          label="Key configured"
        />
      </div>

      {profile.secrets.private_key_configured ? (
        <dl className="grid gap-2 rounded-md border p-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground text-xs">Type</dt>
            <dd className="font-mono text-xs">{profile.secrets.private_key_type ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">Comment</dt>
            <dd className="truncate font-mono text-xs">
              {profile.secrets.private_key_comment ?? '—'}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-muted-foreground text-xs">Fingerprint</dt>
            <dd className="truncate font-mono text-xs">
              {profile.secrets.private_key_fingerprint ?? '—'}
            </dd>
          </div>
        </dl>
      ) : null}

      <form onSubmit={handleUpload} className="space-y-3">
        <div className="space-y-1.5">
          <label htmlFor="private-key" className="text-sm font-medium">
            {profile.secrets.private_key_configured ? 'Replace key' : 'Upload key'}
          </label>
          <textarea
            id="private-key"
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={5}
            spellCheck={false}
            autoComplete="off"
            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
            className="border-input bg-background focus-visible:ring-ring w-full rounded-md border px-3 py-2 font-mono text-xs outline-none focus-visible:ring-2"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="key-passphrase" className="text-sm font-medium">
            Key passphrase <span className="text-muted-foreground">(optional)</span>
          </label>
          <input
            id="key-passphrase"
            type="password"
            autoComplete="new-password"
            value={passphrase}
            onChange={(event) => setPassphrase(event.target.value)}
            className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
          />
        </div>

        {error ? (
          <p role="alert" className="text-destructive flex items-start gap-2 text-sm">
            <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </p>
        ) : null}
        {done ? (
          <p role="status" className="text-muted-foreground text-sm">
            Key stored.
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={!text.trim() || upload.isPending}>
            {upload.isPending ? 'Storing…' : 'Store key'}
          </Button>
          {profile.secrets.private_key_configured ? (
            <Button
              type="button"
              variant="outline"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Remove key
            </Button>
          ) : null}
        </div>
      </form>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Secrets with their own rows                                                 */
/* -------------------------------------------------------------------------- */

function SecretRow({
  id,
  title,
  description,
  configured,
  onSave,
  onClear,
  pending,
}: {
  id: string
  title: string
  description: string
  configured: boolean
  onSave: (value: string) => Promise<void>
  onClear: () => Promise<void>
  pending: boolean
}) {
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setDone(false)
    try {
      await onSave(value)
      setValue('')
      setDone(true)
    } catch (caught) {
      setError(errorMessage(caught))
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">{title}</h3>
          <p className="text-muted-foreground text-xs">{description}</p>
        </div>
        <StatusPill configured={configured} label={configured ? 'Set' : 'Not set'} />
      </div>

      <form onSubmit={handleSave} className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1.5">
          <label htmlFor={id} className="sr-only">
            {title}
          </label>
          <input
            id={id}
            type="password"
            autoComplete="new-password"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="Enter a new value"
            className="border-input bg-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
          />
        </div>
        <Button type="submit" disabled={!value || pending}>
          Save
        </Button>
        {configured ? (
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => void onClear()}
          >
            Clear
          </Button>
        ) : null}
      </form>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
      {done ? (
        <p role="status" className="text-muted-foreground text-sm">
          Saved.
        </p>
      ) : null}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export function SettingsPage() {
  const { data: profile, isPending, error } = useProfile()
  const setPassphrase = useSetPassphrase()
  const clearPassphrase = useClearPassphrase()
  const setSudo = useSetSudoPassword()
  const clearSudo = useClearSudoPassword()
  const [confirmSudoClear, setConfirmSudoClear] = useState(false)

  if (isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (error || !profile) {
    return (
      <p role="alert" className="text-destructive flex items-center gap-2 text-sm">
        <AlertCircle aria-hidden="true" className="size-4" />
        {errorMessage(error) ?? 'Could not load your SSH settings.'}
      </p>
    )
  }

  return (
    <div className="space-y-6">
      <AccountCard />

      <section className="space-y-4 rounded-lg border p-4">
        <div>
          <h2 className="text-sm font-semibold">Connection defaults</h2>
          <p className="text-muted-foreground text-xs">
            Used for every host that does not override these values.
          </p>
        </div>
        <ConnectionForm
          key={`${profile.ssh_username}:${profile.default_port}`}
          profile={profile}
        />
      </section>

      <section className="space-y-4 rounded-lg border p-4">
        <PrivateKeySection profile={profile} />
      </section>

      <section className="space-y-6 rounded-lg border p-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <KeyRound aria-hidden="true" className="size-4" />
            Secrets
          </h2>
          <p className="text-muted-foreground text-xs">
            Encrypted at rest. Only whether a value exists is ever shown back to you.
          </p>
        </div>

        <SecretRow
          id="key-passphrase-value"
          title="Key passphrase"
          description="Unlocks an encrypted private key when connecting."
          configured={profile.secrets.passphrase_configured}
          pending={setPassphrase.isPending || clearPassphrase.isPending}
          onSave={async (value) => {
            await setPassphrase.mutateAsync(value)
          }}
          onClear={async () => {
            await clearPassphrase.mutateAsync()
          }}
        />

        <SecretRow
          id="sudo-password-value"
          title="Sudo password"
          description="The primary sudo password. A separate credential from any Run-As password."
          configured={profile.secrets.sudo_password_configured}
          pending={setSudo.isPending || clearSudo.isPending}
          onSave={async (value) => {
            await setSudo.mutateAsync(value)
          }}
          onClear={async () => {
            setConfirmSudoClear(true)
          }}
        />
      </section>

      <p className="text-muted-foreground flex items-start gap-2 text-xs">
        <ShieldAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
        Private keys and passwords are encrypted with a key held outside the database.
        Exporting the database alone does not expose them.
      </p>

      <Dialog open={confirmSudoClear} onOpenChange={setConfirmSudoClear}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear the sudo password?</DialogTitle>
            <DialogDescription>
              Workflows that rely on sudo will fail until a new password is set.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmSudoClear(false)}>
              Cancel
            </Button>
            <Button
              disabled={clearSudo.isPending}
              onClick={async () => {
                await clearSudo.mutateAsync()
                setConfirmSudoClear(false)
              }}
            >
              {clearSudo.isPending ? 'Clearing…' : 'Clear password'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
