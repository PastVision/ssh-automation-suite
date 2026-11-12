/**
 * Classifying a host failure into something an operator can act on.
 *
 * The backend already writes the useful distinction into `HostRun.error_message`
 * (see `sudo.missing_password_warning` and `ExecutionRunner._transition_detail`),
 * but it writes it as prose that names the condition without naming the host or
 * saying which credential holds the setting. This module does not re-detect the
 * condition -- guessing from a transcript would risk blaming sudo for a script
 * that merely printed the word "password" -- it only recognises the conditions
 * the backend already decided on, and adds the context a reader needs.
 *
 * No password, passphrase or key material is ever read, formatted or stored here:
 * every input is a message the backend wrote specifically to avoid containing it.
 */

export type SudoCondition =
  /** Sudo needs a password and none is stored for this credential. */
  | 'missing-password'
  /** A password is stored but sudo rejected it. */
  | 'rejected-password'
  /** The account may not sudo to the target at all. */
  | 'not-permitted'
  /** Password and standard input would need the same channel. */
  | 'stdin-conflict'
  /** A Run-As target name or method the backend refused before connecting. */
  | 'invalid-run-as'

export interface SudoDiagnosis {
  condition: SudoCondition
  /** One line, action-oriented, naming the host when we know it. */
  summary: string
  /** What to change. Deliberately free of any credential detail. */
  action: string
}

/**
 * Message fragments the backend emits, most specific first.
 *
 * Each fragment is copied verbatim from the Python that writes it, so a backend
 * reword makes this fall back to "unclassified" instead of silently
 * misattributing a failure. Checked in order because a Run-As failure message
 * also contains the phrase "sudo password".
 */
const SIGNATURES: Array<{ fragment: string; condition: SudoCondition }> = [
  // execution_runner.py::_prepare -> "Run-As to 'x' is not usable: ..."
  {
    fragment: 'is not usable:',
    condition: 'invalid-run-as',
  },
  // execution_runner.py::_prepare (stdin conflict)
  {
    fragment: 'also reads standard input',
    condition: 'stdin-conflict',
  },
  // sudo.py::MISSING_PASSWORD_WARNING
  {
    fragment: 'Sudo password not configured',
    condition: 'missing-password',
  },
  // execution_runner.py::_transition_detail (password method)
  {
    fragment: 'sudo rejected the password',
    condition: 'rejected-password',
  },
  // execution_runner.py::_transition_detail (sudoers refusal)
  {
    fragment: 'sudoers does not permit this account',
    condition: 'not-permitted',
  },
]

const CONTEXT: Record<SudoCondition, { action: string }> = {
  'missing-password': {
    action:
      'Store a sudo password on the workflow credential, or grant that account passwordless sudo for the commands it needs.',
  },
  'rejected-password': {
    action:
      'Update the sudo password on the workflow credential. The account is otherwise allowed to sudo.',
  },
  'not-permitted': {
    action:
      'Fix the sudoers rule for this account on the host. No password will change this outcome.',
  },
  'stdin-conflict': {
    action:
      'Grant passwordless sudo for this account, or remove the command that reads standard input from the template.',
  },
  'invalid-run-as': {
    action:
      'Correct the Run-As username or method on the step. The host was never contacted for this step.',
  },
}

const SUMMARIES: Record<SudoCondition, string> = {
  'missing-password': 'Elevation needs a sudo password, but none is configured',
  'rejected-password': 'Sudo rejected the stored password',
  'not-permitted': 'This account may not sudo to the Run-As target',
  'stdin-conflict': 'A password and standard input cannot share the same channel',
  'invalid-run-as': 'The Run-As configuration for this step is not usable',
}

/**
 * Recognise a sudo problem in a host's failure message.
 *
 * Returns `null` for anything else, which is the common case: a failed step is
 * usually the script's own exit code and must not be dressed up as an auth error.
 */
export function diagnoseSudoFailure(
  message: string | null | undefined,
  hostname?: string,
): SudoDiagnosis | null {
  if (!message) return null
  const needle = message.toLowerCase()
  for (const { fragment, condition } of SIGNATURES) {
    if (!needle.includes(fragment.toLowerCase())) continue
    // The backend often already prefixes the host ("web-1: Sudo password not
    // configured..."). Only add it when absent, so nothing reads "web-1 on web-1".
    const where =
      hostname && !needle.includes(hostname.toLowerCase()) ? ` on ${hostname}` : ''
    return {
      condition,
      summary: `${SUMMARIES[condition]}${where}.`,
      action: CONTEXT[condition].action,
    }
  }
  return null
}
