/** Web-only bridge: no suggestion UI, no synthetic keys, no second Hermes. */
import { createHash, randomUUID } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

type Options = {
  sessionId: string
  canSubmit: () => boolean
  submit: (text: string) => void | Promise<void>
  reportError: (text: string) => void
  serviceUrl?: string
  instanceId?: string
  intervalMs?: number
}

/** Keep the authorized target immutable across asynchronous gateway calls. */
export async function submitApprovedInput(sessionId: string, text: string, deps: {
  currentSessionId: () => string | null
  isBusy: () => boolean
  showSubmission: (text: string) => void
  promptSubmit: (params: { session_id: string; text: string }) => Promise<unknown>
}): Promise<void> {
  if (deps.currentSessionId() !== sessionId || deps.isBusy()) {
    throw new Error('Original Session is not ready')
  }
  deps.showSubmission(text)
  // Use the same native prompt.submit RPC as normal input, with no slash,
  // shell interpolation or file-drop preprocessing of the approved action.
  await deps.promptSubmit({ session_id: sessionId, text })
}

export function startWebAgentBridge(options: Options): () => void {
  const url = options.serviceUrl ?? process.env.SN_PROACTIVE_AGENT_SERVICE_URL ?? process.env.PROACTIVE_MEMORY_SERVICE_URL ?? 'http://127.0.0.1:8080'
  const base = url.replace(/\/$/, '')
  const home = resolve(process.env.HERMES_HOME || resolve(homedir(), '.hermes'))
  let realHome = home
  try { realHome = realpathSync(home) } catch { /* diagnostic only */ }
  const identity = {
    platform: 'hermes-tui', session_id: options.sessionId, client_id: randomUUID(),
    instance_id: options.instanceId ?? createHash('sha256').update(realHome).digest('hex')
  }
  const controller = new AbortController()
  let stopped = false
  let cursor = 0
  let probe = ''
  const seen = new Set<string>()
  const request = async (path: string, body?: unknown) => {
    const response = await fetch(base + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)])
    })
    if (!response.ok) throw new Error(`Bridge HTTP ${response.status}`)
    return response.json()
  }
  const fail = async (id: string, reason: string) => {
    options.reportError(reason)
    await request('/v1/events/session.resume.failed', {
      suggestion_id: id, reason, failed_at: new Date().toISOString()
    }).catch(() => {})
  }
  const run = async () => {
    while (!stopped) {
      try {
        const heartbeat = await request('/v1/bridge/heartbeat', {
          ...identity, probe, ready: options.canSubmit()
        })
        probe = heartbeat.probe
        if (!heartbeat.roundtrip) continue
        const query = new URLSearchParams({ platform: identity.platform, session_id: identity.session_id,
          after: String(cursor), timeout: '0' })
        const result = await request(`/v1/bridge/events?${query}`)
        if (!Array.isArray(result.events)) throw new Error('Invalid bridge response')
        for (const event of result.events) {
          if (stopped) return
          if (event.event_type !== 'session.resume.requested') {
            cursor = event.sequence
            continue
          }
          const data = event.payload
          if (data.platform !== identity.platform || data.target_session_id !== identity.session_id) {
            cursor = event.sequence
            continue
          }
          const id = data.suggestion_id
          if (typeof id !== 'string' || seen.has(id)) { cursor = event.sequence; continue }
          // Busy windows keep their cursor; do not interrupt a user's active turn.
          if (!options.canSubmit()) break
          const claimed = await request('/v1/bridge/resume/claim', { ...identity, suggestion_id: id })
          if (claimed.claimed) {
            seen.add(id)
            if (stopped || !options.canSubmit()) {
              await fail(id, 'Session changed or became busy before dispatch; action not submitted.')
            } else if (typeof claimed.suggested_action !== 'string' || !claimed.suggested_action.trim()
                || /^[!\/]/.test(claimed.suggested_action.trim())) {
              await fail(id, 'Suggested action must be conversational input, not a shell or slash command.')
            } else {
              try { await options.submit(claimed.suggested_action) }
              catch { await fail(id, 'Original Session submission failed; action was not retried.') }
            }
          }
          cursor = event.sequence
        }
      } catch {
        // Network failures never trigger local execution or a second Session.
      }
      await new Promise<void>(r => setTimeout(r, options.intervalMs ?? 2000))
    }
  }
  void run()
  return () => { stopped = true; controller.abort() }
}
