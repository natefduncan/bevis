// The Bevis web view (SPEC §4): a report rendered from its JSON, with an
// evidence panel behind every claim and chart mark.
//
// The share link's secret lives in the URL fragment (#key=...), which the
// browser never sends to the server. Everything from the report or its data
// is inserted with textContent; the only HTML inserted is the server's
// sanitized rendering of text blocks.

const PAGE_SIZE = 50;
const FULL_TABLE_ROWS = 25;
const STATUS_LABEL = { ok: "verified", unverified: "not verified", warning: "warning", error: "failed" };
const TIER_LABEL = { direct: "cell value", computed: "computed", asserted: "asserted" };

const state = {
  key: null,
  version: null, // null: whatever the link opens (the draft, for links that see it)
  report: null,
  evidence: new Map(), // "id@version" -> {evidence, row_keys, ...}
  audit: false,
  open: null, // {kind: "claim", id} | {kind: "evidence", id, version, row}
};

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
  };
}

function writeHash() {
  const params = new URLSearchParams();
  params.set("key", state.key);
  if (state.version !== null) params.set("v", String(state.version));
  const o = state.open;
  if (o?.kind === "claim") params.set("c", o.id);
  if (o?.kind === "evidence") {
    params.set("e", `${o.id}@${o.version}`);
    if (o.row !== null && o.row !== undefined) params.set("row", JSON.stringify(o.row));
  }
  history.replaceState(null, "", `#${params}`);
}

async function api(path) {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${state.key}` } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(body?.error?.message || `request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}

async function evidence(id, version) {
  const k = `${id}@${version}`;
  if (!state.evidence.has(k)) {
    state.evidence.set(k, api(`/api/reports/${state.report.id}/evidence/${encodeURIComponent(id)}?version=${version}`));
  }
  return state.evidence.get(k);
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

async function load() {
  const hash = readHash();
  state.key = hash.key;
  if (!state.key) {
    fill($("report"),
      h("div", { class: "notice" },
        h("p", { text: "This page opens a report from a share link." }),
        h("p", { text: "Ask the report's owner for a link. It looks like /view#key=bvl_…" })),
    );
    return;
  }
  state.version = hash.v === null || hash.v === "draft" ? null : Number(hash.v);
  await loadReport();
  if (hash.c && claimById(hash.c)) openClaim(hash.c, { scroll: true });
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
    if (!list.length) throw new Error("This link does not open any report.");
    const rid = list[0].id;
    const q = new URLSearchParams({ render: "true" });
    if (state.version !== null) q.set("version", String(state.version));
    state.report = await api(`/api/reports/${rid}?${q}`);
  } catch (err) {
    fill(report, h("div", { class: "notice error" },
      h("p", { text: err.status === 401 ? "This link is invalid, expired, or revoked." : err.message })));
    return;
  }
  state.evidence.clear();
  renderHeader();
  await renderReport();
}

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
    fill(meta, `Draft, revision ${r.revision}`, summary);
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
        h("figcaption", {}, block.caption || "", evidenceChips(block.evidence)));
      charts.push([el.querySelector(".vega"), block]);
    } else {
      el = h("p", { class: "notice", text: `Unsupported block type: ${block.type}` });
    }
    el.id = `blk-${block.id}`;
    el.dataset.block = block.id;
    root.append(el);
  });
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
  }
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
  try {
    await inlineData(spec, refs);
  } catch (err) {
    el.replaceWith(h("p", { class: "chart-note", text: err.message === "withheld"
      ? "Chart not drawn: its data is not disclosed at this link's level."
      : `Chart not drawn: ${err.message}` }));
    return;
  }
  stripLinks(spec);
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
  $("panel").hidden = true;
  document.querySelector(".layout").classList.remove("with-panel");
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  state.open = null;
  writeHash();
}

async function openClaim(id, { scroll = false } = {}) {
  const claim = claimById(id);
  if (!claim) return;
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
  state.open = { kind: "evidence", id, version, row };
  writeHash();
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  showPanel(`Evidence ${id} v${version}`);
  const section = h("section", {});
  fill($("panel-body"), section);
  await renderEvidence(section, id, version, [], row);
}

async function renderEvidence(section, id, version, cells, focusRow) {
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
    resultTable(doc, res.row_keys, cells, focusRow),
  );
  section.querySelector("tr.focus")?.scrollIntoView({ block: "nearest" });
}

function resultTable(doc, rowKeys, cells, focusRow) {
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
          const cls = [numeric.has(name) ? "num" : "", v === null ? "null" : "", citedCols?.has(name) ? "cited-cell" : ""];
          return h("td", { class: cls.join(" ").trim() || null, text: v === null ? "null" : String(v) });
        }));
    }));
    const notes = [];
    if (withheld) notes.push(`${plural(withheld, "row")} not disclosed at this level`);
    if (doc.disclosure?.withheld_columns?.length) notes.push(`not disclosed: ${doc.disclosure.withheld_columns.join(", ")}`);
    fill(wrap,
      h("div", { class: "table-tools" },
        h("span", { text: [`${indexes.length.toLocaleString()} of ${doc.row_count.toLocaleString()} rows`, ...notes].join(" · ") }),
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
});

try { state.audit = localStorage.getItem("bevis.audit") === "1"; } catch { state.audit = false; }
$("audit").setAttribute("aria-pressed", String(state.audit));
load();
