// Editing prose in a block (SPEC §6.2): the field, preview, save, and conflicts.

import { diffWords, merge3, splitSpans } from "./textdiff.js";
import { $, api, atLeast, fill, h, plural, remembered, state } from "./core.js";
import { loadReport, renderReport } from "./report.js";
import { buildOps, captureSelection, dropChip, edited, moveChip, paintClaimTools, selectClaim, unfinishedClaim } from "./claim-tools.js";
import { updateComposerResults } from "./claim-composer.js";

// -- editing prose (SPEC §6.2) ------------------------------------------------------------

// Editors and admins change the draft; suggesters use the same editor to propose a change.
export const suggesting = () => !atLeast("editor");
export const canEditBlock = (block) => state.report?.kind === "draft" && atLeast("suggester")
  && (block.type === "text" || block.type === "heading");

const blockRevision = (id) => state.report.block_state?.[id]?.revision ?? 0;

function stashKey(id) {
  return `draft.${state.report.id}.${id}`;
}

export function stash(id, ed) {
  // Unsaved text stays in the browser until a save succeeds.
  remembered(stashKey(id), ed ? JSON.stringify({ base: ed.base, text: ed.text,
    pending: [...ed.pending], unlinked: [...ed.unlinked] }) : "");
}

export function upsertOp(block, text) {
  const out = { id: block.id, type: block.type, text };
  if (block.type === "heading") out.level = block.level;
  return { op: "upsert_block", block: out };
}

function createEditor(block) {
  const ed = { text: block.text, base: blockRevision(block.id), original: block.text, findings: null,
    conflict: null, error: null, seq: 0, restored: false, leftover: [],
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
  return ed;
}

// -- editing the whole report ----------------------------------------------------------------
//
// One mode opens every text block and heading at once, and one Save sends every change as a single
// edit (or, for a suggester, a single suggestion). There is no per-block save.

export function ensureEditors() {
  const editable = state.report.blocks.filter((b, i) => canEditBlock(b)
    && !(i === 0 && b.type === "heading" && b.level === 1 && b.text.trim() === state.report.title.trim())); // the title is in the bar
  for (const block of editable) if (!state.editing.has(block.id)) createEditor(block);
  for (const id of [...state.editing.keys()]) if (!editable.some((b) => b.id === id)) state.editing.delete(id);
}

export function startEditing() {
  state.editAll = true;
  state.editNote = "";
  state.editError = "";
  ensureEditors();
  paintEditBar();
  renderReport().then(() => {
    $("report").querySelector(".edit-field")?.focus();
    for (const ed of state.editing.values()) if (isDirty(ed)) schedulePreview(ed.block, ed, 0);
  });
}

export function stopEditing({ ask = true } = {}) {
  const dirty = dirtyEditors();
  if (ask && dirty.length && !confirm(`Discard your unsaved changes to ${plural(dirty.length, "block")}?`)) return;
  for (const ed of state.editing.values()) { clearTimeout(ed.timer); stash(ed.block.id, null); }
  state.editing.clear();
  state.editAll = false;
  state.editNote = "";
  state.editError = "";
  paintEditBar();
  renderReport();
}

export const dirtyEditors = () => [...state.editing.values()].filter(isDirty);

// Why the whole save cannot go yet, or null.
function blocker() {
  const dirty = dirtyEditors();
  for (const ed of dirty) {
    if (ed.conflict) return `Block ${ed.block.id} changed while you were editing it: choose what to keep.`;
  }
  for (const ed of dirty) {
    if (!ed.text.trim()) return `Block ${ed.block.id} is empty. Put its text back, or cancel that change.`;
    const unfinished = unfinishedClaim(ed.block, ed);
    if (unfinished) return `Block ${ed.block.id}: ${unfinished}`;
  }
  return null;
}

export function paintEditBar() {
  $("edit-bar").hidden = !state.editAll;
  $("edit-all").hidden = state.editAll || !(state.report?.kind === "draft" && atLeast("suggester"));
  if (!state.editAll) return;
  const dirty = dirtyEditors();
  const why = blocker();
  const suggest = suggesting();
  $("edit-title").textContent = suggest ? "Suggesting edits" : "Editing the whole report";
  $("edit-summary").textContent = state.editError || why
    || (state.editNote && !dirty.length ? state.editNote
      : dirty.length ? `${plural(dirty.length, "block")} changed` : "No changes yet");
  $("edit-summary").classList.toggle("message", Boolean(state.editError || why));
  $("edit-message").hidden = !suggest;
  $("edit-hint").textContent = richSupported
    ? "Claims are locked: click one for its options, or select text to make a claim. An edit near a claim needs an admin's review before publishing."
    : "Claims show as [text]{#cN}: that text is what the validator checks. An edit near a claim needs an admin's review before publishing.";
  $("edit-save").textContent = suggest ? "Suggest these edits" : "Save";
  $("edit-save").disabled = state.saving || !dirty.length || Boolean(why);
}

export function blockFindings(findings, id, extra = []) {
  const claims = new Set([...state.report.claims.filter((c) => c.block_id === id).map((c) => c.id), ...extra]);
  return findings.filter((f) => f.block === id || (f.claim && claims.has(f.claim)));
}

export function schedulePreview(block, ed, delay = 400) {
  clearTimeout(ed.timer);
  ed.timer = setTimeout(() => preview(block, ed), delay);
}

export const isDirty = (ed) => ed.text !== ed.original || ed.pending.size > 0 || ed.unlinked.size > 0;

export async function preview(block, ed) {
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

// While blocks are open in the editor, ask now and then whether someone else changed any of them, so a
// notice appears without waiting for the next keystroke (SPEC §6.2). Nothing is written: the check is a
// validate call with no ops, which only compares each block's revision with the one we started from.
// A block with no changes of yours simply takes the new text; one with changes gets a notice.
const POLL_MS = 10000;

async function pollEditors() {
  if (document.hidden || !state.report || state.report.kind !== "draft" || state.saving || !state.editing.size) return;
  const eds = [...state.editing.values()];
  const base = Object.fromEntries(eds.map((ed) => [ed.block.id, ed.base]));
  try {
    await api(`/api/reports/${state.report.id}/validate`, { method: "POST", body: { ops: [], base_block_revisions: base } });
  } catch (err) {
    if (err.code !== "block_conflict" || state.saving) return;
    for (const c of err.details.blocks) {
      const ed = state.editing.get(c.id);
      if (!ed || ed.conflict || ed.base !== c.base_revision) continue;
      ed.conflict = c;
      if (!isDirty(ed) && c.block) applyText(ed.block, ed, c.block.text, c.revision, c.claims);
      else paintEditor(ed.block, ed);
    }
  }
}
setInterval(pollEditors, POLL_MS);

function errorText(err) {
  const first = Array.isArray(err.details) ? err.details[0] : null;
  return first?.message ? `${err.message}: ${first.message}` : err.message;
}

export function paintEditor(block, ed) {
  const root = $("report").querySelector(`#blk-${CSS.escape(block.id)}`);
  if (!root) return;
  const unfinished = unfinishedClaim(block, ed);
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
  paintEditBar();
}

export function diffView(from, to) {
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
      h("button", { class: "toggle", type: "button", text: "Discard my text", onclick: () => {
        clearTimeout(ed.timer); state.editing.delete(block.id); stash(block.id, null); renderReport().then(paintEditBar);
      } }));
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
      h("button", { class: "toggle", type: "button", text: "Keep mine", title: "Keep my text; saving puts it over the current one",
        onclick: () => { ed.base = c.revision; ed.conflict = null; stash(block.id, ed); paintEditor(block, ed); schedulePreview(block, ed, 0); } }),
      h("button", { class: "toggle", type: "button", text: "Use current text", title: "Replace my text with the current one",
        onclick: () => applyText(block, ed, c.block.text, c.revision, c.claims) })));
}

export async function saveAll() {
  const dirty = dirtyEditors();
  if (!dirty.length || blocker() || state.saving) return;
  state.saving = true;
  state.editError = "";
  paintEditBar();
  const suggest = suggesting();
  const message = $("edit-message").value.trim();
  try {
    const out = await api(`/api/reports/${state.report.id}/${suggest ? "suggestions" : "edits"}`, { method: "POST", body: {
      ops: dirty.flatMap((ed) => buildOps(ed.block, ed)),
      base_block_revisions: Object.fromEntries(dirty.map((ed) => [ed.block.id, ed.base])),
      ...(suggest ? { message } : {}) } });
    const flagged = Object.values(out.blocks || {}).filter((b) => b.needs_review).length;
    for (const ed of state.editing.values()) { clearTimeout(ed.timer); stash(ed.block.id, null); }
    state.editing.clear(); // reopened from the saved report, at its new revisions
    state.editNote = suggest ? `Suggested ${plural(dirty.length, "block")}. An editor will review them before they change the report.`
      : `Saved ${plural(dirty.length, "block")}.${flagged ? ` ${plural(flagged, "block")} near a claim ${flagged === 1 ? "needs" : "need"} an admin's review before the report is published.` : ""}`;
    $("edit-message").value = "";
    const y = window.scrollY;
    state.saving = false;
    await loadReport();
    window.scrollTo(0, y);
  } catch (err) {
    state.saving = false;
    if (err.code === "block_conflict") {
      for (const c of err.details.blocks) { const ed = state.editing.get(c.id); if (ed) ed.conflict = c; }
      for (const ed of state.editing.values()) paintEditor(ed.block, ed);
    } else {
      state.editError = errorText(err);
    }
    paintEditBar();
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

export function chipEl(span, ed) {
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

export function fieldText(field) {
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

export function blockEditor(block) {
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
      saveAll();
    }
  });
  const el = h("div", { class: "block-editor" },
    field,
    rich ? h("div", { class: "claim-tools" }) : null,
    h("div", { class: "claim-composer" }),
    h("div", { class: "edit-status", role: "status", "aria-live": "polite" }),
    h("div", { class: "edit-conflict" }),
    h("div", { class: "edit-leftover" }),
    h("span", {}));
  queueMicrotask(() => paintEditor(block, ed));
  return el;
}
