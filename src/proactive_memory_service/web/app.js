(function () {
  "use strict";

  const state = {
    projects: [],
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
  };
  const labels = {
    active: "进行中",
    paused: "已暂停",
    completed: "已完成",
    planned: "计划中",
    in_progress: "进行中",
    blocked: "有阻塞",
    pending: "待你决定",
    approved: "已接受",
    ignored: "已忽略",
    resuming: "正在续跑",
    failed: "续跑失败",
  };
  const decisionLabels = {
    silent: "本轮暂不打扰",
    suggest: "建议已生成",
    discarded: "旧判断已丢弃",
    source_suggestion_completed: "建议已执行",
    untracked: "本轮未进入项目状态",
  };
  const dailyReportSeenPrefix = "proactive-memory:daily-report-seen:";

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
    return new Intl.DateTimeFormat("zh-CN", {
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
    return match ? `${Number(match[1])} 年 ${Number(match[2])} 月 ${Number(match[3])} 日` : "今日";
  }

  function reportSeenKey(report) {
    return `${dailyReportSeenPrefix}${report?.report_id || report?.id || report?.date || localDateKey()}`;
  }

  function hasSeenDailyReport(report) {
    try {
      return window.localStorage.getItem(reportSeenKey(report)) === "1";
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
    return project.name || project.project_name || project.projectName || project.project_id || project.id || "未命名项目";
  }

  function reportProjectSummary(project) {
    return project.summary || project.current_progress || project.progress || project.highlight || "这一天暂无新的进展记录。";
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
      subtitle.textContent = "每天 0 点后整理，打开页面即可查看。";
      content.innerHTML = '<div class="report-empty"><span class="report-empty-icon" aria-hidden="true"></span><strong>还没有可展示的日报</strong><span>有新的项目进展后，日报会在这里出现。</span></div>';
      return;
    }
    subtitle.textContent = `${reportDateLabel(report.date)} · ${report.projects.length} 个 Project`;
    if (!report.projects.length) {
      content.innerHTML = '<div class="report-empty"><span class="report-empty-icon" aria-hidden="true"></span><strong>这一天暂无项目进展</strong><span>有新的记录后，日报会自动补充。</span></div>';
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
          ${nextStep ? `<span><b>下一步</b>${escapeHtml(nextStep)}</span>` : ""}
          ${itemCount == null ? "" : `<span>${escapeHtml(String(itemCount))} 个 Item</span>`}
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

  function statusLabel(status) { return labels[status] || status || "未知"; }

  function decisionDisplayReason(decision) {
    const messages = {
      silent: "本轮状态已更新，暂不打扰。",
      suggest: "建议已生成，等待你的决定。",
      discarded: "本轮判断已过期，状态更新仍已保留。",
      source_suggestion_completed: "建议动作已执行，结果已回流。",
      untracked: "本轮没有进入项目状态。",
    };
    return messages[decision?.outcome] || "本轮决策已记录。";
  }

  function progressWidth(project) {
    if (!project.items.length) return 0;
    const weights = { planned: 18, in_progress: 58, blocked: 42, completed: 100 };
    return Math.round(project.items.reduce((total, item) => total + (weights[item.status] || 30), 0) / project.items.length);
  }

  function renderSuggestion() {
    const root = document.getElementById("suggestion-content");
    const count = state.suggestions.filter((item) => item.status === "pending").length;
    document.getElementById("suggestion-count").textContent = `${count} 条待处理`;
    const pending = state.suggestions.filter((item) => item.status === "pending");
    if (pending.length) {
      root.innerHTML = pending.map(renderSuggestionCard).join("");
      root.querySelectorAll("[data-choice]").forEach((button) => {
        button.addEventListener("click", () => respond(button.dataset.id, button.dataset.choice, button));
      });
      return;
    }
    const latestSuggestion = state.suggestions[0];
    if (latestSuggestion && latestSuggestion.status !== "pending") {
      const statusTitle = {
        completed: "最近建议已执行",
        ignored: "最近建议已忽略",
        resuming: "最近建议正在续跑",
        approved: "最近建议已接受",
        failed: "最近建议续跑失败",
      }[latestSuggestion.status] || "最近建议已处理";
      const statusReason = latestSuggestion.status === "failed"
        ? "续跑失败，详细原因已写入 runtime.jsonl。"
        : latestSuggestion.status === "ignored"
          ? "用户选择忽略，系统没有执行建议动作。"
          : latestSuggestion.status === "completed"
            ? "用户已授权，原 ACP Session 已完成建议动作并回流结果。"
            : "用户已授权，系统正在处理原 ACP Session。";
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
      root.innerHTML = '<div class="empty-state"><div><strong>还没有决策记录</strong><br /><span>完成一轮对话后，这里会显示 silent 或 suggestion.ready。</span></div></div>';
      return;
    }
    const outcome = decisionLabels[latest.outcome] || `本轮决策：${latest.outcome || "已记录"}`;
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
        <span class="suggestion-action-label">建议操作</span>
        <p>${escapeHtml(item.suggested_action || "请继续推进当前事项。")}</p>
      </div>
      <div class="suggestion-actions">
        <button class="action-button ignore-button" data-choice="ignore" data-id="${escapeHtml(item.suggestion_id)}">忽略</button>
        <button class="action-button approve-button" data-choice="approve" data-id="${escapeHtml(item.suggestion_id)}">接受并继续</button>
      </div>
    </article>`;
  }

  function renderProjects() {
    const root = document.getElementById("projects-content");
    const projects = state.projects;
    const itemCount = projects.reduce((total, project) => total + project.items.length, 0);
    document.getElementById("project-summary").textContent = `${projects.length} 个 Project · ${itemCount} 个 Item`;
    if (!projects.length) {
      root.innerHTML = '<div class="empty-state"><div><strong>还没有项目</strong><br /><span>收到第一轮可归属的 QA 后，Project 会出现在这里。</span></div></div>';
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
      : '<div class="empty-state">暂无 Item</div>';
    const hasItems = project.items.length > 0;
    const expanded = state.expandedProjects.has(project.id);
    const itemLabel = hasItems ? (expanded ? "收起 Item" : `查看 ${project.items.length} 个 Item`) : "暂无 Item";
    const itemsId = `items-${encodeURIComponent(project.id)}`;
    const projectProgress = progressWidth(project);
    return `<article class="project-card">
      <div class="project-card-top"><button class="project-toggle" data-project-toggle data-project="${escapeHtml(project.id)}" data-item-count="${project.items.length}" type="button" aria-expanded="${expanded ? "true" : "false"}" aria-controls="${escapeHtml(itemsId)}" ${hasItems ? "" : "disabled"}>
        <span class="project-toggle-chevron" aria-hidden="true">${expanded ? "⌃" : "⌄"}</span>
        <span class="project-title-wrap"><span class="entity-label entity-label-project"><span class="entity-label-key">PROJECT</span><span class="entity-label-name">项目</span></span><span class="project-title-row"><span class="project-name" role="heading" aria-level="3">${escapeHtml(project.name)}</span><span class="project-item-count">${escapeHtml(itemLabel)}</span></span><span class="project-summary" title="${escapeHtml(project.summary)}">${escapeHtml(project.summary)}</span></span>
      </button><span class="status-badge project-status status-${escapeHtml(project.status)}">${statusLabel(project.status)}</span></div>
      <div class="project-progress-line" role="progressbar" aria-label="项目进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${projectProgress}" aria-valuetext="${escapeHtml(`${statusLabel(project.status)} ${projectProgress}%`)}"><div class="project-progress-bar" style="width:${projectProgress}%"></div></div>
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
      label.textContent = open ? "收起 Item" : `查看 ${count} 个 Item`;
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
    const blocker = item.blocker || "无";
    const eventsId = `events-${encodeURIComponent(projectId)}-${encodeURIComponent(item.id)}`;
    return `<div class="item-card">
      <div class="item-card-main">
        <div class="item-card-body">
          <div class="item-heading"><div class="item-title-wrap"><div class="entity-label entity-label-item"><span class="entity-label-key">ITEM</span><span class="entity-label-name">事项</span></div><h4>${escapeHtml(item.name)}</h4></div><span class="item-status item-status-${escapeHtml(item.status)}">${statusLabel(item.status)}</span></div>
          <p class="item-progress" title="${escapeHtml(item.current_progress)}">${escapeHtml(item.current_progress)}</p>
          <div class="item-meta"><div class="meta-block"><small>下一步</small><span title="${escapeHtml(item.next_step)}">${escapeHtml(item.next_step)}</span></div><div class="meta-block"><small>阻塞</small><span class="${item.blocker ? "blocker-text" : ""}" title="${escapeHtml(blocker)}">${escapeHtml(blocker)}</span></div></div>
        </div>
        ${renderItemProgressBar(item)}
      </div>
      <div class="item-card-footer">
        <button class="event-toggle" data-events data-project="${escapeHtml(projectId)}" data-item="${escapeHtml(item.id)}" aria-expanded="false" aria-controls="${escapeHtml(eventsId)}">查看 ${item.event_count || 0} 条 Event ↓</button>
        <div id="${escapeHtml(eventsId)}" class="events" data-events-root="${escapeHtml(projectId)}/${escapeHtml(item.id)}"></div>
      </div>
    </div>`;
  }

  function renderItemProgressBar(item) {
    const stages = ["未开始", "计划中", "进行中", "已完成"];
    const stageByStatus = { planned: 1, in_progress: 2, blocked: 2, completed: 3 };
    const activeIndex = Object.prototype.hasOwnProperty.call(stageByStatus, item.status)
      ? stageByStatus[item.status]
      : 0;
    const blocked = item.status === "blocked";
    const currentLabel = blocked ? "进行中（阻塞）" : stages[activeIndex];
    const segments = stages.map((label, index) => {
      const phase = index < activeIndex ? "done" : index === activeIndex ? "current" : "upcoming";
      const blockedClass = blocked && index === 2 ? " item-progress-segment-blocked" : "";
      return `<span class="item-progress-segment item-progress-segment-${phase}${blockedClass}" aria-hidden="true"></span>`;
    }).join("");
    const labels = stages.map((label, index) => {
      const currentAttribute = index === activeIndex ? ' aria-current="step"' : "";
      return `<span class="item-progress-label${index === activeIndex ? " is-current" : ""}"${currentAttribute}>${label}</span>`;
    }).join("");
    return `<div class="item-progress-widget" role="group" aria-label="事项进度：${escapeHtml(currentLabel)}">
      <div class="item-progress-widget-head"><span class="item-progress-widget-title">进度</span><strong>${escapeHtml(currentLabel)}</strong>${blocked ? '<span class="item-progress-warning">阻塞</span>' : ""}</div>
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
      button.textContent = button.textContent.replace("查看", "收起").replace("↓", "↑");
    } else {
      button.textContent = button.textContent.replace("↑", "↓").replace("收起", "查看");
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
      : '<div class="muted-label">暂无历史 Event</div>';
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
      container.innerHTML = '<div class="loading-state"><span class="spinner"></span>正在读取历史…</div>';
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
      if (button.isConnected) container.innerHTML = '<div class="error-state">读取 Event 失败，请稍后重试。</div>';
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
    return `<div class="event-row" data-event-detail data-event-id="${escapeHtml(eventId)}" data-item-key="${escapeHtml(itemKeyValue)}" role="button" tabindex="0" aria-expanded="false" aria-controls="${escapeHtml(detailId)}"><div class="event-summary"><strong>${escapeHtml(event.summary || event.id)}</strong><span class="muted-label">${escapeHtml(formatTime(event.completed_at))}</span></div><div class="event-meta"><span>${escapeHtml(source.platform || "unknown")} · ${escapeHtml(source.session_id || "unknown")}</span><span>点击查看 QA</span></div><div id="${escapeHtml(detailId)}" class="event-detail"><b>问题：</b>${escapeHtml(event.question || "—")}\n\n<b>回答：</b>${escapeHtml(event.answer || "—")}</div></div>`;
  }

  async function respond(id, choice, button) {
    const buttons = button.parentElement.querySelectorAll("button");
    buttons.forEach((item) => { item.disabled = true; });
    try {
      const response = await fetch("/v1/events/suggestion.responded", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ suggestion_id: id, choice, responded_at: new Date().toISOString() }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || body.error || "提交失败");
      showToast(choice === "approve" ? "已接受，正在继续原 ACP Session。" : "已忽略，这条建议不会执行。");
      await refresh();
    } catch (error) {
      buttons.forEach((item) => { item.disabled = false; });
      showToast(`提交失败：${error.message}`, true);
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
      state.suggestions = data.suggestions || [];
      state.latestDecision = data.latest_decision;
      renderSuggestion();
      renderProjects();
      restoreProjectsFocus(focusedProjectsElement);
      await loadDailyReport(data);
      document.getElementById("last-updated").textContent = `更新于 ${formatTime(data.generated_at)}`;
    } catch (_error) {
      document.getElementById("last-updated").textContent = "服务连接失败";
      document.getElementById("suggestion-content").innerHTML = '<div class="error-state">无法连接 Proactive Memory 服务。</div>';
      document.getElementById("projects-content").innerHTML = '<div class="error-state">请确认服务已启动，并刷新页面。</div>';
    }
  }

  document.getElementById("daily-report-open")?.addEventListener("click", () => openDailyReport());
  document.getElementById("daily-report-close")?.addEventListener("click", closeDailyReport);
  document.getElementById("daily-report-done")?.addEventListener("click", closeDailyReport);
  document.querySelector("[data-report-close]")?.addEventListener("click", closeDailyReport);
  document.addEventListener("keydown", (event) => {
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

  refresh();
  window.setInterval(refresh, 2200);
})();
