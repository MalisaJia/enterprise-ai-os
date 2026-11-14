/**
 * Token discovery — same-origin with the Octo main site, so we read the
 * login credential the main site stored in localStorage.
 *
 * Heuristic replicated from octo-web's InviteLanding.findToken
 * (apps/web/src/Components/InviteLanding/index.tsx):
 * scan localStorage for keys starting with "token", skip "tokenCallback",
 * keep values longer than 10 characters.
 */

export function findTokenCandidates(): string[] {
  const candidates: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && key.startsWith('token') && key !== 'tokenCallback') {
      const val = localStorage.getItem(key)
      if (val && val.length > 10) candidates.push(val)
    }
  }
  return candidates
}

/** Mask a token for display, e.g. "ab3f…9c21". */
export function maskToken(token: string): string {
  if (token.length <= 8) return '****'
  return `${token.slice(0, 4)}…${token.slice(-4)}`
}
