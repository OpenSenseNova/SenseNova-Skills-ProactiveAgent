(function () {
  "use strict";

  const state = {
    projects: [],
    harnesses: [],
    suggestions: [],
    latestDecision: null,
    dailyReport: null,
    dailyReportLoaded: false,
    dailyReportOpen: false,
    dailyReportLastFocus: null,
    dailyReportActionQueue: Promise.resolve(),
    dailyReportAutoTimer: null,
    dailyReportSignature: "",
    // Dashboard refreshes periodically. Keep the user's disclosure choice
    // outside the DOM so a refresh does not unexpectedly fold open history.
    expandedProjects: new Set(),
    expandedItems: new Set(),
    eventCache: new Map(),
    eventLoads: new Map(),
    expandedEventDetails: new Set(),
    language: null,
    loaded: false,
    connectionFailed: false,
    generatedAt: null,
    pendingResponses: new Set(),
  };
  const translations = {
    "zh": {
      "settings": "设置",
      "harness_settings": "监测来源",
      "monitor_sources": "选择要记录的 Harness",
      "loading_harnesses": "正在读取…",
      "enabled": "已启用",
      "disabled": "未启用",
      "connected": "已连接",
      "ready": "可用",
      "unavailable": "未配置",
      "harness_settings_note": "关闭后，该来源的新对话不会更新项目状态。",
      "harness_update_failed": "监测设置更新失败：{error}",
      "language_auto": "跟随系统",
      "interface_language": "界面语言",
      "agent_language": "Agent 整理语言",
      "agent_language_value": "中文",
      "agent_language_note": "项目摘要、Item 状态和建议会按当前界面语言生成；已写入内容不会自动翻译。",
      "prompt_details": "查看提示词说明",
      "prompt_language_note": "Organizer / Judge 会收到当前界面语言要求：中文界面使用中文，英文界面使用英文；JSON 键名和枚举值保持不变。",
      "brand_name": "Proactive Agent",
      "local_service": "本地服务",
      "connecting": "正在连接…",
      "updated_at": "更新于 {value}",
      "connection_failed": "服务连接失败",
      "daily_report_short": "日报",
      "view_daily_report": "查看日报",
      "new_daily_report": "有新的日报",
      "pending_label": "待处理",
      "suggestions_title": "建议与决策",
      "loading_decisions": "正在读取最新决策…",
      "project_status": "项目状态",
      "projects_title": "项目进展",
      "loading_projects": "正在读取项目…",
      "loading_project_item": "正在读取 Project / Item…",
      "today_summary": "今日摘要",
      "daily_report_title": "项目日报",
      "loading": "正在读取…",
      "close_daily_report": "关闭日报",
      "loading_progress": "正在整理项目进展…",
      "daily_report_footnote": "日报只保留每个项目的关键进展。",
      "got_it": "知道了",
      "language_label": "语言",
      "language_zh": "中文",
      "language_en": "English",
      "active": "进行中",
      "paused": "已暂停",
      "completed": "已完成",
      "planned": "计划中",
      "in_progress": "进行中",
      "blocked": "有阻塞",
      "pending": "待你决定",
      "approved": "已接受",
      "ignored": "已忽略",
      "resuming": "正在续跑",
      "failed": "续跑失败",
      "unknown": "未知",
      "silent": "本轮暂不打扰",
      "suggest": "建议已生成",
      "discarded": "旧判断已丢弃",
      "source_suggestion_completed": "建议已执行",
      "untracked": "本轮未进入项目状态",
      "today": "今日",
      "no_daily_report": "还没有可展示的日报",
      "no_daily_report_hint": "有新的项目进展后，日报会在这里出现。",
      "no_progress_today": "这一天暂无项目进展",
      "report_will_update": "有新的记录后，日报会自动补充。",
      "next_step": "下一步",
      "no_decisions": "还没有决策记录",
      "no_decisions_hint": "完成一轮对话后，这里会显示 silent 或 suggestion.ready。",
      "suggestion_generated": "建议已生成，等待你的决定。",
      "suggestion_done": "最近建议已执行",
      "suggestion_ignored": "最近建议已忽略",
      "suggestion_resuming": "最近建议正在续跑",
      "suggestion_approved": "最近建议已接受",
      "suggestion_failed": "最近建议续跑失败",
      "suggestion_processed": "最近建议已处理",
      "reason_failed": "续跑失败，详细原因已写入 runtime.jsonl。",
      "reason_ignored": "用户选择忽略，系统没有执行建议动作。",
      "reason_completed": "用户已授权，原会话已完成建议动作并回流结果。",
      "reason_resuming": "用户已授权，系统正在原会话中执行建议动作。",
      "decision_recorded": "本轮决策已记录。",
      "status_updated": "本轮状态已更新，暂不打扰。",
      "decision_expired": "本轮判断已过期，状态更新仍已保留。",
      "no_project": "还没有项目",
      "no_project_hint": "收到第一轮可归属的 QA 后，Project 会出现在这里。",
      "no_item": "暂无 Item",
      "collapse_items": "收起 Item",
      "view_items": "查看 {count} 个 Item",
      "no_items": "暂无 Item",
      "project_progress": "项目进度",
      "item_progress": "事项进度",
      "progress": "进度",
      "blocked_short": "阻塞",
      "event_count": "查看 {count} 条 Event ↓",
      "event_count_up": "收起 {count} 条 Event ↑",
      "no_events": "暂无历史 Event",
      "loading_history": "正在读取历史…",
      "events_failed": "读取 Event 失败，请稍后重试。",
      "view_qa": "点击查看 QA",
      "question": "问题",
      "answer": "回答",
      "suggestion_action": "建议操作",
      "continue_current": "请继续推进当前事项。",
      "ignore": "忽略",
      "approve_continue": "接受并继续",
      "accepted_continue": "已接受，正在继续原会话。",
      "ignored_no_execute": "已忽略，这条建议不会执行。",
      "submit_failed": "提交失败：{error}",
      "cannot_connect": "无法连接 Proactive Agent 服务。",
      "start_service_refresh": "请确认服务已启动，并刷新页面。",
      "day_unit": "个 Project",
      "item_unit": "个 Item",
      "status_count": "{projects} 个 Project · {items} 个 Item",
      "report_projects": "{date} · {count} 个 Project",
      "unknown_source": "unknown"
    },
    "en": {
      "settings": "Settings",
      "harness_settings": "Sources",
      "monitor_sources": "Choose Harnesses to record",
      "loading_harnesses": "Loading…",
      "enabled": "Enabled",
      "disabled": "Disabled",
      "connected": "Connected",
      "ready": "Available",
      "unavailable": "Not configured",
      "harness_settings_note": "When disabled, new turns from this source will not update projects.",
      "harness_update_failed": "Could not update source: {error}",
      "language_auto": "System default",
      "interface_language": "Interface language",
      "agent_language": "Agent content language",
      "agent_language_value": "English",
      "agent_language_note": "Project summaries, item states, and suggestions follow the current interface language; existing content is not translated.",
      "prompt_details": "About the prompt",
      "prompt_language_note": "Organizer / Judge receive the current interface-language requirement: Chinese for a Chinese UI and English for an English UI; JSON keys and enum values stay unchanged.",
      "brand_name": "Proactive Agent",
      "local_service": "Local service",
      "connecting": "Connecting…",
      "updated_at": "Updated {value}",
      "connection_failed": "Service unavailable",
      "daily_report_short": "Daily brief",
      "view_daily_report": "View daily brief",
      "new_daily_report": "New daily brief",
      "pending_label": "Pending",
      "suggestions_title": "Suggestions & decisions",
      "loading_decisions": "Loading latest decision…",
      "project_status": "Project status",
      "projects_title": "Project progress",
      "loading_projects": "Loading projects…",
      "loading_project_item": "Loading Project / Item…",
      "today_summary": "Daily brief",
      "daily_report_title": "Project brief",
      "loading": "Loading…",
      "close_daily_report": "Close daily brief",
      "loading_progress": "Preparing project progress…",
      "daily_report_footnote": "A short view of each project's key progress.",
      "got_it": "Got it",
      "language_label": "Language",
      "language_zh": "中文",
      "language_en": "English",
      "active": "In progress",
      "paused": "Paused",
      "completed": "Completed",
      "planned": "Planned",
      "in_progress": "In progress",
      "blocked": "Blocked",
      "pending": "Your decision",
      "approved": "Accepted",
      "ignored": "Ignored",
      "resuming": "Resuming",
      "failed": "Resume failed",
      "unknown": "Unknown",
      "silent": "No interruption this round",
      "suggest": "Suggestion ready",
      "discarded": "Previous decision discarded",
      "source_suggestion_completed": "Suggestion completed",
      "untracked": "Not added to a project",
      "today": "Today",
      "no_daily_report": "No daily brief yet",
      "no_daily_report_hint": "New project progress will appear here.",
      "no_progress_today": "No project progress for this day",
      "report_will_update": "The brief will update when new records arrive.",
      "next_step": "Next",
      "no_decisions": "No decisions yet",
      "no_decisions_hint": "After a turn completes, silent or suggestion.ready will appear here.",
      "suggestion_generated": "Suggestion ready for your decision.",
      "suggestion_done": "Latest suggestion completed",
      "suggestion_ignored": "Latest suggestion ignored",
      "suggestion_resuming": "Latest suggestion is resuming",
      "suggestion_approved": "Latest suggestion accepted",
      "suggestion_failed": "Latest suggestion failed",
      "suggestion_processed": "Latest suggestion processed",
      "reason_failed": "Resume failed; details are in runtime.jsonl.",
      "reason_ignored": "You chose to ignore it. No action was executed.",
      "reason_completed": "You approved it; the original session completed and returned a result.",
      "reason_resuming": "You approved it; the original session is processing the action.",
      "decision_recorded": "This round's decision was recorded.",
      "status_updated": "State updated without interrupting you.",
      "decision_expired": "This decision expired; the state update was kept.",
      "no_project": "No projects yet",
      "no_project_hint": "Projects appear after the first attributable QA turn.",
      "no_item": "No items",
      "collapse_items": "Hide items",
      "view_items": "View {count} items",
      "no_items": "No items",
      "project_progress": "Project progress",
      "item_progress": "Item progress",
      "progress": "Progress",
      "blocked_short": "Blocked",
      "event_count": "View {count} events ↓",
      "event_count_up": "Hide {count} events ↑",
      "no_events": "No event history",
      "loading_history": "Loading history…",
      "events_failed": "Could not load events. Try again.",
      "view_qa": "View QA",
      "question": "Question",
      "answer": "Answer",
      "suggestion_action": "Suggested action",
      "continue_current": "Continue with the current item.",
      "ignore": "Ignore",
      "approve_continue": "Accept & continue",
      "accepted_continue": "Accepted. Continuing the original session.",
      "ignored_no_execute": "Ignored. No action was executed.",
      "submit_failed": "Could not submit: {error}",
      "cannot_connect": "Could not connect to Proactive Agent.",
      "start_service_refresh": "Make sure the service is running, then refresh.",
      "day_unit": "projects",
      "item_unit": "items",
      "status_count": "{projects} projects · {items} items",
      "report_projects": "{date} · {count} projects",
      "unknown_source": "unknown"
    }
  };
  const languageStorageKey = "sn-proactive-agent:language";
  const dailyReportSeenPrefix = "sn-proactive-agent:daily-report-seen:";

  function detectLanguage() {
    const languages = Array.isArray(navigator.languages) && navigator.languages.length ? navigator.languages : [navigator.language || "en"];
    return languages.some((value) => /^zh(?:[-_]|$)/i.test(String(value))) ? "zh" : "en";
  }

  function currentLanguage() { return state.language === "zh" ? "zh" : state.language === "en" ? "en" : detectLanguage(); }

  function t(key, values = {}) {
    const dictionary = translations[currentLanguage()];
    let value = dictionary[key] ?? translations.en[key] ?? key;
    return String(value).replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ""));
  }

  function applyLanguage() {
    const language = currentLanguage();
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.querySelectorAll("[data-i18n]").forEach((element) => { element.textContent = t(element.dataset.i18n); });
    document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => { element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel)); });
    const selector = document.getElementById("language-select");
    if (selector) selector.value = state.language || "auto";
    const interfaceValue = document.getElementById("interface-language-value");
    if (interfaceValue) interfaceValue.textContent = state.language === "auto" ? t("language_auto") : currentLanguage() === "zh" ? t("language_zh") : t("language_en");
    const agentValue = document.querySelector("[data-i18n='agent_language_value']");
    if (agentValue) agentValue.textContent = t("agent_language_value");
    document.title = `${t("brand_name")} · ${currentLanguage() === "zh" ? "工作台" : "Dashboard"}`;
    syncOutputLanguage();
    renderConnectionState();
    renderHarnesses();
  }

  let lastSyncedOutputLanguage = null;
  let outputLanguageSyncPromise = null;
  function syncOutputLanguage() {
    const language = currentLanguage();
    if (language === lastSyncedOutputLanguage || outputLanguageSyncPromise) return;
    if (typeof fetch !== "function") return;
    let succeeded = false;
    outputLanguageSyncPromise = fetch("/api/preferences/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    }).then((response) => {
      if (!response.ok) throw new Error("language preference request failed");
      succeeded = true;
      if (currentLanguage() === language) lastSyncedOutputLanguage = language;
    }).catch(() => {
      // The dashboard remains usable when an older service has no preference route.
      // Leave the language unsynced so a later refresh or language change retries.
    }).finally(() => {
      outputLanguageSyncPromise = null;
      if (succeeded && currentLanguage() !== lastSyncedOutputLanguage) syncOutputLanguage();
    });
  }

  function renderConnectionState() {
    document.getElementById("last-updated").textContent = state.connectionFailed
      ? t("connection_failed") : state.loaded ? t("updated_at", { value: formatTime(state.generatedAt) }) : t("connecting");
  }

  function harnessStatusLabel(status) {
    return t(status === "connected" ? "connected" : status === "ready" ? "ready" : status === "unavailable" ? "unavailable" : status === "disabled" ? "disabled" : "unknown");
  }

  function renderHarnesses() {
    const root = document.getElementById("harness-list");
    if (!root) return;
    if (!state.harnesses.length) {
      root.innerHTML = `<div class="harness-loading">${escapeHtml(t("unavailable"))}</div>`;
      return;
    }
    root.innerHTML = state.harnesses.map((harness) => {
      const status = harnessStatusLabel(harness.status);
      const disabled = !harness.available ? " disabled" : "";
      return `<label class="harness-row${harness.available ? "" : " is-unavailable"}">
        <span class="harness-row-copy"><strong>${escapeHtml(harness.label || harness.id)}</strong><small>${escapeHtml(status)}</small></span>
        <input type="checkbox" data-harness-id="${escapeHtml(harness.id)}"${harness.enabled ? " checked" : ""}${disabled} aria-label="${escapeHtml(harness.label || harness.id)}" />
      </label>`;
    }).join("");
    root.querySelectorAll("[data-harness-id]").forEach((input) => {
      input.addEventListener("change", () => updateHarness(input));
    });
  }

  async function updateHarness(input) {
    const harnessId = input.dataset.harnessId;
    if (!harnessId) return;
    input.disabled = true;
    try {
      const response = await fetch("/api/harnesses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: harnessId, enabled: input.checked }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || body.error || t("connection_failed"));
      state.harnesses = state.harnesses.map((harness) => harness.id === harnessId ? body.harness : harness);
      renderHarnesses();
      showToast(input.checked ? t("enabled") : t("disabled"));
    } catch (error) {
      showToast(t("harness_update_failed", { error: error.message }), true);
      await refresh();
    }
  }

  function redrawLanguage() {
    applyLanguage();
    if (state.connectionFailed) {
      renderConnectionError();
    } else if (state.loaded) {
      const focus = captureProjectsFocus();
      renderSuggestion();
      renderProjects();
      restoreProjectsFocus(focus);
    }
    if (state.dailyReportLoaded) renderDailyReport();
  }

  function renderConnectionError() {
    document.getElementById("suggestion-content").innerHTML = `<div class="error-state">${escapeHtml(t("cannot_connect"))}</div>`;
    document.getElementById("projects-content").innerHTML = `<div class="error-state">${escapeHtml(t("start_service_refresh"))}</div>`;
  }

  try {
    const savedLanguage = window.localStorage.getItem(languageStorageKey);
    state.language = savedLanguage === "zh" || savedLanguage === "en" ? savedLanguage : "auto";
  } catch (_error) {
    state.language = "auto";
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(currentLanguage() === "zh" ? "zh-CN" : "en-US", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function localDateKey(value) {
    const date = value instanceof Date ? value : new Date(value || Date.now());
    if (Number.isNaN(date.getTime())) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function reportDateLabel(value) {
    const key = typeof value === "string" ? value.slice(0, 10) : localDateKey(value);
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
    if (!match) return t("today");
    return currentLanguage() === "zh"
      ? `${Number(match[1])} 年 ${Number(match[2])} 月 ${Number(match[3])} 日`
      : `${match[1]}-${match[2]}-${match[3]}`;
  }

  function reportSeenKey(report, prefix = dailyReportSeenPrefix) {
    return `${prefix}${report?.report_id || report?.id || report?.date || localDateKey()}`;
  }

  function hasSeenDailyReport(report) {
    try {
      return window.localStorage.getItem(reportSeenKey(report)) === "1"
        || window.localStorage.getItem(reportSeenKey(report, "proactive-memory:daily-report-seen:")) === "1";
    } catch (_error) {
      return false;
    }
  }

  function rememberDailyReportSeen(report) {
    try {
      window.localStorage.setItem(reportSeenKey(report), "1");
    } catch (_error) {
      // Private browsing or a disabled storage policy should not prevent the
      // report from being viewed.
    }
  }

  function normalizeDailyReport(payload) {
    if (!payload || typeof payload !== "object") return null;
    let raw = payload.report || payload.daily_report || payload.dailyReport;
    if (!raw && Array.isArray(payload.projects)) raw = payload;
    if (!raw || typeof raw !== "object") return null;
    const projects = Array.isArray(raw.projects)
      ? raw.projects.filter((project) => project && typeof project === "object")
      : [];
    const generatedAt = raw.generated_at || raw.generatedAt || payload.generated_at;
    const date = raw.date || raw.report_date || raw.reportDate || localDateKey(generatedAt);
    return {
      ...raw,
      report_id: raw.report_id || raw.reportId || raw.id || `daily-${date}`,
      date: String(date || localDateKey()).slice(0, 10),
      generated_at: generatedAt,
      projects,
      first_open_pending: payload.first_open_pending ?? payload.firstOpenPending ?? raw.first_open_pending ?? false,
      viewed_at: payload.viewed_at ?? raw.viewed_at ?? null,
      dismissed_at: payload.dismissed_at ?? raw.dismissed_at ?? null,
      status: payload.status ?? raw.status ?? null,
      available: payload.available !== false && raw.available !== false,
    };
  }

  function reportProjectName(project) {
    return project.name || project.project_name || project.projectName || project.project_id || project.id || (currentLanguage() === "zh" ? "未命名项目" : "Untitled project");
  }

  function reportProjectSummary(project) {
    return project.summary || project.current_progress || project.progress || project.highlight || (currentLanguage() === "zh" ? "这一天暂无新的进展记录。" : "No new progress was recorded that day.");
  }

  function reportProjectNextStep(project) {
    if (project.next_step || project.nextStep) return project.next_step || project.nextStep;
    const items = Array.isArray(project.items) ? project.items : [];
    const active = items.find((item) => item && (item.next_step || item.nextStep));
    return active ? (active.next_step || active.nextStep) : "";
  }

  function renderDailyReport() {
    const content = document.getElementById("daily-report-content");
    const subtitle = document.getElementById("daily-report-subtitle");
    if (!content || !subtitle) return;
    const report = state.dailyReport;
    if (!report || !report.available) {
      subtitle.textContent = currentLanguage() === "zh" ? "每天 0 点后整理，打开页面即可查看。" : "Prepared after midnight and shown when you open the dashboard.";
      content.innerHTML = `<div class="report-empty"><span class="report-empty-icon" aria-hidden="true"></span><strong>${escapeHtml(t("no_daily_report"))}</strong><span>${escapeHtml(t("no_daily_report_hint"))}</span></div>`;
      return;
    }
    subtitle.textContent = t("report_projects", { date: reportDateLabel(report.date), count: report.projects.length });
    if (!report.projects.length) {
      content.innerHTML = `<div class="report-empty"><span class="report-empty-icon" aria-hidden="true"></span><strong>${escapeHtml(t("no_progress_today"))}</strong><span>${escapeHtml(t("report_will_update"))}</span></div>`;
      return;
    }
    content.innerHTML = report.projects.map((project) => {
      const status = project.status || "active";
      const nextStep = reportProjectNextStep(project);
      const itemCount = project.item_count ?? (Array.isArray(project.items) ? project.items.length : null);
      return `<article class="report-project">
        <div class="report-project-heading">
          <div class="report-project-title"><span class="report-project-mark" aria-hidden="true"></span><h3>${escapeHtml(reportProjectName(project))}</h3></div>
          <span class="report-status status-${escapeHtml(status)}">${escapeHtml(statusLabel(status))}</span>
        </div>
        <p class="report-project-summary">${escapeHtml(reportProjectSummary(project))}</p>
        <div class="report-project-meta">
          ${nextStep ? `<span><b>${escapeHtml(t("next_step"))}</b>${escapeHtml(nextStep)}</span>` : ""}
          ${itemCount == null ? "" : `<span>${escapeHtml(String(itemCount))} ${escapeHtml(t("item_unit"))}</span>`}
        </div>
      </article>`;
    }).join("");
  }

  function recordDailyReportAction(action) {
    const report = state.dailyReport;
    if (!report) return Promise.resolve();
    // Keep view → dismiss ordering deterministic when a user closes the modal
    // immediately after it opens. The server journal treats each action as
    // idempotent, while this queue prevents network reordering from changing
    // the last lifecycle state.
    state.dailyReportActionQueue = state.dailyReportActionQueue
      .catch(() => {})
      .then(async () => {
        try {
          const response = await fetch(`/api/daily-report/${action}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ report_id: report.report_id, date: report.date, acted_at: new Date().toISOString() }),
          });
          return response.ok;
        } catch (_error) {
          // Viewing a local read model must remain usable when the optional
          // audit endpoint is unavailable.
          return false;
        }
      });
    return state.dailyReportActionQueue;
  }

  function openDailyReport({ automatic = false } = {}) {
    const modal = document.getElementById("daily-report-modal");
    if (!modal) return;
    if (state.dailyReportAutoTimer) {
      window.clearTimeout(state.dailyReportAutoTimer);
      state.dailyReportAutoTimer = null;
    }
    state.dailyReportLastFocus = document.activeElement;
    state.dailyReportOpen = true;
    renderDailyReport();
    modal.hidden = false;
    document.body.classList.add("report-modal-open");
    const closeButton = document.getElementById("daily-report-close");
    if (closeButton) closeButton.focus();
    const report = state.dailyReport;
    if (report && (automatic || !hasSeenDailyReport(report))) {
      // Mark the local fallback only after the server accepts the view.  A
      // transient network failure should leave the report eligible for the
      // next automatic open instead of hiding it forever in this browser.
      void recordDailyReportAction("view").then((accepted) => {
        if (accepted) rememberDailyReportSeen(report);
      });
    }
    document.getElementById("daily-report-unread")?.setAttribute("hidden", "");
  }

  function closeDailyReport() {
    const modal = document.getElementById("daily-report-modal");
    if (!modal || modal.hidden) return;
    state.dailyReportOpen = false;
    modal.hidden = true;
    document.body.classList.remove("report-modal-open");
    if (state.dailyReport) void recordDailyReportAction("dismiss");
    if (state.dailyReportLastFocus && typeof state.dailyReportLastFocus.focus === "function") {
      state.dailyReportLastFocus.focus();
    }
  }

  function maybeAutoOpenDailyReport() {
    const report = state.dailyReport;
    if (!report || !report.available || !report.projects.length || state.dailyReportOpen || state.dailyReportAutoTimer) return;
    // The service owns the durable first-open lifecycle.  The browser-local
    // marker is only a fallback for refresh races; a date match alone must not
    // reopen a report that the server already marked viewed or dismissed.
    if (!report.first_open_pending || hasSeenDailyReport(report)) return;
    state.dailyReportAutoTimer = window.setTimeout(() => {
      state.dailyReportAutoTimer = null;
      if (!state.dailyReportOpen && state.dailyReport?.first_open_pending && !hasSeenDailyReport(state.dailyReport)) {
        openDailyReport({ automatic: true });
      }
    }, 180);
  }

  async function loadDailyReport(dashboardData) {
    let payload = null;
    try {
      const response = await fetch(`/api/daily-report?ts=${Date.now()}`, { cache: "no-store" });
      if (response.ok) payload = await response.json();
    } catch (_error) {
      // Older service versions do not expose the optional report endpoint.
    }
    if (!payload && dashboardData) payload = dashboardData.daily_report || dashboardData.dailyReport || null;
    const nextReport = normalizeDailyReport(payload);
    const nextSignature = nextReport
      ? JSON.stringify({
        report_id: nextReport.report_id,
        generated_at: nextReport.generated_at,
        status: nextReport.status,
        first_open_pending: nextReport.first_open_pending,
      })
      : "";
    const reportChanged = nextSignature !== state.dailyReportSignature;
    state.dailyReport = nextReport;
    state.dailyReportSignature = nextSignature;
    state.dailyReportLoaded = true;
    // Do not rebuild an open dialog on every dashboard poll: keeping the
    // existing DOM preserves the reader's scroll position and focus.
    if (reportChanged || !state.dailyReportOpen) renderDailyReport();
    const unread = document.getElementById("daily-report-unread");
    if (unread && state.dailyReport && state.dailyReport.first_open_pending && !hasSeenDailyReport(state.dailyReport)) {
      unread.removeAttribute("hidden");
    } else {
      unread?.setAttribute("hidden", "");
    }
    maybeAutoOpenDailyReport();
  }

  function statusLabel(status) {
    const known = ["active", "paused", "completed", "planned", "in_progress", "blocked", "pending", "approved", "ignored", "resuming", "failed"];
    return known.includes(status) ? t(status) : status || t("unknown");
  }

  function decisionDisplayReason(decision) {
    const messages = {
      silent: t("status_updated"),
      suggest: t("suggestion_generated"),
      discarded: t("decision_expired"),
      source_suggestion_completed: t("suggestion_done"),
      untracked: t("untracked"),
    };
    return messages[decision?.outcome] || t("decision_recorded");
  }

  function progressWidth(project) {
    if (!project.items.length) return 0;
    const weights = { planned: 18, in_progress: 58, blocked: 42, completed: 100 };
    return Math.round(project.items.reduce((total, item) => total + (weights[item.status] || 30), 0) / project.items.length);
  }

  function renderSuggestion() {
    const root = document.getElementById("suggestion-content");
    const count = state.suggestions.filter((item) => item.status === "pending").length;
    document.getElementById("suggestion-count").textContent = currentLanguage() === "zh" ? `${count} 条待处理` : `${count} pending`;
    const pending = state.suggestions.filter((item) => item.status === "pending");
    if (pending.length) {
      root.innerHTML = pending.map(renderSuggestionCard).join("");
      root.querySelectorAll("[data-choice]").forEach((button) => {
        button.disabled = state.pendingResponses.has(button.dataset.id);
        button.addEventListener("click", () => respond(button.dataset.id, button.dataset.choice, button));
      });
      return;
    }
    const latestSuggestion = state.suggestions[0];
    if (latestSuggestion && latestSuggestion.status !== "pending") {
      const statusTitle = {
        completed: t("suggestion_done"), ignored: t("suggestion_ignored"), resuming: t("suggestion_resuming"), approved: t("suggestion_approved"), failed: t("suggestion_failed"),
      }[latestSuggestion.status] || t("suggestion_processed");
      const statusReason = latestSuggestion.status === "failed"
        ? t("reason_failed")
        : latestSuggestion.status === "ignored"
          ? t("reason_ignored")
          : latestSuggestion.status === "completed"
            ? t("reason_completed")
            : t("reason_resuming");
      root.innerHTML = `<div class="decision-card">
        <div class="decision-icon">✓</div>
        <div><h3>${escapeHtml(statusTitle)}</h3>
        <p>${escapeHtml(statusReason)}</p>
        <p class="decision-time">${escapeHtml(formatTime(latestSuggestion.created_at))}</p></div>
      </div>`;
      return;
    }
    const latest = state.latestDecision;
    if (!latest) {
      root.innerHTML = `<div class="empty-state"><div><strong>${escapeHtml(t("no_decisions"))}</strong><br /><span>${escapeHtml(t("no_decisions_hint"))}</span></div></div>`;
      return;
    }
    const outcome = ({ silent: t("silent"), suggest: t("suggest"), discarded: t("discarded"), source_suggestion_completed: t("source_suggestion_completed"), untracked: t("untracked") }[latest.outcome]) || `${currentLanguage() === "zh" ? "本轮决策：" : "Decision: "}${latest.outcome || t("decision_recorded")}`;
    root.innerHTML = `<div class="decision-card">
      <div class="decision-icon">✓</div>
      <div><h3>${escapeHtml(outcome)}</h3>
      <p>${escapeHtml(decisionDisplayReason(latest))}</p>
      <p class="decision-time">${escapeHtml(formatTime(latest.recorded_at))}</p></div>
    </div>`;
  }

  function renderSuggestionCard(item) {
    return `<article class="suggestion-card">
      <div class="suggestion-meta"><span class="status-badge status-pending">${statusLabel(item.status)}</span><span>${escapeHtml(item.platform || "ACP")}</span><span>·</span><span>${escapeHtml(formatTime(item.created_at))}</span></div>
      <h3>${escapeHtml(item.title)}</h3>
      <div class="suggestion-action">
        <span class="suggestion-action-label">${escapeHtml(t("suggestion_action"))}</span>
        <p>${escapeHtml(item.suggested_action || t("continue_current"))}</p>
      </div>
      <div class="suggestion-actions">
        <button class="action-button ignore-button" data-choice="ignore" data-id="${escapeHtml(item.suggestion_id)}">${escapeHtml(t("ignore"))}</button>
        <button class="action-button approve-button" data-choice="approve" data-id="${escapeHtml(item.suggestion_id)}">${escapeHtml(t("approve_continue"))}</button>
      </div>
    </article>`;
  }

  function renderProjects() {
    const root = document.getElementById("projects-content");
    const projects = state.projects;
    const itemCount = projects.reduce((total, project) => total + project.items.length, 0);
    document.getElementById("project-summary").textContent = t("status_count", { projects: projects.length, items: itemCount });
    if (!projects.length) {
      root.innerHTML = `<div class="empty-state"><div><strong>${escapeHtml(t("no_project"))}</strong><br /><span>${escapeHtml(t("no_project_hint"))}</span></div></div>`;
      return;
    }
    root.innerHTML = projects.map(renderProject).join("");
    root.querySelectorAll("[data-project-toggle]").forEach((button) => {
      button.addEventListener("click", () => toggleProject(button));
      restoreProject(button);
    });
    root.querySelectorAll("[data-events]").forEach((button) => {
      button.addEventListener("click", () => toggleEvents(button));
      restoreEvents(button);
    });
  }

  function captureProjectsFocus() {
    const active = document.activeElement;
    if (!active || !active.closest("#projects-content")) return null;
    if (active.matches("[data-project-toggle]")) {
      return { type: "project", project: active.dataset.project };
    }
    if (active.matches("[data-events]")) {
      return { type: "events", project: active.dataset.project, item: active.dataset.item };
    }
    if (active.matches("[data-event-detail]")) {
      return { type: "event", itemKey: active.dataset.itemKey, event: active.dataset.eventId };
    }
    return null;
  }

  function restoreProjectsFocus(target) {
    if (!target) return;
    const root = document.getElementById("projects-content");
    if (!root) return;
    let element = null;
    if (target.type === "project") {
      element = [...root.querySelectorAll("[data-project-toggle]")]
        .find((candidate) => candidate.dataset.project === target.project);
    } else if (target.type === "events") {
      element = [...root.querySelectorAll("[data-events]")]
        .find((candidate) => candidate.dataset.project === target.project && candidate.dataset.item === target.item);
    } else if (target.type === "event") {
      element = [...root.querySelectorAll("[data-event-detail]")]
        .find((candidate) => candidate.dataset.itemKey === target.itemKey && candidate.dataset.eventId === target.event);
    }
    if (element && typeof element.focus === "function") element.focus({ preventScroll: true });
  }

  function renderProject(project) {
    const items = project.items.length
      ? project.items.map((item) => renderItem(project.id, item)).join("")
      : `<div class="empty-state">${escapeHtml(t("no_items"))}</div>`;
    const hasItems = project.items.length > 0;
    const expanded = state.expandedProjects.has(project.id);
    const itemLabel = hasItems ? (expanded ? t("collapse_items") : t("view_items", { count: project.items.length })) : t("no_items");
    const itemsId = `items-${encodeURIComponent(project.id)}`;
    const projectProgress = progressWidth(project);
    return `<article class="project-card">
      <div class="project-card-top"><button class="project-toggle" data-project-toggle data-project="${escapeHtml(project.id)}" data-item-count="${project.items.length}" type="button" aria-expanded="${expanded ? "true" : "false"}" aria-controls="${escapeHtml(itemsId)}" ${hasItems ? "" : "disabled"}>
        <span class="project-toggle-chevron" aria-hidden="true">${expanded ? "⌃" : "⌄"}</span>
        <span class="project-title-wrap"><span class="entity-label entity-label-project"><span class="entity-label-key">PROJECT</span><span class="entity-label-name">${currentLanguage() === "zh" ? "项目" : "Project"}</span></span><span class="project-title-row"><span class="project-name" role="heading" aria-level="3">${escapeHtml(project.name)}</span><span class="project-item-count">${escapeHtml(itemLabel)}</span></span><span class="project-summary" title="${escapeHtml(project.summary)}">${escapeHtml(project.summary)}</span></span>
      </button><span class="status-badge project-status status-${escapeHtml(project.status)}">${statusLabel(project.status)}</span></div>
      <div class="project-progress-line" role="progressbar" aria-label="${escapeHtml(t("project_progress"))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${projectProgress}" aria-valuetext="${escapeHtml(`${statusLabel(project.status)} ${projectProgress}%`)}"><div class="project-progress-bar" style="width:${projectProgress}%"></div></div>
      <div id="${escapeHtml(itemsId)}" class="items-list${expanded ? " open" : ""}" data-items-root="${escapeHtml(project.id)}" aria-hidden="${expanded ? "false" : "true"}">${items}</div>
    </article>`;
  }

  function setProjectOpen(button, open) {
    const card = button.closest(".project-card");
    const items = card?.querySelector("[data-items-root]");
    if (!card || !items) return;
    items.classList.toggle("open", open);
    items.setAttribute("aria-hidden", open ? "false" : "true");
    button.setAttribute("aria-expanded", open ? "true" : "false");
    const chevron = button.querySelector(".project-toggle-chevron");
    if (chevron) chevron.textContent = open ? "⌃" : "⌄";
    const label = button.querySelector(".project-item-count");
    if (label) {
      const count = button.dataset.itemCount || "0";
      label.textContent = open ? t("collapse_items") : t("view_items", { count });
    }
  }

  function restoreProject(button) {
    const projectId = button.dataset.project;
    if (!projectId || !state.expandedProjects.has(projectId)) return;
    setProjectOpen(button, true);
  }

  function toggleProject(button) {
    const projectId = button.dataset.project;
    if (!projectId || button.disabled) return;
    const open = !state.expandedProjects.has(projectId);
    if (open) state.expandedProjects.add(projectId);
    else state.expandedProjects.delete(projectId);
    setProjectOpen(button, open);
  }

  function renderItem(projectId, item) {
    const blocker = item.blocker || (currentLanguage() === "zh" ? "无" : "None");
    const eventsId = `events-${encodeURIComponent(projectId)}-${encodeURIComponent(item.id)}`;
    return `<div class="item-card">
      <div class="item-card-main">
        <div class="item-card-body">
          <div class="item-heading"><div class="item-title-wrap"><div class="entity-label entity-label-item"><span class="entity-label-key">ITEM</span><span class="entity-label-name">${escapeHtml(currentLanguage() === "zh" ? "事项" : "Item")}</span></div><h4>${escapeHtml(item.name)}</h4></div><span class="item-status item-status-${escapeHtml(item.status)}">${statusLabel(item.status)}</span></div>
          <p class="item-progress" title="${escapeHtml(item.current_progress)}">${escapeHtml(item.current_progress)}</p>
          <div class="item-meta"><div class="meta-block"><small>${escapeHtml(t("next_step"))}</small><span title="${escapeHtml(item.next_step)}">${escapeHtml(item.next_step)}</span></div><div class="meta-block"><small>${escapeHtml(currentLanguage() === "zh" ? "阻塞" : "Blocker")}</small><span class="${item.blocker ? "blocker-text" : ""}" title="${escapeHtml(blocker)}">${escapeHtml(blocker)}</span></div></div>
        </div>
        ${renderItemProgressBar(item)}
      </div>
      <div class="item-card-footer">
        <button class="event-toggle" data-events data-project="${escapeHtml(projectId)}" data-item="${escapeHtml(item.id)}" data-event-count="${item.event_count || 0}" aria-expanded="false" aria-controls="${escapeHtml(eventsId)}">${escapeHtml(t("event_count", { count: item.event_count || 0 }))}</button>
        <div id="${escapeHtml(eventsId)}" class="events" data-events-root="${escapeHtml(projectId)}/${escapeHtml(item.id)}"></div>
      </div>
    </div>`;
  }

  function renderItemProgressBar(item) {
    const stages = currentLanguage() === "zh" ? ["未开始", "计划中", "进行中", "已完成"] : ["Not started", "Planned", "In progress", "Completed"];
    const stageByStatus = { planned: 1, in_progress: 2, blocked: 2, completed: 3 };
    const activeIndex = Object.prototype.hasOwnProperty.call(stageByStatus, item.status)
      ? stageByStatus[item.status]
      : 0;
    const blocked = item.status === "blocked";
    const currentLabel = blocked ? `${stages[2]} (${t("blocked_short")})` : stages[activeIndex];
    const segments = stages.map((label, index) => {
      const phase = index < activeIndex ? "done" : index === activeIndex ? "current" : "upcoming";
      const blockedClass = blocked && index === 2 ? " item-progress-segment-blocked" : "";
      return `<span class="item-progress-segment item-progress-segment-${phase}${blockedClass}" aria-hidden="true"></span>`;
    }).join("");
    const labels = stages.map((label, index) => {
      const currentAttribute = index === activeIndex ? ' aria-current="step"' : "";
      return `<span class="item-progress-label${index === activeIndex ? " is-current" : ""}"${currentAttribute}>${label}</span>`;
    }).join("");
    return `<div class="item-progress-widget" role="group" aria-label="${escapeHtml(t("item_progress"))}: ${escapeHtml(currentLabel)}">
      <div class="item-progress-widget-head"><span class="item-progress-widget-title">${escapeHtml(t("progress"))}</span><strong>${escapeHtml(currentLabel)}</strong>${blocked ? `<span class="item-progress-warning">${escapeHtml(t("blocked_short"))}</span>` : ""}</div>
      <div class="item-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="3" aria-valuenow="${activeIndex}" aria-valuetext="${escapeHtml(currentLabel)}">${segments}</div>
      <div class="item-progress-labels">${labels}</div>
    </div>`;
  }

  function itemKey(projectId, itemId) {
    return `${projectId}/${itemId}`;
  }

  function eventDetailKey(itemKeyValue, eventId) {
    return `${itemKeyValue}/${eventId}`;
  }

  function setEventsOpen(button, open) {
    const container = button.parentElement.querySelector(".events");
    container.classList.toggle("open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      button.textContent = t("event_count_up", { count: button.dataset.eventCount || "0" });
    } else {
      button.textContent = t("event_count", { count: button.dataset.eventCount || "0" });
    }
  }

  function bindEventDetails(container, itemKeyValue) {
    container.querySelectorAll("[data-event-detail]").forEach((row) => {
      const detailKey = eventDetailKey(itemKeyValue, row.dataset.eventId);
      const setOpen = (open) => {
        row.classList.toggle("expanded", open);
        row.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) state.expandedEventDetails.add(detailKey);
        else state.expandedEventDetails.delete(detailKey);
      };
      setOpen(state.expandedEventDetails.has(detailKey));
      row.addEventListener("click", () => {
        const expanded = row.classList.toggle("expanded");
        setOpen(expanded);
      });
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        setOpen(!row.classList.contains("expanded"));
      });
    });
  }

  function renderEventList(container, events, itemKeyValue) {
    container.innerHTML = events.length
      ? events.map((event, index) => renderEvent(event, itemKeyValue, index)).join("")
      : `<div class="muted-label">${escapeHtml(t("no_events"))}</div>`;
    container.dataset.loaded = "1";
    bindEventDetails(container, itemKeyValue);
  }

  function restoreEvents(button) {
    const key = itemKey(button.dataset.project, button.dataset.item);
    if (!state.expandedItems.has(key)) return;
    const container = button.parentElement.querySelector(".events");
    const cachedEvents = state.eventCache.get(key);
    if (cachedEvents) {
      renderEventList(container, cachedEvents, key);
      setEventsOpen(button, true);
      return;
    }
    // The first request may still be in flight when the polling refresh
    // rebuilds the card. Reattach the result to the new DOM node when it is
    // available instead of losing the user's open state.
    void loadEvents(button).then(() => {
      if (state.expandedItems.has(key) && button.isConnected) setEventsOpen(button, true);
    }).catch(() => {});
  }

  async function loadEvents(button) {
    const projectId = button.dataset.project;
    const itemId = button.dataset.item;
    const key = itemKey(projectId, itemId);
    const container = button.parentElement.querySelector(".events");
    if (state.eventCache.has(key)) {
      renderEventList(container, state.eventCache.get(key), key);
      return state.eventCache.get(key);
    }

    if (!state.eventLoads.has(key)) {
      container.innerHTML = `<div class="loading-state"><span class="spinner"></span>${escapeHtml(t("loading_history"))}</div>`;
      const request = fetch(`/api/projects/${encodeURIComponent(projectId)}/items/${encodeURIComponent(itemId)}/events?limit=12`)
        .then((response) => {
          if (!response.ok) throw new Error("events request failed");
          return response.json();
        })
        .then((data) => {
          const events = data.events || [];
          state.eventCache.set(key, events);
          return events;
        });
      state.eventLoads.set(key, request);
    }

    const request = state.eventLoads.get(key);
    try {
      const events = await request;
      if (button.isConnected) renderEventList(container, events, key);
      return events;
    } catch (error) {
      if (button.isConnected) container.innerHTML = `<div class="error-state">${escapeHtml(t("events_failed"))}</div>`;
      throw error;
    } finally {
      if (state.eventLoads.get(key) === request) state.eventLoads.delete(key);
    }
  }

  async function toggleEvents(button) {
    const key = itemKey(button.dataset.project, button.dataset.item);
    const container = button.parentElement.querySelector(".events");
    if (container.classList.contains("open")) {
      state.expandedItems.delete(key);
      setEventsOpen(button, false);
      return;
    }
    state.expandedItems.add(key);
    try {
      await loadEvents(button);
      if (state.expandedItems.has(key) && button.isConnected) setEventsOpen(button, true);
    } catch (_error) {
      // The error state is rendered by loadEvents; keep the disclosure choice
      // so the next refresh can retry without changing other project state.
    }
  }

  function renderEvent(event, itemKeyValue, index) {
    const source = event.source || {};
    const eventId = event.id || `${event.completed_at || "event"}-${index}`;
    const detailId = `event-detail-${encodeURIComponent(itemKeyValue)}-${encodeURIComponent(eventId)}`;
    return `<div class="event-row" data-event-detail data-event-id="${escapeHtml(eventId)}" data-item-key="${escapeHtml(itemKeyValue)}" role="button" tabindex="0" aria-expanded="false" aria-controls="${escapeHtml(detailId)}"><div class="event-summary"><strong>${escapeHtml(event.summary || event.id)}</strong><span class="muted-label">${escapeHtml(formatTime(event.completed_at))}</span></div><div class="event-meta"><span>${escapeHtml(source.platform || t("unknown_source"))} · ${escapeHtml(source.session_id || t("unknown_source"))}</span><span>${escapeHtml(t("view_qa"))}</span></div><div id="${escapeHtml(detailId)}" class="event-detail"><b>${escapeHtml(t("question"))}：</b>${escapeHtml(event.question || "—")}\n\n<b>${escapeHtml(t("answer"))}：</b>${escapeHtml(event.answer || "—")}</div></div>`;
  }

  async function respond(id, choice, button) {
    if (state.pendingResponses.has(id)) return;
    state.pendingResponses.add(id);
    const buttons = button.parentElement.querySelectorAll("button");
    buttons.forEach((item) => { item.disabled = true; });
    try {
      const response = await fetch("/v1/events/suggestion.responded", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ suggestion_id: id, choice, responded_at: new Date().toISOString() }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || body.error || t("connection_failed"));
      showToast(choice === "approve" ? t("accepted_continue") : t("ignored_no_execute"));
      await refresh();
    } catch (error) {
      buttons.forEach((item) => { item.disabled = false; });
      showToast(t("submit_failed", { error: error.message }), true);
    } finally {
      state.pendingResponses.delete(id);
      if (!state.connectionFailed) renderSuggestion();
    }
  }

  let toastTimer;
  function showToast(message, isError) {
    const toast = document.getElementById("toast");
    toast.textContent = message;
    toast.classList.toggle("toast-error", Boolean(isError));
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 3600);
  }

  async function refresh() {
    try {
      const focusedProjectsElement = captureProjectsFocus();
      const response = await fetch(`/api/dashboard?ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error("dashboard request failed");
      const data = await response.json();
      state.projects = data.projects || [];
      state.harnesses = data.harnesses || [];
      state.suggestions = data.suggestions || [];
      state.latestDecision = data.latest_decision;
      state.loaded = true;
      state.connectionFailed = false;
      state.generatedAt = data.generated_at;
      renderSuggestion();
      renderProjects();
      renderHarnesses();
      restoreProjectsFocus(focusedProjectsElement);
      await loadDailyReport(data);
      syncOutputLanguage();
      renderConnectionState();
    } catch (_error) {
      state.connectionFailed = true;
      renderConnectionState();
      renderConnectionError();
    }
  }

  document.getElementById("daily-report-open")?.addEventListener("click", () => openDailyReport());
  document.getElementById("daily-report-close")?.addEventListener("click", closeDailyReport);
  document.getElementById("daily-report-done")?.addEventListener("click", closeDailyReport);
  document.querySelector("[data-report-close]")?.addEventListener("click", closeDailyReport);
  document.getElementById("language-select")?.addEventListener("change", (event) => {
    const value = event.target.value;
    state.language = value === "zh" || value === "en" ? value : "auto";
    try {
      if (state.language === "auto") window.localStorage.removeItem(languageStorageKey);
      else window.localStorage.setItem(languageStorageKey, state.language);
    } catch (_error) {
      // Language selection still applies for this page when storage is unavailable.
    }
    redrawLanguage();
  });
  window.addEventListener("languagechange", () => {
    if (state.language === "auto") redrawLanguage();
  });
  document.addEventListener("click", (event) => {
    const settings = document.getElementById("language-settings");
    if (settings && !settings.contains(event.target)) settings.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const settings = document.getElementById("language-settings");
      if (settings?.open) {
        settings.open = false;
        settings.querySelector("summary")?.focus();
        return;
      }
    }
    if (event.key === "Escape" && state.dailyReportOpen) {
      event.preventDefault();
      closeDailyReport();
      return;
    }
    if (event.key !== "Tab" || !state.dailyReportOpen) return;
    const modal = document.getElementById("daily-report-modal");
    if (!modal) return;
    const focusable = [...modal.querySelectorAll("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])")]
      .filter((element) => !element.hidden && element.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  });

  applyLanguage();
  refresh();
  window.setInterval(refresh, 2200);
})();
