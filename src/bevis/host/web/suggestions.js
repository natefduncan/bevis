// Suggestions (SPEC §6): listing, previewing, accepting.

import { $, api, atLeast, fill, h, plural, state, writeHash } from "./core.js";
import { loadReport } from "./report.js";
import { showPanel } from "./panel.js";
import { when } from "./people.js";
import { diffView, preview } from "./editor.js";

// -- suggestions (SPEC §6) ------------------------------------------------------------------

const SUGGESTION_CLASS = { open: "unverified", accepted: "ok", rejected: "error" };

function changedBlocks(s) {
  // The blocks an upsert in the suggestion names, for the list; the detail shows the real diff.
  return [...new Set(s.ops.filter((op) => op.op === "upsert_block" && op.block?.id).map((op) => op.block.id))];
}

export async function openSuggestionList() {
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

export async function openSuggestion(id) {
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
