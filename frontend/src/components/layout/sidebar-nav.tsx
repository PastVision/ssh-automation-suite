import { NavLink } from 'react-router-dom'

import { NAV_ITEMS } from '@/components/layout/nav-items'
import { cn } from '@/lib/utils'

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-1 p-3">
      {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === '/'}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
              'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
              'focus-visible:ring-ring focus-visible:ring-[3px] focus-visible:outline-none',
              isActive
                ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                : 'text-muted-foreground',
            )
          }
        >
          {({ isActive }) => (
            <>
              {/* A shape, not a hue: the active item is marked by a bar and by
                  weight, so it stays distinguishable without color vision. */}
              <span
                aria-hidden="true"
                className={cn(
                  'bg-foreground absolute top-1.5 bottom-1.5 -left-3 w-1 rounded-full',
                  isActive ? 'opacity-100' : 'opacity-0',
                )}
              />
              <Icon
                aria-hidden="true"
                className={cn('size-4 shrink-0', isActive && 'text-foreground')}
              />
              <span className={cn('truncate', isActive && 'font-semibold')}>{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
