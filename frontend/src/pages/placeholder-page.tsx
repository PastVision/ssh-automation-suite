import { Construction } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

/**
 * Placeholder for a navigation destination whose feature belongs to a later
 * milestone. Deliberately states what is missing rather than faking a UI.
 */
export function PlaceholderPage({
  title,
  description,
  planned,
}: {
  title: string
  description: string
  planned: string[]
}) {
  return (
    <Card className="mx-auto max-w-2xl">
      <CardHeader>
        <div className="flex items-center gap-3">
          <span className="bg-muted flex size-9 items-center justify-center rounded-md">
            <Construction aria-hidden="true" className="text-muted-foreground size-4" />
          </span>
          <div className="space-y-1">
            <CardTitle className="text-base">{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-2 pb-4">
          <Badge variant="secondary">Not in this milestone</Badge>
          <Badge variant="outline">Foundation only</Badge>
        </div>
        <p className="text-muted-foreground mb-3 text-sm">
          The data model and storage layout for this area already exist. The
          following will be built on top of them:
        </p>
        <ul className="space-y-1.5 text-sm">
          {planned.map((item) => (
            <li key={item} className="text-muted-foreground flex gap-2">
              <span aria-hidden="true" className="text-muted-foreground/60">
                —
              </span>
              {item}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
