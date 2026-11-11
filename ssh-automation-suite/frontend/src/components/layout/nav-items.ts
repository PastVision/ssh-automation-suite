import {
  FileCode2,
  GitBranch,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  Settings,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  description: string
  icon: LucideIcon
}

/** Single source of truth for the sidebar and the mobile navigation menu. */
export const NAV_ITEMS: NavItem[] = [
  {
    to: '/',
    label: 'Dashboard',
    description: 'System status and activity overview',
    icon: LayoutDashboard,
  },
  {
    to: '/credentials',
    label: 'SSH Credentials',
    description: 'Usernames and encrypted private keys',
    icon: KeyRound,
  },
  {
    to: '/templates',
    label: 'Templates',
    description: 'Reusable shell-script templates',
    icon: FileCode2,
  },
  {
    to: '/workflows',
    label: 'Workflows',
    description: 'Ordered template sequences per host',
    icon: GitBranch,
  },
  {
    to: '/executions',
    label: 'Executions',
    description: 'Run history and logs',
    icon: ListChecks,
  },
  {
    to: '/settings',
    label: 'Settings',
    description: 'Your SSH defaults, key and secrets',
    icon: Settings,
  },
  {
    to: '/account',
    label: 'Account',
    description: 'Sessions and security activity',
    icon: ShieldCheck,
  },
]
