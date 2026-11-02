// Review of edits near a claim (SPEC §6.2).

import { $, STATUS_LABEL, api, atLeast, fill, h, state, writeHash } from "./core.js";
import { loadReport, notice } from "./report.js";
import { closePanel, openClaim, showPanel } from "./panel.js";
import { when } from "./people.js";
import { blockFindings, diffView } from "./editor.js";

// -- review of edits near a claim (SPEC §6.2) ----------------------------------------------

export const reviewFlag = (id) => (state.report?.kind === "draft" ? state.report.block_state?.[id]?.needs_review ?? null : null);
export const flaggedBlocks = () => state.report.blocks.map((b) => b.id).filter((id) => reviewFlag(id));

function isMine(author) {
  if (!author || !state.me) return false;
  if (author.person_id || state.me.person_id) return author.person_id === state.me.person_id;
  return author.name === state.me.name && author.kind === state.me.author_kind;
}

// An admin may clear any flag; an editor only for an edit someone else made (SPEC §6.2).
export const canClear = (flag) => atLeast("admin") || (atLeast("editor") && !isMine(flag.edited_by));

export function reviewRow(block, flag) {
  const by = flag.edited_by;
  return h("div", { class: "review-row" },
    h("span", { class: "chip error", text: "Needs review" }),
    h("span", { class: "meta", text: `Edited near a claim by ${isMine(by) ? "you" : by.name} · ${when(flag.edited_at)}` }),
    h("button", { class: "quiet", type: "button", text: canClear(flag) ? "Review" : "See what changed",
      "aria-label": `Review block ${block.id}`, onclick: () => openReview(block.id) }));
}

export async function openReview(bid, { notice = null } = {}) {
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
    canClear(flag) ? [
      mine ? h("p", { class: "meta", text: "You made this edit. You can mark it reviewed yourself; the log records it as a self-review." }) : null,
      h("div", { class: "actions" },
        h("button", { class: "toggle primary", type: "button", text: "Mark reviewed", onclick: (e) => clear(e.currentTarget) }))]
      : h("p", { class: "meta", text: mine && atLeast("editor")
        ? "You made this edit, so another editor or an admin has to mark it reviewed before the report can be published."
        : "An editor who did not make this edit, or an admin, has to mark it reviewed before the report can be published." }));
}
