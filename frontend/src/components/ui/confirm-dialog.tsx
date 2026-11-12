/**
 * Confirmation dialog for an action the operator cannot take back.
 *
 * Deliberately not `window.confirm`: that blocks the event loop, cannot show
 * progress while the request is in flight, and cannot keep the dialog open with
 * an error when the request fails. Here the confirm button stays disabled and
 * shows the pending state until the caller closes the dialog, so a slow request
 * cannot be triggered twice.
 */

import type * as React from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = 'Cancel',
  pending = false,
  destructive = false,
  error = null,
  onConfirm,
  onOpenChange,
}: {
  open: boolean
  title: string
  description: React.ReactNode
  confirmLabel: string
  cancelLabel?: string
  pending?: boolean
  destructive?: boolean
  error?: string | null
  onConfirm: () => void
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={destructive ? 'destructive' : 'default'}
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
