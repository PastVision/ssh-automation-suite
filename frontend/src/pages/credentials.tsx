import { PlaceholderPage } from '@/pages/placeholder-page'

export function CredentialsPage() {
  return (
    <PlaceholderPage
      title="SSH Credentials"
      description="Usernames and SSH private keys used to reach remote hosts."
      planned={[
        'Add, edit and delete credentials (username + private key)',
        'Private keys encrypted at rest with the local Fernet key file',
        'Key fingerprint display and key-rotation support',
        'Connection test against a single host',
      ]}
    />
  )
}
