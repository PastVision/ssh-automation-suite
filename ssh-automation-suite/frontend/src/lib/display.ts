/** Display helpers shared by the shell, the account page and the settings page. */

import type { User } from '@/lib/types'

/** Best-effort display name: accounts may have no display name set. */
export function displayNameOf(user: User | null | undefined): string {
  if (!user) return 'Unknown user'
  return user.display_name?.trim() || user.username
}

/** Two-letter monogram for the avatar; never more than two characters. */
export function initialsOf(user: User | null | undefined): string {
  if (!user) return '?'
  const parts = displayNameOf(user)
    .split(/[\s._-]+/)
    .filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}
