// Challenges (SPEC §8): listing, filing, answering.

import { $, CHALLENGE_CLASS, api, atLeast, canFile, claimById, fill, h, plural, remembered, selKey, state, writeHash } from "./core.js";
import { loadChallenges, renderHeader, renderReport, sameTarget } from "./report.js";
import { closePanel, openClaim, renderEvidence, showPanel } from "./panel.js";
import { openSuggestion } from "./suggestions.js";
import { when } from "./people.js";

// -- challenges (SPEC §8) -----------------------------------------------------------------

function targetLabel(target) {
  if (target.claim) {
    const span = document.querySelector(`span.claim[data-claim="${CSS.escape(target.claim)}"]`);
    return span ? `Claim ${target.claim}: “${span.textContent}”` : `Claim ${target.claim}`;
  }
  const block = state.report.blocks.find((b) => b.id === target.block);
  return block?.caption ? `Chart: ${block.caption}` : `Block ${target.block}`;
}

export function challengeSection(target, noun) {
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

export function toggleCell(td, cell) {
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

export async function openCompose(target) {
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

export async function openChallenge(id) {
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

export function openChallengeList(target = null) {
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
