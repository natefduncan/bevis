// A person's reports and inbox (SPEC §7.2).

import { $, api, fill, h, remembered, state } from "./core.js";
import { notice } from "./report.js";

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

export const when = (stamp) => stamp
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

export async function showIndex(tab) {
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
  const remember = h("label", { class: "remember" },
    h("input", { type: "checkbox", checked: remembered("token") === state.key,
      onchange: (e) => remembered("token", e.target.checked ? state.key : "") }),
    " Remember me on this device",
    h("span", { class: "meta", text: " (keeps this token in the browser until you sign out)" }));
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
    add, remember));
}
