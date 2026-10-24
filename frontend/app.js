(() => {
  "use strict";

  const API = "/api/v1";
  const RUN_ACTIONS = [
    ["Collect news", "/collection/runs", "Fetch configured RSS feeds"],
    ["Deduplicate", "/deduplication/runs", "Cluster syndicated reports"],
    ["Check credibility", "/credibility/runs", "Corroborate claims"],
    ["Build research", "/research/runs", "Create an evidence brief"],
    ["Write scripts", "/script-writer/runs", "Draft neutral narration"],
    ["Plan scenes", "/scene-planner/runs", "Build the visual timeline"],
    ["Narrate", "/tts/runs", "Synthesize local audio"],
    ["Render video", "/video-generator/runs", "Create the draft MP4"],
    ["Process publishing", "/publishing/runs", "Run the approved publish queue"],
  ];
  // Leave publishing out of the automatic advance action: processing the queue is explicit.
  const PIPELINE = RUN_ACTIONS.slice(1, 8);
  const VIEW_META = {
    queue: ["Editorial desk", "Review queue"],
    runs: ["Operations", "Agent runs"],
    publishing: ["Distribution", "Publishing"],
    articles: ["Evidence library", "Articles"],
  };

  const state = {
    view: "queue",
    queue: [],
    selectedId: null,
    detail: null,
    busy: false,
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

  function fmtDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const mins = Math.floor(total / 60);
    const secs = String(total % 60).padStart(2, "0");
    return `${mins}:${secs}`;
  }

  function badge(status) {
    const value = String(status || "unknown");
    const cls = ["failed", "failed-check"].includes(value) ? "failed" : value;
    return `<span class="badge ${esc(cls)}">${esc(value.replaceAll("_", " "))}</span>`;
  }

  function safeUrl(url) {
    try {
      const parsed = new URL(url, window.location.origin);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
    } catch {
      return null;
    }
  }

  function externalLink(url, label) {
    const href = safeUrl(url);
    if (!href) return `<span class="muted">${esc(url || "—")}</span>`;
    return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label || href)}</a>`;
  }

  function toast(message, kind = "ok") {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show${kind === "error" ? " error" : ""}`;
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => {
      node.className = "toast";
    }, 5200);
  }

  function errorMessage(error) {
    if (!error) return "Unknown error";
    if (typeof error === "string") return error;
    if (typeof error.detail === "string") return error.detail;
    if (error.detail?.message) return error.detail.message;
    return error.message || "Request failed";
  }

  async function api(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = text;
    }
    if (!response.ok) {
      const err = new Error(
        typeof payload?.detail === "string"
          ? payload.detail
          : payload?.detail?.message || `${response.status} ${response.statusText}`,
      );
      err.detail = payload?.detail;
      err.status = response.status;
      throw err;
    }
    return payload;
  }

  async function withBusy(button, task) {
    if (state.busy) return;
    state.busy = true;
    const original = button?.textContent;
    if (button) {
      button.disabled = true;
      button.textContent = "Working…";
    }
    try {
      await task();
    } catch (error) {
      console.error(error);
      toast(errorMessage(error), "error");
    } finally {
      state.busy = false;
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  function showView(view) {
    state.view = view;
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    $$(".view").forEach((node) => {
      node.hidden = node.id !== `view-${view}`;
    });
    const [eyebrow, title] = VIEW_META[view];
    $("#view-eyebrow").textContent = eyebrow;
    $("#view-title").textContent = title;
    if (view === "queue") loadQueue();
    if (view === "runs") loadRuns();
    if (view === "publishing") loadPublishing();
    if (view === "articles") loadArticles();
  }

  async function loadStatus() {
    try {
      const [status, analytics] = await Promise.all([
        api("/system/status"),
        api("/reviews/analytics"),
      ]);
      $("#system-dot").classList.toggle("ok", status.status === "ok");
      $("#system-status").textContent = status.scheduler_running
        ? "Scheduler running"
        : "API online · scheduler idle";
      const capabilityLabels = {
        video_ffmpeg: "FFmpeg for video",
        tts_ffmpeg: "FFmpeg for MP3 audio",
        piper_tts: "Piper TTS",
        local_embeddings: "Local embedding model",
        local_publishing: "Local publishing",
        youtube_publishing: "YouTube credentials",
      };
      const missing = Object.entries(status.capabilities || {})
        .filter(([, ready]) => !ready)
        .map(([key]) => capabilityLabels[key] || key);
      const capabilityNode = $("#system-capabilities");
      capabilityNode.textContent = missing.length
        ? `Setup needed: ${missing.join(", ")}`
        : "All optional capabilities are configured.";
      capabilityNode.classList.toggle("ready", missing.length === 0);
      $("#nav-pending").textContent = analytics.pending;
    } catch (error) {
      $("#system-status").textContent = "API unavailable";
      $("#system-dot").classList.remove("ok");
    }
  }

  function statsMarkup(items) {
    return items
      .map(
        ([label, value]) => `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`,
      )
      .join("");
  }

  async function loadReviewStats() {
    try {
      const analytics = await api("/reviews/analytics");
      $("#review-stats").innerHTML = statsMarkup([
        ["Pending review", analytics.pending],
        ["Approved", analytics.approved],
        ["Changes requested", analytics.changes_requested],
        ["Exported / published", Number(analytics.exported) + Number(analytics.published)],
      ]);
      $("#nav-pending").textContent = analytics.pending;
    } catch (error) {
      console.warn(error);
    }
  }

  async function loadQueue() {
    await Promise.all([loadReviewStats(), loadStatus()]);
    const status = $("#queue-filter")?.value || "";
    try {
      const queue = await api(`/reviews?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`);
      state.queue = queue;
      renderQueue();
      if (state.selectedId && !queue.some((item) => item.video_render_id === state.selectedId)) {
        state.selectedId = null;
        state.detail = null;
      }
      if (state.selectedId) {
        await loadDetail(state.selectedId);
      } else if (queue.length) {
        await selectReview(queue[0].video_render_id);
      } else {
        renderEmptyDetail();
      }
    } catch (error) {
      $("#review-list").innerHTML = `<div class="empty-state"><p>${esc(errorMessage(error))}</p></div>`;
    }
  }

  function renderQueue() {
    const list = $("#review-list");
    if (!state.queue.length) {
      list.innerHTML = `<div class="empty-state"><div class="empty-glyph">◇</div><h2>No drafts here</h2><p>Run the pipeline to collect news and produce a reviewable video.</p></div>`;
      return;
    }
    list.innerHTML = state.queue
      .map(
        (item) => `
        <button class="review-card ${state.selectedId === item.video_render_id ? "selected" : ""}" data-review-id="${esc(item.video_render_id)}">
          <div class="chips">${badge(item.approval_status)}${badge(item.credibility_status || "unknown")}</div>
          <h3>${esc(item.title)}</h3>
          <div class="review-meta">
            <span>${esc(item.source_name)}</span>
            <span>${esc(item.category || "uncategorised")}</span>
            <span>${fmtDuration(item.duration_seconds)}</span>
            <span>priority ${esc(item.priority_score)}</span>
          </div>
          <div class="review-meta"><span>${fmtDate(item.created_at)}</span>${item.publish_status ? badge(item.publish_status) : ""}</div>
        </button>`,
      )
      .join("");
    $$(".review-card", list).forEach((card) => {
      card.addEventListener("click", () => selectReview(card.dataset.reviewId));
    });
  }

  async function selectReview(id) {
    state.selectedId = id;
    renderQueue();
    await loadDetail(id);
  }

  function renderEmptyDetail() {
    $("#review-detail").innerHTML = `
      <div class="empty-state">
        <div class="empty-glyph">◎</div>
        <h2>Select a draft</h2>
        <p>Inspect sources, claims, script, scenes, and quality gates before approving or rejecting.</p>
      </div>`;
  }

  async function loadDetail(id) {
    const panel = $("#review-detail");
    panel.innerHTML = `<div class="empty-state"><p>Loading review…</p></div>`;
    try {
      state.detail = await api(`/reviews/${encodeURIComponent(id)}`);
      renderDetail(state.detail);
    } catch (error) {
      if (error.status === 404) {
        state.detail = null;
        state.selectedId = null;
        renderEmptyDetail();
        await loadQueue();
        return;
      }
      panel.innerHTML = `<div class="empty-state"><p>${esc(errorMessage(error))}</p></div>`;
    }
  }

  function renderDetail(detail) {
    const { summary, article, script, credibility, research, scene_plan, approval } = detail;
    const canEdit = !detail.publishing_records.length;
    const claims = credibility?.evidence?.length
      ? credibility.evidence
          .map(
            (evidence) => `
          <li>
            ${externalLink(evidence.url, evidence.publisher)} — ${esc(evidence.title)}
            <span class="muted">(relevance ${esc(evidence.relevance_score)}, trust ${esc(evidence.trust_weight)})</span>
            ${evidence.snippet ? `<br><span class="muted">${esc(evidence.snippet)}</span>` : ""}
          </li>`,
          )
          .join("")
      : `<li class="muted">No corroborating evidence items were stored.</li>`;
    const claimList = credibility?.claims?.length
      ? credibility.claims.map((claim) => `<div class="claim"><p>${esc(claim)}</p></div>`).join("")
      : `<p class="muted">No claims were extracted.</p>`;

    const researchBlock = research
      ? `
        <div class="section-grid">
          <div><h3>Key facts</h3>${renderList(research.key_facts)}</div>
          <div><h3>Context</h3>${renderList(research.context)}</div>
          <div><h3>Uncertainties</h3>${renderList(research.uncertainties)}</div>
        </div>
        <h3 style="margin-top:14px">Sources</h3>
        <div class="source-list">${
          research.sources.length
            ? research.sources
                .map(
                  (source) => `<div class="source-item"><span>${externalLink(source.url, source.publisher)}</span><span>${esc(source.title)}</span></div>`,
                )
                .join("")
            : `<span class="muted">No additional sources were retrieved.</span>`
        }</div>`
      : `<p class="muted">No research brief is available.</p>`;

    panelShell(detail, `
      <div class="detail-head">
        <div>
          <p class="eyebrow">${esc(article.source_name)} · ${esc(article.category || "uncategorised")}</p>
          <h2>${esc(summary.title)}</h2>
          <div class="chips" style="margin-top:8px">
            ${badge(approval.status)}${badge(summary.credibility_status || "unknown")}${badge(summary.video_status)}
            <span class="chip">${fmtDuration(summary.duration_seconds)}</span>
            <span class="chip">priority ${esc(summary.priority_score)}</span>
          </div>
        </div>
        <div class="chips">
          ${article.url ? `<a class="button ghost small" href="${esc(safeUrl(article.url) || "#")}" target="_blank" rel="noopener noreferrer">Open source</a>` : ""}
        </div>
      </div>

      <video controls preload="metadata" src="${API}/reviews/videos/${esc(detail.video.id)}/file"></video>

      <section class="detail-section">
        <h3>Story provenance</h3>
        <div class="section-grid">
          <div class="kv"><span class="k">Original headline</span><span class="v">${esc(article.title)}</span></div>
          <div class="kv"><span class="k">Published</span><span class="v">${fmtDate(article.published_at)}</span></div>
          <div class="kv"><span class="k">Collected</span><span class="v">${fmtDate(article.collected_at)}</span></div>
          <div class="kv"><span class="k">Credibility</span><span class="v">${esc(credibility ? `${credibility.status} (${credibility.confidence_score})` : "not assessed")}</span></div>
        </div>
        <p class="muted" style="margin-top:12px">${esc(credibility?.explanation || "No credibility explanation stored.")}</p>
        <h3 style="margin-top:14px">Reported claims</h3>
        <div class="claims">${claimList}</div>
        <h3 style="margin-top:14px">Evidence trail</h3>
        <ul class="evidence">${claims}</ul>
      </section>

      <section class="detail-section">
        <h3>Research brief <span class="muted">${esc(research?.source_count ?? 0)} sources</span></h3>
        ${researchBlock}
      </section>

      <section class="detail-section">
        <h3>Script
          <span class="muted">${canEdit ? "Editing invalidates generated media" : "Locked by publishing activity"}</span>
        </h3>
        <form id="script-form" class="form-grid">
          <div class="form-row"><label for="script-title">Headline</label><input class="input" id="script-title" name="title" maxlength="300" value="${esc(script.title)}" ${canEdit ? "" : "disabled"}></div>
          <div class="form-row"><label for="script-hook">Hook</label><input class="input" id="script-hook" name="hook" maxlength="2000" value="${esc(script.hook)}" ${canEdit ? "" : "disabled"}></div>
          <div class="form-row"><label for="script-intro">Introduction</label><textarea id="script-intro" name="introduction" maxlength="4000" ${canEdit ? "" : "disabled"}>${esc(script.introduction)}</textarea></div>
          <div class="form-row"><label for="script-main">Main story — one paragraph per line</label><textarea id="script-main" name="main_story" ${canEdit ? "" : "disabled"}>${esc(script.main_story.join("\n"))}</textarea></div>
          <div class="form-row"><label for="script-context">Context — one paragraph per line</label><textarea id="script-context" name="context" ${canEdit ? "" : "disabled"}>${esc(script.context.join("\n"))}</textarea></div>
          <div class="form-row"><label for="script-closing">Closing</label><textarea id="script-closing" name="closing" maxlength="2000" ${canEdit ? "" : "disabled"}>${esc(script.closing)}</textarea></div>
          <div class="form-actions">
            <button class="button" type="submit" ${canEdit ? "" : "disabled"}>Save script</button>
            <span class="muted">Status: ${esc(script.status)}${script.review_reasons.length ? ` · ${esc(script.review_reasons.join(" "))}` : ""}</span>
          </div>
        </form>
      </section>

      <section class="detail-section">
        <h3>Scene plan <span class="muted">${scene_plan.scenes.length} scenes · ${fmtDuration(scene_plan.total_duration_seconds)}</span></h3>
        ${scene_plan.scenes.map((scene) => sceneEditor(scene, canEdit)).join("") || `<p class="muted">No scenes.</p>`}
      </section>

      <section class="detail-section" id="quality-section">
        <h3>Automated quality gate</h3>
        <div class="quality-list">${qualityMarkup(approval.quality_checks)}</div>
        <p class="muted" style="margin-top:10px">Approval also pins the SHA-256 fingerprint of this exact video artifact. If the file changes, publishing is blocked.</p>
      </section>

      <section class="detail-section">
        <h3>Human decision</h3>
        ${approval.reviewer ? `<p class="muted">Last decision by ${esc(approval.reviewer)} on ${fmtDate(approval.decided_at)}. ${esc(approval.notes || "")}</p>` : ""}
        ${
          detail.publishing_records.length
            ? `<p class="muted">Approval is locked because this draft already has publishing activity. This prevents a queued or published artifact from being re-approved after modification.</p>`
            : `<div class="form-row"><label for="reviewer">Reviewer</label><input class="input" id="reviewer" maxlength="120" value="${esc(approval.reviewer || "operator")}"></div>
        <div class="form-row" style="margin-top:10px"><label for="review-notes">Review notes</label><textarea id="review-notes" maxlength="4000" placeholder="Required context for rejection or requested changes">${esc(approval.notes || "")}</textarea></div>
        <div class="form-actions" style="margin-top:11px">
          <button class="button ok" data-decision="approve">Approve</button>
          <button class="button ghost" data-decision="request_changes">Request changes</button>
          <button class="button danger" data-decision="reject">Reject</button>
        </div>`
        }
      </section>

      <section class="detail-section">
        <h3>Publishing <span class="muted">Approval status: ${esc(approval.status)}</span></h3>
        ${publishingMarkup(detail)}
      </section>
    `);

    wireDetail(detail);
  }

  function panelShell(detail, content) {
    $("#review-detail").innerHTML = `<div class="detail">${content}</div>`;
  }

  function renderList(items) {
    if (!items?.length) return `<p class="muted">None recorded.</p>`;
    return `<ul class="list">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
  }

  function qualityMarkup(checks) {
    if (!checks?.length) return `<p class="muted">No checks have been recorded yet.</p>`;
    return checks
      .map(
        (check) => `
        <div class="quality-item ${esc(check.status)}">
          <span class="status">${esc(check.status)}</span>
          <span><strong>${esc(check.name.replaceAll("_", " "))}</strong><br><span class="muted">${esc(check.detail)}</span></span>
        </div>`,
      )
      .join("");
  }

  function sceneEditor(scene, canEdit) {
    return `
      <details class="scene-card">
        <summary><span><span class="scene-number">SCENE ${esc(scene.position)}</span> · ${fmtDuration(scene.start_seconds)}–${fmtDuration(scene.start_seconds + scene.duration_seconds)}</span><span class="muted">${esc(scene.transition)}</span></summary>
        <div class="scene-body">
          <div class="timeline-text">${esc(scene.narration)}</div>
          <div class="form-row"><label>Subtitle</label><textarea data-scene-field="subtitle" ${canEdit ? "" : "disabled"}>${esc(scene.subtitle)}</textarea></div>
          <div class="form-row"><label>Visual description</label><textarea data-scene-field="scene_description" ${canEdit ? "" : "disabled"}>${esc(scene.scene_description)}</textarea></div>
          <div class="form-row two">
            <div><label>Stock footage query</label><input class="input" data-scene-field="stock_footage_query" value="${esc(scene.stock_footage_query)}" ${canEdit ? "" : "disabled"}></div>
            <div><label>Image search query</label><input class="input" data-scene-field="image_search_query" value="${esc(scene.image_search_query)}" ${canEdit ? "" : "disabled"}></div>
          </div>
          <div class="form-row"><label>Animation suggestion</label><input class="input" data-scene-field="animation_suggestion" value="${esc(scene.animation_suggestion)}" ${canEdit ? "" : "disabled"}></div>
          <div class="form-row two">
            <div><label>Keywords — comma separated</label><input class="input" data-scene-field="keywords" value="${esc(scene.keywords.join(", "))}" ${canEdit ? "" : "disabled"}></div>
            <div><label>Transition</label><input class="input" data-scene-field="transition" value="${esc(scene.transition)}" ${canEdit ? "" : "disabled"}></div>
          </div>
          <div class="form-actions">
            <button class="button small" data-save-scene="${esc(scene.id)}" ${canEdit ? "" : "disabled"}>Save scene</button>
            <span class="muted">Timing and narration stay tied to synthesized audio.</span>
          </div>
        </div>
      </details>`;
  }

  function publishingMarkup(detail) {
    const records = detail.publishing_records || [];
    const approved = detail.approval.status === "approved";
    const queueForm = approved
      ? `
      <form id="publish-form" class="form-grid" style="margin-top:12px">
        <div class="form-row two">
          <div><label for="publish-platform">Platform</label><select class="input" id="publish-platform"><option value="local">Local export (no external upload)</option><option value="youtube">YouTube (optional credentials)</option></select></div>
          <div><label for="publish-title">Title override</label><input class="input" id="publish-title" maxlength="100" placeholder="${esc(detail.summary.title)}"></div>
        </div>
        <div class="form-row"><label for="publish-description">Description override</label><textarea id="publish-description" maxlength="4900" placeholder="Leave blank to use the script plus cited sources"></textarea></div>
        <div class="form-actions"><button class="button" type="submit">Queue for publishing</button><span class="muted">Queuing does not upload until the publishing queue is processed.</span></div>
      </form>`
      : `<p class="muted">Approve this draft before queueing a publishing action.</p>`;

    const recordList = records.length
      ? `<div class="record-list" style="margin-top:12px">${records
          .map(
            (record) => `
          <div class="record">
            <div><strong>${esc(record.platform)}</strong> ${badge(record.status)}<small>Queued ${fmtDate(record.created_at)} · attempts ${esc(record.attempts)}</small>
              ${record.last_error ? `<small class="errors">${esc(record.last_error)}</small>` : ""}
            </div>
            <div style="text-align:right">
              ${record.external_url ? externalLink(record.external_url, "Open result") : `<span class="muted">${esc(record.external_id || "No external result")}</span>`}
              ${["failed", "blocked"].includes(record.status) ? `<br><button class="button ghost small" data-retry-record="${esc(record.id)}">Retry</button>` : ""}
            </div>
          </div>`,
          )
          .join("")}</div>`
      : "";
    return `${queueForm}${recordList}`;
  }

  function wireDetail(detail) {
    $$("[data-decision]", $("#review-detail")).forEach((button) => {
      button.addEventListener("click", () =>
        withBusy(button, async () => {
          const action = button.dataset.decision;
          const notes = $("#review-notes")?.value.trim() || null;
          const reviewer = $("#reviewer")?.value.trim() || "operator";
          if (action !== "approve" && !notes) {
            throw new Error("Add review notes before rejecting or requesting changes.");
          }
          if (!window.confirm(`Record decision “${action.replaceAll("_", " ")}”?`)) return;
          try {
            await api(`/reviews/${encodeURIComponent(detail.video.id)}/decision`, {
              method: "POST",
              body: JSON.stringify({ action, reviewer, notes }),
            });
            toast(`Decision recorded: ${action.replaceAll("_", " ")}`);
          } catch (error) {
            if (error.status === 422 && Array.isArray(error.detail?.checks)) {
              $("#quality-section .quality-list").innerHTML = qualityMarkup(error.detail.checks);
            }
            throw error;
          }
          await loadQueue();
          await loadDetail(detail.video.id);
          await loadStatus();
        }),
      );
    });

    $("#script-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = $("button[type=submit]", form);
      withBusy(submit, async () => {
        if (!window.confirm("Saving the script deletes generated audio, scenes, video, and approval so the pipeline can rebuild them. Continue?")) return;
        const payload = {
          title: $("#script-title").value.trim(),
          hook: $("#script-hook").value.trim(),
          introduction: $("#script-intro").value.trim(),
          main_story: $("#script-main").value.split("\n").map((item) => item.trim()).filter(Boolean),
          context: $("#script-context").value.split("\n").map((item) => item.trim()).filter(Boolean),
          closing: $("#script-closing").value.trim(),
          acknowledge_media_invalidation: true,
          actor: "operator",
        };
        await api(`/reviews/scripts/${encodeURIComponent(detail.script.id)}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        state.selectedId = null;
        state.detail = null;
        renderEmptyDetail();
        await loadQueue();
        toast("Script saved. Generated media was invalidated; advance the pipeline to rebuild it.");
      });
    });

    $$("[data-save-scene]", $("#review-detail")).forEach((button) => {
      button.addEventListener("click", () =>
        withBusy(button, async () => {
          const card = button.closest(".scene-card");
          const values = {};
          $$("[data-scene-field]", card).forEach((input) => {
            values[input.dataset.sceneField] =
              input.dataset.sceneField === "keywords"
                ? input.value.split(",").map((item) => item.trim()).filter(Boolean)
                : input.value.trim();
          });
          if (!window.confirm("Saving scene edits invalidates the current video and approval. Continue?")) return;
          await api(`/reviews/scenes/${encodeURIComponent(button.dataset.saveScene)}`, {
            method: "PATCH",
            body: JSON.stringify({ ...values, acknowledge_media_invalidation: true, actor: "operator" }),
          });
          state.selectedId = null;
          state.detail = null;
          renderEmptyDetail();
          await loadQueue();
          toast("Scene saved. The previous video was invalidated; rerun video generation.");
        }),
      );
    });

    $("#publish-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const submit = $("button[type=submit]", event.currentTarget);
      withBusy(submit, async () => {
        const payload = {
          video_render_id: detail.video.id,
          platform: $("#publish-platform").value,
          title: $("#publish-title").value.trim() || null,
          description: $("#publish-description").value.trim() || null,
          tags: [],
        };
        await api("/publishing/queue", { method: "POST", body: JSON.stringify(payload) });
        toast("Queued. Use “Process queue” to run the approved publishing action.");
        await loadDetail(detail.video.id);
        await loadQueue();
      });
    });

    $$("[data-retry-record]", $("#review-detail")).forEach((button) => {
      button.addEventListener("click", () =>
        withBusy(button, async () => {
          await api(`/publishing/records/${encodeURIComponent(button.dataset.retryRecord)}/retry`, {
            method: "POST",
          });
          toast("Publishing record re-queued.");
          await loadDetail(detail.video.id);
        }),
      );
    });
  }

  async function triggerRun(label, path, button) {
    await withBusy(button, async () => {
      const result = await api(path, { method: "POST" });
      toast(`${label}: ${result.status || "completed"} · ${result.items_created ?? 0} created`);
      await refreshCurrent();
    });
  }

  async function loadRuns() {
    renderRunActions();
    try {
      const runs = await api("/agent-runs?limit=80");
      $("#runs-body").innerHTML = runs.length
        ? runs
            .map(
              (run) => `
          <tr>
            <td>${esc(run.agent_name)}</td>
            <td>${badge(run.status)}</td>
            <td>${esc(run.items_seen)}</td>
            <td>${esc(run.items_created)}</td>
            <td>${esc(run.items_updated)}</td>
            <td>${fmtDate(run.started_at)}</td>
            <td>
              ${run.errors.length ? `<div class="errors">${run.errors.map(esc).join("<br>")}</div>` : ""}
              ${run.warnings.length ? `<div class="warnings">${run.warnings.map(esc).join("<br>")}</div>` : ""}
              ${!run.errors.length && !run.warnings.length ? `<span class="muted">—</span>` : ""}
            </td>
          </tr>`,
            )
            .join("")
        : `<tr><td colspan="7" class="muted">No runs recorded yet.</td></tr>`;
    } catch (error) {
      $("#runs-body").innerHTML = `<tr><td colspan="7">${esc(errorMessage(error))}</td></tr>`;
    }
  }

  function renderRunActions() {
    const container = $("#run-actions");
    if (container.dataset.ready) return;
    container.dataset.ready = "1";
    container.innerHTML = RUN_ACTIONS.map(
      ([label, path, description]) =>
        `<button class="button ghost run-button" data-run-path="${esc(path)}" data-run-label="${esc(label)}"><span>${esc(label)}</span><small>${esc(description)}</small></button>`,
    ).join("");
    $$("[data-run-path]", container).forEach((button) => {
      button.addEventListener("click", () =>
        triggerRun(button.dataset.runLabel, button.dataset.runPath, button),
      );
    });
  }

  async function runPipeline() {
    const button = $("#pipeline-button");
    if (state.busy) return;
    state.busy = true;
    button.disabled = true;
    const original = button.textContent;
    try {
      for (const [label, path] of PIPELINE) {
        button.textContent = `${label}…`;
        try {
          const result = await api(path, { method: "POST" });
          console.info(label, result.status);
        } catch (error) {
          toast(`Pipeline stopped at ${label}: ${errorMessage(error)}`, "error");
          await loadRuns();
          return;
        }
      }
      toast("Pipeline advanced. Review new drafts or inspect run logs for failures.");
      await loadQueue();
      await loadStatus();
    } finally {
      state.busy = false;
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function loadPublishing() {
    try {
      const [analytics, records] = await Promise.all([
        api("/publishing/analytics?days=30"),
        api("/publishing/records?limit=100"),
      ]);
      $("#publishing-stats").innerHTML = statsMarkup([
        ["Approved videos", analytics.approved_videos],
        ["Pending reviews", analytics.pending_reviews],
        ["Published externally", analytics.external_publish_count],
        ["Success rate (30d)", `${analytics.publishing_success_rate}%`],
      ]);
      $("#publishing-body").innerHTML = records.length
        ? records
            .map(
              (record) => `
          <tr>
            <td>${esc(record.title)}</td>
            <td>${esc(record.platform)}</td>
            <td>${badge(record.status)}</td>
            <td>${esc(record.attempts)}</td>
            <td>${record.external_url ? externalLink(record.external_url, "Open result") : `<span class="muted">${esc(record.external_id || record.last_error || "—")}</span>`}</td>
            <td>${fmtDate(record.updated_at)}</td>
            <td>${["failed", "blocked"].includes(record.status) ? `<button class="button ghost small" data-publishing-retry="${esc(record.id)}">Retry</button>` : ""}</td>
          </tr>`,
            )
            .join("")
        : `<tr><td colspan="7" class="muted">No publishing records yet. Approve a draft, then queue it from the review panel.</td></tr>`;
      $$("[data-publishing-retry]").forEach((button) => {
        button.addEventListener("click", () =>
          withBusy(button, async () => {
            await api(`/publishing/records/${encodeURIComponent(button.dataset.publishingRetry)}/retry`, {
              method: "POST",
            });
            toast("Publishing record re-queued.");
            await loadPublishing();
          }),
        );
      });
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  }

  async function loadArticles() {
    try {
      const articles = await api("/articles?limit=100");
      $("#articles-list").innerHTML = articles.length
        ? articles
            .map(
              (article) => `
          <article class="article-card">
            <div class="chips">${badge(article.category || "uncategorised")}<span class="chip">${esc(article.country || "—")}</span><span class="chip">${esc(article.language || "—")}</span></div>
            <h3>${externalLink(article.url, article.title)}</h3>
            <p>${esc(article.summary || article.normalized_text.slice(0, 260))}</p>
            <small>${esc(article.source_name)} · ${fmtDate(article.published_at || article.collected_at)}</small>
          </article>`,
            )
            .join("")
        : `<div class="empty-state"><p>No articles collected yet.</p></div>`;
    } catch (error) {
      $("#articles-list").innerHTML = `<div class="empty-state"><p>${esc(errorMessage(error))}</p></div>`;
    }
  }

  async function refreshCurrent() {
    await loadStatus();
    if (state.view === "queue") await loadQueue();
    if (state.view === "runs") await loadRuns();
    if (state.view === "publishing") await loadPublishing();
    if (state.view === "articles") await loadArticles();
  }

  function wireGlobal() {
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => showView(item.dataset.view));
    });
    $("#refresh-button").addEventListener("click", () => refreshCurrent());
    $("#pipeline-button").addEventListener("click", runPipeline);
    $("#queue-filter").addEventListener("change", () => loadQueue());
    $("#run-publishing-button").addEventListener("click", (event) =>
      triggerRun("Process publishing", "/publishing/runs", event.currentTarget),
    );
  }

  wireGlobal();
  showView("queue");
  loadStatus();
})();
