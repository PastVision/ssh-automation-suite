import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { AuthProvider } from '@/lib/auth'
import { router } from '@/router'
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 10_000,
      // A 401 is a definitive answer, not a transient failure; retrying it just
      // delays the redirect to the login page.
      retry: (failureCount, error) => {
        const status =
          error && typeof error === 'object' && 'status' in error
            ? (error as { status: unknown }).status
            : null
        if (status === 401 || status === 403 || status === 404) return false
        return failureCount < 2
      },
    },
  },
})
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      {/* AuthProvider sits inside the query provider (it clears the cache on
          identity changes) and outside the router (routes read the session). */}
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  )
}
