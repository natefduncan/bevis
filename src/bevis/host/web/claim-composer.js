// Making and editing a claim: picking cells and numbers.

import { cellExpression, derivePins, detectNumbers, isComplete, nextClaimId } from "./claims.js";
import { $, STATUS_LABEL, evidence, fill, h, shortValue, state } from "./core.js";
import { load } from "./report.js";
import { chipEl, fieldText, isDirty, paintEditor, schedulePreview, stash } from "./editor.js";
import { blockRoot, fieldOf, latestVersions, spanInner, toolButton, unlinkClaim } from "./claim-tools.js";

// -- making and editing a claim ---------------------------------------------------------------

export function startClaim(block, ed) {
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

export function editClaim(block, ed, id) {
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

export function drawComposer(block, ed) {
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

export function updateComposerResults(ed) {
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
