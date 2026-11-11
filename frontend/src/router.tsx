import { createBrowserRouter, Navigate } from 'react-router-dom'

import { AppShell } from '@/components/layout/app-shell'
import { RequireAuth } from '@/components/layout/require-auth'
import { AccountPage } from '@/pages/account'
import { CredentialsPage } from '@/pages/credentials'
import { DashboardPage } from '@/pages/dashboard'
import { ExecutionsPage } from '@/pages/executions'
import { LoginPage } from '@/pages/login'
import { SettingsPage } from '@/pages/settings'
import { TemplatesPage } from '@/pages/templates'
import { WorkflowsPage } from '@/pages/workflows'

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'credentials', element: <CredentialsPage /> },
      { path: 'templates', element: <TemplatesPage /> },
      { path: 'workflows', element: <WorkflowsPage /> },
      { path: 'executions', element: <ExecutionsPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'account', element: <AccountPage /> },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
