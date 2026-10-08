import { createHash, randomUUID } from "node:crypto";

const PLATFORM = "openclaw";
const MAX_TEXT = 120_000;
const SHARED_STATE = Symbol.for("sn-proactive-agent.openclaw.runs.v1");

/**
 * Minimal OpenClaw observation plugin.
 *
 * The plugin runs inside the user's existing OpenClaw Gateway. It never
 * starts a second chat process. It observes turns and submits only actions
 * explicitly approved in the Web Dashboard to the original Session.
 */
export default {
  id: "sn-proactive-agent",
  name: "SN Proactive Agent",
  description: "Reports OpenClaw turns to the local SN Proactive Agent service.",
  register(api) {
    const configured = api.pluginConfig ?? {};
    const serviceUrl = String(
      configured.serviceUrl || process.env.SN_PROACTIVE_AGENT_SERVICE_URL || "http://127.0.0.1:8080",
    ).replace(/\/$/, "");
    const enabled = configured.enabled !== false;
    const pending = new Map();
    // Gateway HTTP routes and agent hooks can be registered on distinct plugin
    // instances. Keep approval correlation shared, keyed by exact run identity.
    const states = globalThis[SHARED_STATE] ??= new Map();
    if (!states.has(serviceUrl)) states.set(serviceUrl, { runs: new Map(), accepted: new Map() });
    const { runs: approvedRuns, accepted } = states.get(serviceUrl);

    api.registerHttpRoute?.({
      path: "/sn-proactive-agent/resume",
      auth: "gateway",
      match: "exact",
      handler: async (request, response) => {
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.setHeader("allow", "POST");
          response.end();
          return true;
        }
        try {
          if (!enabled) throw new Error("OpenClaw Connector is disabled");
          if (request.headers?.origin) throw new Error("Browser requests must use the Proactive Agent service");
          const body = await readJson(request);
          const suggestionId = textOf(body?.suggestion_id);
          const sessionId = textOf(body?.session_id);
          const session = resolveSession(api, sessionId);
          const sessionKey = session?.sessionKey;
          const action = textOf(body?.suggested_action);
          if (!suggestionId || !sessionId || !sessionKey || !action) {
            throw new Error("suggestion_id, session_id, session_key, and suggested_action are required");
          }
          if (accepted.has(suggestionId)) {
            const previous = accepted.get(suggestionId);
            if (previous.sessionId !== sessionId || previous.action !== action) {
              throw new Error("Conflicting retry for suggestion_id");
            }
            response.statusCode = 202;
            response.setHeader("content-type", "application/json");
            response.end(JSON.stringify({ accepted: true, run_id: previous.runId }));
            return true;
          }
          const runId = randomUUID();
          approvedRuns.set(runId, {
            sessionId,
            runtimeSessionId: session.sessionId,
            sessionKey,
            question: action,
            turnId: runId,
            sourceSuggestionId: suggestionId,
          });
          accepted.set(suggestionId, { runId, sessionId, action });
          // Acknowledge admission before executing: hooks call back into Core,
          // which must not be blocked waiting on this HTTP request.
          response.statusCode = 202;
          response.setHeader("content-type", "application/json");
          response.end(JSON.stringify({ accepted: true, run_id: runId }));
          void Promise.resolve().then(() => api.runtime.agent.runEmbeddedAgent({
            sessionId: session.sessionId,
            sessionKey,
            agentId: sessionKey.split(":")[1] || "main",
            runId,
            prompt: action,
            trigger: "manual",
            workspaceDir: api.runtime.agent.resolveAgentWorkspaceDir?.(api.config),
            timeoutMs: api.runtime.agent.resolveAgentTimeoutMs(api.config),
          })).then(async (result) => {
            if (result?.meta?.aborted || result?.meta?.error) throw new Error("OpenClaw run did not complete successfully");
          }).catch(async (error) => {
            approvedRuns.delete(runId);
            await post("session.resume.failed", {
              suggestion_id: suggestionId,
              reason: String(error),
              failed_at: new Date().toISOString(),
            });
          });
        } catch (error) {
          response.statusCode = 400;
          response.setHeader("content-type", "application/json");
          response.end(JSON.stringify({ accepted: false, error: String(error) }));
        }
        return true;
      },
    });

    const post = async (eventName, payload) => {
      if (!enabled) return;
      try {
        const response = await fetch(`${serviceUrl}/v1/events/${eventName}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(2500),
        });
        if (!response.ok) {
          api.logger.warn?.(`sn-proactive-agent: ${eventName} returned HTTP ${response.status}`);
        }
      } catch (error) {
        api.logger.warn?.(`sn-proactive-agent: ${eventName} failed: ${String(error)}`);
      }
    };

    const startTurn = async (event, context, questionValue) => {
      const observedSessionId = sessionIdOf(event, context);
      const sessionKey = sessionKeyOf(event, context);
      const question = textOf(questionValue);
      if (!observedSessionId || !question) return;
      const runId = textOf(context?.runId) || textOf(event?.runId)
        || textOf(event?.messageId) || turnId(observedSessionId, question);
      const approved = approvedRuns.get(runId);
      const matchesApproval = approved && approved.question === question
        && (approved.runtimeSessionId === observedSessionId || approved.sessionId === observedSessionId)
        && (!sessionKey || approved.sessionKey === sessionKey);
      const sessionId = matchesApproval ? approved.sessionId : observedSessionId;
      if (pending.has(runId)) return;
      // Channel messages and the agent-run hook can describe the same turn.
      // Avoid emitting two freshness signals when both paths are available.
      for (const item of pending.values()) {
        if (item.sessionId === sessionId && item.question === question) return;
      }
      pending.set(runId, { sessionId, sessionKey, question, turnId: runId });
      await post("turn.started", {
        platform: PLATFORM,
        session_id: sessionId,
        turn_id: runId,
        started_at: new Date().toISOString(),
      });
    };

    api.on("message_received", async (event, context) => {
      await startTurn(event, context, event?.content ?? event?.body ?? event?.message);
    });

    // message_received is channel-oriented and is not fired for every local
    // `openclaw agent` invocation. before_agent_run covers CLI/Gateway runs
    // while still using the same runId/sessionKey correlation fields.
    api.on("before_agent_run", async (event, context) => {
      await startTurn(event, context, event?.prompt);
    });

    api.on("agent_end", async (event, context) => {
      if (event?.success === false) return;
      const messages = Array.isArray(event?.messages) ? event.messages : [];
      const runId = textOf(event?.runId) || textOf(context?.runId);
      const eventSessionId = sessionIdOf(event, context);
      const eventSessionKey = sessionKeyOf(event, context);
      const eventQuestion = latestUserMessage(messages);
      const approved = approvedRuns.get(runId);
      const saved = (runId && pending.get(runId)) || latestPending(
        pending,
        eventSessionId,
        eventSessionKey,
      );
      const sourceSuggestionId = approved
        && (approved.sessionId === eventSessionId || approved.runtimeSessionId === eventSessionId)
        && (!eventSessionKey || approved.sessionKey === eventSessionKey)
        && approved.question === (saved?.question || eventQuestion)
        ? approved.sourceSuggestionId : null;
      const sessionId = textOf(saved?.sessionId) || eventSessionId;
      const question = textOf(saved?.question) || eventQuestion;
      const answer = latestAssistantMessage(messages);
      if (!sessionId || !question || !answer) return;
      const turn = saved ?? { turnId: runId || turnId(sessionId, question) };
      if (sourceSuggestionId) {
        turn.sourceSuggestionId = sourceSuggestionId;
        approvedRuns.delete(runId);
      }
      if (runId) pending.delete(runId);
      else pending.delete(turn.turnId);
      await post("turn.completed", {
        platform: PLATFORM,
        session_id: sessionId,
        turn_id: textOf(turn.turnId) || turnId(sessionId, question),
        user_question: question,
        final_answer: answer,
        completed_at: new Date().toISOString(),
        ...(turn.sourceSuggestionId ? { source_suggestion_id: turn.sourceSuggestionId } : {}),
      });
    });
  },
};

function sessionIdOf(event, context) {
  return textOf(context?.sessionId) || textOf(event?.sessionId)
    || textOf(event?.metadata?.sessionId) || textOf(context?.sessionKey)
    || textOf(event?.sessionKey) || textOf(event?.metadata?.sessionKey)
    || textOf(event?.threadId);
}

function sessionKeyOf(event, context) {
  return textOf(context?.sessionKey) || textOf(event?.sessionKey)
    || textOf(context?.session_id) || textOf(event?.session_id);
}

function latestPending(pending, sessionId, sessionKey) {
  if (!sessionId && !sessionKey) return null;
  let result = null;
  for (const item of pending.values()) {
    if ((sessionId && item.sessionId === sessionId)
      || (sessionKey && item.sessionKey === sessionKey)) {
      result = item;
    }
  }
  return result;
}

function latestUserMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (String(message?.role || message?.type || "").toLowerCase() === "user") {
      return textOf(message?.content ?? message?.text ?? message?.message);
    }
  }
  return "";
}

function latestAssistantMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    const role = String(message?.role || message?.type || "").toLowerCase();
    if (role === "assistant" || role === "agent") {
      return textOf(message?.content ?? message?.text ?? message?.message);
    }
  }
  return "";
}

function textOf(value) {
  if (typeof value === "string") return value.trim().slice(0, MAX_TEXT);
  if (Array.isArray(value)) return value.map(textOf).filter(Boolean).join("\n").slice(0, MAX_TEXT);
  if (value && typeof value === "object") {
    return textOf(value.text ?? value.content ?? value.body ?? value.value);
  }
  return "";
}

function turnId(sessionId, question) {
  return `turn-${createHash("sha256").update(`${sessionId}\0${question}`).digest("hex").slice(0, 20)}`;
}

export const testing = { sessionIdOf, latestUserMessage, latestAssistantMessage, textOf, turnId };

function readJson(request) {
  return new Promise((resolve, reject) => {
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1_000_000) reject(new Error("request body too large"));
    });
    request.on("end", () => {
      try { resolve(JSON.parse(raw || "{}")); } catch (error) { reject(error); }
    });
    request.on("error", reject);
  });
}

function resolveSession(api, sessionId) {
  try {
    const agentId = /^agent:([^:]+):/.exec(sessionId)?.[1] || "main";
    const rows = api.runtime?.agent?.session?.listSessionEntries?.({ agentId }) || [];
    for (const row of rows) {
      const entry = row?.entry ?? row;
      const sessionKey = textOf(row?.sessionKey) || textOf(entry?.sessionKey);
      const runtimeSessionId = textOf(entry?.sessionId);
      if (runtimeSessionId && sessionKey && (runtimeSessionId === sessionId || sessionKey === sessionId)) {
        return { sessionId: runtimeSessionId, sessionKey };
      }
    }
  } catch (_) {
    // The Gateway runtime may not expose session listings in metadata mode.
  }
  return null;
}
