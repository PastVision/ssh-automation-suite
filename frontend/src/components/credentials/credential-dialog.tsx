/**
 * Create-credential dialog.
 *
 * The key textarea is the only place in the app that holds private-key material,
 * and it is cleared as soon as the mutation settles, on both success and failure,
 * so it cannot linger on screen or in a rendered list.
 */

import { useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ApiError } from '@/lib/api-client'
import { useCreateCredential } from '@/hooks/use-credentials'

export function CredentialDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [description, setDescription] = useState('')
  const [port, setPort] = useState('22')
  const [privateKey, setPrivateKey] = useState('')
  const [error, setError] = useState<string | null>(null)

  const create = useCreateCredential()

  function reset() {
    setName('')
    setUsername('')
    setDescription('')
    setPort('22')
    setPrivateKey('')
    setError(null)
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      const created = await create.mutateAsync({
        name: name.trim(),
        username: username.trim(),
        description: description.trim() || null,
        default_port: Number(port),
        private_key: privateKey,
      })
      onCreated(created.name)
      reset()
      onOpenChange(false)
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.detail ?? 'Could not save this credential.')
          : 'Could not save this credential.',
      )
    } finally {
      // Never keep key material around after an attempt, successful or not.
      setPrivateKey('')
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset()
        onOpenChange(next)
      }}
    >
      <DialogContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>New SSH credential</DialogTitle>
            <DialogDescription>
              The private key is encrypted at rest and never returned by the API.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Name</span>
              <input
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="prod-web"
                className="border-input bg-background w-full rounded-md border px-3 py-2 text-sm"
              />
            </label>

            <label className="block space-y-1 text-sm">
              <span className="font-medium">Username</span>
              <input
                required
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="deploy"
                className="border-input bg-background w-full rounded-md border px-3 py-2 text-sm"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default port</span>
                <input
                  required
                  type="number"
                  min={1}
                  max={65535}
                  value={port}
                  onChange={(event) => setPort(event.target.value)}
                  className="border-input bg-background w-full rounded-md border px-3 py-2 text-sm"
                />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Description</span>
                <input
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  placeholder="Optional"
                  className="border-input bg-background w-full rounded-md border px-3 py-2 text-sm"
                />
              </label>
            </div>

            <label className="block space-y-1 text-sm">
              <span className="font-medium">Private key</span>
              <textarea
                required
                rows={8}
                value={privateKey}
                onChange={(event) => setPrivateKey(event.target.value)}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                spellCheck={false}
                className="border-input bg-background w-full rounded-md border px-3 py-2 font-mono text-xs"
              />
              <span className="text-muted-foreground block text-xs">
                Unencrypted keys only. Passphrase-protected keys are refused rather
                than stored unusably.
              </span>
            </label>
          </div>

          {error ? <p className="text-sm text-destructive">{error}</p> : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? 'Saving…' : 'Save credential'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
