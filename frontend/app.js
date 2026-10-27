(() => {
  "use strict";

  const API = "/api/v1";
  const STAGE_TIMEOUT_MS = 20 * 60 * 1000;
  const REQUEST_TIMEOUT_MS = 45 * 1000;

  // Keep this registry aligned with backend/app/services/workflow_stages.py.
  const STAGES = [
    {
      key: "collection",
      label: "Collect news",
      description: "Fetch configured RSS feeds and normalize new articles.",
      agentName: "news_collector",
    },
    {
      key: "deduplication",
      label: "Deduplicate",
      description: "Cluster syndicated and near-identical reports.",
      agentName: "deduplication",
    },
    {
      key: "credibility",
      label: "Check credibility",
      description: "Score corroboration and flag unsupported claims.",
      agentName: "credibility",
    },
    {
      key: "research",
      label: "Build research",
      description: "Create an evidence brief with source context.",
      agentName: "research",
    },
    {
      key: "script",
      label: "Write scripts",
      description: "Draft neutral narration from the verified brief.",
      agentName: "script_writer",
    },
    {
      key: "scenes",
      label: "Plan scenes",
      description: "Break the script into timed visual scenes.",
      agentName: "scene_planner",
    },
    {
      key: "assets",
      label: "Source visuals",
      description: "Retrieve licensed imagery or record a designed fallback.",
      agentName: "asset_retriever",
    },
    {
      key: "tts",
      label: "Narrate",
      description: "Synthesize mastered narration with sentence and paragraph pacing.",
      agentName: "tts",
    },
    {
      key: "video",
      label: "Render video",
      description: "Compose scene graphics, motion, captions, and audio into MP4 variants.",
      agentName: "video_generator",
    },
    {
      key: "publishing",
      label: "Process publishing",
      description: "Send approved renders to configured platforms.",
      agentName: "publishing",
    },
  ];

  const STAGE_BY_KEY = Object.fromEntries(STAGES.map((stage) => [stage.key, stage]));
  const STAGE_BY_AGENT = Object.fromEntries(
    STAGES.map((stage) => [stage.agentName, stage]),
  );
  // Collection and publishing are explicit actions; advancing the pipeline moves
  // verified work from deduplication through the rendered draft.
  const PIPELINE_KEYS = STAGES.filter(
    (stage) => stage.key !== "collection" && stage.key !== "publishing",
  ).map((stage) => stage.key);

  const VIEWS = {
    overview: {
      path: "overview",
      eyebrow: "Operations",
      title: "Command center",
      description: "What is happening, what needs attention, and what to do next.",
    },
    production: {
      path: "production",
      eyebrow: "Production studio",
      title: "Generate a complete news video",
      description:
        "One action builds the script, scenes, sourced visuals, mastered narration, and 16:9 + 9:16 renders.",
    },
    queue: {
      path: "review",
      eyebrow: "Editorial desk",
      title: "Review queue",
      description:
        "Inspect evidence, edit the script and scenes, then record a human decision.",
    },
    runs: {
      path: "runs",
      eyebrow: "Pipeline",
      title: "Agent runs",
      description: "Trigger stages, follow active work, and audit every execution.",
    },
    publishing: {
      path: "publishing",
      eyebrow: "Distribution",
      title: "Publishing",
      description:
        "Queue approved artifacts and process local exports or external delivery.",
    },
    articles: {
      path: "articles",
      eyebrow: "Library",
      title: "Articles",
      description: "Source evidence collected from configured feeds.",
    },
  };

  const VIEW_BY_PATH = Object.fromEntries(
    Object.entries(VIEWS).map(([view, meta]) => [meta.path, view]),
  );

  const state = {
    view: "overview",
    booted: false,
    overview: null,
    queue: [],
    selectedId: null,
    detail: null,
    detailTab: "story",
    detailDirty: false,
    runs: [],
    runsVisible: 40,
    articles: [],
    articlesVisible: 24,
    refreshing: false,
    pipelineBusy: false,
    stageBusy: new Set(),
    stageErrors: new Map(),
    lastQueueFilter: "",
    production: {
      config: null,
      stories: [],
      jobs: [],
      job: null,
      selectedJobId: null,
      pollTimer: null,
      loading: false,
    },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function parseDate(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    let text = String(value);
    // SQLite stores naive timestamps that are actually UTC.
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(text)) text += "Z";
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function dateMs(value) {
    const date = parseDate(value);
    return date ? date.getTime() : null;
  }

  function fmtDate(value) {
    const date = parseDate(value);
    if (!date) return "—";
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function fmtRelative(value) {
    const date = parseDate(value);
    if (!date) return "—";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 45) return "just now";
    if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`;
    if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
    if (seconds < 604_800) return `${Math.round(seconds / 86_400)}d ago`;
    return fmtDate(date);
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const mins = Math.floor(total / 60);
    const secs = String(total % 60).padStart(2, "0");
    return `${mins}:${secs}`;
  }

  function fmtRunDuration(ms) {
    if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
    const value = Math.max(0, Number(ms));
    if (value < 1_000) return `${Math.round(value)} ms`;
    if (value < 60_000) {
      return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`;
    }
    const minutes = Math.floor(value / 60_000);
    const seconds = Math.round((value % 60_000) / 1_000);
    if (minutes < 60) return `${minutes}m ${seconds}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  function titleCase(value) {
    return String(value || "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function stageLabel(agentName) {
    return STAGE_BY_AGENT[agentName]?.label || titleCase(agentName || "unknown");
  }

  const STATUS_TONES = {
    ok: "success",
    completed: "success",
    approved: "success",
    published: "success",
    exported: "success",
    corroborated: "success",
    pending: "warning",
    ready: "warning",
    warning: "warning",
    needs_review: "warning",
    changes_requested: "warning",
    completed_with_errors: "warning",
    failed: "danger",
    blocked: "danger",
    rejected: "danger",
    error: "danger",
    stalled: "danger",
    running: "info",
    queued: "info",
    processing: "info",
    awaiting_approval: "warning",
    draft: "muted",
    idle: "muted",
    unknown: "muted",
    interrupted: "warning",
    skipped: "muted",
  };

  function statusTone(status) {
    return STATUS_TONES[String(status || "unknown")] || "muted";
  }

  function badge(status, label) {
    const value = String(status || "unknown");
    return `<span class="badge" data-tone="${esc(statusTone(value))}">${esc(
      label || value.replaceAll("_", " "),
    )}</span>`;
  }

  function statusDot(tone) {
    return `<span class="status-dot" data-tone="${esc(tone || "muted")}" aria-hidden="true"></span>`;
  }

  function safeUrl(value) {
    try {
      const parsed = new URL(value, window.location.origin);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
    } catch {
      return null;
    }
  }

  function externalLink(url, label) {
    const href = safeUrl(url);
    if (!href) return `<span class="muted">${esc(label || url || "—")}</span>`;
    return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(
      label || href,
    )}</a>`;
  }

  function internalHref(value) {
    const text = String(value || "");
    return text.startsWith("#/") ? text : null;
  }

  function publishingResultMarkup(record) {
    const href = safeUrl(record.external_url);
    if (href) return externalLink(href, "Open result");
    if (
      record.platform === "local" &&
      ["exported", "published"].includes(record.status)
    ) {
      return `<a href="${API}/publishing/records/${encodeURIComponent(
        record.id,
      )}/file" target="_blank" rel="noopener noreferrer">Open export</a>`;
    }
    const fallback = record.external_id || record.last_error || "—";
    return `<span class="muted" title="${esc(record.external_url || fallback)}">${esc(
      fallback,
    )}</span>`;
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function describeErrorPayload(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (typeof detail !== "object") return String(detail);
    const parts = [detail.message || "Request failed"];
    if (detail.code) parts.push(`(${String(detail.code).replaceAll("_", " ")})`);
    if (Array.isArray(detail.errors) && detail.errors.length) {
      parts.push(
        detail.errors
          .slice(0, 3)
          .map((item) => item.message || item.location || String(item))
          .join("; "),
      );
    }
    if (detail.request_id) parts.push(`req ${detail.request_id}`);
    return parts.filter(Boolean).join(" ");
  }

  function describeError(error) {
    if (!error) return "Unknown error";
    if (typeof error === "string") return error;
    if (error.html) {
      return `${error.message}${error.requestId ? ` (req ${error.requestId})` : ""}`;
    }
    const detail = error.detail;
    if (detail) {
      const described = describeErrorPayload(detail);
      if (described) return described;
    }
    return error.message || "Request failed";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(),
      options.timeout || REQUEST_TIMEOUT_MS,
    );
    const headers = {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    };
    try {
      const response = await fetch(`${API}${path}`, {
        credentials: "same-origin",
        ...options,
        headers,
        signal: controller.signal,
      });
      const text = await response.text();
      const contentType = response.headers.get("content-type") || "";
      const looksLikeJson =
        contentType.includes("application/json") || /^[\[{]/.test(text.trim());
      let payload = null;
      if (text && looksLikeJson) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }
      const htmlResponse =
        contentType.includes("text/html") || text.trim().startsWith("<");
      if (response.ok && htmlResponse) {
        const err = new Error(
          `The server returned an HTML page instead of JSON. This usually means a proxy timeout or upstream outage. Try again, and check the server log if it persists.`,
        );
        err.status = response.status;
        err.html = true;
        err.requestId = response.headers.get("x-request-id");
        throw err;
      }
      if (!response.ok) {
        if (htmlResponse || !payload) {
          const err = new Error(
            `The server returned ${response.status} ${
              response.statusText || "an unexpected response"
            } instead of JSON. This usually means a proxy timeout or upstream outage. Try again, and check the server log if it persists.`,
          );
          err.status = response.status;
          err.html = true;
          err.requestId = response.headers.get("x-request-id");
          throw err;
        }
        const err = new Error(
          describeErrorPayload(payload?.detail) || `Request failed (${response.status})`,
        );
        err.status = response.status;
        err.detail = payload?.detail;
        err.payload = payload;
        err.requestId =
          payload?.detail?.request_id || response.headers.get("x-request-id");
        throw err;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") {
        const err = new Error(
          "The request timed out. The server may still be processing; check Agent runs before retrying.",
        );
        err.timeout = true;
        throw err;
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  const toastTimers = new Map();

  function toast(message, kind = "success", options = {}) {
    const text = String(message || "").trim();
    if (!text) return;
    const region = $("#toast-region");
    if (!region) return;
    const key = `${kind}:${text}`;
    const existing = toastTimers.get(key);
    if (existing) {
      window.clearTimeout(existing.timer);
      existing.timer = window.setTimeout(() => removeToast(key), existing.duration);
      return;
    }

    const duration = options.persist ? 12_000 : kind === "error" ? 9_000 : 5_500;
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.kind = kind;
    node.dataset.toastKey = key;
    const icon = kind === "error" ? "!" : kind === "warning" ? "!" : kind === "info" ? "i" : "✓";
    node.innerHTML = `
      <span class="toast-icon" aria-hidden="true">${esc(icon)}</span>
      <span class="toast-message">${esc(text)}</span>
      <button class="toast-close" type="button" aria-label="Dismiss notification">×</button>`;
    node.querySelector(".toast-close").addEventListener("click", () => removeToast(key));
    region.appendChild(node);

    const nodes = $$(".toast", region);
    if (nodes.length > 4) nodes.slice(0, nodes.length - 4).forEach((item) => item.remove());

    const timer = window.setTimeout(() => removeToast(key), duration);
    toastTimers.set(key, { timer, duration });
  }

  function removeToast(key) {
    const entry = toastTimers.get(key);
    if (entry) window.clearTimeout(entry.timer);
    toastTimers.delete(key);
    $$(".toast").forEach((node) => {
      if (node.dataset.toastKey === key) node.remove();
    });
  }

  function setButtonBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${esc(
        label || "Working…",
      )}</span>`;
      return;
    }
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.dataset.idleHtml) {
      button.innerHTML = button.dataset.idleHtml;
      delete button.dataset.idleHtml;
    }
  }

  async function withButton(button, label, task) {
    if (button && button.dataset.busy === "1") return false;
    if (button) button.dataset.busy = "1";
    setButtonBusy(button, true, label);
    try {
      await task();
      return true;
    } catch (error) {
      toast(describeError(error), "error", { persist: true });
      return false;
    } finally {
      if (button) delete button.dataset.busy;
      setButtonBusy(button, false);
    }
  }

  function setLastRefreshed() {
    const node = $("#last-refreshed");
    if (!node) return;
    const now = new Date();
    node.textContent = `Updated ${now.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })}`;
    node.title = now.toLocaleString();
  }

  function setHealth(tone, label) {
    const dot = $("#health-dot");
    const text = $("#health-label");
    const chip = $("#health-chip");
    if (dot) dot.dataset.tone = tone;
    if (text) text.textContent = label;
    if (chip) chip.title = label;
  }

  function updateSystemStatus(data) {
    const status = data?.status || "unknown";
    const tone = status === "ok" ? "ok" : status === "degraded" ? "warning" : "error";
    const label = status === "ok" ? "Operational" : status === "degraded" ? "Degraded" : "Unknown";
    setHealth(tone, label);

    const systemDot = $("#system-dot");
    if (systemDot) systemDot.dataset.tone = tone;
    const systemStatus = $("#system-status");
    if (systemStatus) systemStatus.textContent = label;

    const missing = (data?.services || [])
      .filter((service) => service.required && !service.ready)
      .map((service) => service.label);
    const detail = $("#system-detail");
    if (detail) {
      if (missing.length) {
        detail.textContent = `Setup needed: ${missing.join(", ")}`;
      } else if (data) {
        const active = Number(data.active_runs || 0);
        detail.textContent = `${data.scheduler_running ? "Scheduler active" : "Scheduler idle"} · ${active} running`;
      } else {
        detail.textContent = "Checking services";
      }
    }
  }

  function updateStatusFallback(status) {
    setHealth("warning", "Overview unavailable");
    const systemDot = $("#system-dot");
    if (systemDot) systemDot.dataset.tone = "warning";
    const systemStatus = $("#system-status");
    if (systemStatus) systemStatus.textContent = "API online";
    const detail = $("#system-detail");
    if (detail) {
      detail.textContent = status?.scheduler_running
        ? "Scheduler active · overview unavailable"
        : "Scheduler idle · overview unavailable";
    }
  }

  function updateSystemUnavailable() {
    setHealth("error", "Unavailable");
    const systemDot = $("#system-dot");
    if (systemDot) systemDot.dataset.tone = "error";
    const systemStatus = $("#system-status");
    if (systemStatus) systemStatus.textContent = "API unavailable";
    const detail = $("#system-detail");
    if (detail) detail.textContent = "Check the server or proxy";
  }

  async function loadStatusFallback() {
    try {
      const status = await api("/system/status");
      updateStatusFallback(status);
    } catch {
      updateSystemUnavailable();
    }
  }

  function updateNavPending(value) {
    const node = $("#nav-pending");
    if (node) node.textContent = String(Number(value) || 0);
  }

  function skeletonLine(extra = "") {
    return `<div class="skeleton skeleton-line ${extra}"></div>`;
  }

  function skeletonCard() {
    return `<div class="skeleton-card">${skeletonLine("short")}${skeletonLine()}${skeletonLine(
      "tiny",
    )}</div>`;
  }

  function skeletonRows(columns, rows) {
    return Array.from({ length: rows })
      .map(
        () =>
          `<tr>${Array.from({ length: columns })
            .map(
              (_, index) =>
                `<td><div class="skeleton skeleton-line ${
                  index === 0 ? "short" : index === columns - 1 ? "tiny" : ""
                }"></div></td>`,
            )
            .join("")}</tr>`,
      )
      .join("");
  }

  function tableErrorRow(columns, message, retryKey) {
    return `<tr><td colspan="${columns}" class="table-empty"><p class="error-text">${esc(
      message,
    )}</p><button class="button small" type="button" data-retry="${esc(
      retryKey,
    )}">Retry</button></td></tr>`;
  }

  function tableEmptyRow(columns, message, action = "") {
    return `<tr><td colspan="${columns}" class="table-empty"><p>${esc(
      message,
    )}</p>${action}</td></tr>`;
  }

  function wireRetry(key, handler) {
    $$(`[data-retry="${key}"]`).forEach((button) => {
      button.addEventListener("click", handler);
    });
  }

  function metricMarkup(metrics) {
    return metrics
      .map(
        (metric) => `<article class="metric" data-tone="${esc(metric.tone || "")}">
          <div class="metric-label">${esc(metric.label)}</div>
          <div class="metric-value">${esc(metric.value)}</div>
          <p class="metric-detail">${esc(metric.detail)}</p>
        </article>`,
      )
      .join("");
  }

  function metricTone(metric) {
    if (!Number(metric.value)) return "";
    if (metric.key === "publishing_attention") return "danger";
    if (metric.key === "approvals" || metric.key === "pending_reviews") return "warning";
    if (metric.key === "verification" || metric.key === "renders") return "info";
    if (metric.key === "external_publishes") return "info";
    if (metric.key === "success_rate") return "success";
    return "";
  }

  function renderMetrics(metrics) {
    const container = $("#overview-metrics");
    if (!container) return;
    container.innerHTML = metricMarkup(
      (metrics || []).map((metric) => ({ ...metric, tone: metricTone(metric) })),
    );
  }

  function renderPipeline(pipeline) {
    const container = $("#pipeline-grid");
    const summary = $("#pipeline-summary");
    if (!container) return;
    const stages = (pipeline || []).length ? pipeline : [];
    if (!stages.length) {
      container.innerHTML = `<div class="stage-card span-all"><p class="stage-description">Pipeline status is unavailable.</p></div>`;
      if (summary) summary.textContent = "Unavailable";
      return;
    }

    container.innerHTML = stages
      .map((stage, index) => {
        const key = stage.key;
        const busy = state.stageBusy.has(key) || state.pipelineBusy;
        const error = state.stageErrors.get(key);
        const last = stage.last_run
          ? `Last run ${fmtRelative(stage.last_run.started_at)} · ${fmtRunDuration(
              stage.last_run.duration_ms,
            )}`
          : "No runs recorded yet";
        const actionLabel = busy
          ? "Running…"
          : key === "publishing"
            ? "Process"
            : "Run";
        return `<article class="stage-card" data-status="${esc(
          stage.status,
        )}" data-stage-key="${esc(key)}">
          <div class="stage-top">
            <h3 class="stage-name">${esc(stage.label)}</h3>
            <span class="stage-index">${String(index + 1).padStart(2, "0")}</span>
          </div>
          <span class="stage-status" title="${esc(last)}">${statusDot(
            stage.status,
          )}${esc(stage.status_label || titleCase(stage.status))}</span>
          <p class="stage-description">${esc(stage.description || "")}</p>
          <p class="stage-error" data-stage-error="${esc(key)}" ${
            error ? "" : "hidden"
          }>${esc(error || "")}</p>
          <div class="stage-footer">
            <div class="stage-count">
              <strong>${esc(stage.count ?? 0)}</strong>
              <span>${esc(stage.count_label || "")}</span>
            </div>
            <button class="button small stage-action" type="button" data-stage-action="${esc(
              key,
            )}" aria-label="${esc(`${actionLabel} ${stage.label}`)}" ${
              busy ? "disabled" : ""
            }>${esc(actionLabel)}</button>
          </div>
        </article>`;
      })
      .join("");

    if (summary) {
      const ready = stages.filter((stage) => stage.status === "ready").length;
      const running = stages.filter(
        (stage) => stage.status === "running" || stage.status === "stalled",
      ).length;
      const failed = stages.filter((stage) => stage.status === "failed").length;
      summary.textContent =
        [
          ready ? `${ready} ready` : "",
          running ? `${running} running` : "",
          failed ? `${failed} failed` : "",
        ]
          .filter(Boolean)
          .join(" · ") || "All clear";
    }
    wireOverviewActions();
  }

  function renderAttention(items) {
    const container = $("#attention-list");
    const count = $("#attention-count");
    if (count) count.textContent = String((items || []).length);
    if (!container) return;
    if (!(items || []).length) {
      container.innerHTML = `<div class="attention-item">
        <div class="attention-bar"></div>
        <div>
          <p class="attention-title">Nothing needs attention</p>
          <p class="attention-detail">No failures, pending approvals, or blocked publishing records.</p>
        </div>
      </div>`;
      return;
    }
    container.innerHTML = items
      .map((item) => {
        const content = `<div class="attention-bar"></div>
          <div>
            <p class="attention-title">${esc(item.title)}</p>
            <p class="attention-detail">${esc(item.detail)}</p>
            <div class="attention-meta"><span>${esc(
              titleCase(item.severity || "info"),
            )}</span><span>${esc(
              titleCase(item.kind || "attention"),
            )}</span><span>${esc(fmtRelative(item.created_at))}</span></div>
          </div>`;
        const href = internalHref(item.href);
        return href
          ? `<a class="attention-item" href="${esc(href)}" data-severity="${esc(
              item.severity,
            )}">${content}</a>`
          : `<div class="attention-item" data-severity="${esc(item.severity)}">${content}</div>`;
      })
      .join("");
  }

  function renderServices(services) {
    const container = $("#services-list");
    if (!container) return;
    if (!(services || []).length) {
      container.innerHTML = `<div class="service-row">${statusDot(
        "muted",
      )}<div><p class="service-name">No service data</p><p class="service-detail">Service readiness could not be determined.</p></div></div>`;
      return;
    }
    container.innerHTML = services
      .map((service) => {
        const tone = service.ready ? "success" : service.required ? "error" : "warning";
        return `<div class="service-row">
          ${statusDot(tone)}
          <div>
            <p class="service-name">${esc(service.label)}</p>
            <p class="service-detail">${esc(service.detail)}</p>
          </div>
          <span class="service-requirement">${
            service.required ? "Required" : "Optional"
          }</span>
        </div>`;
      })
      .join("");
  }

  function summarizeItems(items, toneClass, limit = 3) {
    const list = items || [];
    if (!list.length) return "";
    const shown = list
      .slice(-limit)
      .map((item) => `<div class="${toneClass}">${esc(item)}</div>`)
      .join("");
    const more =
      list.length > limit
        ? `<div class="muted">+${list.length - limit} more</div>`
        : "";
    return `${shown}${more}`;
  }

  function runDetailMarkup(run) {
    const errors = summarizeItems(run.errors, "error-text", 3);
    const warnings = summarizeItems(run.warnings, "warning-text", 2);
    if (errors || warnings) return `${errors}${warnings}`;
    if (run.status === "running") return `<span class="muted">In progress…</span>`;
    return `<span class="muted">—</span>`;
  }

  function runRow(run) {
    return `<tr>
      <td>
        <span class="cell-title">${esc(stageLabel(run.agent_name))}</span>
        <span class="cell-sub">${esc(run.agent_name)}</span>
      </td>
      <td>${badge(run.status)}</td>
      <td>
        <span class="cell-title">${esc(run.items_seen)}</span>
        <span class="cell-sub">${esc(run.items_created)} created · ${esc(
          run.items_updated,
        )} updated</span>
      </td>
      <td class="cell-nowrap">${esc(fmtRunDuration(run.duration_ms))}</td>
      <td class="cell-nowrap">
        <span class="cell-title">${esc(fmtDate(run.started_at))}</span>
        ${run.finished_at ? `<span class="cell-sub">${esc(fmtDate(run.finished_at))}</span>` : ""}
      </td>
      <td>${runDetailMarkup(run)}</td>
    </tr>`;
  }

  function renderOverviewRuns(runs) {
    const tbody = $("#overview-runs-body");
    if (!tbody) return;
    if (!(runs || []).length) {
      tbody.innerHTML = tableEmptyRow(
        6,
        "No agent runs recorded yet. Trigger a stage to create the first audit record.",
        `<a class="button ghost small" href="#/runs">Open agent runs</a>`,
      );
      return;
    }
    tbody.innerHTML = runs.map(runRow).join("");
  }

  function renderFocusStats(data) {
    const metrics = data?.metrics || [];
    const pending = Number(data?.journal?.pending_approvals ?? 0);
    const renders = Number(metrics.find((metric) => metric.key === "renders")?.value ?? 0);
    const attention = (data?.attention || []).length;
    const setStat = (selector, value, href) => {
      const node = $(selector);
      if (!node) return;
      node.innerHTML = href
        ? `<a href="${esc(href)}">${esc(value)}</a>`
        : esc(value);
    };
    setStat("#focus-reviews", pending, "#/review");
    setStat("#focus-renders", renders, "#/production");
    setStat("#focus-attention", attention);
  }

  function overviewJobProgress(job) {
    const stages = job.stages || [];
    if (!stages.length) {
      const text =
        job.status === "awaiting_approval"
          ? "Awaiting editorial review"
          : job.status === "completed"
            ? "Completed"
            : job.status === "failed"
              ? "Failed — open for details"
              : job.status === "interrupted"
                ? "Interrupted — resumable"
                : job.status === "running"
                  ? job.current_stage
                    ? `Running · ${titleCase(job.current_stage)}`
                    : "Rendering"
                  : job.status === "queued"
                    ? "Queued"
                    : titleCase(job.status);
      return { percent: null, text };
    }
    const completed = stages.filter((stage) => stage.status === "completed").length;
    const running = stages.some((stage) => stage.status === "running");
    const percent = Math.round(((completed + (running ? 0.5 : 0)) / stages.length) * 100);
    return {
      percent,
      text: `${completed}/${stages.length} stages${running ? " · rendering" : ""}`,
    };
  }

  function renderOverviewProduction(jobs) {
    const list = $("#overview-production");
    if (!list) return;
    const visible = (jobs || [])
      .filter((job) =>
        ["queued", "running", "awaiting_approval", "failed", "interrupted"].includes(
          job.status,
        ),
      )
      .slice(0, 5);
    if (!visible.length) {
      list.innerHTML = `<div class="inline-state">
        <strong>No active productions</strong>
        <span>Queued and rendering jobs appear here. Finished videos move to the review queue.</span>
        <a class="button ghost small" href="#/production">Start a production</a>
      </div>`;
      return;
    }
    list.innerHTML = visible
      .map((job) => {
        const progress = overviewJobProgress(job);
        const title =
          job.title ||
          job.article_title ||
          `${titleCase(job.style_key)} production`;
        const aspect = job.aspect_ratio === "both" ? "16:9 + 9:16" : job.aspect_ratio;
        const tone = job.status === "failed" || job.status === "interrupted" ? "danger" : "";
        return `<a class="active-job" href="#/production" data-overview-job="${esc(
          job.id,
        )}" aria-label="Open ${esc(title)} in the video studio">
          <span class="active-job-main">
            <span class="active-job-title">${esc(title)}</span>
            <span class="active-job-meta">
              <span>${esc(titleCase(job.style_key))}</span>
              <span>${esc(aspect || "16:9")}</span>
              <span>${esc(progress.text)}</span>
              <span>${esc(fmtRelative(job.created_at))}</span>
              <span>#${esc(String(job.id).slice(0, 8))}</span>
            </span>
            ${
              progress.percent === null
                ? ""
                : `<span class="active-job-progress" aria-hidden="true"><span style="width:${progress.percent}%" data-tone="${esc(
                    tone,
                  )}"></span></span>`
            }
          </span>
          <span class="active-job-side">
            ${badge(job.status)}
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
          </span>
        </a>`;
      })
      .join("");
    $$("[data-overview-job]", list).forEach((row) => {
      row.addEventListener("click", () => {
        productionState().selectedJobId = row.dataset.overviewJob;
      });
    });
  }

  function renderOverviewProductionError(message) {
    const list = $("#overview-production");
    if (!list) return;
    list.innerHTML = `<div class="inline-state">
      <strong>Production activity unavailable</strong>
      <span class="error-text">${esc(message)}</span>
      <button class="button ghost small" type="button" data-retry="overview-production">Retry</button>
    </div>`;
    wireRetry("overview-production", () => loadOverviewProduction());
  }

  async function loadOverviewProduction() {
    const list = $("#overview-production");
    if (!list) return false;
    if (!list.dataset.loaded) {
      list.innerHTML = `<div class="inline-state">
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line short"></div>
      </div>`;
    }
    try {
      const jobs = await api("/production/jobs?limit=6");
      list.dataset.loaded = "1";
      renderOverviewProduction(jobs || []);
      return true;
    } catch (error) {
      list.dataset.loaded = "1";
      renderOverviewProductionError(describeError(error));
      return false;
    }
  }

  function renderOverview(data) {
    state.overview = data;
    updateSystemStatus(data);
    updateNavPending(data.journal?.pending_approvals);
    renderFocusStats(data);
    renderMetrics(data.metrics);
    renderPipeline(data.pipeline);
    renderAttention(data.attention);
    renderServices(data.services);
    renderOverviewRuns(data.recent_runs);
    setLastRefreshed();
  }

  function renderOverviewSkeleton() {
    const production = $("#overview-production");
    if (production && !production.dataset.loaded) {
      production.innerHTML = `<div class="inline-state">
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line short"></div>
      </div>`;
    }
    const metrics = $("#overview-metrics");
    if (metrics) {
      metrics.innerHTML = Array.from(
        { length: 5 },
        () => `<div class="skeleton-metric"></div>`,
      ).join("");
    }
    const pipeline = $("#pipeline-grid");
    if (pipeline) {
      pipeline.innerHTML = Array.from({ length: 9 }, skeletonCard).join("");
    }
    const summary = $("#pipeline-summary");
    if (summary) summary.textContent = "Loading…";
    const attention = $("#attention-list");
    if (attention) {
      attention.innerHTML = Array.from(
        { length: 3 },
        () => `<div class="attention-item"><div class="attention-bar"></div><div>${skeletonLine()}${skeletonLine("short")}</div></div>`,
      ).join("");
    }
    const attentionCount = $("#attention-count");
    if (attentionCount) attentionCount.textContent = "…";
    const services = $("#services-list");
    if (services) {
      services.innerHTML = Array.from(
        { length: 4 },
        () => `<div class="service-row">${statusDot("muted")}<div>${skeletonLine()}${skeletonLine("short")}</div></div>`,
      ).join("");
    }
    const runs = $("#overview-runs-body");
    if (runs) runs.innerHTML = skeletonRows(6, 4);
  }

  function renderOverviewError(message) {
    ["#focus-reviews", "#focus-renders", "#focus-attention"].forEach((selector) => {
      const node = $(selector);
      if (node) node.textContent = "—";
    });
    const metrics = $("#overview-metrics");
    if (metrics) {
      metrics.innerHTML = `<article class="metric span-all">
        <div class="metric-label">Overview unavailable</div>
        <p class="metric-detail error-text">${esc(message)}</p>
        <button class="button small mt-2" type="button" data-retry="overview">Retry</button>
      </article>`;
    }
    const pipeline = $("#pipeline-grid");
    if (pipeline) {
      pipeline.innerHTML = `<div class="stage-card span-all">
        <p class="stage-description error-text">${esc(message)}</p>
        <button class="button small" type="button" data-retry="overview">Retry</button>
      </div>`;
    }
    const summary = $("#pipeline-summary");
    if (summary) summary.textContent = "Unavailable";
    const attention = $("#attention-list");
    if (attention) {
      attention.innerHTML = `<div class="attention-item"><div class="attention-bar"></div><div><p class="attention-title">Unable to load attention items</p><p class="attention-detail">${esc(
        message,
      )}</p></div></div>`;
    }
    const attentionCount = $("#attention-count");
    if (attentionCount) attentionCount.textContent = "—";
    const services = $("#services-list");
    if (services) {
      services.innerHTML = `<div class="service-row">${statusDot(
        "error",
      )}<div><p class="service-name">Service readiness unavailable</p><p class="service-detail">${esc(
        message,
      )}</p></div></div>`;
    }
    const runs = $("#overview-runs-body");
    if (runs) runs.innerHTML = tableErrorRow(6, message, "overview");
    wireRetry("overview", () => refreshCurrent({ manual: true }));
  }

  function wireOverviewActions() {
    wireRetry("overview", () => refreshCurrent({ manual: true }));
    $$("[data-stage-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.stageAction;
        if (!key || state.stageBusy.has(key) || state.pipelineBusy) return;
        await runStage(key);
        await refreshCurrent({ silent: true });
      });
    });
  }

  async function loadOverview({ silent = false } = {}) {
    if (!silent) renderOverviewSkeleton();
    const productionLoad = loadOverviewProduction();
    try {
      const data = await api("/system/overview");
      renderOverview(data);
      await productionLoad;
      return true;
    } catch (error) {
      renderOverviewError(describeError(error));
      await productionLoad;
      await loadStatusFallback();
      return false;
    }
  }

  function queueSkeleton() {
    return Array.from({ length: 5 }, skeletonCard).join("");
  }

  function renderQueueError(message) {
    const list = $("#review-list");
    if (!list) return;
    list.innerHTML = `<div class="empty-state">
      <span class="empty-glyph" aria-hidden="true">!</span>
      <h2>Review queue unavailable</h2>
      <p class="error-text">${esc(message)}</p>
      <button class="button small mt-3" type="button" data-retry="queue">Retry</button>
    </div>`;
    wireRetry("queue", () => refreshCurrent({ manual: true }));
  }

  async function loadQueue({ silent = false } = {}) {
    const list = $("#review-list");
    if (list && !silent && !state.queue.length) list.innerHTML = queueSkeleton();
    const status = $("#queue-filter")?.value || "";
    try {
      const [analyticsResult, queueResult] = await Promise.allSettled([
        api("/reviews/analytics"),
        api(`/reviews?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`),
      ]);
      if (queueResult.status === "rejected") throw queueResult.reason;
      const queue = queueResult.value || [];
      state.queue = queue;
      if (analyticsResult.status === "fulfilled") {
        updateNavPending(analyticsResult.value?.pending);
      }
      renderQueue();

      if (
        state.selectedId &&
        !queue.some((item) => item.video_render_id === state.selectedId)
      ) {
        state.selectedId = null;
        state.detail = null;
        state.detailDirty = false;
        renderEmptyDetail();
      }
      if (!state.selectedId && queue.length) {
        state.selectedId = queue[0].video_render_id;
        state.detailTab = "story";
        renderQueue();
        await loadDetail(state.selectedId, { resetTab: true });
      } else if (state.selectedId && !(state.detailDirty && state.detail)) {
        await loadDetail(state.selectedId, { keepTab: true });
      } else if (!queue.length) {
        renderEmptyDetail();
      }
      return true;
    } catch (error) {
      renderQueueError(describeError(error));
      return false;
    }
  }

  function renderQueue() {
    const list = $("#review-list");
    if (!list) return;
    if (!state.queue.length) {
      const filter = $("#queue-filter")?.value || "";
      if (filter) {
        list.innerHTML = `<div class="empty-state">
          <span class="empty-glyph" aria-hidden="true">◇</span>
          <h2>No drafts match this filter</h2>
          <p>No reviews currently have the “${esc(
            titleCase(filter),
          )}” status. Clear the filter to see the rest of the desk.</p>
          <button class="button ghost small mt-3" type="button" data-clear-filter>Clear filter</button>
        </div>`;
        $("[data-clear-filter]", list)?.addEventListener("click", () => {
          const select = $("#queue-filter");
          if (select) {
            select.value = "";
            state.lastQueueFilter = "";
          }
          loadQueue();
        });
        return;
      }
      list.innerHTML = `<div class="empty-state">
        <span class="empty-glyph" aria-hidden="true">◇</span>
        <h2>No drafts here</h2>
        <p>Run the pipeline to collect news and produce a reviewable video.</p>
        <a class="button ghost small mt-3" href="#/runs">Open agent runs</a>
      </div>`;
      return;
    }
    list.innerHTML = state.queue
      .map((item) => {
        const selected = state.selectedId === item.video_render_id;
        return `<button class="review-card ${selected ? "selected" : ""}" type="button" data-review-id="${esc(
          item.video_render_id,
        )}" ${selected ? 'aria-current="true"' : ""}>
          <div class="chips">${badge(item.approval_status)}${badge(
            item.credibility_status || "unknown",
          )}</div>
          <h3>${esc(item.title)}</h3>
          <div class="review-meta">
            <span>${esc(item.source_name)}</span>
            <span>${esc(item.category || "uncategorised")}</span>
            <span>${fmtDuration(item.duration_seconds)}</span>
            <span>priority ${esc(item.priority_score)}</span>
          </div>
          <div class="review-meta">
            <span>${fmtDate(item.created_at)}</span>
            ${item.publish_status ? badge(item.publish_status) : ""}
          </div>
        </button>`;
      })
      .join("");
    $$(".review-card", list).forEach((card) => {
      card.addEventListener("click", () => selectReview(card.dataset.reviewId));
    });
  }

  async function selectReview(id) {
    if (!id) return;
    if (id === state.selectedId && state.detail) return;
    if (state.detailDirty) {
      if (!window.confirm("Discard unsaved changes in the current review?")) return;
      state.detailDirty = false;
    }
    state.selectedId = id;
    state.detail = null;
    state.detailTab = "story";
    renderQueue();
    await loadDetail(id, { resetTab: true });
    // Below 1180px the queue and detail stack vertically, so selecting a draft
    // otherwise leaves the reader stranded above the workspace they just opened.
    if (window.innerWidth <= 1180 && !state.detailDirty) {
      $("#review-detail")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function renderEmptyDetail() {
    const panel = $("#review-detail");
    if (!panel) return;
    panel.innerHTML = `<div class="empty-state">
      <span class="empty-glyph" aria-hidden="true">◎</span>
      <h2>Select a draft</h2>
      <p>Inspect sources, claims, script, scenes, and quality gates before approving or rejecting.</p>
    </div>`;
  }

  function detailLoadingMarkup() {
    return `<div class="empty-state">
      <span class="spinner" aria-hidden="true"></span>
      <p class="mt-2">Loading review workspace…</p>
    </div>`;
  }

  function detailErrorMarkup(message) {
    return `<div class="empty-state">
      <span class="empty-glyph" aria-hidden="true">!</span>
      <h2>Review unavailable</h2>
      <p class="error-text">${esc(message)}</p>
      <button class="button small mt-3" type="button" data-retry="detail">Retry</button>
    </div>`;
  }

  async function loadDetail(id, { keepTab = false, resetTab = false } = {}) {
    if (resetTab) state.detailTab = "story";
    const panel = $("#review-detail");
    if (!panel) return false;
    panel.innerHTML = detailLoadingMarkup();
    try {
      const detail = await api(`/reviews/${encodeURIComponent(id)}`);
      state.detail = detail;
      state.detailDirty = false;
      if (!keepTab && !resetTab && !state.detailTab) state.detailTab = "story";
      renderDetail(detail);
      return true;
    } catch (error) {
      if (error.status === 404) {
        state.selectedId = null;
        state.detail = null;
        state.detailDirty = false;
        renderEmptyDetail();
        await loadQueue({ silent: true });
        return false;
      }
      panel.innerHTML = detailErrorMarkup(describeError(error));
      wireRetry("detail", () => {
        if (state.detailDirty && !window.confirm("Discard unsaved changes and reload?")) {
          return;
        }
        state.detailDirty = false;
        loadDetail(id, { keepTab: true });
      });
      return false;
    }
  }

  function tabButton(key, label, active, count) {
    const suffix =
      count === null || count === undefined
        ? ""
        : ` <span class="muted">${esc(count)}</span>`;
    return `<button class="tab-button" type="button" role="tab" id="tab-btn-${esc(
      key,
    )}" aria-selected="${active ? "true" : "false"}" aria-controls="tab-${esc(
      key,
    )}" data-tab="${esc(key)}" tabindex="${active ? 0 : -1}">${esc(label)}${suffix}</button>`;
  }

  function tabPanel(key, active, content) {
    return `<section class="tab-panel" role="tabpanel" id="tab-${esc(
      key,
    )}" aria-labelledby="tab-btn-${esc(key)}" data-tab-panel="${esc(key)}" ${
      active ? "" : "hidden"
    }>${content}</section>`;
  }

  function renderDetail(detail) {
    const panel = $("#review-detail");
    if (!panel) return;
    const { summary, article, script, credibility, research, narration, approval } = detail;
    const records = detail.publishing_records || [];
    const canEdit = !records.length;
    const checks = detail.quality_checks || approval?.quality_checks || [];
    const passed = checks.filter((check) => check.status === "passed").length;
    const scenes = detail.scene_plan?.scenes || [];
    const claims = credibility?.claims || [];
    const evidence = credibility?.evidence || [];
    const activeTab = ["story", "evidence", "script", "storyboard", "review"].includes(
      state.detailTab,
    )
      ? state.detailTab
      : "story";
    state.detailTab = activeTab;
    state.detailDirty = false;

    const header = `<div class="detail-head">
      <div>
        <p class="eyebrow">${esc(article.source_name)} · ${esc(
          article.category || "uncategorised",
        )}</p>
        <h2>${esc(summary.title)}</h2>
        <div class="chips">
          ${badge(approval.status)}
          ${badge(summary.credibility_status || "unknown")}
          ${badge(summary.video_status)}
          <span class="chip">${fmtDuration(summary.duration_seconds)}</span>
          <span class="chip">priority ${esc(summary.priority_score)}</span>
          ${summary.publish_status ? badge(summary.publish_status) : ""}
        </div>
      </div>
      <div class="chips">
        ${
          article.url
            ? `<a class="button ghost small" href="${esc(
                safeUrl(article.url) || "#",
              )}" target="_blank" rel="noopener noreferrer">Open source</a>`
            : ""
        }
        <button class="button ghost small" type="button" data-refresh-detail>Refresh review</button>
      </div>
    </div>`;

    const tabs = `<div class="tab-list" role="tablist" aria-label="Review workspace">
      ${tabButton("story", "Overview", activeTab === "story")}
      ${tabButton("evidence", "Evidence", activeTab === "evidence", claims.length + evidence.length)}
      ${tabButton("script", "Script", activeTab === "script")}
      ${tabButton("storyboard", "Scenes", activeTab === "storyboard", scenes.length)}
      ${tabButton("review", "Decision", activeTab === "review", `${passed}/${checks.length}`)}
    </div>`;

    panel.innerHTML = `<div class="detail">
      ${header}
      ${tabs}
      ${tabPanel("story", activeTab === "story", renderStoryPanel(detail))}
      ${tabPanel("evidence", activeTab === "evidence", renderEvidencePanel(detail))}
      ${tabPanel("script", activeTab === "script", renderScriptPanel(detail, canEdit))}
      ${tabPanel("storyboard", activeTab === "storyboard", renderStoryboardPanel(detail, canEdit))}
      ${tabPanel("review", activeTab === "review", renderReviewPanel(detail, canEdit))}
    </div>`;
    wireDetail(detail);
  }

  function renderList(items) {
    if (!items?.length) return `<p class="muted">None recorded.</p>`;
    return `<ul class="list">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
  }

  function renderResearch(research) {
    if (!research) {
      return `<p class="muted">No research brief is available yet. Run the research stage.</p>`;
    }
    const sources = research.sources || [];
    return `<div class="section-grid">
      <div><h3>Key facts</h3>${renderList(research.key_facts)}</div>
      <div><h3>Context</h3>${renderList(research.context)}</div>
      <div><h3>Uncertainties</h3>${renderList(research.uncertainties)}</div>
    </div>
    <h3 class="mt-3">Sources</h3>
    <div class="source-list">${
      sources.length
        ? sources
            .map(
              (source) => `<div class="source-item">
                <span>${externalLink(source.url, source.publisher)}</span>
                <span>${esc(source.title)}</span>
                <span class="source-meta">${esc(source.source_type)} · rel ${esc(
                  source.relevance_score,
                )}</span>
              </div>`,
            )
            .join("")
        : `<span class="muted">No additional sources were retrieved.</span>`
    }</div>`;
  }

  function renderStoryPanel(detail) {
    const { summary, article, credibility, research, narration } = detail;
    const credibilityText = credibility
      ? `${titleCase(credibility.status)} · ${credibility.confidence_score}`
      : "Not assessed";
    return `<div class="review-video" data-review-video>
      <video controls preload="metadata" src="${API}/reviews/videos/${esc(
        detail.video.id,
      )}/file"></video>
      <p class="inline-state" data-video-error hidden>
        <strong>Render file unavailable</strong>
        <span>The stored record exists, but its video file could not be loaded. Re-render this story from the video studio before publishing.</span>
      </p>
    </div>
    <section class="detail-section">
      <h3>Story provenance <span class="muted">Collected ${fmtDate(
        article.collected_at,
      )}</span></h3>
      <div class="section-grid">
        <div class="kv"><span class="k">Original headline</span><span class="v">${esc(
          article.title,
        )}</span></div>
        <div class="kv"><span class="k">Source</span><span class="v">${esc(
          article.source_name,
        )}</span></div>
        <div class="kv"><span class="k">Published</span><span class="v">${fmtDate(
          article.published_at,
        )}</span></div>
        <div class="kv"><span class="k">Credibility</span><span class="v">${esc(
          credibilityText,
        )}</span></div>
        <div class="kv"><span class="k">Language</span><span class="v">${esc(
          article.language || "—",
        )}</span></div>
        <div class="kv"><span class="k">Country</span><span class="v">${esc(
          article.country || "—",
        )}</span></div>
        <div class="kv"><span class="k">Narration</span><span class="v">${
          narration
            ? `${esc(narration.provider_name)} · ${fmtDuration(
                narration.duration_seconds,
              )} · ${esc(narration.status)}`
            : "Not generated"
        }</span></div>
        <div class="kv"><span class="k">Render</span><span class="v">${esc(
          detail.video.width,
        )}×${esc(detail.video.height)} · ${esc(detail.video.fps)} fps · ${esc(
          detail.video.renderer_name,
        )}</span></div>
      </div>
      <p class="muted mt-2">${esc(
        credibility?.explanation || "No credibility explanation stored.",
      )}</p>
    </section>
    <section class="detail-section">
      <h3>Research brief <span class="muted">${esc(
        research?.source_count ?? 0,
      )} sources</span></h3>
      ${renderResearch(research)}
    </section>`;
  }

  function renderEvidencePanel(detail) {
    const credibility = detail.credibility;
    const claims = credibility?.claims || [];
    const evidence = credibility?.evidence || [];
    if (!credibility) {
      return `<section class="detail-section"><p class="muted">No credibility assessment is available for this story.</p></section>`;
    }
    return `<section class="detail-section">
      <h3>Reported claims <span class="muted">${claims.length} extracted · ${badge(
        credibility.status,
      )}</span></h3>
      <div class="claim-list">${
        claims.length
          ? claims
              .map(
                (claim) =>
                  `<div class="claim-card"><p class="claim-text">${esc(claim)}</p></div>`,
              )
              .join("")
          : `<p class="muted">No claims were extracted.</p>`
      }</div>
    </section>
    <section class="detail-section">
      <h3>Evidence trail <span class="muted">${evidence.length} items · ${esc(
        credibility.independent_source_count,
      )} independent sources</span></h3>
      <div class="claim-list">${
        evidence.length
          ? evidence
              .map(
                (item) => `<div class="claim-card" data-status="${esc(item.finding)}">
                  <p class="claim-text">${externalLink(item.url, item.title)}</p>
                  <p class="claim-note">${esc(item.publisher)} · claim: ${esc(
                    item.claim,
                  )}</p>
                  <p class="claim-note">${esc(item.finding)} · relevance ${esc(
                    item.relevance_score,
                  )} · trust ${esc(item.trust_weight)}</p>
                  ${item.snippet ? `<p class="claim-note">${esc(item.snippet)}</p>` : ""}
                </div>`,
              )
              .join("")
          : `<p class="muted">No corroborating evidence items were stored.</p>`
      }</div>
    </section>`;
  }

  function scriptStatsText(words) {
    const safeWords = Math.max(0, Number(words) || 0);
    const seconds = Math.round((safeWords / 150) * 60);
    return `${safeWords} words · about ${fmtDuration(seconds)} at 150 wpm`;
  }

  function scriptWordCount(script) {
    const text = [
      script?.title,
      script?.hook,
      script?.introduction,
      ...(script?.main_story || []),
      ...(script?.context || []),
      script?.closing,
    ]
      .filter(Boolean)
      .join(" ");
    return text.trim().split(/\s+/).filter(Boolean).length;
  }

  function updateScriptStats(form) {
    const node = $("#script-stats");
    if (!node || !form) return;
    const values = [
      "#script-title",
      "#script-hook",
      "#script-intro",
      "#script-main",
      "#script-context",
      "#script-closing",
    ].map((selector) => $(selector, form)?.value || "");
    const words = values.join(" ").trim().split(/\s+/).filter(Boolean).length;
    node.textContent = scriptStatsText(words);
  }

  function renderScriptPanel(detail, canEdit) {
    const script = detail.script;
    return `<section class="detail-section">
      <h3>Script
        <span class="muted">${esc(
          script.estimated_duration_seconds,
        )}s estimated · <span id="script-stats">${esc(
          scriptStatsText(scriptWordCount(script)),
        )}</span> · ${badge(script.status)}</span>
      </h3>
      <form id="script-form" class="form-grid">
        <div class="form-row">
          <label for="script-title">Headline</label>
          <input class="input" id="script-title" name="title" maxlength="300" data-dirty value="${esc(
            script.title,
          )}" ${canEdit ? "" : "disabled"}>
        </div>
        <div class="form-row">
          <label for="script-hook">Hook</label>
          <input class="input" id="script-hook" name="hook" maxlength="2000" data-dirty value="${esc(
            script.hook,
          )}" ${canEdit ? "" : "disabled"}>
        </div>
        <div class="form-row">
          <label for="script-intro">Introduction</label>
          <textarea id="script-intro" name="introduction" maxlength="4000" data-dirty ${
            canEdit ? "" : "disabled"
          }>${esc(script.introduction)}</textarea>
        </div>
        <div class="form-row">
          <label for="script-main">Main story — one paragraph per line</label>
          <textarea id="script-main" class="script-textarea script-main" name="main_story" data-dirty ${
            canEdit ? "" : "disabled"
          }>${esc((script.main_story || []).join("\n"))}</textarea>
        </div>
        <div class="form-row">
          <label for="script-context">Context — one paragraph per line</label>
          <textarea id="script-context" class="script-textarea" name="context" data-dirty ${
            canEdit ? "" : "disabled"
          }>${esc((script.context || []).join("\n"))}</textarea>
        </div>
        <div class="form-row">
          <label for="script-closing">Closing</label>
          <textarea id="script-closing" name="closing" maxlength="2000" data-dirty ${
            canEdit ? "" : "disabled"
          }>${esc(script.closing)}</textarea>
        </div>
        <div class="form-actions">
          <button class="button" type="submit" ${canEdit ? "" : "disabled"}>Save script</button>
          <span class="dirty-note" data-dirty-note hidden>Unsaved changes</span>
          <span class="muted">${
            canEdit
              ? "Saving deletes generated audio, scenes, video, and approval so the pipeline can rebuild them."
              : "Locked because this draft already has publishing activity."
          }</span>
        </div>
      </form>
      ${
        script.review_reasons?.length
          ? `<p class="warning-text mt-2">${script.review_reasons.map(esc).join(" ")}</p>`
          : ""
      }
    </section>`;
  }

  function sceneEditor(scene, canEdit) {
    const disabled = canEdit ? "" : "disabled";
    return `<details class="scene-card"${scene.position === 1 ? " open" : ""}>
      <summary>
        <span><span class="scene-number">SCENE ${String(scene.position).padStart(
          2,
          "0",
        )}</span> · ${fmtDuration(scene.start_seconds)}–${fmtDuration(
          scene.start_seconds + scene.duration_seconds,
        )}</span>
        <span class="muted">${esc(scene.transition)}</span>
      </summary>
      <div class="scene-body">
        <div class="timeline-text">${esc(scene.narration)}</div>
        <div class="form-row">
          <label>Subtitle</label>
          <textarea data-scene-field="subtitle" data-dirty maxlength="4000" ${disabled}>${esc(
            scene.subtitle,
          )}</textarea>
        </div>
        <div class="form-row">
          <label>Visual description</label>
          <textarea data-scene-field="scene_description" data-dirty maxlength="4000" ${disabled}>${esc(
            scene.scene_description,
          )}</textarea>
        </div>
        <div class="form-row two">
          <div><label>Stock footage query</label><input class="input" data-scene-field="stock_footage_query" data-dirty maxlength="1000" value="${esc(
            scene.stock_footage_query,
          )}" ${disabled}></div>
          <div><label>Image search query</label><input class="input" data-scene-field="image_search_query" data-dirty maxlength="1000" value="${esc(
            scene.image_search_query,
          )}" ${disabled}></div>
        </div>
        <div class="form-row">
          <label>Animation suggestion</label>
          <input class="input" data-scene-field="animation_suggestion" data-dirty maxlength="1000" value="${esc(
            scene.animation_suggestion,
          )}" ${disabled}>
        </div>
        <div class="form-row two">
          <div><label>Keywords — comma separated</label><input class="input" data-scene-field="keywords" data-dirty value="${esc(
            (scene.keywords || []).join(", "),
          )}" ${disabled}></div>
          <div><label>Transition</label><input class="input" data-scene-field="transition" data-dirty maxlength="80" value="${esc(
            scene.transition,
          )}" ${disabled}></div>
        </div>
        <div class="form-actions">
          <button class="button small" type="button" data-save-scene="${esc(
            scene.id,
          )}" ${disabled}>Save scene</button>
          <span class="muted">Timing and narration stay tied to synthesized audio.</span>
        </div>
      </div>
    </details>`;
  }

  function renderStoryboardPanel(detail, canEdit) {
    const plan = detail.scene_plan;
    const scenes = plan?.scenes || [];
    return `<section class="detail-section">
      <h3>Scene plan
        <span class="muted">${scenes.length} scenes · ${fmtDuration(
          plan?.total_duration_seconds || 0,
        )} · ${badge(plan?.status || "unknown")}</span>
      </h3>
      ${scenes.length ? scenes.map((scene) => sceneEditor(scene, canEdit)).join("") : `<p class="muted">No scenes have been planned.</p>`}
      <div class="form-actions mt-2">
        <span class="dirty-note" data-dirty-note hidden>Unsaved scene changes</span>
        <span class="muted">${
          canEdit
            ? "Saving a scene invalidates the current render and approval."
            : "Locked because this draft already has publishing activity."
        }</span>
      </div>
    </section>`;
  }

  function qualityMarkup(checks) {
    if (!checks?.length) return `<p class="muted">No checks have been recorded yet.</p>`;
    return checks
      .map(
        (check) => `<div class="quality-item ${esc(check.status)}">
          <span class="status">${esc(check.status)}</span>
          <span><strong>${esc(
            String(check.name || "").replaceAll("_", " "),
          )}</strong><br><span class="muted">${esc(check.detail)}</span></span>
        </div>`,
      )
      .join("");
  }

  function publishingMarkup(detail) {
    const approval = detail.approval;
    const records = detail.publishing_records || [];
    const approved = approval?.status === "approved";
    const queueForm = approved && !records.length
      ? `<form id="publish-form" class="form-grid mt-3">
          <div class="form-row two">
            <div>
              <label for="publish-platform">Platform</label>
              <select class="input" id="publish-platform">
                <option value="local">Local export (no external upload)</option>
                <option value="youtube">YouTube (optional credentials)</option>
              </select>
            </div>
            <div>
              <label for="publish-title">Title override</label>
              <input class="input" id="publish-title" maxlength="100" placeholder="${esc(
                detail.summary.title,
              )}">
            </div>
          </div>
          <div class="form-row">
            <label for="publish-description">Description override</label>
            <textarea id="publish-description" maxlength="4900" placeholder="Leave blank to use the script plus cited sources"></textarea>
          </div>
          <div class="form-actions">
            <button class="button" type="submit">Queue for publishing</button>
            <span class="muted">Queuing does not upload until the publishing queue is processed.</span>
          </div>
        </form>`
      : approved
        ? `<p class="muted mt-2">This draft already has a publishing record. Manage it from the publishing page instead of queueing it again.</p>`
        : `<p class="muted mt-2">Approve this draft before queueing a publishing action.</p>`;

    const recordList = records.length
      ? `<div class="record-list mt-3">${records
          .map(
            (record) => `<div class="record">
              <div>
                <strong>${esc(record.platform)}</strong> ${badge(record.status)}
                <small>Queued ${fmtDate(record.created_at)} · attempts ${esc(
                  record.attempts,
                )}</small>
                ${
                  record.last_error
                    ? `<small class="error-text">${esc(record.last_error)}</small>`
                    : ""
                }
              </div>
              <div>
                ${publishingResultMarkup(record)}
                ${
                  ["failed", "blocked"].includes(record.status)
                    ? `<br><button class="button ghost small" type="button" data-retry-record="${esc(
                        record.id,
                      )}">Retry</button>`
                    : ""
                }
              </div>
            </div>`,
          )
          .join("")}</div>`
      : "";
    return `${queueForm}${recordList}`;
  }

  function renderReviewPanel(detail, canEdit) {
    const approval = detail.approval;
    const checks = detail.quality_checks || approval?.quality_checks || [];
    const passed = checks.filter((check) => check.status === "passed").length;
    const records = detail.publishing_records || [];
    const decisionForm = records.length
      ? `<p class="muted">Approval is locked because this draft already has publishing activity. This prevents a queued or published artifact from being re-approved after modification.</p>`
      : `<div class="form-row">
          <label for="reviewer">Reviewer</label>
          <input class="input" id="reviewer" maxlength="120" value="${esc(
            approval?.reviewer || "operator",
          )}">
        </div>
        <div class="form-row mt-2">
          <label for="review-notes">Review notes</label>
          <textarea id="review-notes" maxlength="4000" placeholder="Required context for rejection or requested changes">${esc(
            approval?.notes || "",
          )}</textarea>
        </div>
        <div class="form-actions mt-3">
          <button class="button ok" type="button" data-decision="approve">Approve</button>
          <button class="button ghost" type="button" data-decision="request_changes">Request changes</button>
          <button class="button danger" type="button" data-decision="reject">Reject</button>
        </div>`;
    return `<section class="detail-section" id="quality-section">
      <h3>Automated quality gate
        <span class="muted">${passed}/${checks.length} passed</span>
      </h3>
      <div class="quality-list">${qualityMarkup(checks)}</div>
      <p class="muted mt-2">Approval also pins the SHA-256 fingerprint of this exact video artifact. If the file changes, publishing is blocked.</p>
    </section>
    <section class="detail-section">
      <h3>Human decision <span class="muted">${badge(approval?.status || "unknown")}</span></h3>
      ${
        approval?.reviewer
          ? `<p class="muted mb-2">Last decision by ${esc(
              approval.reviewer,
            )} on ${fmtDate(approval.decided_at)}. ${esc(approval.notes || "")}</p>`
          : ""
      }
      ${decisionForm}
    </section>
    <section class="detail-section">
      <h3>Publishing <span class="muted">Approval status: ${esc(
        approval?.status || "unknown",
      )}</span></h3>
      ${publishingMarkup(detail)}
    </section>`;
  }

  function markDirty() {
    state.detailDirty = true;
    $$("[data-dirty-note]", $("#review-detail")).forEach((node) => {
      node.hidden = false;
    });
  }

  function setDetailTab(key) {
    if (!state.detail) return false;
    if (key === state.detailTab) return false;
    if (state.detailDirty) {
      if (!window.confirm("Discard unsaved changes in this review?")) return false;
      state.detailDirty = false;
      state.detailTab = key;
      renderDetail(state.detail);
      return true;
    }
    state.detailTab = key;
    $$("[data-tab]", $("#review-detail")).forEach((button) => {
      const selected = button.dataset.tab === key;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    $$("[data-tab-panel]", $("#review-detail")).forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== key;
    });
    return true;
  }

  function wireDetail(detail) {
    const panel = $("#review-detail");
    if (!panel) return;

    const reviewVideo = $("[data-review-video]", panel);
    if (reviewVideo) {
      const video = $("video", reviewVideo);
      const videoError = $("[data-video-error]", reviewVideo);
      const showVideoFallback = () => {
        if (!video || !videoError) return;
        video.hidden = true;
        videoError.hidden = false;
      };
      video?.addEventListener("error", showVideoFallback);
      if (video?.error) showVideoFallback();
    }

    $$("[data-tab]", panel).forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.tab;
        if (setDetailTab(key)) $(`#tab-btn-${key}`, panel)?.focus();
      });
    });

    const tablist = $(".tab-list", panel);
    tablist?.addEventListener("keydown", (event) => {
      const tabs = $$("[data-tab]", tablist);
      const index = tabs.indexOf(document.activeElement);
      if (index < 0) return;
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      else if (event.key === "ArrowLeft") {
        next = tabs[(index - 1 + tabs.length) % tabs.length];
      } else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      const key = next.dataset.tab;
      if (setDetailTab(key)) $(`#tab-btn-${key}`, panel)?.focus();
    });

    $$("[data-dirty]", panel).forEach((input) => {
      input.addEventListener("input", markDirty);
      input.addEventListener("change", markDirty);
    });

    const scriptForm = $("#script-form", panel);
    if (scriptForm) {
      scriptForm.addEventListener("input", () => updateScriptStats(scriptForm));
      updateScriptStats(scriptForm);
    }

    $("[data-refresh-detail]", panel)?.addEventListener("click", () => {
      if (state.detailDirty && !window.confirm("Discard unsaved changes and reload?")) {
        return;
      }
      state.detailDirty = false;
      loadDetail(detail.video.id, { keepTab: true });
    });

    $$("[data-decision]", panel).forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.decision;
        const notes = $("#review-notes")?.value.trim() || null;
        const reviewer = $("#reviewer")?.value.trim() || "operator";
        if (action !== "approve" && !notes) {
          toast("Add review notes before rejecting or requesting changes.", "warning");
          $("#review-notes")?.focus();
          return;
        }
        if (!window.confirm(`Record decision “${action.replaceAll("_", " ")}”?`)) return;
        await withButton(button, "Recording…", async () => {
          try {
            await api(`/reviews/${encodeURIComponent(detail.video.id)}/decision`, {
              method: "POST",
              body: JSON.stringify({ action, reviewer, notes }),
            });
          } catch (error) {
            if (error.status === 422 && Array.isArray(error.detail?.checks)) {
              const list = $("#quality-section .quality-list");
              if (list) list.innerHTML = qualityMarkup(error.detail.checks);
            }
            throw error;
          }
          state.detailTab = "review";
          toast(
            `Decision recorded: ${action.replaceAll("_", " ")}.`,
            action === "approve" ? "success" : "warning",
          );
          await loadQueue({ silent: true });
        });
      });
    });

    $("#script-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = $("button[type=submit]", form);
      const payload = {
        title: $("#script-title").value.trim(),
        hook: $("#script-hook").value.trim(),
        introduction: $("#script-intro").value.trim(),
        main_story: $("#script-main")
          .value.split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
        context: $("#script-context")
          .value.split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
        closing: $("#script-closing").value.trim(),
      };
      if (
        !payload.title ||
        !payload.hook ||
        !payload.introduction ||
        !payload.main_story.length ||
        !payload.closing
      ) {
        toast("Complete all required script fields before saving.", "warning");
        return;
      }
      withButton(submit, "Saving…", async () => {
        if (
          !window.confirm(
            "Saving the script deletes generated audio, scenes, video, and approval so the pipeline can rebuild them. Continue?",
          )
        ) {
          return;
        }
        await api(`/reviews/scripts/${encodeURIComponent(detail.script.id)}`, {
          method: "PATCH",
          body: JSON.stringify({
            ...payload,
            acknowledge_media_invalidation: true,
            actor: "operator",
          }),
        });
        state.detailDirty = false;
        state.selectedId = null;
        state.detail = null;
        state.detailTab = "story";
        renderEmptyDetail();
        toast(
          "Script saved. Generated media was invalidated; advance the pipeline to rebuild it.",
          "success",
        );
        await loadQueue({ silent: true });
      });
    });

    $$("[data-save-scene]", panel).forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest(".scene-card");
        const values = {};
        $$("[data-scene-field]", card).forEach((input) => {
          const key = input.dataset.sceneField;
          values[key] =
            key === "keywords"
              ? input.value
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean)
              : input.value.trim();
        });
        withButton(button, "Saving…", async () => {
          if (!values.subtitle) throw new Error("Subtitle cannot be empty.");
          if (!values.scene_description) {
            throw new Error("Visual description cannot be empty.");
          }
          if (
            !window.confirm(
              "Saving scene edits invalidates the current video and approval. Continue?",
            )
          ) {
            return;
          }
          const payload = {
            acknowledge_media_invalidation: true,
            actor: "operator",
          };
          Object.entries(values).forEach(([key, value]) => {
            if (key === "keywords") payload.keywords = value;
            else if (value) payload[key] = value;
          });
          await api(`/reviews/scenes/${encodeURIComponent(button.dataset.saveScene)}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
          state.detailDirty = false;
          state.selectedId = null;
          state.detail = null;
          state.detailTab = "story";
          renderEmptyDetail();
          toast(
            "Scene saved. The previous video was invalidated; rerun video generation.",
            "success",
          );
          await loadQueue({ silent: true });
        });
      });
    });

    $("#publish-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const submit = $("button[type=submit]", event.currentTarget);
      withButton(submit, "Queueing…", async () => {
        const payload = {
          video_render_id: detail.video.id,
          platform: $("#publish-platform").value,
          title: $("#publish-title").value.trim() || null,
          description: $("#publish-description").value.trim() || null,
          tags: [],
        };
        await api("/publishing/queue", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        state.detailTab = "review";
        toast(
          "Queued. Use “Process queue” to run the approved publishing action.",
          "success",
        );
        await loadQueue({ silent: true });
      });
    });

    $$("[data-retry-record]", panel).forEach((button) => {
      button.addEventListener("click", () => {
        withButton(button, "Retrying…", async () => {
          await api(
            `/publishing/records/${encodeURIComponent(button.dataset.retryRecord)}/retry`,
            { method: "POST" },
          );
          toast("Publishing record re-queued.", "success");
          await loadDetail(detail.video.id, { keepTab: true });
        });
      });
    });
  }

  function lastDetail(run) {
    const errors = run?.errors || [];
    if (errors.length) return errors[errors.length - 1];
    const warnings = run?.warnings || [];
    if (warnings.length) return warnings[warnings.length - 1];
    return "";
  }

  function setStageError(key, message) {
    if (message) state.stageErrors.set(key, message);
    else state.stageErrors.delete(key);
    const node = document.querySelector(`[data-stage-error="${key}"]`);
    if (node) {
      node.textContent = message || "";
      node.hidden = !message;
    }
  }

  function setStageUiBusy(key, busy, phase) {
    const action = document.querySelector(`[data-stage-action="${key}"]`);
    if (action) {
      if (busy) {
        if (!action.dataset.idleLabel) {
          action.dataset.idleLabel = action.textContent.trim();
        }
        action.disabled = true;
        action.setAttribute("aria-busy", "true");
        action.innerHTML = `<span class="spinner" aria-hidden="true"></span> ${esc(
          phase || "Running",
        )}`;
      } else {
        action.disabled = state.pipelineBusy || state.stageBusy.has(key);
        action.removeAttribute("aria-busy");
        action.textContent =
          action.dataset.idleLabel || (key === "publishing" ? "Process" : "Run");
        delete action.dataset.idleLabel;
      }
    }
    const runButton = document.querySelector(`[data-run-stage="${key}"]`);
    if (runButton) {
      runButton.disabled = busy || state.pipelineBusy;
      const runState = $("[data-run-state]", runButton);
      if (runState) runState.textContent = busy ? phase || "Running…" : "Ready";
    }
  }

  function syncStageButtons() {
    $$("[data-stage-action]").forEach((button) => {
      button.disabled =
        state.pipelineBusy || state.stageBusy.has(button.dataset.stageAction);
    });
    $$("[data-run-stage]").forEach((button) => {
      button.disabled =
        state.pipelineBusy || state.stageBusy.has(button.dataset.runStage);
    });
  }

  function resetStageActionLabels() {
    $$("[data-stage-action]").forEach((button) => {
      const key = button.dataset.stageAction;
      button.disabled = state.pipelineBusy || state.stageBusy.has(key);
      button.removeAttribute("aria-busy");
      if (!state.stageBusy.has(key)) {
        button.textContent = key === "publishing" ? "Process" : "Run";
      }
      delete button.dataset.idleLabel;
    });
    $$("[data-run-stage]").forEach((button) => {
      const key = button.dataset.runStage;
      button.disabled = state.pipelineBusy || state.stageBusy.has(key);
      const runState = $("[data-run-state]", button);
      if (runState) runState.textContent = state.stageBusy.has(key) ? "Running…" : "Ready";
    });
  }

  async function waitForRun(agentName, acceptedAt, options = {}) {
    const acceptedMs = acceptedAt ? dateMs(acceptedAt) : null;
    const started = Date.now();
    let sawRunning = false;
    while (Date.now() - started < STAGE_TIMEOUT_MS) {
      let runs = [];
      try {
        runs = await api(
          `/agent-runs?agent_name=${encodeURIComponent(agentName)}&limit=5`,
        );
      } catch (error) {
        if (Date.now() - started > 20_000) throw error;
      }
      const top = (runs || [])[0] || null;
      if (acceptedMs !== null) {
        const run =
          (runs || []).find(
            (candidate) => (dateMs(candidate.started_at) ?? 0) >= acceptedMs - 50,
          ) || null;
        if (run) {
          if (run.status !== "running") return run;
          sawRunning = true;
          options.onTick?.(run);
        } else if (Date.now() - started > 30_000) {
          throw new Error(
            "The stage was accepted but no run record appeared after 30 seconds. Check Agent runs and the server log.",
          );
        }
      } else {
        if (top && top.status === "running") {
          sawRunning = true;
          options.onTick?.(top);
        } else if (top && (sawRunning || Date.now() - started > 10_000)) {
          return top;
        } else if (!top && Date.now() - started > 15_000) {
          throw new Error("No active run record was found for this stage.");
        }
      }
      await sleep(900);
    }
    throw new Error(
      "Timed out waiting for the stage to finish. Check Agent runs for the latest status.",
    );
  }

  async function runStage(key, options = {}) {
    const stage = STAGE_BY_KEY[key];
    if (!stage) return { ok: false, error: new Error("Unknown workflow stage.") };
    if (state.pipelineBusy && !options.fromPipeline) {
      const error = new Error("The pipeline is already advancing.");
      if (!options.quiet) toast(error.message, "warning");
      return { ok: false, error };
    }
    if (state.stageBusy.has(key)) {
      const error = new Error(`${stage.label} is already running.`);
      if (!options.quiet) toast(error.message, "warning");
      return { ok: false, busy: true, error };
    }

    state.stageBusy.add(key);
    setStageError(key, "");
    setStageUiBusy(key, true, "Starting…");
    if (options.extraButton) setButtonBusy(options.extraButton, true, "Starting…");

    try {
      let accepted = null;
      try {
        accepted = await api("/workflow/runs", {
          method: "POST",
          body: JSON.stringify({ stage: key }),
        });
      } catch (error) {
        if (error.status === 409) {
          accepted = { accepted_at: null, conflict: true };
          if (!options.quiet) {
            toast(
              `${stage.label} is already running. Waiting for the active run…`,
              "warning",
            );
          }
        } else {
          throw error;
        }
      }

      const phase = accepted?.conflict ? "Waiting…" : "Running…";
      setStageUiBusy(key, true, phase);
      if (options.extraButton) setButtonBusy(options.extraButton, true, phase);
      options.onProgress?.(`${stage.label}: running…`);

      const run = await waitForRun(stage.agentName, accepted?.accepted_at || null, {
        conflict: Boolean(accepted?.conflict),
        onTick: (active) => {
          const status = String(active.status || "running").replaceAll("_", " ");
          setStageUiBusy(key, true, "Running…");
          options.onProgress?.(`${stage.label}: ${status}…`);
        },
      });
      if (!run) throw new Error(`No audit record appeared for ${stage.label}.`);

      if (run.status === "failed") {
        const message = lastDetail(run) || "The run failed without an error message.";
        setStageError(key, message);
        if (!options.quiet) {
          toast(`${stage.label} failed: ${message}`, "error", { persist: true });
        }
        return { ok: false, run, error: new Error(message) };
      }
      if (run.status === "completed_with_errors" || (run.warnings || []).length) {
        const message = lastDetail(run) || "Completed with warnings.";
        if (!options.quiet) {
          toast(`${stage.label} completed with warnings: ${message}`, "warning");
        }
        return { ok: true, warning: true, run };
      }
      if (!options.quiet) {
        toast(
          `${stage.label} completed · ${run.items_created ?? 0} created, ${
            run.items_updated ?? 0
          } updated`,
          "success",
        );
      }
      return { ok: true, run };
    } catch (error) {
      const message = describeError(error);
      setStageError(key, message);
      if (!options.quiet) toast(`${stage.label}: ${message}`, "error", { persist: true });
      return { ok: false, error };
    } finally {
      state.stageBusy.delete(key);
      setStageUiBusy(key, false);
      if (options.extraButton) setButtonBusy(options.extraButton, false);
    }
  }

  async function advancePipeline() {
    if (state.pipelineBusy) return;
    if (state.stageBusy.size) {
      toast("Wait for the running stage to finish before advancing the pipeline.", "warning");
      return;
    }

    state.pipelineBusy = true;
    const button = $("#pipeline-button");
    const label = $("#pipeline-label");
    const progress = $("#pipeline-progress");
    const progressText = $("#pipeline-progress-text");
    if (button) button.disabled = true;
    if (label) label.textContent = "Advancing…";
    if (progress) progress.hidden = false;
    if (progressText) progressText.textContent = "Advancing pipeline…";
    syncStageButtons();

    let completed = 0;
    let warnings = 0;
    try {
      for (let index = 0; index < PIPELINE_KEYS.length; index += 1) {
        const key = PIPELINE_KEYS[index];
        const stage = STAGE_BY_KEY[key];
        if (progressText) {
          progressText.textContent = `Advancing pipeline · ${stage.label} (${index + 1}/${
            PIPELINE_KEYS.length
          })…`;
        }
        const result = await runStage(key, {
          quiet: true,
          fromPipeline: true,
          onProgress: (text) => {
            if (progressText) {
              progressText.textContent = `Advancing pipeline · ${text} (${
                index + 1
              }/${PIPELINE_KEYS.length})`;
            }
          },
        });
        if (!result.ok) {
          toast(
            `Pipeline stopped at ${stage.label}: ${describeError(result.error)}`,
            "error",
            { persist: true },
          );
          await refreshCurrent({ silent: true, allowDuringPipeline: true });
          return;
        }
        if (result.warning) warnings += 1;
        completed += 1;
        await refreshCurrent({ silent: true, allowDuringPipeline: true });
      }
      toast(
        `Pipeline advanced · ${completed} stages processed${
          warnings ? ` · ${warnings} with warnings` : ""
        }.`,
        warnings ? "warning" : "success",
      );
    } catch (error) {
      toast(`Pipeline stopped: ${describeError(error)}`, "error", { persist: true });
    } finally {
      state.pipelineBusy = false;
      if (button) button.disabled = false;
      if (label) label.textContent = "Advance pipeline";
      if (progress) progress.hidden = true;
      resetStageActionLabels();
      await refreshCurrent({ silent: true });
    }
  }

  function populateRunsFilter() {
    const select = $("#runs-filter");
    if (!select) return;
    const current = select.value;
    select.innerHTML = `<option value="">All stages</option>${STAGES.map(
      (stage) => `<option value="${esc(stage.agentName)}">${esc(stage.label)}</option>`,
    ).join("")}`;
    select.value = current;
  }

  function renderRuns() {
    const tbody = $("#runs-body");
    if (!tbody) return;
    const agent = $("#runs-filter")?.value || "";
    const runs = state.runs.filter((run) => !agent || run.agent_name === agent);
    const foot = $("#runs-foot");
    const footMeta = $("#runs-foot-meta");
    const more = $("#runs-more");
    if (!runs.length) {
      tbody.innerHTML = tableEmptyRow(
        6,
        agent
          ? `No runs for ${stageLabel(agent)} yet.`
          : "No agent runs recorded yet. Trigger a stage above to create the first audit record.",
        agent ? "" : `<a class="button ghost small" href="#/overview">Open command center</a>`,
      );
      if (foot) foot.hidden = true;
      return;
    }
    const visible = runs.slice(0, state.runsVisible);
    tbody.innerHTML = visible.map(runRow).join("");
    if (foot && footMeta && more) {
      foot.hidden = false;
      footMeta.textContent = `Showing ${visible.length} of ${runs.length} runs`;
      more.hidden = visible.length >= runs.length;
    }
  }

  function renderRunActions() {
    const container = $("#run-actions");
    if (!container) return;
    container.innerHTML = STAGES.map((stage) => {
      const busy = state.stageBusy.has(stage.key);
      return `<button class="run-button" type="button" data-run-stage="${esc(
        stage.key,
      )}" ${busy || state.pipelineBusy ? "disabled" : ""}>
        <strong>${esc(stage.label)}</strong>
        <small>${esc(stage.description)}</small>
        <span class="run-state" data-run-state="${esc(stage.key)}">${
          busy ? "Running…" : "Ready"
        }</span>
      </button>`;
    }).join("");
    $$("[data-run-stage]", container).forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.runStage;
        if (!key || state.stageBusy.has(key) || state.pipelineBusy) return;
        await runStage(key);
        await refreshCurrent({ silent: true });
      });
    });
  }

  async function loadRuns({ silent = false } = {}) {
    const tbody = $("#runs-body");
    if (!tbody) return false;
    renderRunActions();
    populateRunsFilter();
    if (!silent) tbody.innerHTML = skeletonRows(6, 7);
    try {
      const runs = await api("/agent-runs?limit=120");
      state.runs = runs || [];
      renderRuns();
      return true;
    } catch (error) {
      tbody.innerHTML = tableErrorRow(6, describeError(error), "runs");
      wireRetry("runs", () => refreshCurrent({ manual: true }));
      return false;
    }
  }

  function renderPublishingStats(analytics) {
    const container = $("#publishing-stats");
    if (!container) return;
    container.innerHTML = metricMarkup([
      {
        label: "Approved videos",
        value: analytics.approved_videos,
        detail: "Ready for publishing",
        tone: Number(analytics.approved_videos) ? "success" : "",
      },
      {
        label: "Pending reviews",
        value: analytics.pending_reviews,
        detail: "Drafts awaiting a human decision",
        tone: Number(analytics.pending_reviews) ? "warning" : "",
      },
      {
        label: "Published externally",
        value: analytics.external_publish_count,
        detail: "Successful external deliveries",
        tone: Number(analytics.external_publish_count) ? "info" : "",
      },
      {
        label: "Success rate (30d)",
        value: `${analytics.publishing_success_rate}%`,
        detail: "Publishing attempts that completed",
        tone: "success",
      },
    ]);
  }

  function renderPublishingRecords(records) {
    const tbody = $("#publishing-body");
    if (!tbody) return;
    if (!(records || []).length) {
      tbody.innerHTML = tableEmptyRow(
        7,
        "No publishing records yet. Approve a draft, then queue it from the review workspace.",
        `<a class="button ghost small" href="#/review">Open review queue</a>`,
      );
      return;
    }
    tbody.innerHTML = records
      .map(
        (record) => `<tr>
          <td>
            <span class="cell-title">${esc(record.title)}</span>
            <span class="cell-sub">${esc(record.video_render_id)}</span>
          </td>
          <td>${esc(record.platform)}</td>
          <td>${badge(record.status)}</td>
          <td>${esc(record.attempts)}</td>
          <td>${publishingResultMarkup(record)}</td>
          <td class="cell-nowrap">${fmtDate(record.updated_at)}</td>
          <td>${
            ["failed", "blocked"].includes(record.status)
              ? `<button class="button ghost small" type="button" data-retry-record="${esc(
                  record.id,
                )}">Retry</button>`
              : ""
          }</td>
        </tr>`,
      )
      .join("");
    $$("[data-retry-record]", tbody).forEach((button) => {
      button.addEventListener("click", () => {
        withButton(button, "Retrying…", async () => {
          await api(
            `/publishing/records/${encodeURIComponent(button.dataset.retryRecord)}/retry`,
            { method: "POST" },
          );
          toast("Publishing record re-queued.", "success");
          await refreshCurrent({ silent: true });
        });
      });
    });
  }

  async function loadPublishing({ silent = false } = {}) {
    const stats = $("#publishing-stats");
    const tbody = $("#publishing-body");
    if (stats && !silent) {
      stats.innerHTML = Array.from(
        { length: 4 },
        () => `<div class="skeleton-metric"></div>`,
      ).join("");
    }
    if (tbody && !silent) tbody.innerHTML = skeletonRows(7, 6);
    try {
      const [analytics, records] = await Promise.all([
        api("/publishing/analytics?days=30"),
        api("/publishing/records?limit=100"),
      ]);
      renderPublishingStats(analytics);
      renderPublishingRecords(records);
      return true;
    } catch (error) {
      const message = describeError(error);
      if (stats) {
        stats.innerHTML = `<article class="metric span-all">
          <div class="metric-label">Publishing analytics unavailable</div>
          <p class="metric-detail error-text">${esc(message)}</p>
          <button class="button small mt-2" type="button" data-retry="publishing">Retry</button>
        </article>`;
      }
      if (tbody) tbody.innerHTML = tableErrorRow(7, message, "publishing");
      wireRetry("publishing", () => refreshCurrent({ manual: true }));
      return false;
    }
  }

  function renderArticles(articles) {
    const container = $("#articles-list");
    const count = $("#articles-count");
    if (count) count.textContent = String((articles || []).length);
    if (!container) return;
    const foot = $("#articles-foot");
    const footMeta = $("#articles-foot-meta");
    const more = $("#articles-more");
    if (!(articles || []).length) {
      container.innerHTML = `<div class="empty-state span-all">
        <span class="empty-glyph" aria-hidden="true">▤</span>
        <h2>No articles yet</h2>
        <p>Run Collect news to pull the configured feeds into the evidence library.</p>
        <a class="button ghost small mt-3" href="#/runs">Open agent runs</a>
      </div>`;
      if (foot) foot.hidden = true;
      return;
    }
    const visible = articles.slice(0, state.articlesVisible);
    container.innerHTML = visible
      .map(
        (article) => `<article class="article-card">
          <div class="chips">
            ${badge(article.category || "uncategorised")}
            <span class="chip">${esc(article.country || "—")}</span>
            <span class="chip">${esc(article.language || "—")}</span>
          </div>
          <h3>${externalLink(article.url, article.title)}</h3>
          <p>${esc(article.summary || (article.normalized_text || "").slice(0, 260))}</p>
          <small>${esc(article.source_name)} · ${fmtDate(
            article.published_at || article.collected_at,
          )}</small>
        </article>`,
      )
      .join("");
    if (foot && footMeta && more) {
      foot.hidden = false;
      footMeta.textContent = `Showing ${visible.length} of ${articles.length} articles`;
      more.hidden = visible.length >= articles.length;
    }
  }

  async function loadArticles({ silent = false } = {}) {
    const container = $("#articles-list");
    const count = $("#articles-count");
    if (container && !silent) {
      container.innerHTML = Array.from({ length: 8 }, skeletonCard).join("");
    }
    if (count && !silent) count.textContent = "…";
    try {
      const articles = await api("/articles?limit=100");
      state.articles = articles || [];
      renderArticles(state.articles);
      return true;
    } catch (error) {
      if (count) count.textContent = "—";
      if (container) {
        container.innerHTML = `<div class="empty-state span-all">
          <span class="empty-glyph" aria-hidden="true">!</span>
          <h2>Articles unavailable</h2>
          <p class="error-text">${esc(describeError(error))}</p>
          <button class="button small mt-3" type="button" data-retry="articles">Retry</button>
        </div>`;
      }
      wireRetry("articles", () => refreshCurrent({ manual: true }));
      return false;
    }
  }

  // ---------------------------------------------------------------- production

  const JOB_ACTIVE = new Set(["queued", "running"]);
  const STAGE_TONES = {
    queued: "muted",
    running: "info",
    completed: "success",
    failed: "danger",
    interrupted: "warning",
  };

  function productionState() {
    return state.production;
  }

  function selectedStyle() {
    const key = $("#prod-style")?.value;
    return (productionState().config?.styles || []).find(
      (style) => style.key === key,
    );
  }

  function renderProductionStyleSummary() {
    const summary = $("#prod-style-summary");
    const style = selectedStyle();
    if (!summary) return;
    if (!style) {
      summary.innerHTML = `<p class="muted">Choose an editing style to see its craft rules.</p>`;
      return;
    }
    const craft = style.craft || {};
    const shots = Array.isArray(craft.shot_mix) ? craft.shot_mix.join(" → ") : "—";
    summary.innerHTML = `
      <div class="style-summary-head">
        <strong>${esc(style.name)}</strong>
        <span class="muted">${esc(style.best_for || "")}</span>
      </div>
      <p class="style-summary-copy">${esc(style.description || "")}</p>
      <div class="chips">
        <span class="chip">${esc(style.transition)} transitions</span>
        <span class="chip">${esc(style.motion)} motion</span>
        <span class="chip">${esc(style.caption_style)} captions</span>
        <span class="chip">${esc(style.music)} music</span>
      </div>
      <dl class="style-craft">
        <div><dt>Shot mix</dt><dd>${esc(shots)}</dd></div>
        <div><dt>Cut rhythm</dt><dd>${esc(craft.cut_rhythm || "—")}</dd></div>
        <div><dt>Color</dt><dd>${esc(craft.color_grade || "—")}</dd></div>
        <div><dt>Graphics</dt><dd>${esc((style.graphics || []).join(", "))}</dd></div>
        <div><dt>Safe area</dt><dd>${esc(craft.safe_area_note || "—")}</dd></div>
      </dl>`;
  }

  function renderProductionConfig() {
    const config = productionState().config;
    if (!config) return;
    const prod = productionState();
    const styleSelect = $("#prod-style");
    if (styleSelect && !styleSelect.options.length) {
      styleSelect.innerHTML = config.styles
        .map(
          (style) =>
            `<option value="${esc(style.key)}">${esc(style.name)}</option>`,
        )
        .join("");
    }
    const assetSelect = $("#prod-asset-strategy");
    if (assetSelect && !assetSelect.options.length) {
      assetSelect.innerHTML = `
        <option value="hybrid">Licensed photos with designed fallbacks</option>
        <option value="licensed_only">Licensed photos only</option>
        <option value="graphics_only">Designed graphics only</option>`;
    }
    if (!prod.defaultsApplied) {
      const defaults = config.defaults || {};
      if (styleSelect && defaults.style_key) styleSelect.value = defaults.style_key;
      const aspect = $("#prod-aspect");
      if (aspect && defaults.aspect_ratio) aspect.value = defaults.aspect_ratio;
      const captions = $("#prod-captions");
      if (captions) captions.checked = defaults.captions !== false;
      if (assetSelect && defaults.asset_strategy) {
        assetSelect.value = defaults.asset_strategy;
      }
      const destination = $("#prod-destination");
      if (destination && defaults.destination) {
        destination.value = defaults.destination;
      }
      prod.defaultsApplied = true;
    }
    const voice = $("#prod-voice");
    if (voice) voice.value = config.voice_name || "Piper voice";
    const music = $("#prod-music");
    if (music && !music.placeholder) {
      music.placeholder = `${config.music_dir}/track.mp3`;
    }
    renderProductionReadiness();
    renderProductionStyleSummary();
  }

  function renderProductionReadiness() {
    const config = productionState().config;
    const list = $("#prod-readiness");
    if (!config || !list) return;
    const rows = [
      {
        label: "FFmpeg",
        ready: config.ffmpeg_available,
        detail: "Scene rendering, crossfades, and loudness mastering",
      },
      {
        label: "Piper voice",
        ready: config.piper_available,
        detail: config.voice_name || "Local narration",
      },
      {
        label: "Captions",
        ready: config.captions_available,
        detail: "Aspect-aware, inside the safe area",
      },
      {
        label: "Asset providers",
        ready: (config.asset_providers || []).length > 0,
        detail: (config.asset_providers || []).join(", ") || "None configured",
      },
    ];
    list.innerHTML = rows
      .map(
        (row) => `<div class="service-row">
          ${statusDot(row.ready ? "success" : "warning")}
          <div>
            <p class="service-name">${esc(row.label)}</p>
            <p class="service-detail">${esc(row.detail)}</p>
          </div>
        </div>`,
      )
      .join("");
    const note = $("#prod-licensing");
    if (note) note.textContent = config.licensing_note || "";
    const stateLabel = $("#production-config-state");
    if (stateLabel) {
      const ready = config.ffmpeg_available && config.piper_available;
      stateLabel.textContent = ready ? "Ready to produce" : "Setup required";
    }
  }

  function storyLabel(story) {
    const flags = [];
    if (story.has_render) flags.push("rendered");
    else if (story.has_narration) flags.push("narrated");
    else if (story.has_scene_plan) flags.push("scenes planned");
    if (story.production_ready === false) flags.push("blocked");
    const suffix = flags.length ? ` · ${flags.join(", ")}` : "";
    return `${story.title} — ${story.source_name}${suffix}`;
  }

  function selectedStory() {
    const prod = productionState();
    const id = $("#prod-story")?.value;
    return prod.stories.find((story) => story.script_id === id);
  }

  function renderProductionStories() {
    const prod = productionState();
    const select = $("#prod-story");
    const hint = $("#prod-story-hint");
    if (!select) return;
    const previous = select.value;
    if (!prod.stories.length) {
      select.innerHTML = `<option value="">No draft scripts available</option>`;
      select.disabled = true;
      if (hint) {
        hint.textContent =
          "Run the pipeline through the script stage to create a story.";
      }
      return;
    }
    select.disabled = false;
    const ordered = [...prod.stories].sort(
      (a, b) =>
        Number(b.production_ready !== false) -
        Number(a.production_ready !== false),
    );
    select.innerHTML = ordered
      .map(
        (story) =>
          `<option value="${esc(story.script_id)}">${esc(storyLabel(story))}</option>`,
      )
      .join("");
    if (ordered.some((story) => story.script_id === previous)) {
      select.value = previous;
    }
    renderProductionStoryHint();
  }

  function renderProductionStoryHint() {
    const hint = $("#prod-story-hint");
    const story = selectedStory();
    const button = $("#prod-generate");
    if (!hint) return;
    if (!story) {
      hint.textContent = "";
      if (button) button.disabled = false;
      return;
    }
    const parts = [
      `${story.estimated_duration_seconds}s script`,
      story.credibility_status
        ? `credibility: ${story.credibility_status}`
        : "credibility: not scored",
    ];
    if (story.asset_ready || story.asset_fallback) {
      parts.push(
        `${story.asset_ready} licensed asset(s), ${story.asset_fallback} fallback(s)`,
      );
    }
    hint.textContent = parts.join(" · ");
    const blocking = story.blocking_reasons || [];
    hint.dataset.tone = blocking.length ? "warning" : "";
    if (blocking.length) {
      hint.textContent = `${hint.textContent} · Blocked: ${blocking.join(" ")}`;
    }
    if (button) {
      button.disabled = story.production_ready === false;
      button.title = blocking.length ? blocking.join(" ") : "";
    }
  }

  function renderProductionJobs() {
    const prod = productionState();
    const list = $("#prod-jobs");
    if (!list) return;
    if (!prod.jobs.length) {
      list.innerHTML = `<p class="muted">No production jobs yet.</p>`;
      return;
    }
    list.innerHTML = prod.jobs
      .map((job) => {
        const selected = prod.selectedJobId === job.id;
        const progress = overviewJobProgress(job);
        const aspect = job.aspect_ratio === "both" ? "16:9 + 9:16" : job.aspect_ratio;
        return `<button class="production-job-row ${selected ? "selected" : ""}" type="button" data-production-job="${esc(
          job.id,
        )}" ${selected ? 'aria-current="true"' : ""}>
          <span class="production-job-main">
            <strong>${esc(
              job.title ||
                job.article_title ||
                `${titleCase(job.style_key)} production`,
            )}</strong>
            <small>${esc(aspect)} · ${esc(progress.text)} · ${esc(
              fmtRelative(job.created_at),
            )}</small>
          </span>
          ${badge(job.status)}
        </button>`;
      })
      .join("");
  }

  function frameUrl(jobId, aspect, position) {
    const name = `scene-${String(position).padStart(3, "0")}.jpg`;
    return `${API}/production/jobs/${encodeURIComponent(
      jobId,
    )}/frames/${encodeURIComponent(aspect)}/${name}`;
  }

  function assetMarkup(asset) {
    if (!asset) {
      return `<p class="muted">No asset decision recorded.</p>`;
    }
    if (asset.status === "fallback") {
      return `<p class="production-asset-note">${esc(
        asset.match_reason || "Designed graphic fallback",
      )}</p>`;
    }
    const links = [
      asset.license_url ? externalLink(asset.license_url, "License") : "",
      asset.source_url ? externalLink(asset.source_url, "Source") : "",
    ]
      .filter(Boolean)
      .join(" ");
    return `<p class="production-asset-title">${esc(
      asset.title || "Licensed asset",
    )}</p>
      <p class="production-asset-meta">${esc(
        [asset.provider, asset.creator, asset.license_name]
          .filter(Boolean)
          .join(" · "),
      )}</p>
      ${links ? `<p class="production-asset-links">${links}</p>` : ""}`;
  }

  function variantMarkup(variant) {
    return `<article class="variant-card">
      <div class="variant-head">
        <strong>${esc(variant.aspect_ratio)}</strong>
        <span class="muted">${variant.width}×${variant.height} · ${fmtDuration(
          variant.duration_seconds,
        )}</span>
      </div>
      <video controls preload="metadata" src="${esc(variant.file_url)}"></video>
      <div class="variant-actions">
        <a class="button ghost small" href="${esc(
          variant.file_url,
        )}" download>Download MP4</a>
        ${badge(variant.status)}
      </div>
    </article>`;
  }

  function scenePreviewMarkup(job, scene, aspects) {
    const frames = aspects
      .map((aspect) => {
        const label = aspect === "16:9" ? "Landscape" : "Vertical";
        return `<figure class="scene-frame" data-aspect="${esc(aspect)}">
          <img src="${esc(
            frameUrl(job.id, aspect, scene.position),
          )}" alt="Scene ${scene.position} ${label} preview" loading="lazy" />
          <figcaption>${esc(label)}</figcaption>
        </figure>`;
      })
      .join("");
    return `<article class="scene-preview">
      <div class="scene-preview-head">
        <span class="scene-number">${String(scene.position).padStart(2, "0")}</span>
        <div>
          <p class="scene-preview-subtitle">${esc(
            scene.subtitle || scene.narration,
          )}</p>
          <p class="muted">${fmtDuration(scene.duration_seconds)} · ${esc(
            scene.transition,
          )}</p>
        </div>
      </div>
      <div class="scene-frames">${frames}</div>
      <div class="scene-license">${assetMarkup(scene.asset)}</div>
    </article>`;
  }

  function renderProductionJob() {
    const prod = productionState();
    const job = prod.job;
    const empty = $("#prod-job-empty");
    const body = $("#prod-job-body");
    if (!empty || !body) return;
    if (!job) {
      empty.hidden = false;
      body.hidden = true;
      return;
    }
    empty.hidden = true;
    body.hidden = false;

    const stages = job.stages || [];
    const completed = stages.filter((stage) => stage.status === "completed").length;
    const running = stages.find((stage) => stage.status === "running");
    const failed = stages.find((stage) => stage.status === "failed");
    const progress = stages.length
      ? Math.round(((completed + (running ? 0.5 : 0)) / stages.length) * 100)
      : 0;
    const subtitle = $("#prod-job-subtitle");
    if (subtitle) {
      subtitle.textContent = [
        job.title || job.article_title || "Production job",
        `Style: ${titleCase(job.style_key)}`,
        job.aspect_ratio === "both" ? "16:9 + 9:16" : job.aspect_ratio,
        job.requested_by ? `Requested by ${job.requested_by}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
    }
    const chips = $("#prod-job-chips");
    if (chips) {
      chips.innerHTML = `${badge(job.status)}${
        job.approval_status ? badge(job.approval_status) : ""
      }`;
    }
    const fill = $("#prod-progress-fill");
    if (fill) {
      fill.style.width = `${progress}%`;
      fill.dataset.tone = failed
        ? "danger"
        : job.status === "awaiting_approval" || job.status === "completed"
          ? "success"
          : "info";
    }
    const progressText = $("#prod-progress-text");
    if (progressText) {
      progressText.textContent = running
        ? `${completed}/${stages.length} stages · ${running.label}`
        : `${completed}/${stages.length} stages · ${titleCase(job.status)}`;
    }
    const stageList = $("#prod-stages");
    if (stageList) {
      stageList.innerHTML = stages
        .map(
          (stage) => `<div class="production-stage" data-status="${esc(
            stage.status,
          )}">
            ${statusDot(STAGE_TONES[stage.status] || "muted")}
            <div class="production-stage-main">
              <strong>${esc(stage.label)}</strong>
              <small>${esc(
                stage.status === "completed"
                  ? fmtRunDuration(stage.duration_ms)
                  : stage.status === "running"
                    ? "In progress"
                    : stage.status === "failed"
                      ? stage.error || "Failed"
                      : "Waiting",
              )}</small>
            </div>
            ${badge(stage.status)}
          </div>`,
        )
        .join("");
    }
    const actions = $("#prod-job-actions");
    if (actions) {
      const retry =
        job.status === "failed" || job.status === "interrupted"
          ? `<button class="button small" type="button" data-retry-production="${esc(
              job.id,
            )}">Retry failed stage</button>`
          : "";
      const review = job.video_render_id
        ? `<a class="button ghost small" href="#/review">Review and approve</a>`
        : "";
      actions.innerHTML = `${retry}${review}`;
    }
    const error = $("#prod-job-error");
    if (error) {
      error.hidden = !job.error;
      error.textContent = job.error || "";
    }
    const aspects = (job.variants || []).map((variant) => variant.aspect_ratio);
    const sceneList = $("#prod-scenes");
    if (sceneList) {
      if (!job.scenes?.length) {
        sceneList.innerHTML = `<p class="muted">Scene previews appear after planning and rendering.</p>`;
      } else if (!aspects.length) {
        sceneList.innerHTML = `<p class="muted">Frames are generated during the render stage.</p>`;
      } else {
        sceneList.innerHTML = job.scenes
          .map((scene) => scenePreviewMarkup(job, scene, aspects))
          .join("");
      }
    }
    const variantList = $("#prod-variants");
    if (variantList) {
      variantList.innerHTML = (job.variants || []).length
        ? job.variants.map(variantMarkup).join("")
        : `<p class="muted">Both aspect ratios appear here when rendering finishes.</p>`;
    }
    const logs = $("#prod-logs");
    if (logs) {
      const entries = (job.logs || []).slice(-14).reverse();
      logs.innerHTML = entries.length
        ? entries
            .map(
              (entry) => `<li data-tone="${esc(
                statusTone(entry.level),
              )}"><span>${esc(entry.message)}</span><small>${esc(
                fmtDate(entry.at),
              )}</small></li>`,
            )
            .join("")
        : `<li><span class="muted">No log entries yet.</span></li>`;
    }
    if ((job.warnings || []).length) {
      const warning = (job.warnings || [])
        .map((item) => `<li>${esc(item)}</li>`)
        .join("");
      const warningNode = $("#prod-job-warnings");
      if (warningNode) {
        warningNode.hidden = false;
        warningNode.innerHTML = `<strong>Review notes</strong><ul>${warning}</ul>`;
      }
    } else {
      const warningNode = $("#prod-job-warnings");
      if (warningNode) warningNode.hidden = true;
    }
  }

  function renderProductionError(message) {
    const empty = $("#prod-job-empty");
    const body = $("#prod-job-body");
    if (body) body.hidden = true;
    if (empty) {
      empty.hidden = false;
      empty.innerHTML = `<span class="empty-glyph" aria-hidden="true">!</span>
        <h2>Production studio unavailable</h2>
        <p class="error-text">${esc(message)}</p>
        <button class="button small mt-3" type="button" data-retry="production">Retry</button>`;
      wireRetry("production", () => refreshCurrent({ manual: true }));
    }
  }

  async function loadProductionJob(id, { silent = false } = {}) {
    const job = await api(`/production/jobs/${encodeURIComponent(id)}`);
    productionState().job = job;
    productionState().selectedJobId = id;
    renderProductionJob();
    if (JOB_ACTIVE.has(job.status)) ensureProductionPolling();
    return job;
  }

  async function loadProductionLists({ silent = false } = {}) {
    const prod = productionState();
    const [stories, jobs] = await Promise.all([
      api("/production/stories"),
      api("/production/jobs?limit=12"),
    ]);
    prod.stories = stories || [];
    prod.jobs = jobs || [];
    renderProductionStories();
    renderProductionJobs();
  }

  async function loadProduction({ silent = false } = {}) {
    const prod = productionState();
    if (prod.loading) return false;
    prod.loading = true;
    try {
      if (!prod.config) {
        prod.config = await api("/production/config");
        renderProductionConfig();
      }
      await loadProductionLists({ silent });
      const target =
        prod.selectedJobId &&
        prod.jobs.some((job) => job.id === prod.selectedJobId)
          ? prod.selectedJobId
          : prod.jobs[0]?.id;
      if (target) {
        await loadProductionJob(target, { silent: true });
      } else {
        prod.job = null;
        renderProductionJob();
      }
      return true;
    } catch (error) {
      renderProductionError(describeError(error));
      return false;
    } finally {
      prod.loading = false;
    }
  }

  function stopProductionPolling() {
    const prod = productionState();
    if (prod.pollTimer) {
      window.clearInterval(prod.pollTimer);
      prod.pollTimer = null;
    }
  }

  function ensureProductionPolling() {
    const prod = productionState();
    if (prod.pollTimer) return;
    prod.pollTimer = window.setInterval(async () => {
      const job = productionState().job;
      if (state.view !== "production" || !job || !JOB_ACTIVE.has(job.status)) {
        stopProductionPolling();
        return;
      }
      try {
        const updated = await loadProductionJob(job.id, { silent: true });
        if (!JOB_ACTIVE.has(updated.status)) {
          stopProductionPolling();
          await loadProductionLists({ silent: true });
          toast(
            updated.status === "awaiting_approval"
              ? "Production finished. Review and approve the renders."
              : `Production ${updated.status}.`,
            updated.status === "awaiting_approval" ? "success" : "error",
          );
        }
      } catch (error) {
        stopProductionPolling();
        toast(describeError(error), "error");
      }
    }, 4_000);
  }

  async function generateProduction(event) {
    event.preventDefault();
    const button = $("#prod-generate");
    const scriptId = $("#prod-story")?.value;
    if (!scriptId) {
      toast("Choose a story with a draft script first.", "warning");
      return;
    }
    const story = selectedStory();
    if (story && story.production_ready === false) {
      toast(
        `This story is blocked: ${(story.blocking_reasons || []).join(" ")}`,
        "warning",
      );
      return;
    }
    const payload = {
      script_id: scriptId,
      style_key: $("#prod-style")?.value || "broadcast",
      aspect_ratio: $("#prod-aspect")?.value || "both",
      captions: Boolean($("#prod-captions")?.checked),
      asset_strategy: $("#prod-asset-strategy")?.value || "hybrid",
      destination: $("#prod-destination")?.value || "local",
      music_path: $("#prod-music")?.value.trim() || null,
      regenerate_narration: Boolean($("#prod-renarrate")?.checked),
    };
    await withButton(button, "Starting production…", async () => {
      const job = await api("/production/jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await api(`/production/jobs/${encodeURIComponent(job.id)}/start`, {
        method: "POST",
      });
      api("/production/defaults", {
        method: "PATCH",
        body: JSON.stringify({
          style_key: payload.style_key,
          aspect_ratio: payload.aspect_ratio,
          captions: payload.captions,
          asset_strategy: payload.asset_strategy,
          destination: payload.destination,
        }),
      }).catch(() => {});
      const prod = productionState();
      prod.selectedJobId = job.id;
      prod.job = job;
      renderProductionJob();
      ensureProductionPolling();
      toast(
        "Production started. Stages are saved as they complete, so a retry resumes where it stopped.",
        "info",
      );
      await loadProductionLists({ silent: true });
    });
  }

  async function retryProductionJob(id, button) {
    await withButton(button, "Retrying…", async () => {
      const job = await api(
        `/production/jobs/${encodeURIComponent(id)}/retry`,
        { method: "POST" },
      );
      productionState().job = job;
      productionState().selectedJobId = id;
      renderProductionJob();
      ensureProductionPolling();
      toast("Retrying the failed stage.", "info");
    });
  }

  function wireProduction() {
    $("#production-form")?.addEventListener("submit", generateProduction);
    $("#prod-style")?.addEventListener("change", renderProductionStyleSummary);
    $("#prod-story")?.addEventListener("change", renderProductionStoryHint);
    $("#prod-refresh")?.addEventListener("click", () =>
      refreshCurrent({ manual: true }),
    );
    $("#prod-jobs")?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-production-job]");
      if (!button) return;
      const id = button.dataset.productionJob;
      if (!id) return;
      try {
        await loadProductionJob(id);
        renderProductionJobs();
      } catch (error) {
        toast(describeError(error), "error");
      }
    });
    $("#prod-job-body")?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-retry-production]");
      if (!button) return;
      await retryProductionJob(button.dataset.retryProduction, button);
    });
  }

  async function loadViewData(view, options = {}) {
    if (view === "overview") return loadOverview(options);
    if (view === "production") return loadProduction(options);
    if (view === "queue") return loadQueue(options);
    if (view === "runs") return loadRuns(options);
    if (view === "publishing") return loadPublishing(options);
    if (view === "articles") return loadArticles(options);
    return false;
  }

  async function refreshCurrent({
    manual = false,
    silent = false,
    allowDuringPipeline = false,
  } = {}) {
    if (state.refreshing) return false;
    if (state.pipelineBusy && !allowDuringPipeline) {
      if (manual) toast("The pipeline is running. Wait for it to finish.", "info");
      return false;
    }
    state.refreshing = true;
    const button = $("#refresh-button");
    const label = $("#refresh-label");
    if (button) {
      button.classList.add("is-spinning");
      button.disabled = true;
    }
    if (label) label.textContent = "Refreshing…";
    try {
      const ok = await loadViewData(state.view, { silent: true });
      if (ok) {
        setLastRefreshed();
        if (manual && !silent) toast("Dashboard refreshed.", "success");
      } else if (manual) {
        toast("Refresh failed. See the error shown on this page.", "error", {
          persist: true,
        });
      }
      return ok;
    } finally {
      state.refreshing = false;
      if (button) {
        button.classList.remove("is-spinning");
        button.disabled = false;
      }
      if (label) label.textContent = "Refresh";
    }
  }

  async function showView(view) {
    const previousView = state.view;
    state.view = view;
    if (view !== "production") stopProductionPolling();
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.view === view;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $$("[data-view-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.viewPanel !== view;
    });
    const meta = VIEWS[view];
    if (meta) {
      const eyebrow = $("#view-eyebrow");
      const title = $("#view-title");
      const description = $("#view-description");
      if (eyebrow) eyebrow.textContent = meta.eyebrow;
      if (title) title.textContent = meta.title;
      if (description) description.textContent = meta.description;
      document.title = `${meta.title} · Newsroom`;
    }
    closeMobileNav();
    if (previousView && previousView !== view) {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }
    const ok = await loadViewData(view);
    if (ok) setLastRefreshed();
  }

  function route() {
    const raw = (window.location.hash || "")
      .replace(/^#\/?/, "")
      .replace(/\/+$/, "");
    let path = raw || "overview";
    let view = VIEW_BY_PATH[path];
    if (!view) {
      view = "overview";
      path = "overview";
      window.history.replaceState(null, "", "#/overview");
    } else if (!window.location.hash) {
      window.history.replaceState(null, "", "#/overview");
    }

    if (state.booted && view === state.view) return;
    if (state.detailDirty && state.view === "queue" && view !== "queue") {
      if (!window.confirm("Discard unsaved changes in this review?")) {
        window.history.replaceState(null, "", `#/${VIEWS[state.view].path}`);
        return;
      }
      state.detailDirty = false;
    }
    state.booted = true;
    showView(view);
  }

  function applySidebarState() {
    const shell = $("#app-shell");
    const button = $("#sidebar-collapse");
    if (!shell) return;
    const collapsed =
      window.innerWidth > 960 &&
      window.localStorage.getItem("newsroom.sidebar.collapsed") === "1";
    shell.classList.toggle("sidebar-collapsed", collapsed);
    if (button) {
      button.setAttribute("aria-expanded", String(!collapsed));
      button.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    }
  }

  function toggleSidebar() {
    const collapsed = !$("#app-shell")?.classList.contains("sidebar-collapsed");
    window.localStorage.setItem(
      "newsroom.sidebar.collapsed",
      collapsed ? "1" : "0",
    );
    applySidebarState();
    const tooltip = $("#nav-tooltip");
    if (tooltip) tooltip.hidden = true;
  }

  function openMobileNav() {
    const shell = $("#app-shell");
    const scrim = $("#nav-scrim");
    const toggle = $("#menu-toggle");
    if (!shell) return;
    if (shell.classList.contains("nav-open")) {
      closeMobileNav();
      return;
    }
    shell.classList.add("nav-open");
    if (scrim) scrim.hidden = false;
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    $(".nav-item", $("#sidebar"))?.focus({ preventScroll: true });
  }

  function closeMobileNav() {
    const shell = $("#app-shell");
    const scrim = $("#nav-scrim");
    const toggle = $("#menu-toggle");
    if (shell) shell.classList.remove("nav-open");
    if (scrim) scrim.hidden = true;
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  }

  function wireNavTooltips() {
    const tooltip = $("#nav-tooltip");
    if (!tooltip) return;
    const hide = () => {
      tooltip.hidden = true;
    };
    const show = (item) => {
      const shell = $("#app-shell");
      if (!shell?.classList.contains("sidebar-collapsed")) return;
      const label = item.dataset.label || item.getAttribute("aria-label");
      if (!label) return;
      const rect = item.getBoundingClientRect();
      tooltip.textContent = label;
      tooltip.style.top = `${Math.round(rect.top + rect.height / 2)}px`;
      tooltip.style.left = `${Math.round(rect.right + 10)}px`;
      tooltip.hidden = false;
    };
    $$(".nav-item").forEach((item) => {
      item.addEventListener("mouseenter", () => show(item));
      item.addEventListener("mouseleave", hide);
      item.addEventListener("focus", () => show(item));
      item.addEventListener("blur", hide);
    });
    window.addEventListener("resize", hide);
    window.addEventListener("scroll", hide, true);
  }

  function wireGlobal() {
    wireNavTooltips();
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => closeMobileNav());
    });

    // The shell uses hash routing, so a plain #main-content skip link would be
    // read as a route change and bounce the reader back to the overview.
    $(".skip-link")?.addEventListener("click", (event) => {
      event.preventDefault();
      const main = $("#main-content");
      if (!main) return;
      main.focus({ preventScroll: true });
      main.scrollIntoView({ behavior: "auto", block: "start" });
    });

    $("#refresh-button")?.addEventListener("click", () =>
      refreshCurrent({ manual: true }),
    );
    $("#pipeline-button")?.addEventListener("click", advancePipeline);
    wireProduction();

    const queueFilter = $("#queue-filter");
    if (queueFilter) {
      state.lastQueueFilter = queueFilter.value;
      queueFilter.addEventListener("change", (event) => {
        if (
          state.detailDirty &&
          !window.confirm("Discard unsaved changes and change the filter?")
        ) {
          event.target.value = state.lastQueueFilter;
          return;
        }
        state.detailDirty = false;
        state.lastQueueFilter = event.target.value;
        loadQueue();
      });
    }

    $("#runs-filter")?.addEventListener("change", () => {
      state.runsVisible = 40;
      renderRuns();
    });

    $("#runs-more")?.addEventListener("click", () => {
      state.runsVisible += 40;
      renderRuns();
    });

    $("#articles-more")?.addEventListener("click", () => {
      state.articlesVisible += 24;
      renderArticles(state.articles);
    });

    $("#run-publishing-button")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (state.stageBusy.has("publishing")) return;
      await runStage("publishing", { extraButton: button });
      await refreshCurrent({ silent: true });
    });

    $("#menu-toggle")?.addEventListener("click", openMobileNav);
    $("#nav-scrim")?.addEventListener("click", closeMobileNav);
    $("#sidebar-collapse")?.addEventListener("click", toggleSidebar);

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const wasOpen = $("#app-shell")?.classList.contains("nav-open");
      closeMobileNav();
      if (wasOpen) $("#menu-toggle")?.focus();
    });
    window.addEventListener("resize", () => {
      applySidebarState();
      if (window.innerWidth > 960) closeMobileNav();
    });
    window.addEventListener("beforeunload", (event) => {
      if (!state.detailDirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
    window.addEventListener("hashchange", route);

    window.setInterval(() => {
      if (document.hidden) return;
      if (state.view === "queue") return;
      if (
        state.stageBusy.size ||
        state.pipelineBusy ||
        state.refreshing ||
        state.detailDirty
      ) {
        return;
      }
      refreshCurrent({ silent: true });
    }, 45_000);
  }

  function init() {
    applySidebarState();
    wireGlobal();
    populateRunsFilter();
    route();
  }

  init();
})();
