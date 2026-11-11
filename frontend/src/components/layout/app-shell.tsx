import { Terminal } from 'lucide-react'
import { useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'

import { SidebarNav } from '@/components/layout/sidebar-nav'
import { NAV_ITEMS } from '@/components/layout/nav-items'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { UserMenu } from '@/components/layout/user-menu'
import { Separator } from '@/components/ui/separator'
import { useAuth } from '@/lib/auth'
import { displayNameOf } from '@/lib/display'

function Brand() {
  return (
    <Link to="/" className="flex items-center gap-2.5 px-5 py-4">
      <span className="bg-primary text-primary-foreground flex size-8 items-center justify-center rounded-md">
        <Terminal aria-hidden="true" className="size-4" />
      </span>
      <span className="flex flex-col leading-tight">
        <span className="text-sm font-semibold tracking-tight">SSH Automation</span>
        <span className="text-muted-foreground text-xs">Suite</span>
      </span>
    </Link>
  )
}

function MobileNav() {
  const { user } = useAuth()
  const [open, setOpen] = useState(false)
  const location = useLocation()
  const current =
    NAV_ITEMS.find((item) =>
      item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to),
    ) ?? NAV_ITEMS[0]

  return (
    <div className="bg-sidebar/95 supports-[backdrop-filter]:bg-sidebar/80 sticky top-0 z-30 flex items-center gap-3 border-b px-4 py-3 backdrop-blur md:hidden">
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button variant="outline" size="icon" aria-label="Open navigation">
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="size-4"
            >
              <path d="M4 6h16M4 12h16M4 18h16" strokeLinecap="round" />
            </svg>
          </Button>
        </DialogTrigger>
        <DialogContent className="w-72 p-0">
          <DialogHeader className="border-b px-4 py-3">
            <DialogTitle className="text-sm">Navigation</DialogTitle>
          </DialogHeader>
          <SidebarNav onNavigate={() => setOpen(false)} />
        </DialogContent>
      </Dialog>

      <span className="text-sm font-semibold">{current.label}</span>
      <span className="text-muted-foreground ml-auto truncate text-xs">
        {displayNameOf(user)}
      </span>
    </div>
  )
}

export function AppShell() {
  const { user } = useAuth()
  const location = useLocation()
  const current =
    NAV_ITEMS.find((item) =>
      item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to),
    ) ?? NAV_ITEMS[0]

  return (
    <div className="bg-background flex min-h-svh">
      {/* Desktop sidebar */}
      <aside
        className="bg-sidebar border-sidebar-border sticky top-0 hidden h-svh w-64 shrink-0 flex-col border-r md:flex"
        aria-label="Sidebar"
      >
        <Brand />
        <Separator />
        <div className="flex-1 overflow-y-auto">
          <SidebarNav />
        </div>
        <Separator />
        <UserMenu />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <MobileNav />

        <header className="hidden border-b px-8 py-5 md:block">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h1 className="text-xl font-semibold tracking-tight">{current.label}</h1>
              <p className="text-muted-foreground text-sm">{current.description}</p>
            </div>
            <Badge variant="outline">{displayNameOf(user)}</Badge>
          </div>
        </header>

        <main className="flex-1 px-4 py-6 md:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
