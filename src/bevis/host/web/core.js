// State, constants, and helpers shared by every part of the view.



export const PAGE_SIZE = 50;
export const FULL_TABLE_ROWS = 25;
export const STATUS_LABEL = { ok: "verified", unverified: "not verified", warning: "warning", error: "failed" };
export const TIER_LABEL = { direct: "cell value", computed: "computed", asserted: "asserted" };
export const ROLES = ["reader", "commenter", "suggester", "editor", "admin"];
export const CHALLENGE_CLASS = { open: "error", answered: "unverified", resolved: "ok", dismissed: "" };

export const state = {
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
  editAll: false, // the whole report is open for editing (SPEC §6.2)
  saving: false, // the whole-report save is in flight
  editNote: "", // what the last save did, shown in the edit bar
  editError: "", // why the last save failed
};

export const atLeast = (role) => ROLES.indexOf(state.role) >= ROLES.indexOf(role);
export const canDiscuss = () => atLeast("commenter");
export const canFile = () => canDiscuss() && state.report?.kind === "version";

export function remembered(name, value) {
  // Per-viewer conveniences only; the page works without storage.
  try {
    if (value === undefined) return localStorage.getItem(`bevis.${name}`) || "";
    localStorage.setItem(`bevis.${name}`, value);
  } catch { /* storage may be blocked */ }
  return value ?? "";
}

export const selKey = (c) => `${c.evidence}@${c.version}|${JSON.stringify(c.key)}|${c.column}`;

// -- helpers ------------------------------------------------------------------------

export function h(tag, attrs = {}, ...children) {
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

export const $ = (id) => document.getElementById(id);

// replaceChildren, but arrays are flattened and null/false are skipped, as in h().
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}

export function readHash() {
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

export function writeHash() {
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

export async function api(path, { method = "GET", body } = {}) {
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

export async function evidence(id, version) {
  const k = `${id}@${version}`;
  if (!state.evidence.has(k)) {
    state.evidence.set(k, api(`/api/reports/${state.report.id}/evidence/${encodeURIComponent(id)}?version=${version}`));
  }
  return state.evidence.get(k);
}

export function saveBlob(blob, filename) {
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

export function evidenceCsv(doc) {
  // Exactly what the table shows: withheld cells stay empty, nulls are empty, decimals keep their text.
  const withheld = new Set((doc.disclosure?.withheld_cells || []).map(([i, name]) => `${i}\u0000${name}`));
  const names = doc.columns.map((c) => c.name);
  const lines = [names.map(csvField).join(",")];
  doc.rows.forEach((row, i) => {
    lines.push(row.map((v, j) => (v === null || withheld.has(`${i}\u0000${names[j]}`) ? "" : csvField(String(v)))).join(","));
  });
  return `\ufeff${lines.join("\r\n")}\r\n`;
}

export async function downloadPdf(button) {
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

export function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

export function shortValue(text) {
  // Computed values carry up to 34 significant digits; show a readable prefix.
  if (text === null || text === undefined) return "—";
  const n = Number(text);
  if (!Number.isFinite(n)) return text;
  const short = Math.abs(n) >= 1e6 || Number.isInteger(n) ? n.toLocaleString() : n.toPrecision(6);
  return short.length < text.length ? `${short}…` : text;
}

export function sameKey(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

export function claimById(id) {
  return state.report.claims.find((c) => c.id === id);
}
