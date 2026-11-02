// Claims in the editor: chips, the bar under the text, moving, unlinking, deleting.

import { claimForOps, isComplete, moveOverWord } from "./claims.js";
import { findSpans } from "./textdiff.js";
import { $, fill, h, state } from "./core.js";
import { openClaim } from "./panel.js";
import { chipEl, fieldText, paintEditor, schedulePreview, stash, upsertOp } from "./editor.js";
import { drawComposer, editClaim, editableClaim, startClaim } from "./claim-composer.js";

// -- claims in the editor (SPEC §6.2) ----------------------------------------------------------
//
// Select text to make it a claim, click a chip to edit, unlink, or delete its claim, and drag a chip
// (or press Alt+Arrow on it) to move it. Each of these is part of one edit: the block's text plus
// link_claim and unlink_claim ops, so a claim is never dropped between a removal and an addition.
// The validator's reply, shown live, is the authority on whether a claim holds.

// The chip being dragged, and the editor it came from: a drop may land in another block's field.
export const drag = { chip: null, ed: null };

export const latestVersions = () => Object.fromEntries((state.report.evidence || []).map((e) => [e.id, e.latest_version]));
export const blockRoot = (block) => $("report").querySelector(`#blk-${CSS.escape(block.id)}`);
export const fieldOf = (block) => blockRoot(block)?.querySelector(".edit-field");
export const spanInner = (src) => src.slice(1, src.lastIndexOf("]{#"));
const shorten = (text, n = 60) => (text.length > n ? `${text.slice(0, n)}…` : text);
export const toolButton = (text, onclick, cls = "") => h("button", { class: `quiet ${cls}`.trim(), type: "button", text, onclick });

export function buildOps(block, ed, { lenient = false } = {}) {
  const present = new Set(findSpans(ed.text, ed.claimIds).map((s) => s.id));
  const latest = latestVersions();
  return [
    upsertOp(block, ed.text),
    ...[...ed.pending].filter(([id]) => present.has(id))
      .map(([, def]) => ({ op: "link_claim", claim: claimForOps(def, block.id, latest, { lenient }) })),
    ...[...ed.unlinked].map((id) => ({ op: "unlink_claim", id })),
  ];
}

export function edited(block, ed, field) {
  ed.text = fieldText(field);
  ed.restored = false;
  ed.sel = null;
  stash(block.id, ed);
  paintEditor(block, ed);
  schedulePreview(block, ed);
}

export function unfinishedClaim(block, ed) {
  if (ed.composer) return `Finish claim ${ed.composer.id} (Done) before saving.`;
  const present = new Set(findSpans(ed.text, ed.claimIds).map((s) => s.id));
  for (const [id, def] of ed.pending) {
    if (present.has(id) && !def.numbers.every(isComplete)) return `Claim ${id} has a number that is not filled in yet.`;
  }
  return null;
}

// -- the bar under the text -----------------------------------------------------------------

export function paintClaimTools(block, ed) {
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
      toolButton("Delete claim and text", () => unlinkClaim(block, ed, id, false)),
      moveToPicker(block, ed, id));
  } else if (ed.sel) {
    fill(bar, h("span", { class: "meta", text: `“${shorten(ed.sel.text)}”` }),
      toolButton("Make this a claim", () => startClaim(block, ed), "primary-tool"));
  } else {
    fill(bar);
  }
}

// Other blocks that can hold a claim chip: text blocks, open in the editor as rich fields.
function moveToPicker(block, ed, id) {
  const targets = [...state.editing.values()].filter((o) => o !== ed && fieldOf(o.block)?.classList.contains("rich-field"));
  if (!targets.length) return null;
  const plain = (text) => shorten(text.replace(/\[([^\]]*)\]\{#c\d+\}/g, "$1").replace(/\s+/g, " ").trim(), 40);
  const select = h("select", { "aria-label": "Move claim to block",
    onchange: (e) => {
      const to = state.editing.get(e.target.value);
      const chip = fieldOf(block)?.querySelector(`.claim-chip[data-claim="${id}"]`);
      if (to && chip) transferClaim(ed, to, chip);
    } },
  h("option", { value: "", text: "Move to block…" }),
  targets.map((o) => h("option", { value: o.block.id, text: `${o.block.id}: ${plain(o.text)}` })));
  return select;
}

export function selectClaim(block, ed, id) {
  ed.selected = id;
  paintClaimTools(block, ed);
}

export function captureSelection(block, ed, field) {
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

export function dropChip(block, ed, field, e) {
  const { chip, ed: from } = drag;
  drag.chip = null;
  drag.ed = null;
  const range = caretRangeAt(e.clientX, e.clientY);
  if (!chip || !from || !range || !field.contains(range.startContainer)) return;
  const at = range.startContainer;
  if (chip.contains(at) || at.parentElement?.closest(".claim-chip") || at.nodeType === 1 && at.closest?.(".claim-chip")) return;
  range.collapse(true);
  if (from === ed) {
    range.insertNode(chip); // moves it: the claim id and its source go with it
    field.normalize();
    edited(block, ed, field);
  } else {
    transferClaim(from, ed, chip, range);
  }
}

// Move a claim to another block. It is one edit: the first block's text without the span, the second
// block's text with it, and the claim now pointing at the second block (a link_claim that replaces it).
// With no `range` the chip goes at the end of the second block.
export function transferClaim(from, to, chip, range = null) {
  const id = chip.dataset.claim;
  const stored = state.report.claims.find((c) => c.id === id);
  const def = from.pending.get(id) ?? (stored ? editableClaim(stored) : null);
  const fromField = fieldOf(from.block);
  const toField = fieldOf(to.block);
  if (!def || !fromField || !toField) return;
  const moved = chipEl({ id, source: chip.dataset.src, inner: spanInner(chip.dataset.src) }, to);
  if (range) {
    range.insertNode(moved);
  } else {
    const last = toField.lastChild;
    if (last?.nodeType === 3 && last.nodeValue && !/\s$/.test(last.nodeValue)) toField.append(document.createTextNode(" "));
    toField.append(moved);
  }
  chip.remove();
  fromField.normalize();
  toField.normalize();
  from.pending.delete(id);
  from.claimIds.delete(id);
  to.pending.set(id, def);
  to.claimIds.add(id);
  from.partners.add(to.block.id);
  to.partners.add(from.block.id);
  from.selected = null;
  to.selected = id;
  if (from.composer?.id === id) { from.composer = null; drawComposer(from.block, from); }
  edited(from.block, from, fromField);
  edited(to.block, to, toField);
}

export function moveChip(block, ed, chip, direction) {
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

export function unlinkClaim(block, ed, id, keepText) {
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
