import { PlaceholderPage } from '@/pages/placeholder-page'

export function ExecutionsPage() {
  return (
    <PlaceholderPage
      title="Executions"
      description="Persistent run history with per-host and per-step results."
      planned={[
        'Execution timeline with per-host status',
        'Streamed log viewer backed by filesystem transcripts',
        'Snapshot of the exact template version each run executed',
        'Live updates over WebSockets',
      ]}
    />
  )
}
