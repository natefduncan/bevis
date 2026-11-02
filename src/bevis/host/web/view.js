// The Bevis web view (SPEC §4): a report rendered from its JSON, with an
// evidence panel behind every claim and chart mark.
//
// The share link's secret (or a person's token, SPEC §7.2) lives in the URL
// fragment (#key=...), which the browser never sends to the server. Everything from the report or its data
// is inserted with textContent; the only HTML inserted is the server's
// sanitized rendering of text blocks.

import { $, downloadPdf, remembered, state, writeHash } from "./core.js";
import { load, loadReport, renderReport } from "./report.js";
import { closePanel } from "./panel.js";
import { openChallengeList } from "./challenges.js";
import { openSuggestionList } from "./suggestions.js";
import { openShare } from "./share.js";
import { openPublish } from "./publish.js";
import { saveAll, startEditing, stopEditing } from "./editor.js";

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
$("publish").addEventListener("click", openPublish);
$("edit-all").addEventListener("click", startEditing);
$("edit-save").addEventListener("click", saveAll);
$("edit-done").addEventListener("click", () => stopEditing());
$("suggestions").addEventListener("click", () => openSuggestionList());
$("home").addEventListener("click", () => { location.hash = new URLSearchParams({ key: state.key }).toString(); });
$("signout").addEventListener("click", () => {
  remembered("token", ""); // and the copy kept for "Remember me"
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
  if (e.target.closest?.("input, select, textarea") || e.target.isContentEditable) return;
  if (e.key === "Escape" && !$("panel").hidden) closePanel();
  if (e.key === "a" && !e.metaKey && !e.ctrlKey && !e.altKey) setAudit(!state.audit);
});
// The view writes its own state to the hash with replaceState, which fires no event, so any hashchange
// is a link followed or a hash edited: open what it names.
window.addEventListener("hashchange", () => location.reload());

try { state.audit = localStorage.getItem("bevis.audit") === "1"; } catch { state.audit = false; }
$("audit").setAttribute("aria-pressed", String(state.audit));
load();
