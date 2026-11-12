/**
 * SSH credentials page.
 *
 * Create / list / delete only -- the minimum needed to run a workflow. Editing a
 * credential is intentionally absent: a key rotation is a delete plus a create,
 * which keeps the "key is write-only" rule intact.
 *
 * Nothing on this page can display key material, because the API never returns it.
 */

import { Fingerprint, KeyRound, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { CredentialDialog } from '@/components/credentials/credential-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useCredentials, useDeleteCredential } from '@/hooks/use-credentials'
import { ApiError } from '@/lib/api-client'
import { formatApiInstant } from '@/lib/display'
import type { Credential, CredentialDeleteBlocked } from '@/lib/types'

function formatDate(value: string): string {
  // Offset-less UTC from the API; `new Date(value)` would shift it by the viewer.
  return formatApiInstant(value)
}

/**
 * A credential still referenced by workflows answers 409 with a structured
 * detail whose message already names the blocking references.
 */
function describeDeleteFailure(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not delete this credential.'
  if (error.status !== 409) return error.detail ?? 'Could not delete this credential.'

  const blocked = (error.body as { detail?: CredentialDeleteBlocked } | null)?.detail
  return blocked?.message ?? error.detail ?? 'This credential is still in use.'
}

function CredentialRow({
  credential,
  onDelete,
  deleting,
}: {
  credential: Credential
  onDelete: (credential: Credential) => void
  deleting: boolean
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border p-4">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{credential.name}</span>
          <Badge variant="outline">port {credential.default_port}</Badge>
          {credential.has_private_key ? (
            <Badge variant="success">key stored</Badge>
          ) : (
            <Badge variant="destructive">no key</Badge>
          )}
        </div>
        <p className="text-muted-foreground text-sm">
          {credential.username}
          {credential.description ? ` — ${credential.description}` : ''}
        </p>
        <p className="text-muted-foreground flex items-center gap-1 font-mono text-xs break-all">
          <Fingerprint className="size-3 shrink-0" />
          {credential.private_key_fingerprint ?? 'no fingerprint'}
        </p>
        <p className="text-muted-foreground text-xs">
          {credential.private_key_type ?? 'unknown type'} · created{' '}
          {formatDate(credential.created_at)}
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={() => onDelete(credential)}
        disabled={deleting}
      >
        <Trash2 className="size-3.5" />
        Delete
      </Button>
    </div>
  )
}

export function CredentialsPage() {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const list = useCredentials()
  const remove = useDeleteCredential()

  async function handleDelete(credential: Credential) {
    const confirmed = window.confirm(
      `Delete the credential “${credential.name}”? The stored private key is removed with it.`,
    )
    if (!confirmed) return
    setNotice(null)
    try {
      await remove.mutateAsync(credential.id)
      setNotice(`Deleted “${credential.name}”.`)
    } catch (error) {
      setNotice(describeDeleteFailure(error))
    }
  }

  const items = list.data?.items ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">SSH Credentials</h1>
          <p className="text-muted-foreground text-sm">
            Usernames and private keys used to reach remote hosts. Keys are
            encrypted at rest and never leave the server once stored.
          </p>
        </div>
        <Button onClick={() => setDialogOpen(true)}>
          <Plus className="size-4" />
          New credential
        </Button>
      </div>

      {notice ? (
        <div className="bg-muted/40 rounded-md border px-4 py-2 text-sm">{notice}</div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="size-4" />
            Your credentials
          </CardTitle>
          <CardDescription>
            {list.isPending
              ? 'Loading…'
              : `${items.length} credential${items.length === 1 ? '' : 's'}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {list.isPending ? (
            <>
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </>
          ) : list.isError ? (
            <p className="text-destructive text-sm">Could not load credentials.</p>
          ) : items.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              No credentials yet. A workflow needs one before it can run.
            </p>
          ) : (
            items.map((credential) => (
              <CredentialRow
                key={credential.id}
                credential={credential}
                onDelete={handleDelete}
                deleting={remove.isPending}
              />
            ))
          )}
        </CardContent>
      </Card>

      <CredentialDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={(name) => setNotice(`Created “${name}”.`)}
      />
    </div>
  )
}
