import { PlaceholderPage } from '@/pages/placeholder-page'

export function WorkflowsPage() {
  return (
    <PlaceholderPage
      title="Workflows"
      description="Ordered template sequences executed across a list of hostnames."
      planned={[
        'Compose workflows from ordered template steps',
        'Manage the target hostname list per workflow',
        'Run manually with fail-fast per host',
        'Cron scheduling through APScheduler',
      ]}
    />
  )
}
