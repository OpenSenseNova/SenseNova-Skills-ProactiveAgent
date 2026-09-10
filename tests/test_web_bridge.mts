import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import { startWebAgentBridge, submitApprovedInput } from '../src/sn_proactive_agent_connectors/hermes/tui/web_bridge.ts'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

test('dashboard retains already-seen daily reports after the rename', () => {
  const source = readFileSync(new URL('../src/sn_proactive_agent/web/app.js', import.meta.url), 'utf8')
  const binding = source.indexOf('  document.getElementById("daily-report-open")?.addEventListener')
  assert.ok(binding > 0)
  const records = new Map<string, string>()
  const context: any = { window: { localStorage: {
    getItem: (key: string) => records.get(key) ?? null,
    setItem: (key: string, value: string) => records.set(key, value),
  } } }
  // Exercise the actual helpers without starting the dashboard's DOM polling.
  runInNewContext(source.slice(0, binding)
    + 'globalThis.reportTest = { hasSeenDailyReport, rememberDailyReportSeen }; })();', context)
  const report = { report_id: 'same-report' }
  assert.equal(context.reportTest.hasSeenDailyReport(report), false)
  records.set('proactive-memory:daily-report-seen:same-report', '1')
  assert.equal(context.reportTest.hasSeenDailyReport(report), true)
  context.reportTest.rememberDailyReportSeen(report)
  assert.equal(records.get('sn-proactive-agent:daily-report-seen:same-report'), '1')
  assert.equal(records.get('proactive-memory:daily-report-seen:same-report'), '1')
})

async function until(predicate: () => boolean) {
  for (let count = 0; count < 200; count++) {
    if (predicate()) return
    await delay(10)
  }
  assert.fail('Bridge did not reach expected state')
}
async function fixture(t: any, options: { action?: string; busy?: boolean; changeDuringClaim?: boolean; rejectSubmission?: boolean; envUrl?: 'legacy' | 'canonical' } = {}) {
  const state = { busy: options.busy ?? false, submissions: [] as string[], failures: [] as object[],
    claims: 0, polls: 0, claimed: false, events: [] as object[] }
  const server = createServer(async (req, res) => {
    let input = ''
    for await (const chunk of req) input += chunk
    const body = input ? JSON.parse(input) : {}
    let output: unknown = {}
    if (req.url?.includes('/heartbeat')) output = { probe: 'challenge', roundtrip: body.probe === 'challenge' }
    else if (req.url?.includes('/bridge/events')) { state.polls++; output = { events: state.events } }
    else if (req.url?.includes('/resume/claim')) {
      state.claims++
      output = { claimed: !state.claimed, suggested_action: options.action ?? 'Update the report' }
      state.claimed = true
      if (options.changeDuringClaim) state.busy = true
    } else if (req.url?.includes('/session.resume.failed')) state.failures.push(body)
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(output))
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address() as { port: number }
  const serviceUrl = `http://127.0.0.1:${address.port}`
  if (options.envUrl) {
    const names = ['SN_PROACTIVE_AGENT_SERVICE_URL', 'PROACTIVE_MEMORY_SERVICE_URL']
    const previous = names.map(name => process.env[name])
    delete process.env.SN_PROACTIVE_AGENT_SERVICE_URL
    process.env.PROACTIVE_MEMORY_SERVICE_URL = serviceUrl
    if (options.envUrl === 'canonical') {
      process.env.SN_PROACTIVE_AGENT_SERVICE_URL = serviceUrl
      process.env.PROACTIVE_MEMORY_SERVICE_URL = 'http://127.0.0.1:1'
    }
    t.after(() => names.forEach((name, index) => {
      if (previous[index] === undefined) delete process.env[name]
      else process.env[name] = previous[index]
    }))
  }
  const stop = startWebAgentBridge({ sessionId: 'same-session', instanceId: 'fixture',
    serviceUrl: options.envUrl ? undefined : serviceUrl, intervalMs: 10,
    canSubmit: () => !state.busy, submit: async text => {
      if (options.rejectSubmission) throw new Error('Gateway rejected input')
      state.submissions.push(text)
    }, reportError: () => {} })
  t.after(async () => { stop(); server.closeAllConnections(); server.close(); await delay(20) })
  const approved = { event_type: 'session.resume.requested', sequence: 1,
    payload: { platform: 'hermes-tui', target_session_id: 'same-session', suggestion_id: 'approved-id', suggested_action: 'Untrusted presentation copy' } }
  return { state, approved }
}

test('legacy service URL remains usable by the renamed bridge', async t => {
  const { state, approved } = await fixture(t, { envUrl: 'legacy' })
  state.events = [approved]
  await until(() => state.submissions.length === 1)
})

test('canonical service URL takes precedence over the legacy variable', async t => {
  const { state, approved } = await fixture(t, { envUrl: 'canonical' })
  state.events = [approved]
  await until(() => state.submissions.length === 1)
})

test('ready suggestions do not execute; approval uses authoritative action exactly once', async t => {
  const { state, approved } = await fixture(t)
  state.events = [{ ...approved, event_type: 'suggestion.ready' }]
  await until(() => state.polls >= 3)
  assert.equal(state.claims, 0)
  state.events = [approved]
  await until(() => state.submissions.length === 1)
  await delay(60)
  assert.deepEqual(state.submissions, ['Update the report'])
  assert.equal(state.claims, 1)
})

test('busy window waits and does not consume or interrupt the active turn', async t => {
  const { state, approved } = await fixture(t, { busy: true })
  state.events = [approved]
  await until(() => state.polls >= 3)
  assert.equal(state.claims, 0)
  state.busy = false
  await until(() => state.submissions.length === 1)
})

test('session change during claim records failure instead of executing elsewhere', async t => {
  const { state, approved } = await fixture(t, { changeDuringClaim: true })
  state.events = [approved]
  await until(() => state.failures.length === 1)
  assert.deepEqual(state.submissions, [])
})

test('shell and slash commands are not dispatched as approval input', async t => {
  const { state, approved } = await fixture(t, { action: '!delete something' })
  state.events = [approved]
  await until(() => state.failures.length === 1)
  assert.deepEqual(state.submissions, [])
})

test('wrong Session never claims an action', async t => {
  const { state, approved } = await fixture(t)
  state.events = [{ ...approved, payload: { ...approved.payload, target_session_id: 'other-session' } }]
  await until(() => state.polls >= 3)
  assert.equal(state.claims, 0)
  assert.deepEqual(state.submissions, [])
})

test('asynchronous gateway rejection is reported and never retried', async t => {
  const { state, approved } = await fixture(t, { rejectSubmission: true })
  state.events = [approved]
  await until(() => state.failures.length === 1)
  await delay(50)
  assert.equal(state.claims, 1)
  assert.deepEqual(state.submissions, [])
})

test('native submission keeps exact Session and literal action across asynchronous work', async () => {
  let current = 'original'
  let request: unknown
  const action = 'Explain the literal text {!example}'
  await submitApprovedInput('original', action, {
    currentSessionId: () => current, isBusy: () => false,
    showSubmission: () => { current = 'another-session' },
    promptSubmit: async params => { await delay(5); request = params }
  })
  assert.deepEqual(request, { session_id: 'original', text: action })
})

test('native submission rejects an already switched or busy window', async () => {
  for (const busy of [true, false]) {
    await assert.rejects(submitApprovedInput('original', 'Update', {
      currentSessionId: () => busy ? 'original' : 'another-session', isBusy: () => busy,
      showSubmission: () => assert.fail('Unexpected UI change'),
      promptSubmit: async () => assert.fail('Unexpected dispatch')
    }), /not ready/)
  }
})
