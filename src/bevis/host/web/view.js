// The Bevis web view (SPEC §4): a report rendered from its JSON, with an
// evidence panel behind every claim and chart mark.
//
// The share link's secret (or a person's token, SPEC §7.2) lives in the URL
// fragment (#key=...), which the browser never sends to the server. Everything from the report or its data
// is inserted with textContent; the only HTML inserted is the server's
// sanitized rendering of text blocks.

import { cellExpression, claimForOps, derivePins, detectNumbers, isComplete, moveOverWord, nextClaimId } from "./claims.js";
import { diffWords, findSpans, merge3, splitSpans } from "./textdiff.js";

const PAGE_SIZE = 50;
const FULL_TABLE_ROWS = 25;
const STATUS_LABEL = { ok: "verified", unverified: "not verified", warning: "warning", error: "failed" };
const TIER_LABEL = { direct: "cell value", computed: "computed", asserted: "asserted" };
const ROLES = ["reader", "commenter", "suggester", "editor", "admin"];
const CHALLENGE_CLASS = { open: "error", answered: "unverified", resolved: "ok", dismissed: "" };

const state = {
  key: null,
  person: false, // the key is a person token: it opens an index of reports (SPEC §7.2)
  rid: null, // the report a person opened
  tab: "reports",
  version: null, // null: whatever the link opens (the draft, for links that see it)
  report: null,
  evidence: new Map(), // "id@version" -> {evidence, row_keys, ...}
  audit: false,
  open: null, // {kind: "claim" | "challenge", id} | {kind: "evidence", id, version, row}
  me: null, // who the link is (GET /api/me)
  role: null,
  challenges: [], // as seen from the shown version
  compose: null, // {target, cells: Map(selKey -> cell)} while writing a challenge
  suggestions: [], // open suggestions on the draft (suggesters and up)
  editing: new Map(), // block id -> editor state, for blocks open for editing (SPEC §6.2)
  flash: null, // {block, text}: a note shown once, after a save
};

const atLeast = (role) => ROLES.indexOf(state.role) >= ROLES.indexOf(role);
const canDiscuss = () => atLeast("commenter");
const canFile = () => canDiscuss() && state.report?.kind === "version";

function remembered(name, value) {
  // Per-viewer conveniences only; the page works without storage.
  try {
    if (value === undefined) return localStorage.getItem(`bevis.${name}`) || "";
    localStorage.setItem(`bevis.${name}`, value);
  } catch { /* storage may be blocked */ }
  return value ?? "";
}

const selKey = (c) => `${c.evidence}@${c.version}|${JSON.stringify(c.key)}|${c.column}`;

// -- helpers ------------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);

// replaceChildren, but arrays are flattened and null/false are skipped, as in h().
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
    key: params.get("key"),
    v: params.get("v"),
    c: params.get("c"),
    e: params.get("e"),
    row: params.get("row"),
    ch: params.get("ch"),
    report: params.get("report"),
    tab: params.get("tab"),
    s: params.get("s"),
    rv: params.get("rv"),
  };
}

function writeHash() {
  const params = new URLSearchParams();
  params.set("key", state.key);
  if (state.rid) params.set("report", state.rid);
  if (state.version !== null) params.set("v", String(state.version));
  const o = state.open;
  if (o?.kind === "claim") params.set("c", o.id);
  if (o?.kind === "challenge") params.set("ch", o.id);
  if (o?.kind === "suggestion") params.set("s", o.id);
  if (o?.kind === "review") params.set("rv", o.id);
  if (o?.kind === "evidence") {
    params.set("e", `${o.id}@${o.version}`);
    if (o.row !== null && o.row !== undefined) params.set("row", JSON.stringify(o.row));
  }
  history.replaceState(null, "", `#${params}`);
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Authorization: `Bearer ${state.key}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(data?.error?.message || `request failed (${res.status})`);
    err.status = res.status;
    err.code = data?.error?.code;
    err.details = data?.error?.details;
    throw err;
  }
  return data;
}

async function evidence(id, version) {
  const k = `${id}@${version}`;
  if (!state.evidence.has(k)) {
    state.evidence.set(k, api(`/api/reports/${state.report.id}/evidence/${encodeURIComponent(id)}?version=${version}`));
  }
  return state.evidence.get(k);
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvField(text) {
  // Quote what CSV needs quoted; a leading = + - @ would run as a formula in a spreadsheet.
  if (/^[=+\-@\t\r]/.test(text) && Number.isNaN(Number(text))) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function evidenceCsv(doc) {
  // Exactly what the table shows: withheld cells stay empty, nulls are empty, decimals keep their text.
  const withheld = new Set((doc.disclosure?.withheld_cells || []).map(([i, name]) => `${i}\u0000${name}`));
  const names = doc.columns.map((c) => c.name);
  const lines = [names.map(csvField).join(",")];
  doc.rows.forEach((row, i) => {
    lines.push(row.map((v, j) => (v === null || withheld.has(`${i}\u0000${names[j]}`) ? "" : csvField(String(v)))).join(","));
  });
  return `\ufeff${lines.join("\r\n")}\r\n`;
}

async function downloadPdf(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Rendering…";
  try {
    const res = await fetch(`/api/reports/${state.report.id}/export.pdf?version=${state.report.version}`,
      { headers: { Authorization: `Bearer ${state.key}` } });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new Error(data?.error?.message || `request failed (${res.status})`);
    }
    saveBlob(await res.blob(), `${state.report.id}-v${state.report.version}.pdf`);
  } catch (err) {
    alert(`Could not export the PDF: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function shortValue(text) {
  // Computed values carry up to 34 significant digits; show a readable prefix.
  if (text === null || text === undefined) return "—";
  const n = Number(text);
  if (!Number.isFinite(n)) return text;
  const short = Math.abs(n) >= 1e6 || Number.isInteger(n) ? n.toLocaleString() : n.toPrecision(6);
  return short.length < text.length ? `${short}…` : text;
}

function sameKey(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function claimById(id) {
  return state.report.claims.find((c) => c.id === id);
}

// -- page -----------------------------------------------------------------------------

function notice(err, fallback) {
  return h("div", { class: "notice error" }, h("p", { text: err.status === 401 ? fallback : err.message }));
}

async function load() {
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

async function loadReport() {
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

async function loadChallenges() {
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

const sameTarget = (a, b) => a.claim === b.claim && a.block === b.block;

function renderHeader() {
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
  $("pdf").hidden = r.kind !== "version"; // only a published version exports
}

async function renderReport() {
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

// -- charts ----------------------------------------------------------------------------

function rowObjects(doc, ref) {
  // Values for drawing only: decimals become numbers here, never in the panel.
  // Each datum remembers its record and row, so a click opens the right one.
  const cols = doc.columns;
  return doc.rows.map((row, i) => {
    const o = { __bevis_ref: ref, __bevis_row: i };
    cols.forEach((c, j) => {
      const v = row[j];
      o[c.name] = c.type === "decimal" && v !== null ? Number(v) : v;
    });
    return o;
  });
}

function stripLinks(node) {
  // A chart's href channel would make marks into links; the panel is the only click target.
  if (Array.isArray(node)) node.forEach(stripLinks);
  else if (node && typeof node === "object") {
    delete node.href;
    Object.values(node).forEach(stripLinks);
  }
}

async function inlineData(node, refs) {
  if (Array.isArray(node)) { for (const n of node) await inlineData(n, refs); return; }
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node)) {
    if (k === "data" && v && v.evidence) {
      const res = await evidence(v.evidence.id, v.evidence.version);
      if (!res.evidence.rows) throw new Error("withheld");
      refs.push(v.evidence);
      node.data = { values: rowObjects(res.evidence, refs.length - 1) };
    } else {
      await inlineData(v, refs);
    }
  }
}

async function drawChart(el, block) {
  const spec = structuredClone(block.spec);
  const refs = [];
  stripLinks(spec); // before inlining: data columns named href are not channels
  try {
    await inlineData(spec, refs);
  } catch (err) {
    el.replaceWith(h("p", { class: "chart-note", text: err.message === "withheld"
      ? "Chart not drawn: its data is not disclosed at this link's level."
      : `Chart not drawn: ${err.message}` }));
    return;
  }
  spec.width = spec.width === undefined ? "container" : spec.width;
  try {
    const result = await vegaEmbed(el, spec, {
      actions: false,
      renderer: "svg",
      ast: true, // CSP-safe: author expressions are interpreted, never compiled with eval
      expr: vega.expressionInterpreter,
      loader: vega.loader({ mode: "http" }),
      config: chartTheme(),
    });
    result.view.addEventListener("click", (_event, item) => {
      // Strict charts (SPEC §3.6) draw one mark per row, so the datum is the row.
      const ref = refs[item?.datum?.__bevis_ref];
      const row = item?.datum?.__bevis_row;
      if (ref && row !== undefined) openEvidence(ref.id, ref.version, row);
    });
  } catch (err) {
    el.replaceWith(h("p", { class: "chart-note", text: `Chart not drawn: ${err.message}` }));
  }
}

function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const text = css.getPropertyValue("--text").trim();
  const muted = css.getPropertyValue("--muted").trim();
  const line = css.getPropertyValue("--line").trim();
  return {
    background: null,
    view: { stroke: null },
    axis: { labelColor: muted, titleColor: text, domainColor: line, gridColor: line, tickColor: line },
    legend: { labelColor: muted, titleColor: text },
    title: { color: text },
  };
}

// -- panel -------------------------------------------------------------------------------

function showPanel(title) {
  $("panel").hidden = false;
  document.querySelector(".layout").classList.add("with-panel");
  $("panel-title").textContent = title;
  fill($("panel-body"), h("p", { class: "meta", text: "Loading…" }));
  $("panel").scrollTop = 0;
}

function closePanel() {
  state.compose = null;
  $("panel").hidden = true;
  document.querySelector(".layout").classList.remove("with-panel");
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  state.open = null;
  writeHash();
}

async function openClaim(id, { scroll = false } = {}) {
  const claim = claimById(id);
  if (!claim) return;
  state.compose = null;
  state.open = { kind: "claim", id };
  writeHash();
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  const span = document.querySelector(`span.claim[data-claim="${CSS.escape(id)}"]`);
  span?.classList.add("active");
  if (scroll) span?.scrollIntoView({ behavior: "smooth", block: "center" });

  showPanel(`Claim ${id}`);
  const body = [];
  body.push(h("p", { class: "quote", text: span ? `“${span.textContent}”` : "" }));
  body.push(h("p", {}, h("span", { class: `chip ${claim.status}`, text: STATUS_LABEL[claim.status] || claim.status })));
  const claimFindings = state.report.validation.findings.filter((f) => f.claim === id && f.number === undefined);
  for (const f of claimFindings) {
    body.push(h("p", { class: `message ${f.severity}`, text: f.message }));
  }
  if (claim.derivation) body.push(h("h3", { text: "Derivation" }), h("p", { text: claim.derivation }));
  if (canDiscuss()) body.push(challengeSection({ claim: id }, "claim"));

  if (claim.numbers.length) {
    body.push(h("h3", { text: "Numbers" }));
    body.push(h("ul", { class: "numbers" }, claim.numbers.map((n) => numberItem(n))));
  } else {
    body.push(h("p", { class: "meta", text: "This claim states no numbers, so nothing in it is checked against the data." }));
  }

  const cited = new Map(); // "id@v" -> [{key, column}]
  for (const n of claim.numbers) {
    for (const c of n.cells || []) {
      const k = `${c.evidence}@${c.version}`;
      if (!cited.has(k)) cited.set(k, []);
      cited.get(k).push(c);
    }
  }
  fill($("panel-body"), ...body);
  for (const pin of claim.evidence) {
    const section = h("section", { class: "evidence" });
    $("panel-body").append(section);
    await renderEvidence(section, pin.id, pin.version, cited.get(`${pin.id}@${pin.version}`) || [], null);
  }
}

function numberItem(n) {
  const item = h("li", {},
    h("div", { class: "num-head" },
      h("span", { class: "num-text", text: n.text }),
      h("span", { class: "tier", text: TIER_LABEL[n.tier] || n.tier }),
      h("span", { class: `chip ${n.status}`, text: STATUS_LABEL[n.status] || n.status })));
  if (n.tier === "computed") {
    item.append(h("code", { class: "expr", text: n.expression }));
    item.append(h("div", { class: "value", title: n.value ?? "", text: `= ${shortValue(n.value)}` }));
  } else if (n.tier === "direct") {
    const c = n.cell;
    item.append(h("code", { class: "expr", text: `cell(${[c.evidence, ...c.key.map((k) => JSON.stringify(k)), c.column].join(", ")})` }));
  } else {
    item.append(h("p", { text: n.derivation }));
  }
  if (n.message) item.append(h("div", { class: `message ${n.status}`, text: n.message }));
  return item;
}

async function openEvidence(id, version, row) {
  state.compose = null;
  state.open = { kind: "evidence", id, version, row };
  writeHash();
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  showPanel(`Evidence ${id} v${version}`);
  const section = h("section", {});
  fill($("panel-body"), section);
  await renderEvidence(section, id, version, [], row);
}

async function renderEvidence(section, id, version, cells, focusRow, marks = []) {
  let res;
  try {
    res = await evidence(id, version);
  } catch (err) {
    fill(section, h("p", { class: "message", text: `${id} v${version}: ${err.message}` }));
    return;
  }
  const doc = res.evidence;
  const rowsText = doc.partial
    ? `sample of ${doc.row_count.toLocaleString()} of ${doc.source_row_count.toLocaleString()} rows`
    : plural(doc.row_count, "row");
  fill(section,
    h("h4", { text: `${doc.id} v${doc.version}` }),
    h("dl", { class: "facts" },
      h("dt", { text: "Source" }), h("dd", { text: doc.source }),
      h("dt", { text: "Rows" }), h("dd", { text: rowsText }),
      h("dt", { text: "Run at" }), h("dd", { text: `${doc.run_at} (reported by the agent)` }),
      h("dt", { text: "Provenance" }), h("dd", { text: doc.provenance }),
      h("dt", { text: "Data hash" }), h("dd", { class: "mono", text: doc.data_hash }),
      Object.keys(doc.params || {}).length ? [h("dt", { text: "Params" }), h("dd", { class: "mono", text: JSON.stringify(doc.params) })] : null),
    doc.assumptions.length ? [h("h3", { text: "Assumptions" }), h("ul", { class: "assumptions" }, doc.assumptions.map((a) => h("li", { text: a })))] : null,
    h("h3", { text: "SQL" }),
    h("pre", { class: "sql", text: doc.sql }),
    h("h3", { text: "Result" }),
    resultTable(doc, res.row_keys, cells, focusRow, { id, version }, marks),
  );
  section.querySelector("tr.focus")?.scrollIntoView({ block: "nearest" });
}

function resultTable(doc, rowKeys, cells, focusRow, ident, marks = []) {
  if (!doc.rows) {
    return h("p", { class: "meta", text: `Rows are not disclosed at this link's level (${plural(doc.row_count, "row")}).` });
  }
  const cols = doc.columns.map((c) => c.name);
  const numeric = new Set(doc.columns.filter((c) => c.type === "integer" || c.type === "decimal").map((c) => c.name));
  const citedRows = new Map(); // row index -> Set(columns)
  rowKeys.forEach((key, i) => {
    for (const c of cells) {
      if (sameKey(key, c.key)) {
        if (!citedRows.has(i)) citedRows.set(i, new Set());
        citedRows.get(i).add(c.column);
      }
    }
  });
  const withheld = doc.disclosure?.withheld_rows || 0;
  const withheldCells = new Set((doc.disclosure?.withheld_cells || []).map(([i, name]) => `${i}\u0000${name}`));
  const marked = new Set(marks.map(selKey)); // cells a challenge points at
  for (const m of marks) {
    rowKeys.forEach((key, i) => { if (sameKey(key, m.key) && !citedRows.has(i)) citedRows.set(i, new Set()); });
  }
  let citedOnly = citedRows.size > 0 && doc.rows.length > FULL_TABLE_ROWS;
  let page = focusRow !== null && focusRow !== undefined ? Math.floor(focusRow / PAGE_SIZE) : 0;
  if (focusRow !== null && focusRow !== undefined) citedOnly = false;

  const wrap = h("div", {});
  const draw = () => {
    const indexes = citedOnly ? [...citedRows.keys()].sort((a, b) => a - b) : doc.rows.map((_, i) => i);
    const pages = Math.max(1, Math.ceil(indexes.length / PAGE_SIZE));
    page = Math.min(page, pages - 1);
    const shown = indexes.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const tbody = h("tbody", {}, shown.map((i) => {
      const row = doc.rows[i];
      const citedCols = citedRows.get(i);
      return h("tr", { class: [citedCols ? "cited" : "", i === focusRow ? "focus" : ""].join(" ").trim() || null },
        row.map((v, j) => {
          const name = cols[j];
          const cell = { evidence: ident.id, version: ident.version, key: rowKeys[i], column: name };
          const k = selKey(cell);
          const hidden = withheldCells.has(`${i}\u0000${name}`);
          const cls = [numeric.has(name) ? "num" : "", hidden ? "withheld" : v === null ? "null" : "", citedCols?.has(name) ? "cited-cell" : "",
            marked.has(k) ? "challenged-cell" : "", state.compose?.cells.has(k) ? "selected-cell" : ""];
          return h("td", {
            class: cls.join(" ").trim() || null,
            text: hidden ? "withheld" : v === null ? "null" : String(v),
            onclick: state.compose && !hidden ? (e) => toggleCell(e.currentTarget, cell) : null,
          });
        }));
    }));
    const notes = [];
    if (withheld) notes.push(`${plural(withheld, "row")} not disclosed at this level`);
    if (doc.disclosure?.withheld_columns?.length) notes.push(`not disclosed: ${doc.disclosure.withheld_columns.join(", ")}`);
    fill(wrap,
      h("div", { class: "table-tools" },
        h("span", { text: [`${indexes.length.toLocaleString()} of ${doc.row_count.toLocaleString()} rows`, ...notes].join(" · ") }),
        h("button", { class: "quiet", type: "button", text: "Download CSV",
          title: "All rows of this result, as disclosed at this link's level",
          onclick: () => saveBlob(new Blob([evidenceCsv(doc)], { type: "text/csv;charset=utf-8" }), `${ident.id}.v${ident.version}.csv`) }),
        citedRows.size ? h("label", {},
          h("input", { type: "checkbox", checked: citedOnly, onchange: (e) => { citedOnly = e.target.checked; page = 0; draw(); } }),
          "cited rows only") : null),
      h("div", { class: "table-wrap" },
        h("table", { class: "rows" },
          h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col", class: numeric.has(c) ? "num" : null, text: c })))),
          tbody)),
      pages > 1 ? h("div", { class: "pager" },
        h("button", { type: "button", text: "‹", "aria-label": "Previous page", disabled: page === 0, onclick: () => { page -= 1; draw(); } }),
        h("span", { text: `page ${page + 1} of ${pages}` }),
        h("button", { type: "button", text: "›", "aria-label": "Next page", disabled: page >= pages - 1, onclick: () => { page += 1; draw(); } })) : null,
    );
  };
  draw();
  return wrap;
}

// -- challenges (SPEC §8) -----------------------------------------------------------------

function targetLabel(target) {
  if (target.claim) {
    const span = document.querySelector(`span.claim[data-claim="${CSS.escape(target.claim)}"]`);
    return span ? `Claim ${target.claim}: “${span.textContent}”` : `Claim ${target.claim}`;
  }
  const block = state.report.blocks.find((b) => b.id === target.block);
  return block?.caption ? `Chart: ${block.caption}` : `Block ${target.block}`;
}

function challengeSection(target, noun) {
  const items = state.challenges.filter((c) => sameTarget(c.target, target));
  return h("section", { class: "challenges-section" },
    h("h3", { text: "Challenges" }),
    items.length ? h("ul", { class: "challenge-list" }, items.map(challengeItem))
      : h("p", { class: "meta", text: `No one has challenged this ${noun}.` }),
    canFile() ? h("button", { class: "toggle", type: "button", text: `Challenge this ${noun}`,
      onclick: () => openCompose(target) }) : null);
}

function challengeItem(c) {
  return h("li", {},
    h("button", { type: "button", class: "challenge-link", onclick: () => openChallenge(c.id) },
      h("span", { class: `chip ${CHALLENGE_CLASS[c.status]}`, text: c.status }),
      h("span", { class: "challenge-body", text: c.body }),
      h("span", { class: "meta", text: [c.author.name, c.carried_over ? `from version ${c.version}` : null,
        c.thread.length ? plural(c.thread.filter((e) => e.kind === "reply").length, "reply") : null]
        .filter(Boolean).join(" · ") })));
}

function nameField() {
  // A shared link may be used by several people; each says who they are (recorded as unverified).
  if (state.me?.kind !== "link") return null;
  return h("label", { class: "field" }, "Your name",
    h("input", { type: "text", name: "display_name", maxlength: "80", value: remembered("name"),
      placeholder: state.me.name, autocomplete: "name" }));
}

function readName(form) {
  const v = form.querySelector('input[name="display_name"]')?.value.trim() || "";
  if (v) remembered("name", v);
  return v || undefined;
}

function toggleCell(td, cell) {
  const k = selKey(cell);
  if (state.compose.cells.has(k)) state.compose.cells.delete(k);
  else state.compose.cells.set(k, cell);
  td.classList.toggle("selected-cell", state.compose.cells.has(k));
  renderSelection();
}

function renderSelection() {
  const box = $("selected-cells");
  if (!box) return;
  const cells = [...state.compose.cells.values()];
  fill(box, cells.length
    ? cells.map((c) => h("span", { class: "cell-chip" }, `${c.evidence} · ${c.key.join(", ")} · ${c.column}`))
    : h("span", { class: "meta", text: "None yet. Click cells in the tables below to point at them." }));
}

async function openCompose(target) {
  state.compose = { target, cells: new Map() };
  state.open = null;
  writeHash();
  showPanel("New challenge");
  const status = h("p", { class: "message", role: "alert" });
  const form = h("form", { class: "compose" },
    h("p", { class: "quote", text: targetLabel(target) }),
    h("label", { class: "field" }, "What is in doubt?",
      h("textarea", { name: "body", rows: "4", required: true, placeholder: "e.g. Does this include partial refunds?" })),
    nameField(),
    h("div", { class: "field" }, h("span", { text: "Cells you are pointing at" }), h("div", { id: "selected-cells", class: "cell-chips" })),
    status,
    h("div", { class: "actions" },
      h("button", { class: "toggle primary", type: "submit", text: "File challenge" }),
      h("button", { class: "quiet", type: "button", text: "Cancel", onclick: closePanel })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = form.querySelector("textarea").value.trim();
    if (!body) return;
    try {
      const ch = await api(`/api/reports/${state.report.id}/challenges`, { method: "POST", body: {
        target, body, version: state.report.version, cells: [...state.compose.cells.values()],
        display_name: readName(form),
      } });
      await refreshChallenges();
      openChallenge(ch.id);
    } catch (err) {
      status.textContent = err.message;
    }
  });
  fill($("panel-body"), form);
  renderSelection();
  form.querySelector("textarea").focus();
  for (const pin of targetPins(target)) {
    const section = h("section", { class: "evidence" });
    $("panel-body").append(section);
    await renderEvidence(section, pin.id, pin.version, citedCells(target, pin), null);
  }
}

function targetPins(target) {
  if (target.claim) return claimById(target.claim)?.evidence || [];
  return state.report.blocks.find((b) => b.id === target.block)?.evidence || [];
}

function citedCells(target, pin) {
  if (!target.claim) return [];
  return (claimById(target.claim)?.numbers || []).flatMap((n) => n.cells || [])
    .filter((c) => c.evidence === pin.id && c.version === pin.version);
}

async function refreshChallenges() {
  await loadChallenges();
  renderHeader();
  await renderReport();
}

async function openChallenge(id) {
  state.compose = null;
  state.open = { kind: "challenge", id };
  writeHash();
  showPanel("Challenge");
  let c;
  try {
    c = await api(`/api/reports/${state.report.id}/challenges/${encodeURIComponent(id)}`);
  } catch (err) {
    fill($("panel-body"), h("p", { class: "message", text: err.message }));
    return;
  }
  const when = (t) => new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  const status = h("p", { class: "message", role: "alert" });
  const act = async (path, body) => {
    try {
      await api(`/api/reports/${state.report.id}/challenges/${encodeURIComponent(id)}/${path}`, { method: "POST", body });
      await refreshChallenges();
      openChallenge(id);
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const reply = c.can_reply ? h("form", { class: "compose" },
    h("label", { class: "field" }, "Reply", h("textarea", { name: "body", rows: "3", required: true })),
    nameField(),
    h("div", { class: "actions" }, h("button", { class: "toggle primary", type: "submit", text: "Send reply" }))) : null;
  reply?.addEventListener("submit", (e) => {
    e.preventDefault();
    const body = reply.querySelector("textarea").value.trim();
    if (body) act("replies", { body, display_name: readName(reply) });
  });
  const statusButton = (label, to, allowed) => allowed ? h("button", {
    class: "quiet", type: "button", text: label,
    onclick: () => act("status", { status: to, display_name: remembered("name") || undefined }),
  }) : null;

  const target = h("button", { type: "button", class: "challenge-link", text: targetLabel(c.target),
    onclick: () => (c.target.claim ? openClaim(c.target.claim, { scroll: true })
      : $(`blk-${c.target.block}`)?.scrollIntoView({ behavior: "smooth", block: "center" })) });
  fill($("panel-body"),
    h("p", {}, h("span", { class: `chip ${CHALLENGE_CLASS[c.status]}`, text: c.status }),
      c.carried_over ? h("span", { class: "chip", text: `filed on version ${c.version}` }) : null,
      c.target_changed ? h("span", { class: "chip warning", text: "the claim has changed since" }) : null),
    target,
    h("article", { class: "message-card" },
      h("p", { class: "meta", text: `${c.author.name}${c.author.verified ? "" : " (unverified)"} · ${when(c.created_at)}` }),
      h("p", { text: c.body })),
    c.thread.map((e) => h("article", { class: `message-card ${e.kind}` },
      h("p", { class: "meta", text: [`${e.author.name}${e.author.kind === "agent" ? " (agent)" : ""}`, when(e.created_at),
        e.kind === "status" ? `marked ${e.to_status}` : null].filter(Boolean).join(" · ") }),
      e.body ? h("p", { text: e.body }) : null,
      e.suggestion_id ? h("p", { class: "meta" }, `Proposed an edit (suggestion ${e.suggestion_id}) for an editor to review. `,
        atLeast("suggester") ? h("button", { class: "quiet", type: "button", text: "Review it", onclick: () => openSuggestion(e.suggestion_id) }) : null) : null)),
    status,
    reply,
    h("div", { class: "actions" },
      statusButton("Mark resolved", "resolved", c.can_resolve),
      statusButton("Dismiss", "dismissed", c.can_dismiss),
      statusButton("Reopen", "open", c.can_reopen)),
    c.cells.length ? h("h3", { text: "Cells pointed at" }) : null);
  const byRecord = new Map();
  for (const cell of c.cells) {
    const k = `${cell.evidence}@${cell.version}`;
    if (!byRecord.has(k)) byRecord.set(k, []);
    byRecord.get(k).push(cell);
  }
  for (const cells of byRecord.values()) {
    const section = h("section", { class: "evidence" });
    $("panel-body").append(section);
    await renderEvidence(section, cells[0].evidence, cells[0].version, [], null, cells);
  }
}

function openChallengeList(target = null) {
  state.compose = null;
  state.open = null;
  writeHash();
  const items = target ? state.challenges.filter((c) => sameTarget(c.target, target)) : state.challenges;
  showPanel(target ? `Challenges on ${target.claim || target.block}` : "Challenges");
  const groups = [["Open", ["open"]], ["Answered", ["answered"]], ["Closed", ["resolved", "dismissed"]]];
  fill($("panel-body"),
    state.report.kind === "draft" ? h("p", { class: "meta", text: "Challenges are filed against published versions. Open a version to file one." }) : null,
    items.length ? groups.map(([label, statuses]) => {
      const group = items.filter((c) => statuses.includes(c.status));
      return group.length ? [h("h3", { text: `${label} (${group.length})` }), h("ul", { class: "challenge-list" }, group.map((c) =>
        h("li", {}, h("p", { class: "meta", text: targetLabel(c.target) }), challengeItem(c).firstChild)))] : null;
    }) : h("p", { class: "meta", text: "No challenges yet. Open a claim to challenge it." }));
}

// -- review of edits near a claim (SPEC §6.2) ----------------------------------------------

const reviewFlag = (id) => (state.report?.kind === "draft" ? state.report.block_state?.[id]?.needs_review ?? null : null);
const flaggedBlocks = () => state.report.blocks.map((b) => b.id).filter((id) => reviewFlag(id));

function isMine(author) {
  if (!author || !state.me) return false;
  if (author.person_id || state.me.person_id) return author.person_id === state.me.person_id;
  return author.name === state.me.name && author.kind === state.me.author_kind;
}

function reviewRow(block, flag) {
  const by = flag.edited_by;
  return h("div", { class: "review-row" },
    h("span", { class: "chip error", text: "Needs review" }),
    h("span", { class: "meta", text: `Edited near a claim by ${isMine(by) ? "you" : by.name} · ${when(flag.edited_at)}` }),
    h("button", { class: "quiet", type: "button", text: atLeast("admin") ? "Review" : "See what changed",
      "aria-label": `Review block ${block.id}`, onclick: () => openReview(block.id) }));
}

async function openReview(bid, { notice = null } = {}) {
  state.compose = null;
  state.open = { kind: "review", id: bid };
  writeHash();
  showPanel("Review");
  const block = state.report.blocks.find((b) => b.id === bid);
  const flag = reviewFlag(bid);
  if (!block || !flag) {
    fill($("panel-body"), h("p", { class: "meta", text: "Nothing in this block needs review." }));
    return;
  }
  const revision = state.report.block_state[bid].revision;
  const claims = state.report.claims.filter((c) => c.block_id === bid);
  const findings = blockFindings(state.report.validation.findings, bid).filter((f) => f.severity !== "info");
  const mine = isMine(flag.edited_by);
  const status = h("p", { class: "message", role: "alert", text: notice || "" });
  const clear = async (button) => {
    button.disabled = true;
    status.textContent = "";
    try {
      const out = await api(`/api/reports/${state.report.id}/blocks/${encodeURIComponent(bid)}/clear_review`,
        { method: "POST", body: { revision } });
      state.flash = { block: bid, text: out.self_cleared
        ? "Marked reviewed. You made this edit yourself, so the log records it as a self-review."
        : "Marked reviewed." };
      closePanel();
      const y = window.scrollY;
      await loadReport();
      window.scrollTo(0, y);
    } catch (err) {
      button.disabled = false;
      if (err.code === "stale_block") {
        await loadReport();
        openReview(bid, { notice: "This block changed while you were looking. Read the new text before marking it reviewed." });
      } else {
        status.textContent = err.message;
      }
    }
  };
  fill($("panel-body"),
    h("p", {}, h("span", { class: "chip error", text: "Needs review" })),
    h("p", { class: "meta", text: `Edited near a claim by ${mine ? "you" : flag.edited_by.name} · ${when(flag.edited_at)}` }),
    h("button", { type: "button", class: "challenge-link", text: `Block ${bid}`,
      onclick: () => $(`blk-${bid}`)?.scrollIntoView({ behavior: "smooth", block: "center" }) }),
    h("h3", { text: flag.from === null ? "New text" : "Changes since the last reviewed text" }),
    h("article", { class: "message-card" },
      block.type === "chart" ? h("p", { class: "meta", text: "This block's claims changed." })
        : diffView(flag.from ?? "", block.text)),
    claims.length ? [h("h3", { text: "Claims in this block" }),
      h("ul", { class: "review-claims" }, claims.map((c) => h("li", {},
        h("span", { class: `chip ${c.status}`, text: STATUS_LABEL[c.status] || c.status }), " ",
        h("button", { class: "quiet", type: "button", text: c.id, onclick: () => openClaim(c.id, { scroll: true }) }), " ",
        c.numbers.map((n) => n.text).join(", "))))] : null,
    h("h3", { text: "Validator" }),
    findings.length ? findings.slice(0, 8).map((f) => h("p", { class: "finding" },
      h("span", { class: `chip ${f.severity}`, text: f.severity }), " ", f.claim ? h("code", { text: f.claim }) : null,
      f.claim ? " " : null, f.message)) : h("p", { class: "meta" }, h("span", { class: "chip ok", text: "no problems" })),
    status,
    atLeast("admin") ? [
      mine ? h("p", { class: "meta", text: "You made this edit. You can mark it reviewed yourself; the log records it as a self-review." }) : null,
      h("div", { class: "actions" },
        h("button", { class: "toggle primary", type: "button", text: "Mark reviewed", onclick: (e) => clear(e.currentTarget) }))]
      : h("p", { class: "meta", text: "An admin has to mark this reviewed before the report can be published." }));
}

// -- suggestions (SPEC §6) ------------------------------------------------------------------

const SUGGESTION_CLASS = { open: "unverified", accepted: "ok", rejected: "error" };

function changedBlocks(s) {
  // The blocks an upsert in the suggestion names, for the list; the detail shows the real diff.
  return [...new Set(s.ops.filter((op) => op.op === "upsert_block" && op.block?.id).map((op) => op.block.id))];
}

async function openSuggestionList() {
  state.compose = null;
  state.open = null;
  writeHash();
  showPanel("Suggestions");
  let all;
  try {
    all = await api(`/api/reports/${state.report.id}/suggestions?status=all`);
  } catch (err) {
    fill($("panel-body"), h("p", { class: "message", text: err.message }));
    return;
  }
  const groups = [["Open", "open"], ["Accepted", "accepted"], ["Rejected", "rejected"]];
  fill($("panel-body"),
    atLeast("editor") ? null : h("p", { class: "meta", text: "An editor accepts or rejects a suggestion. You can see where yours stands." }),
    all.length ? groups.map(([label, status]) => {
      const group = all.filter((s) => s.status === status).reverse();
      return group.length ? [h("h3", { text: `${label} (${group.length})` }), h("ul", { class: "challenge-list" }, group.map((s) =>
        h("li", {}, h("button", { type: "button", class: "challenge-link", onclick: () => openSuggestion(s.id),
          text: s.message || `Edit to ${changedBlocks(s).join(", ") || "the report"}` }),
        h("p", { class: "meta", text: [s.author.name, when(s.created_at), changedBlocks(s).length ? `blocks ${changedBlocks(s).join(", ")}` : null].filter(Boolean).join(" · ") }))))] : null;
    }) : h("p", { class: "meta", text: "No suggestions yet. Use Suggest edit under a paragraph to make one." }));
}

function blockChange(c) {
  const side = c.after || c.before;
  const verb = !c.before ? "Added" : !c.after ? "Removed" : "Changed";
  const noun = side.type === "heading" ? "heading" : side.type === "chart" ? "chart" : "paragraph";
  return h("article", { class: "message-card" },
    h("p", { class: "meta", text: `${verb} ${noun} ${c.id}` }),
    side.type === "chart"
      ? h("p", { class: "meta", text: "The chart or its caption changed." })
      : diffView(c.before?.text ?? "", c.after?.text ?? ""));
}

async function openSuggestion(id) {
  state.compose = null;
  state.open = { kind: "suggestion", id };
  writeHash();
  showPanel("Suggestion");
  const rid = state.report.id;
  let s;
  try {
    s = await api(`/api/reports/${rid}/suggestions/${encodeURIComponent(id)}`);
  } catch (err) {
    fill($("panel-body"), h("p", { class: "message", text: err.message }));
    return;
  }
  const preview = s.preview;
  const diff = preview?.diff || {};
  const blocks = diff.blocks || [];
  const status = h("p", { class: "message", role: "alert" });
  const note = h("input", { type: "text", name: "note", maxlength: "300", placeholder: "A note (optional)" });
  const resolve = async (accept) => {
    status.textContent = "";
    try {
      const out = await api(`/api/reports/${rid}/suggestions/${encodeURIComponent(id)}/${accept ? "accept" : "reject"}`,
        { method: "POST", body: { note: note.value.trim() } });
      const touched = Object.keys(out.blocks || {});
      if (accept && touched.length) state.flash = { block: touched[0], text: "Accepted." };
      const y = window.scrollY;
      await loadReport();
      window.scrollTo(0, y);
      openSuggestionList();
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const errors = preview?.validation?.counts?.errors ?? 0;
  fill($("panel-body"),
    h("p", {}, h("span", { class: `chip ${SUGGESTION_CLASS[s.status] || ""}`, text: s.status }),
      preview?.validation ? h("span", { class: `chip ${errors ? "error" : "ok"}`, text: errors ? plural(errors, "error") : "no errors" }) : null),
    h("p", { class: "meta", text: `${s.author.name}${s.author.kind === "agent" ? " (agent)" : ""} · ${when(s.created_at)}` }),
    s.message ? h("p", { text: s.message }) : null,
    preview?.error ? h("p", { class: "message", text: `This no longer applies to the draft: ${preview.error.message}` }) : null,
    diff.title ? h("article", { class: "message-card" }, h("p", { class: "meta", text: "Title" }),
      diffView(diff.title.before ?? "", diff.title.after ?? "")) : null,
    blocks.map(blockChange),
    (diff.claims || []).length ? h("article", { class: "message-card" }, h("p", { class: "meta", text: "Claims" }),
      h("ul", {}, diff.claims.map((c) => h("li", {}, `${c.id}: ${!c.before ? "added" : !c.after ? "removed" : "changed"}`)))) : null,
    s.status === "open" && atLeast("editor") ? [
      h("label", { class: "field" }, "Note", note),
      status,
      h("div", { class: "actions" },
        h("button", { class: "toggle primary", type: "button", text: "Accept", onclick: () => resolve(true) }),
        h("button", { class: "toggle", type: "button", text: "Reject", onclick: () => resolve(false) }))] : null,
    s.status !== "open" ? h("p", { class: "meta", text: `${s.status === "accepted" ? "Accepted" : "Rejected"} by ${s.resolved_by?.name || "someone"} · ${when(s.resolved_at)}${s.note ? ` · ${s.note}` : ""}` }) : null);
}

// -- a person's reports and inbox (SPEC §7.2) -------------------------------------------

const INBOX_LABEL = { challenge: "Challenge", suggestion: "Suggestion", review: "Review", publish: "Publish" };

function reportHref(rid, extra = {}) {
  return `#${new URLSearchParams({ key: state.key, report: rid, ...extra })}`;
}

function linkSecret(text) {
  // Accepts a whole share link (…/view#key=bvl_…) or just its secret.
  const m = String(text).match(/key=([^&\s]+)/);
  return m ? decodeURIComponent(m[1]) : String(text).trim();
}

const when = (stamp) => stamp
  ? new Date(stamp).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
  : "—";

function needsYou(r) {
  // A null count is data this role cannot see, so it shows nothing rather than a zero.
  const chips = [];
  const add = (n, cls, one, many) => { if (n) chips.push(h("span", { class: `chip ${cls}`, text: `${n} ${n === 1 ? one : many}` })); };
  add(r.needs_review, "error", "block to review", "blocks to review");
  add(r.pending_suggestions, "", "suggestion", "suggestions");
  add(r.open_challenges?.awaiting_me, "warning", "challenge needs you", "challenges need you");
  add(r.open_challenges?.awaiting_agent, "info", "challenge waiting on the agent", "challenges waiting on the agent");
  add(r.unresolved_pins, "warning", "pin out of date", "pins out of date");
  return chips.length ? h("span", { class: "chips" }, chips) : h("span", { class: "meta", text: "Nothing" });
}

function reportsTable(reports) {
  return h("div", { class: "table-wrap" }, h("table", { class: "rows index" },
    h("thead", {}, h("tr", {}, ["Report", "Your role", "Version", "Needs you", "Last activity"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, reports.map((r) => h("tr", {},
      h("th", { scope: "row" }, h("a", { href: reportHref(r.id), text: r.title })),
      h("td", { text: r.role }),
      h("td", {},
        r.latest_version ? `v${r.latest_version}` : "unpublished",
        r.unpublished_changes ? h("span", { class: "chip", text: "draft ahead" }) : null),
      h("td", {}, needsYou(r)),
      h("td", { text: when(r.last_activity) }))))));
}

function summaryText(item, titles) {
  // The title is already shown beside the label; some summaries begin with it.
  const title = titles.get(item.report_id);
  const text = String(item.summary || "");
  const bare = title && text.startsWith(`${title}: `) ? text.slice(title.length + 2) : text;
  return (bare.charAt(0).toUpperCase() + bare.slice(1)).slice(0, 240);
}

function inboxList(items, reports) {
  if (!items.length) return h("p", { class: "meta", text: "Nothing needs you right now." });
  const titles = new Map(reports.map((r) => [r.id, r.title]));
  return h("ul", { class: "inbox" }, items.map((i) => h("li", {},
    h("a", { href: reportHref(i.report_id, i.open?.challenge ? { ch: i.open.challenge } : i.open?.suggestion ? { s: i.open.suggestion } : i.open?.block ? { rv: i.open.block } : {}) },
      h("span", { class: `chip ${i.kind === "review" ? "error" : ""}`, text: INBOX_LABEL[i.kind] || i.kind }),
      " ", h("strong", { text: titles.get(i.report_id) || i.report_id })),
    h("p", { class: "inbox-summary", text: summaryText(i, titles) }),
    h("p", { class: "meta", text: [i.author?.name, when(i.at)].filter(Boolean).join(" · ") }))));
}

async function showIndex(tab) {
  state.tab = tab === "inbox" ? "inbox" : "reports";
  document.title = "My reports · Bevis";
  $("title").textContent = "My reports";
  fill($("meta"), state.me.name);
  for (const id of ["version-control", "challenges", "pdf", "share", "audit"]) $(id).hidden = true;
  const root = $("report");
  fill(root, h("p", { class: "meta", text: "Loading…" }));
  let reports, inbox;
  try {
    [reports, inbox] = await Promise.all([api("/api/me/reports"), api("/api/me/inbox")]);
  } catch (err) {
    fill(root, notice(err, "This token is invalid or has been revoked."));
    return;
  }
  drawIndex(root, reports, inbox);
}

function drawIndex(root, reports, inbox) {
  const status = h("p", { class: "message", role: "alert" });
  const tabButton = (name, label) => h("button", {
    class: "tab", type: "button", role: "tab", "aria-selected": String(state.tab === name), text: label,
    onclick: () => {
      state.tab = name;
      history.replaceState(null, "", `#${new URLSearchParams({ key: state.key, tab: name })}`);
      drawIndex(root, reports, inbox);
    },
  });
  const add = h("form", { class: "compose add-report" },
    h("label", { class: "field" }, "Add a report",
      h("input", { name: "link", required: true, autocomplete: "off", spellcheck: "false", placeholder: "Paste a share link" })),
    status,
    h("div", { class: "actions" }, h("button", { class: "toggle", type: "submit", text: "Add" })));
  add.addEventListener("submit", async (e) => {
    e.preventDefault();
    status.textContent = "";
    try {
      await api("/api/me/claims", { method: "POST", body: { link: linkSecret(new FormData(add).get("link")) } });
      await showIndex(state.tab);
    } catch (err) {
      status.textContent = err.status === 404 ? "That link is not valid, or it has been revoked." : err.message;
    }
  });
  fill(root, h("div", { class: "index-view" },
    h("div", { class: "tabs", role: "tablist" },
      tabButton("reports", `Reports (${reports.length})`),
      tabButton("inbox", inbox.length ? `Inbox (${inbox.length})` : "Inbox"),
      h("button", { class: "quiet", type: "button", text: "Refresh", onclick: () => showIndex(state.tab) })),
    state.tab === "inbox"
      ? inboxList(inbox, reports)
      : reports.length
        ? reportsTable(reports)
        : h("div", { class: "notice" }, h("p", { text: "You have no reports yet. Add one with a share link below." })),
    add));
}

// -- editing prose (SPEC §6.2) ------------------------------------------------------------

// Editors and admins change the draft; suggesters use the same editor to propose a change.
const suggesting = () => !atLeast("editor");
const canEditBlock = (block) => state.report?.kind === "draft" && atLeast("suggester")
  && (block.type === "text" || block.type === "heading");

const blockRevision = (id) => state.report.block_state?.[id]?.revision ?? 0;

function stashKey(id) {
  return `draft.${state.report.id}.${id}`;
}

function stash(id, ed) {
  // Unsaved text stays in the browser until a save succeeds.
  remembered(stashKey(id), ed ? JSON.stringify({ base: ed.base, text: ed.text,
    pending: [...ed.pending], unlinked: [...ed.unlinked] }) : "");
}

function upsertOp(block, text) {
  const out = { id: block.id, type: block.type, text };
  if (block.type === "heading") out.level = block.level;
  return { op: "upsert_block", block: out };
}

function openEditor(block) {
  const ed = { text: block.text, base: blockRevision(block.id), original: block.text, findings: null,
    conflict: null, error: null, saving: false, seq: 0, restored: false, leftover: [], message: "",
    block, pending: new Map(), unlinked: new Set(), selected: null, sel: null, composer: null, check: null, drag: null,
    claimIds: new Set(state.report.claims.filter((c) => c.block_id === block.id).map((c) => c.id)) };
  try {
    const saved = JSON.parse(remembered(stashKey(block.id)) || "null");
    if (saved && typeof saved.text === "string" && (saved.text !== block.text || saved.pending?.length || saved.unlinked?.length)) {
      Object.assign(ed, { text: saved.text, base: Number.isInteger(saved.base) ? saved.base : ed.base, restored: true,
        pending: new Map(saved.pending || []), unlinked: new Set(saved.unlinked || []) });
      for (const id of ed.pending.keys()) ed.claimIds.add(id);
    }
  } catch { /* an unreadable stash is ignored */ }
  state.editing.set(block.id, ed);
  renderReport().then(() => {
    $("report").querySelector(`#blk-${CSS.escape(block.id)} .edit-field`)?.focus();
    schedulePreview(block, ed, 0);
  });
}

function closeEditor(block) {
  state.editing.delete(block.id);
  stash(block.id, null);
  renderReport();
}

function blockFindings(findings, id, extra = []) {
  const claims = new Set([...state.report.claims.filter((c) => c.block_id === id).map((c) => c.id), ...extra]);
  return findings.filter((f) => f.block === id || (f.claim && claims.has(f.claim)));
}

function schedulePreview(block, ed, delay = 400) {
  clearTimeout(ed.timer);
  ed.timer = setTimeout(() => preview(block, ed), delay);
}

const isDirty = (ed) => ed.text !== ed.original || ed.pending.size > 0 || ed.unlinked.size > 0;

async function preview(block, ed) {
  if (!isDirty(ed) || !ed.text.trim()) {
    ed.findings = null;
    ed.check = null;
    paintEditor(block, ed);
    return;
  }
  const seq = ++ed.seq;
  try {
    const out = await api(`/api/reports/${state.report.id}/validate`, { method: "POST", body: {
      ops: buildOps(block, ed, { lenient: true }), base_block_revisions: { [block.id]: ed.base } } });
    if (seq !== ed.seq) return; // a newer keystroke is already being checked
    ed.findings = blockFindings(out.validation.findings, block.id, ed.pending.keys());
    ed.check = Object.fromEntries((out.diff?.claims || []).filter((c) => c.after).map((c) => [c.id, c.after]));
    ed.error = null;
    ed.conflict = null;
  } catch (err) {
    if (seq !== ed.seq) return;
    if (err.code === "block_conflict") ed.conflict = err.details.blocks[0];
    else { ed.error = errorText(err); ed.findings = null; ed.check = null; }
  }
  paintEditor(block, ed);
  updateComposerResults(ed);
}

function errorText(err) {
  const first = Array.isArray(err.details) ? err.details[0] : null;
  return first?.message ? `${err.message}: ${first.message}` : err.message;
}

function paintEditor(block, ed) {
  const root = $("report").querySelector(`#blk-${CSS.escape(block.id)}`);
  if (!root) return;
  const changed = isDirty(ed) && ed.text.trim() !== "";
  const unfinished = unfinishedClaim(block, ed);
  root.querySelector(".edit-save").disabled = ed.saving || !changed || Boolean(unfinished);
  const errors = ed.findings?.filter((f) => f.severity === "error").length ?? 0;
  const warnings = ed.findings?.filter((f) => f.severity === "warning").length ?? 0;
  fill(root.querySelector(".edit-status"),
    ed.restored && ed.text !== ed.original ? h("p", { class: "meta", text: "Restored your unsaved text." }) : null,
    ed.error ? h("p", { class: "message", text: ed.error }) : null,
    unfinished ? h("p", { class: "meta", text: unfinished }) : null,
    ed.findings === null ? null : h("p", { class: "meta" },
      errors ? h("span", { class: "chip error", text: plural(errors, "error") }) : null,
      warnings ? h("span", { class: "chip warning", text: plural(warnings, "warning") }) : null,
      !errors && !warnings ? h("span", { class: "chip ok", text: "no problems" }) : null),
    ed.findings?.filter((f) => f.severity !== "info").slice(0, 6).map((f) =>
      h("p", { class: "finding" }, h("span", { class: `chip ${f.severity}`, text: f.severity }), " ",
        f.claim ? h("code", { text: f.claim }) : null, f.claim ? " " : null, f.message)));
  paintClaimTools(block, ed);
  fill(root.querySelector(".edit-conflict"), ed.conflict ? conflictNotice(block, ed) : null);
  fill(root.querySelector(".edit-leftover"), ed.leftover.length ? h("div", { class: "notice" },
    h("p", { class: "meta", text: "Your version of what you both changed (copy from it, then dismiss):" }),
    ed.leftover.map((t) => h("pre", { class: "current-text", text: t })),
    h("button", { class: "quiet", type: "button", text: "Dismiss", onclick: () => { ed.leftover = []; paintEditor(block, ed); } })) : null);
}

function diffView(from, to) {
  return h("p", { class: "diff" }, diffWords(from, to).map((d) =>
    d.type === "eq" ? d.text : h(d.type === "ins" ? "ins" : "del", { text: d.text })));
}

function applyText(block, ed, text, base, claims) {
  // Put `text` in the editor, as the text that was current at revision `base`.
  ed.text = text;
  ed.original = ed.conflict?.block?.text ?? ed.original;
  ed.base = base;
  ed.conflict = null;
  ed.findings = null;
  ed.claimIds = new Set([...claims.map((c) => c.id), ...ed.pending.keys()]);
  stash(block.id, isDirty(ed) ? ed : null);
  setFieldText($("report").querySelector(`#blk-${CSS.escape(block.id)} .edit-field`), text, ed.claimIds, ed);
  paintEditor(block, ed);
  schedulePreview(block, ed, 0);
}

function conflictNotice(block, ed) {
  const c = ed.conflict;
  if (c.block === null) {
    return h("div", { class: "notice error" },
      h("p", { text: "This block was deleted while you were editing it. Your text is kept below." }),
      h("button", { class: "toggle", type: "button", text: "Discard my text", onclick: () => closeEditor(block) }));
  }
  // The text you started from, yours, and the current one: merge by paragraph where it is safe.
  const merged = merge3(ed.original, ed.text, c.block.text);
  const clean = merged.conflicts.length === 0;
  return h("div", { class: "notice error", role: "alert" },
    h("p", {}, `This block changed while you were editing it (revision ${c.base_revision} → ${c.revision}). Your text is kept.`),
    h("p", { class: "meta", text: "What changed since you started:" }),
    diffView(ed.original, c.block.text),
    clean ? null : h("p", { class: "message", text: `${merged.conflicts.length === 1 ? "A paragraph" : `${merged.conflicts.length} places`} you both changed. A merge keeps the current version of ${merged.conflicts.length === 1 ? "it" : "them"} and leaves yours below to copy from.` }),
    h("div", { class: "actions" },
      h("button", { class: "toggle primary", type: "button", text: clean ? "Merge" : "Merge, keeping theirs where we clash",
        title: "Combine your changes with the current text, then review it before saving",
        onclick: () => {
          ed.leftover = merged.conflicts.map((x) => x.mine.join("\n\n"));
          applyText(block, ed, merged.text, c.revision, c.claims);
        } }),
      h("button", { class: "toggle", type: "button", text: "Keep mine", title: "Save my text over the current one",
        onclick: () => { ed.base = c.revision; ed.conflict = null; saveBlock(block, ed); } }),
      h("button", { class: "toggle", type: "button", text: "Use current text", title: "Replace my text with the current one",
        onclick: () => applyText(block, ed, c.block.text, c.revision, c.claims) })));
}

async function saveBlock(block, ed) {
  ed.saving = true;
  ed.error = null;
  paintEditor(block, ed);
  const suggest = suggesting();
  try {
    const out = await api(`/api/reports/${state.report.id}/${suggest ? "suggestions" : "edits"}`, { method: "POST", body: {
      ops: buildOps(block, ed), base_block_revisions: { [block.id]: ed.base },
      ...(suggest ? { message: ed.message.trim() } : {}) } });
    state.editing.delete(block.id);
    stash(block.id, null);
    state.flash = { block: block.id, text: suggest
      ? "Suggested. An editor will review it before it changes the report."
      : out.blocks?.[block.id]?.needs_review
        ? "Saved. This edit is near a claim, so an admin has to review it before the report can be published."
        : "Saved." };
    const y = window.scrollY;
    await loadReport();
    window.scrollTo(0, y);
  } catch (err) {
    ed.saving = false;
    if (err.code === "block_conflict") ed.conflict = err.details.blocks[0];
    else ed.error = err.message;
    paintEditor(block, ed);
  }
}

// A claim span in the editor is a chip that cannot be typed into. It carries the span's whole
// Markdown source, so what is saved is exactly what was loaded. Where the browser cannot edit
// plain text in a contenteditable, the editor is a plain textarea of Markdown instead.
const richSupported = (() => {
  const d = document.createElement("div");
  d.setAttribute("contenteditable", "plaintext-only");
  return d.contentEditable === "plaintext-only";
})();

function chipEl(span, ed) {
  const pending = ed.pending.has(span.id);
  const select = () => selectClaim(ed.block, ed, span.id);
  return h("span", { class: `claim-chip${pending ? " pending" : ""}${ed.selected === span.id ? " selected" : ""}`,
    contenteditable: "false", role: "button", tabindex: "0", draggable: "true",
    "data-src": span.source, "data-claim": span.id,
    title: `Claim ${span.id}: locked. Click for what you can do with it; drag it, or press Alt+Arrow, to move it.`,
    onclick: select,
    ondragstart: (e) => { ed.drag = e.currentTarget; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", ""); },
    ondragend: () => { ed.drag = null; },
    onkeydown: (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
      else if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        e.preventDefault();
        moveChip(ed.block, ed, e.currentTarget, e.key === "ArrowLeft" ? -1 : 1);
      }
    } },
  span.inner, h("sup", { text: span.id }));
}

const isRich = (field) => field.classList.contains("rich-field");

function setFieldText(field, text, ids, ed) {
  if (!isRich(field)) { field.value = text; return; }
  field.replaceChildren(...splitSpans(text, ids).map((p) => (p.span ? chipEl(p.span, ed) : document.createTextNode(p.text))));
}

function fieldText(field) {
  if (!isRich(field)) return field.value;
  let out = "";
  const walk = (node) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) out += n.nodeValue;
      else if (n.dataset?.src !== undefined) out += n.dataset.src;
      else if (n.nodeName === "BR") { if (!(node === field && n === node.lastChild)) out += "\n"; }
      else if (n.nodeName === "DIV" || n.nodeName === "P") { if (out && !out.endsWith("\n")) out += "\n"; walk(n); }
      else walk(n);
    }
  };
  walk(field);
  // After a final newline the browser adds one more, so the empty last line shows; it is not text.
  return out.endsWith("\n") ? out.slice(0, -1) : out;
}

function blockEditor(block) {
  const ed = state.editing.get(block.id);
  const heading = block.type === "heading";
  const rich = !heading && richSupported;
  const label = heading ? "Heading text" : `Text of block ${block.id}${rich ? "" : ", as Markdown"}`;
  const field = heading
    ? h("input", { class: "edit-field", type: "text", "aria-label": label })
    : rich
      ? h("div", { class: "edit-field rich-field", contenteditable: "plaintext-only", role: "textbox",
        "aria-multiline": "true", "aria-label": label, spellcheck: "true" })
      : h("textarea", { class: "edit-field", rows: String(Math.max(4, Math.min(24, ed.text.split("\n").length + 2))),
        spellcheck: "true", "aria-label": label });
  setFieldText(field, ed.text, ed.claimIds, ed);
  field.addEventListener("input", () => edited(block, ed, field));
  if (rich) {
    field.addEventListener("mouseup", () => captureSelection(block, ed, field));
    field.addEventListener("keyup", () => captureSelection(block, ed, field));
    field.addEventListener("dragover", (e) => { if (ed.drag) e.preventDefault(); });
    field.addEventListener("drop", (e) => { if (ed.drag) { e.preventDefault(); dropChip(block, ed, field, e); } });
  }
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (!$("report").querySelector(`#blk-${CSS.escape(block.id)} .edit-save`).disabled) saveBlock(block, ed);
    }
  });
  const hasClaims = ed.claimIds.size > 0 || rich;
  const suggest = suggesting();
  const el = h("div", { class: "block-editor" },
    suggest ? h("p", { class: "meta", text: "You are suggesting an edit. An editor reviews it before it changes the report." }) : null,
    hasClaims ? h("p", { class: "meta", text: rich
      ? "Claims are locked: click one for what you can do with it, or select text to make a claim. An edit near a claim needs an admin's review before the report is published."
      : "This block has claims. Text inside [ ]{#cN} is what the validator checks, and an edit near a claim needs an admin's review before the report is published." }) : null,
    field,
    rich ? h("div", { class: "claim-tools" }) : null,
    h("div", { class: "claim-composer" }),
    h("div", { class: "edit-status", role: "status", "aria-live": "polite" }),
    h("div", { class: "edit-conflict" }),
    h("div", { class: "edit-leftover" }),
    suggest ? h("label", { class: "field" }, "Why? (optional)",
      h("input", { class: "edit-message", type: "text", maxlength: "300", value: ed.message, placeholder: "A line for the editor",
        oninput: (e) => { ed.message = e.target.value; } })) : null,
    h("div", { class: "actions" },
      h("button", { class: "toggle primary edit-save", type: "button", text: suggest ? "Suggest edit" : "Save", disabled: true, onclick: () => saveBlock(block, ed) }),
      h("button", { class: "toggle", type: "button", text: "Cancel", onclick: () => {
        if (isDirty(ed) && !confirm("Discard your changes to this block?")) return;
        closeEditor(block);
      } }),
      h("span", { class: "meta", text: "Ctrl+Enter saves" })));
  queueMicrotask(() => paintEditor(block, ed));
  return el;
}

// -- claims in the editor (SPEC §6.2) ----------------------------------------------------------
//
// Select text to make it a claim, click a chip to edit, unlink, or delete its claim, and drag a chip
// (or press Alt+Arrow on it) to move it. Each of these is part of one edit: the block's text plus
// link_claim and unlink_claim ops, so a claim is never dropped between a removal and an addition.
// The validator's reply, shown live, is the authority on whether a claim holds.

const latestVersions = () => Object.fromEntries((state.report.evidence || []).map((e) => [e.id, e.latest_version]));
const blockRoot = (block) => $("report").querySelector(`#blk-${CSS.escape(block.id)}`);
const fieldOf = (block) => blockRoot(block)?.querySelector(".edit-field");
const spanInner = (src) => src.slice(1, src.lastIndexOf("]{#"));
const shorten = (text, n = 60) => (text.length > n ? `${text.slice(0, n)}…` : text);
const toolButton = (text, onclick, cls = "") => h("button", { class: `quiet ${cls}`.trim(), type: "button", text, onclick });

function buildOps(block, ed, { lenient = false } = {}) {
  const present = new Set(findSpans(ed.text, ed.claimIds).map((s) => s.id));
  const latest = latestVersions();
  return [
    upsertOp(block, ed.text),
    ...[...ed.pending].filter(([id]) => present.has(id))
      .map(([, def]) => ({ op: "link_claim", claim: claimForOps(def, block.id, latest, { lenient }) })),
    ...[...ed.unlinked].map((id) => ({ op: "unlink_claim", id })),
  ];
}

function edited(block, ed, field) {
  ed.text = fieldText(field);
  ed.restored = false;
  ed.sel = null;
  stash(block.id, ed);
  paintEditor(block, ed);
  schedulePreview(block, ed);
}

function unfinishedClaim(block, ed) {
  if (ed.composer) return `Finish claim ${ed.composer.id} (Done) before saving.`;
  const present = new Set(findSpans(ed.text, ed.claimIds).map((s) => s.id));
  for (const [id, def] of ed.pending) {
    if (present.has(id) && !def.numbers.every(isComplete)) return `Claim ${id} has a number that is not filled in yet.`;
  }
  return null;
}

// -- the bar under the text -----------------------------------------------------------------

function paintClaimTools(block, ed) {
  const bar = blockRoot(block)?.querySelector(".claim-tools");
  const field = fieldOf(block);
  if (!bar || !field) return;
  if (ed.selected && !field.querySelector(`.claim-chip[data-claim="${ed.selected}"]`)) ed.selected = null;
  for (const chip of field.querySelectorAll(".claim-chip")) {
    chip.classList.toggle("selected", chip.dataset.claim === ed.selected);
    chip.classList.toggle("pending", ed.pending.has(chip.dataset.claim));
  }
  const id = ed.selected;
  if (id) {
    const inReport = state.report.claims.some((c) => c.id === id);
    fill(bar, h("span", { class: "meta", text: `Claim ${id}:` }),
      inReport ? toolButton("Show evidence", () => openClaim(id)) : null,
      toolButton("Edit claim", () => editClaim(block, ed, id)),
      toolButton("Unlink, keep the text", () => unlinkClaim(block, ed, id, true)),
      toolButton("Delete claim and text", () => unlinkClaim(block, ed, id, false)));
  } else if (ed.sel) {
    fill(bar, h("span", { class: "meta", text: `“${shorten(ed.sel.text)}”` }),
      toolButton("Make this a claim", () => startClaim(block, ed), "primary-tool"));
  } else {
    fill(bar, h("span", { class: "meta", text: "Select text to make it a claim." }));
  }
}

function selectClaim(block, ed, id) {
  ed.selected = id;
  paintClaimTools(block, ed);
}

function captureSelection(block, ed, field) {
  const sel = getSelection();
  ed.sel = null;
  if (sel && sel.rangeCount && !sel.isCollapsed) {
    const r = sel.getRangeAt(0);
    const node = r.startContainer;
    if (node === r.endContainer && node.nodeType === 3 && field.contains(node) && !node.parentElement.closest(".claim-chip")) {
      const text = node.nodeValue.slice(r.startOffset, r.endOffset);
      if (text.trim()) ed.sel = { node, start: r.startOffset, end: r.endOffset, text };
    }
  }
  if (ed.sel) ed.selected = null;
  paintClaimTools(block, ed);
}

// -- moving, unlinking, deleting ------------------------------------------------------------------

function caretRangeAt(x, y) {
  if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
  const pos = document.caretPositionFromPoint?.(x, y);
  if (!pos) return null;
  const range = document.createRange();
  range.setStart(pos.offsetNode, pos.offset);
  range.collapse(true);
  return range;
}

function dropChip(block, ed, field, e) {
  const chip = ed.drag;
  ed.drag = null;
  const range = caretRangeAt(e.clientX, e.clientY);
  if (!chip || !range || !field.contains(range.startContainer)) return;
  const at = range.startContainer;
  if (chip.contains(at) || at.parentElement?.closest(".claim-chip") || at.nodeType === 1 && at.closest?.(".claim-chip")) return;
  range.collapse(true);
  range.insertNode(chip); // moves it: the claim id and its source go with it
  field.normalize();
  edited(block, ed, field);
}

function moveChip(block, ed, chip, direction) {
  const prev = chip.previousSibling;
  const next = chip.nextSibling;
  const moved = moveOverWord(prev?.nodeType === 3 ? prev.nodeValue : "", next?.nodeType === 3 ? next.nodeValue : "", direction);
  if (!moved) return;
  if (prev?.nodeType === 3) prev.nodeValue = moved.before;
  else if (moved.before) chip.before(document.createTextNode(moved.before));
  if (next?.nodeType === 3) next.nodeValue = moved.after;
  else if (moved.after) chip.after(document.createTextNode(moved.after));
  chip.focus();
  edited(block, ed, fieldOf(block));
}

function unlinkClaim(block, ed, id, keepText) {
  const field = fieldOf(block);
  const chip = field.querySelector(`.claim-chip[data-claim="${id}"]`);
  if (chip) {
    if (keepText) chip.replaceWith(document.createTextNode(spanInner(chip.dataset.src)));
    else chip.remove();
  }
  field.normalize();
  ed.pending.delete(id);
  ed.claimIds.delete(id);
  if (state.report.claims.some((c) => c.id === id)) ed.unlinked.add(id);
  ed.selected = null;
  if (ed.composer?.id === id) { ed.composer = null; drawComposer(block, ed); }
  edited(block, ed, field);
}

// -- making and editing a claim ---------------------------------------------------------------

function startClaim(block, ed) {
  const { node, start, end } = ed.sel;
  const raw = node.nodeValue.slice(start, end);
  const from = start + (raw.length - raw.trimStart().length);
  const to = end - (raw.length - raw.trimEnd().length);
  const selected = node.nodeValue.slice(from, to);
  if (/[[\]\n]/.test(selected)) {
    ed.error = "Select plain text within one line: a claim cannot hold brackets or a line break.";
    paintEditor(block, ed);
    return;
  }
  const id = nextClaimId([...state.report.claims.map((c) => c.id), ...ed.pending.keys()]);
  ed.pending.set(id, { id, numbers: detectNumbers(selected).map((text) => ({ text, tier: "direct" })), extraEvidence: [] });
  ed.claimIds.add(id);
  ed.error = null;
  const middle = node.splitText(from);
  middle.splitText(to - from);
  middle.replaceWith(chipEl({ id, source: `[${selected}]{#${id}}`, inner: selected }, ed));
  ed.sel = null;
  ed.selected = id;
  const field = fieldOf(block);
  ed.text = fieldText(field);
  openComposer(block, ed, id, { isNew: true });
}

const editableClaim = (claim) => ({
  id: claim.id, derivation: claim.derivation, extraEvidence: [],
  numbers: claim.numbers.map((n) => ({ text: n.text, tier: n.tier, cell: n.cell && { ...n.cell, key: [...n.cell.key] },
    expression: n.expression, derivation: n.derivation })),
});

function editClaim(block, ed, id) {
  const snapshot = ed.pending.has(id) ? structuredClone(ed.pending.get(id)) : null;
  if (!snapshot) {
    const claim = state.report.claims.find((c) => c.id === id);
    if (!claim) return;
    ed.pending.set(id, editableClaim(claim));
  }
  openComposer(block, ed, id, { isNew: false, snapshot });
}

function openComposer(block, ed, id, opts = {}) {
  const chip = fieldOf(block).querySelector(`.claim-chip[data-claim="${id}"]`);
  ed.restored = false;
  ed.composer = { id, isNew: Boolean(opts.isNew), snapshot: opts.snapshot ?? null, text: chip ? spanInner(chip.dataset.src) : "" };
  drawComposer(block, ed);
  stash(block.id, ed);
  paintEditor(block, ed);
  schedulePreview(block, ed, 0);
}

function finishComposer(block, ed, keep) {
  const cmp = ed.composer;
  ed.composer = null;
  if (!keep) {
    if (cmp.isNew) {
      unlinkClaim(block, ed, cmp.id, true); // the text goes back to plain text
    } else {
      if (cmp.snapshot) ed.pending.set(cmp.id, cmp.snapshot);
      else ed.pending.delete(cmp.id);
    }
  }
  drawComposer(block, ed);
  stash(block.id, isDirty(ed) ? ed : null);
  paintEditor(block, ed);
  schedulePreview(block, ed, 0);
}

const TIERS = [["direct", "A cell's value"], ["computed", "Computed from cells"], ["asserted", "Asserted, no data"]];

function drawComposer(block, ed) {
  const box = blockRoot(block)?.querySelector(".claim-composer");
  if (!box) return;
  const cmp = ed.composer;
  if (!cmp) { fill(box); return; }
  const def = ed.pending.get(cmp.id);
  const pinsLine = h("p", { class: "meta" });
  const paintPins = () => {
    const pins = derivePins(def.numbers, latestVersions(), def.extraEvidence);
    pinsLine.textContent = pins.length ? `Pins: ${pins.map((p) => `${p.id} v${p.version}`).join(", ")}` : "Pins: no evidence yet";
  };
  const changed = () => { ed.restored = false; paintPins(); stash(block.id, ed); paintEditor(block, ed); schedulePreview(block, ed); };
  const redraw = () => { drawComposer(block, ed); changed(); };

  const rows = def.numbers.map((n, i) => {
    const body = n.tier === "direct"
      ? directBody(n, changed)
      : n.tier === "computed" ? computedBody(n, changed)
        : h("label", { class: "field" }, "Where it comes from",
          h("input", { type: "text", value: n.derivation || "", placeholder: "e.g. told by finance on a call",
            oninput: (e) => { n.derivation = e.target.value; changed(); } }));
    return h("div", { class: "cmp-number" },
      h("div", { class: "row" },
        h("label", { class: "field" }, "Number as written",
          h("input", { type: "text", value: n.text, oninput: (e) => { n.text = e.target.value; changed(); } })),
        h("label", { class: "field" }, "Supported by",
          h("select", { onchange: (e) => { n.tier = e.target.value; redraw(); } },
            TIERS.map(([v, t]) => h("option", { value: v, text: t, selected: n.tier === v })))),
        toolButton("Remove", () => { def.numbers.splice(i, 1); redraw(); })),
      body,
      h("p", { class: "cmp-result", "data-i": String(i) }));
  });

  const addInput = h("input", { type: "text", placeholder: "e.g. 12%", "aria-label": "Another number in the text" });
  const evidenceIds = (state.report.evidence || []).map((e) => e.id);
  const extraSel = h("select", { "aria-label": "Evidence to cite" }, evidenceIds.map((id) => h("option", { value: id, text: id })));
  const extraList = h("span", { class: "chips" });
  const paintExtras = () => fill(extraList, def.extraEvidence.map((p, i) => h("span", { class: "chip" }, `${p.id} v${p.version} `,
    h("button", { class: "chip-x", type: "button", "aria-label": `Stop citing ${p.id}`, text: "×",
      onclick: () => { def.extraEvidence.splice(i, 1); paintExtras(); changed(); } }))));

  fill(box, h("div", { class: "notice composer" },
    h("h4", { text: `Claim ${cmp.id}` }),
    h("p", { class: "meta", text: `“${cmp.text}”. Every number in it needs a source; years and dates do not.` }),
    rows,
    h("div", { class: "row" },
      h("label", { class: "field" }, "Another number", addInput),
      toolButton("Add", () => { if (addInput.value.trim()) { def.numbers.push({ text: addInput.value.trim(), tier: "direct" }); redraw(); } }),
      toolButton("Find numbers in the text", () => {
        for (const text of detectNumbers(cmp.text)) if (!def.numbers.some((n) => n.text === text)) def.numbers.push({ text, tier: "direct" });
        redraw();
      })),
    h("div", { class: "row" },
      h("label", { class: "field" }, "Also cite evidence (for a claim with no number)", extraSel),
      toolButton("Cite it", () => {
        if (extraSel.value && !def.extraEvidence.some((p) => p.id === extraSel.value)) {
          def.extraEvidence.push({ id: extraSel.value, version: latestVersions()[extraSel.value] || 1 });
          paintExtras(); changed();
        }
      }), extraList),
    pinsLine,
    h("div", { class: "cmp-findings" }),
    h("div", { class: "actions" },
      h("button", { class: "toggle primary", type: "button", text: "Done", onclick: () => finishComposer(block, ed, true) }),
      h("button", { class: "toggle", type: "button", text: cmp.isNew ? "Cancel, not a claim" : "Cancel",
        onclick: () => finishComposer(block, ed, false) }))));
  paintExtras();
  paintPins();
  updateComposerResults(ed);
}

function updateComposerResults(ed) {
  const cmp = ed.composer;
  if (!cmp) return;
  const box = blockRoot(ed.block)?.querySelector(".claim-composer");
  if (!box) return;
  // The check ran on the numbers that were filled in, in order, so line them up with the form's rows.
  // A claim the edit left unchanged is not in the check, and shows what the report already says of it.
  const def = ed.pending.get(cmp.id);
  const checked = ed.check?.[cmp.id];
  const stored = checked ? null : state.report.claims.find((c) => c.id === cmp.id);
  const filled = def.numbers.map((n, i) => (isComplete(n) ? i : -1)).filter((i) => i >= 0);
  for (const el of box.querySelectorAll(".cmp-result")) {
    const i = Number(el.dataset.i);
    const n = checked ? checked.numbers?.[filled.indexOf(i)] : stored?.numbers?.[i];
    if (!isComplete(def.numbers[i])) { fill(el, h("span", { class: "meta", text: "Not filled in yet." })); continue; }
    fill(el, n ? [
      h("span", { class: `chip ${n.status}`, text: STATUS_LABEL[n.status] || n.status }),
      n.tier === "computed" && n.value != null ? ` computed ${shortValue(n.value)}` : null,
      n.message ? ` ${n.message}` : null]
      : h("span", { class: "meta", text: ed.error ? "" : "Checking…" }));
  }
  fill(box.querySelector(".cmp-findings"),
    (ed.findings || []).filter((f) => f.claim === cmp.id && f.severity !== "info").slice(0, 6).map((f) =>
      h("p", { class: "finding" }, h("span", { class: `chip ${f.severity}`, text: f.severity }), " ", f.message)));
}

function computedBody(n, changed) {
  const input = h("input", { type: "text", value: n.expression || "", spellcheck: "false",
    placeholder: "pct_change(cell(q-rev, 2024, revenue), cell(q-rev, 2025, revenue))",
    oninput: (e) => { n.expression = e.target.value; changed(); } });
  const picker = cellPicker({ insert: true, onPick: (text) => {
    const at = input.selectionStart ?? input.value.length;
    input.value = input.value.slice(0, at) + text + input.value.slice(input.selectionEnd ?? at);
    input.focus();
    input.setSelectionRange(at + text.length, at + text.length);
    n.expression = input.value;
    changed();
  } });
  return h("div", {}, h("label", { class: "field" }, "Expression", input),
    h("details", {}, h("summary", { text: "Insert a cell" }), picker));
}

function directBody(n, changed) {
  const summary = h("p", { class: "meta" });
  const paint = () => {
    summary.textContent = n.cell ? `Cell: ${n.cell.evidence} v${n.cell.version} · ${n.cell.key.join(" · ")} · ${n.cell.column}`
      : "Pick the row and column that hold this number.";
  };
  paint();
  return h("div", {}, summary, cellPicker({ cell: n.cell, onPick: (cell) => { n.cell = cell; paint(); changed(); } }));
}

// Pick a cell from evidence in the report: evidence and version, then a row by its key, then a column.
function cellPicker({ cell, onPick, insert = false }) {
  const evidenceList = state.report.evidence || [];
  if (!evidenceList.length) return h("p", { class: "meta", text: "This report has no evidence yet; an agent adds it." });
  const message = h("p", { class: "meta" });
  const evSel = h("select", { "aria-label": "Evidence" }, evidenceList.map((e) => h("option", { value: e.id, text: e.id })));
  const verSel = h("select", { "aria-label": "Version" });
  const filter = h("input", { type: "search", placeholder: "Find a row", "aria-label": "Find a row" });
  const rowSel = h("select", { size: "6", "aria-label": "Row" });
  const colSel = h("select", { "aria-label": "Column" });
  const shown = h("p", { class: "meta" });
  let doc = null;
  let keys = [];

  const versions = (keep) => {
    const latest = latestVersions()[evSel.value] || 1;
    verSel.replaceChildren(...Array.from({ length: latest }, (_, i) => h("option", { value: String(i + 1), text: `v${i + 1}` })));
    verSel.value = String(keep && keep <= latest ? keep : latest);
  };
  const names = () => doc.columns.map((c) => c.name);
  const drawRows = () => {
    const q = filter.value.trim().toLowerCase();
    const options = [];
    keys.forEach((k, i) => {
      const label = k.join(" · ");
      if (!q || label.toLowerCase().includes(q)) options.push(h("option", { value: String(i), text: label }));
    });
    rowSel.replaceChildren(...options.slice(0, 200));
    message.textContent = options.length > 200 ? `Showing 200 of ${options.length} rows; type to narrow them.` : "";
  };
  const pick = () => {
    if (!doc || rowSel.value === "" || !colSel.value) return;
    const r = Number(rowSel.value);
    const key = doc.key.map((k) => doc.rows[r][names().indexOf(k)]);
    const keyTypes = doc.key.map((k) => doc.columns.find((c) => c.name === k).type);
    const column = colSel.value;
    shown.textContent = `Value: ${doc.rows[r][names().indexOf(column)]}`;
    if (!insert) onPick({ evidence: evSel.value, version: Number(verSel.value), key, column });
    return { key, keyTypes, column };
  };
  const load = async (preselect) => {
    message.textContent = "Loading…";
    try {
      const res = await evidence(evSel.value, Number(verSel.value));
      doc = res.evidence;
      keys = res.row_keys || [];
      if (!doc.rows) { message.textContent = "The rows of this evidence are not available."; return; }
      const keyNames = new Set(doc.key);
      colSel.replaceChildren(...doc.columns.filter((c) => !keyNames.has(c.name)).map((c) => h("option", { value: c.name, text: c.name })));
      drawRows();
      if (preselect) {
        const want = JSON.stringify(preselect.key);
        const r = doc.rows.findIndex((row) => JSON.stringify(doc.key.map((k) => row[names().indexOf(k)])) === want);
        if (r >= 0) rowSel.value = String(r);
        colSel.value = preselect.column;
        pick();
      }
    } catch (err) {
      message.textContent = err.message;
    }
  };
  evSel.addEventListener("change", () => { versions(); load(); });
  verSel.addEventListener("change", () => load());
  filter.addEventListener("input", drawRows);
  rowSel.addEventListener("change", pick);
  colSel.addEventListener("change", pick);
  if (cell) { evSel.value = cell.evidence; versions(cell.version); } else versions();
  load(cell);
  return h("div", { class: "cell-picker" },
    h("div", { class: "row" },
      h("label", { class: "field" }, "Evidence", evSel), h("label", { class: "field" }, "Version", verSel),
      h("label", { class: "field" }, "Column", colSel)),
    h("label", { class: "field" }, "Row", filter, rowSel),
    shown, message,
    insert ? toolButton("Insert this cell", () => {
      const p = pick();
      if (p) onPick(cellExpression(evSel.value, p.key, p.keyTypes, p.column));
    }, "primary-tool") : null);
}

// -- sharing (admin) ------------------------------------------------------------------------

async function openShare() {
  state.compose = null;
  state.open = null;
  writeHash();
  showPanel("Share");
  const rid = state.report.id;
  const status = h("p", { class: "message", role: "alert" });
  const created = h("div", {});
  const list = h("div", {});

  const drawList = async () => {
    const links = await api(`/api/reports/${rid}/links`);
    fill(list, h("div", { class: "table-wrap" }, h("table", { class: "rows links" },
      h("thead", {}, h("tr", {}, ["Name", "Role", "Data", "Sees", "Expires", ""].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, links.map((l) => h("tr", { class: l.revoked_at ? "revoked" : null },
        h("td", { text: l.name }), h("td", { text: l.role }), h("td", { text: l.disclosure }),
        h("td", { text: l.scope === "draft" ? "draft" : l.scope === "latest" ? "latest version" : `version ${l.scope}` }),
        h("td", { text: l.revoked_at ? "revoked" : l.expires_at ? l.expires_at.slice(0, 10) : "never" }),
        h("td", {}, l.current ? h("span", { class: "meta", text: "yours" }) : l.revoked_at ? null : h("button", { class: "quiet", type: "button", text: "Revoke",
          onclick: async () => {
            if (!confirm(`Revoke the link for ${l.name}? It stops working at once.`)) return;
            try { await api(`/api/reports/${rid}/links/${l.id}`, { method: "DELETE" }); await drawList(); }
            catch (err) { status.textContent = err.message; }
          } }))))))));
  };

  const members = h("div", {});
  const drawMembers = async () => {
    const rows = await api(`/api/reports/${rid}/members`);
    if (!rows.length) {
      fill(members, h("p", { class: "meta", text: "No one has joined with a person token yet." }));
      return;
    }
    const scopeText = (s) => (s === "draft" ? "draft" : s === "latest" ? "latest version" : `version ${s}`);
    fill(members, h("div", { class: "table-wrap" }, h("table", { class: "rows members" },
      h("thead", {}, h("tr", {}, ["Person", "Role", "Data", "Sees", "Joined", ""].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, rows.map((m) => h("tr", {},
        h("td", {}, m.name, m.shared_with ? h("span", { class: "meta", text: ` (link shared with ${m.shared_with} other${m.shared_with === 1 ? "" : "s"})` }) : null),
        h("td", { text: m.role }), h("td", { text: m.disclosure }), h("td", { text: scopeText(m.scope) }),
        h("td", { text: (m.since || "").slice(0, 10) }),
        h("td", {}, m.current ? h("span", { class: "meta", text: "you" }) : h("button", { class: "quiet", type: "button", text: "Remove",
          onclick: async () => {
            const also = m.shared_with ? ` The link is shared, so ${m.shared_with} other${m.shared_with === 1 ? "" : "s"} will lose that link too.` : "";
            if (!confirm(`Remove ${m.name} from this report? Their links are revoked and they cannot rejoin with them.${also}`)) return;
            try {
              const res = await api(`/api/reports/${rid}/members/${m.person_id}`, { method: "DELETE" });
              status.textContent = res.also_removed.length ? `Removed ${m.name}; ${res.also_removed.length} other member(s) lost that link too.` : `Removed ${m.name}.`;
              await Promise.all([drawMembers(), drawList()]);
            } catch (err) { status.textContent = err.message; }
          } }))))))));
  };

  const versions = state.report.versions.map((v) => v.number);
  const form = h("form", { class: "compose share-form" },
    h("label", { class: "field" }, "Whose link is it?", h("input", { name: "name", required: true, placeholder: "e.g. Finance team" })),
    h("div", { class: "row" },
      h("label", { class: "field" }, "Role", h("select", { name: "role" },
        [["reader", "Reader"], ["commenter", "Commenter (can challenge)"], ["suggester", "Suggester (sees draft)"],
          ["editor", "Editor (sees draft)"], ["admin", "Admin"]].map(([v, t]) => h("option", { value: v, text: t })))),
      h("label", { class: "field" }, "Data", h("select", { name: "disclosure" },
        [["full", "All rows"], ["cited", "Cited rows only"], ["metadata", "No data values"]].map(([v, t]) => h("option", { value: v, text: t })))),
      h("label", { class: "field" }, "Sees", h("select", { name: "scope" },
        h("option", { value: "latest", text: "Latest version" }),
        versions.map((n) => h("option", { value: String(n), text: `Version ${n} only` })))),
      h("label", { class: "field" }, "Expires", h("input", { name: "expires", type: "date" }))),
    status,
    h("div", { class: "actions" }, h("button", { class: "toggle primary", type: "submit", text: "Create link" })));
  const roleSelect = form.querySelector('select[name="role"]');
  roleSelect.addEventListener("change", () => {
    // Suggesters and up work on the draft, so a version scope does not apply.
    form.querySelector('select[name="scope"]').disabled = ROLES.indexOf(roleSelect.value) >= ROLES.indexOf("suggester");
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const expires = f.get("expires");
    try {
      const link = await api(`/api/reports/${rid}/links`, { method: "POST", body: {
        name: f.get("name"), role: f.get("role"), disclosure: f.get("disclosure"),
        scope: f.get("scope") ?? "latest", expires_at: expires ? `${expires}T23:59:59Z` : undefined,
      } });
      const url = `${location.origin}/view#key=${encodeURIComponent(link.secret)}`;
      const input = h("input", { class: "secret", readonly: true, value: url, "aria-label": "New link" });
      fill(created, h("div", { class: "created" },
        h("p", {}, h("strong", { text: `Link for ${f.get("name")}` }), " — copy it now; it is not shown again."),
        input,
        h("button", { class: "toggle", type: "button", text: "Copy", onclick: async (ev) => {
          try { await navigator.clipboard.writeText(url); ev.target.textContent = "Copied"; }
          catch { input.select(); }
        } })));
      form.reset();
      status.textContent = "";
      await drawList();
    } catch (err) {
      status.textContent = err.message;
    }
  });
  fill($("panel-body"), h("p", { class: "meta", text: "Anyone holding a link can open this report with that link's role. Revoke a link to cut it off." }),
    form, created, h("h3", { text: "Links" }), list, h("h3", { text: "Members" }), members);
  try { await Promise.all([drawList(), drawMembers()]); } catch (err) { status.textContent = err.message; }
}

// -- controls ---------------------------------------------------------------------------

function setAudit(on) {
  state.audit = on;
  $("audit").setAttribute("aria-pressed", String(on));
  try { localStorage.setItem("bevis.audit", on ? "1" : "0"); } catch { /* storage may be blocked */ }
  if (state.report) renderReport().then(() => {
    if (state.open?.kind === "claim") {
      document.querySelector(`span.claim[data-claim="${CSS.escape(state.open.id)}"]`)?.classList.add("active");
    }
  });
}

$("audit").addEventListener("click", () => setAudit(!state.audit));
$("challenges").addEventListener("click", () => openChallengeList());
$("share").addEventListener("click", openShare);
$("suggestions").addEventListener("click", () => openSuggestionList());
$("home").addEventListener("click", () => { location.hash = new URLSearchParams({ key: state.key }).toString(); });
$("signout").addEventListener("click", () => {
  // The token lives only in the fragment; dropping it signs this tab out.
  history.replaceState(null, "", location.pathname + location.search);
  location.reload();
});
$("pdf").addEventListener("click", (e) => downloadPdf(e.currentTarget));
$("close-panel").addEventListener("click", closePanel);
$("copy-link").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    $("copy-link").textContent = "Copied";
    setTimeout(() => { $("copy-link").textContent = "Copy link"; }, 1500);
  } catch {
    prompt("Copy this link:", location.href);
  }
});
$("version").addEventListener("change", async (e) => {
  state.version = e.target.value === "draft" ? null : Number(e.target.value);
  state.open = null;
  closePanel();
  await loadReport();
  writeHash();
});
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, select, textarea")) return;
  if (e.key === "Escape" && !$("panel").hidden) closePanel();
  if (e.key === "a" && !e.metaKey && !e.ctrlKey && !e.altKey) setAudit(!state.audit);
});
window.addEventListener("hashchange", () => {
  const hash = readHash();
  if (hash.key !== state.key) location.reload();
  else if (state.person && (hash.report || null) !== (state.rid || null)) location.reload();
  else if (state.person && !state.rid && (hash.tab === "inbox" ? "inbox" : "reports") !== state.tab) location.reload();
});

try { state.audit = localStorage.getItem("bevis.audit") === "1"; } catch { state.audit = false; }
$("audit").setAttribute("aria-pressed", String(state.audit));
load();
