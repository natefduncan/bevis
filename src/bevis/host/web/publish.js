// Publishing (editor): the panel that makes a version.

import { $, api, fill, h, plural, state, writeHash } from "./core.js";
import { loadReport } from "./report.js";
import { closePanel, showPanel } from "./panel.js";
import { flaggedBlocks, openReview } from "./review.js";
import { edited } from "./claim-tools.js";

// -- publishing (editor) ---------------------------------------------------------------

export async function openPublish() {
  state.compose = null;
  state.open = null;
  writeHash();
  showPanel("Publish");
  const rid = state.report.id;
  const rev = state.report.revision;
  const counts = state.report.validation.counts;
  const flagged = flaggedBlocks();
  const status = h("p", { class: "message", role: "alert" });
  const reason = h("textarea", { name: "reason", rows: "3", maxlength: "500", placeholder: "Why publish with these errors?" });
  const button = h("button", { class: "toggle primary", type: "button", text: "Publish" });
  const blocked = h("div", {});

  const findings = () => (state.report.validation.findings || []).filter((f) => f.severity === "error");
  const needsReason = () => counts.errors > 0;
  const refresh = () => {
    fill(blocked,
      flagged.length ? h("div", { class: "notice" },
        h("p", {}, `${plural(flagged.length, "block")} must be reviewed first. Someone other than the author, or an admin, clears each one.`),
        h("ul", {}, flagged.map((id) => h("li", {}, h("button", { class: "quiet", type: "button", text: `Review ${id}`,
          onclick: () => openReview(id) }))))) : null);
    button.disabled = flagged.length > 0 || (needsReason() && !reason.value.trim());
  };
  reason.addEventListener("input", refresh);

  button.addEventListener("click", async () => {
    status.textContent = "";
    button.disabled = true;
    try {
      const body = { base_revision: rev };
      if (needsReason()) body.override_reason = reason.value.trim();
      const out = await api(`/api/reports/${rid}/publish`, { method: "POST", body });
      state.version = out.version;
      state.open = null;
      closePanel();
      await loadReport();
      writeHash();
    } catch (err) {
      status.textContent = err.code === "stale_revision"
        ? "The draft changed since you opened this. Reload the draft and try again."
        : err.message;
      if (err.code === "needs_review" || err.code === "stale_revision") {
        await loadReport();
        openPublish();
        return;
      }
      refresh();
    }
  });

  fill($("panel-body"),
    h("p", { class: "meta", text: `Publishes draft revision ${rev} as version ${(state.report.versions.at(-1)?.number ?? 0) + 1}. It cannot be edited afterwards.` }),
    h("p", {}, h("span", { class: `chip ${counts.errors ? "error" : counts.warnings ? "warning" : "ok"}`,
      text: counts.errors ? plural(counts.errors, "error") : counts.warnings ? plural(counts.warnings, "warning") : "no errors" })),
    counts.errors ? h("div", {},
      h("p", { class: "meta", text: "Errors do not block publishing, but publishing with them needs a reason, which is kept with the version." }),
      h("ul", {}, findings().slice(0, 8).map((f) => h("li", { text: f.message || f.code }))),
      counts.errors > 8 ? h("p", { class: "meta", text: `and ${counts.errors - 8} more` }) : null,
      h("label", { class: "field" }, "Reason", reason)) : null,
    blocked, status, h("div", { class: "actions" }, button));
  refresh();
}
