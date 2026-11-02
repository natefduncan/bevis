// Loading and rendering a report: the header, the blocks, and the audit marks.

import { $, STATUS_LABEL, api, atLeast, canDiscuss, canFile, claimById, evidence, fill, h, plural, readHash, state } from "./core.js";
import { drawChart } from "./charts.js";
import { openClaim, openEvidence } from "./panel.js";
import { openChallenge, openChallengeList, openCompose } from "./challenges.js";
import { flaggedBlocks, openReview, reviewFlag, reviewRow } from "./review.js";
import { openSuggestion } from "./suggestions.js";
import { showIndex, when } from "./people.js";
import { blockEditor, canEditBlock, openEditor, suggesting } from "./editor.js";

// -- page -----------------------------------------------------------------------------

export function notice(err, fallback) {
  return h("div", { class: "notice error" }, h("p", { text: err.status === 401 ? fallback : err.message }));
}

export async function load() {
  const hash = readHash();
  state.key = hash.key;
  if (!state.key) {
    fill($("report"),
      h("div", { class: "notice" },
        h("p", { text: "This page opens a report from a share link, or your reports from a person token." }),
        h("p", { text: "Ask the report's owner for a link. It looks like /view#key=bvl_…" })),
    );
    return;
  }
  try {
    state.me = await api("/api/me");
  } catch (err) {
    fill($("report"), notice(err, "This link or token is invalid, expired, or revoked."));
    return;
  }
  state.person = state.me.kind === "person";
  state.rid = state.person ? hash.report : null;
  $("signout").hidden = !state.person;
  $("home").hidden = !state.person || !state.rid;
  if (state.person && !state.rid) {
    await showIndex(hash.tab);
    return;
  }
  state.version = hash.v === null || hash.v === "draft" ? null : Number(hash.v);
  await loadReport();
  if (hash.c && claimById(hash.c)) openClaim(hash.c, { scroll: true });
  else if (hash.ch) openChallenge(hash.ch);
  else if (hash.s) openSuggestion(hash.s);
  else if (hash.rv) openReview(hash.rv);
  else if (hash.e) {
    const [id, ver] = hash.e.split("@");
    let row = null;
    try { row = hash.row ? JSON.parse(hash.row) : null; } catch { row = null; }
    openEvidence(id, Number(ver), row);
  }
}

export async function loadReport() {
  const report = $("report");
  fill(report, h("p", { class: "meta", text: "Loading…" }));
  try {
    const list = await api("/api/reports");
    const entry = state.person ? list.find((r) => r.id === state.rid) : list[0];
    if (!entry) {
      throw new Error(state.person ? "That report is not in your reports any more." : "This link does not open any report.");
    }
    const rid = entry.id;
    state.role = entry.role;
    state.me = state.me || await api("/api/me");
    const q = new URLSearchParams({ render: "true" });
    if (state.version !== null) q.set("version", String(state.version));
    state.report = await api(`/api/reports/${rid}?${q}`);
  } catch (err) {
    fill(report, notice(err, state.person ? "This token is invalid or has been revoked." : "This link is invalid, expired, or revoked."));
    return;
  }
  state.evidence.clear();
  await loadChallenges();
  await loadSuggestions();
  renderHeader();
  await renderReport();
}

async function loadSuggestions() {
  state.suggestions = [];
  if (state.report.kind !== "draft" || !atLeast("suggester")) return;
  try {
    state.suggestions = await api(`/api/reports/${state.report.id}/suggestions`);
  } catch {
    state.suggestions = [];
  }
}

export async function loadChallenges() {
  state.challenges = [];
  if (!canDiscuss()) return;
  const q = state.report.kind === "version" ? `?version=${state.report.version}` : "";
  try {
    state.challenges = await api(`/api/reports/${state.report.id}/challenges${q}`);
  } catch {
    state.challenges = [];
  }
}

const liveChallenges = (target) => state.challenges.filter((c) =>
  (c.status === "open" || c.status === "answered") && (!target || sameTarget(c.target, target)));

export const sameTarget = (a, b) => a.claim === b.claim && a.block === b.block;

export function renderHeader() {
  const r = state.report;
  document.title = `${r.title} · Bevis`;
  $("title").textContent = r.title;
  const meta = $("meta");
  const counts = r.validation.counts;
  const summary = counts.errors
    ? h("span", { class: "chip error", text: plural(counts.errors, "error") })
    : counts.warnings
      ? h("span", { class: "chip warning", text: plural(counts.warnings, "warning") })
      : h("span", { class: "chip ok", text: "no errors" });
  if (r.kind === "draft") {
    const flagged = flaggedBlocks();
    fill(meta, `Draft, revision ${r.revision}`, summary,
      flagged.length ? h("button", { class: "chip error chip-button", type: "button",
        title: "Blocks edited near a claim wait for an admin's review before the report can be published",
        text: `${plural(flagged.length, "block")} to review`, onclick: () => openReview(flagged[0]) }) : null);
  } else {
    const by = r.published_by;
    const when = new Date(r.published_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    fill(meta,
      `Version ${r.version} · published ${when} by ${by.name}${by.verified ? "" : " (unverified)"}`,
      summary,
      r.override ? h("span", { class: "chip error", title: r.override.reason, text: "published with override" }) : null,
      r.disclosure !== "full" ? h("span", { class: "chip", text: `${r.disclosure} disclosure` }) : null);
  }

  const select = $("version");
  const options = [];
  const seesDraft = ["suggester", "editor", "admin"].includes(r.role);
  if (seesDraft) options.push(h("option", { value: "draft", text: "Draft" }));
  for (const v of r.versions) options.push(h("option", { value: String(v.number), text: `Version ${v.number}` }));
  fill(select, ...options);
  select.value = r.kind === "draft" ? "draft" : String(r.version);
  $("version-control").hidden = options.length < 2;

  const live = liveChallenges().length;
  $("challenges").hidden = !canDiscuss();
  $("challenges").textContent = live ? `Challenges (${live} unresolved)` : "Challenges";
  const suggestable = r.kind === "draft" && atLeast("suggester");
  $("suggestions").hidden = !suggestable;
  $("suggestions").textContent = state.suggestions.length ? `Suggestions (${state.suggestions.length})` : "Suggestions";
  $("share").hidden = !atLeast("admin");
  $("publish").hidden = !(r.kind === "draft" && atLeast("editor"));
  $("pdf").hidden = r.kind !== "version"; // only a published version exports
}

export async function renderReport() {
  const r = state.report;
  const root = $("report");
  fill(root);
  root.classList.toggle("audit", state.audit);
  if (state.audit) root.append(auditSummary());

  const charts = [];
  r.blocks.forEach((block, i) => {
    let el;
    if (block.type === "heading") {
      if (i === 0 && block.level === 1 && block.text.trim() === r.title.trim()) return; // already in the bar
      el = h(`h${Math.min(block.level + 1, 6)}`, { text: block.text });
    } else if (block.type === "text") {
      el = h("div", { class: "text-block" });
      el.innerHTML = block.html; // sanitized by the server: no raw HTML, attributes, or unsafe links
      wireClaims(el);
    } else if (block.type === "chart") {
      el = h("figure", { class: "chart" },
        h("div", { class: "vega", role: "img", "aria-label": block.caption || "Chart" }),
        h("figcaption", {}, block.caption || "", evidenceChips(block.evidence), challengeFlag({ block: block.id }),
          canFile() ? h("button", { class: "ev-chip quiet-chip", type: "button", text: "Challenge",
            onclick: () => openCompose({ block: block.id }) }) : null));
      charts.push([el.querySelector(".vega"), block]);
    } else {
      el = h("p", { class: "notice", text: `Unsupported block type: ${block.type}` });
    }
    if (state.editing.has(block.id) && canEditBlock(block)) el = blockEditor(block);
    el.id = `blk-${block.id}`;
    el.dataset.block = block.id;
    const review = reviewFlag(block.id);
    if (review) el.classList.add("needs-review");
    root.append(el);
    if (review) root.append(reviewRow(block, review));
    if (canEditBlock(block) && !state.editing.has(block.id)) {
      root.append(h("div", { class: "edit-row" },
        h("button", { class: "quiet edit-block", type: "button", text: suggesting() ? "Suggest edit" : "Edit",
          "aria-label": `${suggesting() ? "Suggest an edit to" : "Edit"} ${block.type === "heading" ? "heading" : "paragraph"} ${block.id}`,
          onclick: () => openEditor(block) })));
    }
    if (state.flash?.block === block.id) {
      root.append(h("div", { class: "notice flash", role: "status", text: state.flash.text }));
      state.flash = null;
    }
  });
  state.flash = null;
  markFlaggedBlocks();
  await Promise.all(charts.map(([el, block]) => drawChart(el, block)));
}

function wireClaims(container) {
  for (const span of container.querySelectorAll("span.claim")) {
    const claim = claimById(span.dataset.claim);
    if (!claim) continue;
    span.classList.add(`s-${claim.status}`);
    span.tabIndex = 0;
    span.setAttribute("role", "button");
    span.setAttribute("aria-label", `${span.textContent} (${STATUS_LABEL[claim.status] || claim.status}); show evidence`);
    span.addEventListener("click", () => openClaim(claim.id));
    span.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openClaim(claim.id); }
    });
    const flag = challengeFlag({ claim: claim.id });
    if (flag) span.after(flag);
  }
}

function challengeFlag(target) {
  const live = liveChallenges(target);
  if (!live.length) return null;
  const label = `${plural(live.length, "unresolved challenge")}`;
  return h("button", {
    class: "flag", type: "button", title: label, "aria-label": label,
    onclick: () => (live.length === 1 ? openChallenge(live[0].id) : openChallengeList(target)),
  }, "⚑", live.length > 1 ? String(live.length) : null);
}

function evidenceChips(pins) {
  return h("span", { class: "ev-chips" }, pins.map((p) =>
    h("button", {
      class: "ev-chip", type: "button", text: p.id, title: `Evidence ${p.id} v${p.version}`,
      onclick: () => openEvidence(p.id, p.version, null),
    })));
}

function markFlaggedBlocks() {
  // In audit mode, outline blocks with findings that are not about one claim (e.g. uncited numbers).
  if (!state.audit) return;
  for (const f of state.report.validation.findings) {
    if (!f.claim && f.block && f.severity !== "info") $(`blk-${f.block}`)?.classList.add("flagged");
  }
}

function auditSummary() {
  const r = state.report;
  const counts = { ok: 0, unverified: 0, warning: 0, error: 0 };
  for (const c of r.claims) counts[c.status] = (counts[c.status] || 0) + 1;
  const findings = r.validation.findings.filter((f) => f.severity !== "info" || f.code.endsWith("asserted"));
  return h("section", { class: "audit-summary", "aria-label": "Audit summary" },
    h("div", { class: "legend" },
      h("strong", { text: `${plural(r.claims.length, "claim")}:` }),
      ...["ok", "unverified", "warning", "error"].filter((s) => counts[s]).map((s) =>
        h("span", { class: `chip ${s}`, text: `${counts[s]} ${STATUS_LABEL[s]}` }))),
    findings.length ? h("ul", {}, findings.map((f) => h("li", {},
      h("span", { class: `chip ${f.severity}`, text: f.severity }),
      h("button", {
        type: "button",
        text: `${f.claim || f.block || ""}${f.number !== undefined ? ` #${f.number + 1}` : ""}: ${f.message}`,
        onclick: () => (f.claim ? openClaim(f.claim, { scroll: true }) : $(`blk-${f.block}`)?.scrollIntoView({ behavior: "smooth", block: "center" })),
      })))) : null);
}
