// Sharing (admin): links and members.

import { $, ROLES, api, fill, h, state, writeHash } from "./core.js";
import { showPanel } from "./panel.js";

// -- sharing (admin) ------------------------------------------------------------------------

export async function openShare() {
  state.compose = null;
  state.open = null;
  writeHash();
  showPanel("Share");
  const rid = state.report.id;
  const status = h("p", { class: "message", role: "alert" });
  const created = h("div", {});
  const list = h("div", {});

  const drawList = async () => {
    const links = await api(`/api/reports/${rid}/links`);
    fill(list, h("div", { class: "table-wrap" }, h("table", { class: "rows links" },
      h("thead", {}, h("tr", {}, ["Name", "Role", "Data", "Sees", "Expires", ""].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, links.map((l) => h("tr", { class: l.revoked_at ? "revoked" : null },
        h("td", { text: l.name }), h("td", { text: l.role }), h("td", { text: l.disclosure }),
        h("td", { text: l.scope === "draft" ? "draft" : l.scope === "latest" ? "latest version" : `version ${l.scope}` }),
        h("td", { text: l.revoked_at ? "revoked" : l.expires_at ? l.expires_at.slice(0, 10) : "never" }),
        h("td", {}, l.current ? h("span", { class: "meta", text: "yours" }) : l.revoked_at ? null : h("button", { class: "quiet", type: "button", text: "Revoke",
          onclick: async () => {
            if (!confirm(`Revoke the link for ${l.name}? It stops working at once.`)) return;
            try { await api(`/api/reports/${rid}/links/${l.id}`, { method: "DELETE" }); await drawList(); }
            catch (err) { status.textContent = err.message; }
          } }))))))));
  };

  const members = h("div", {});
  const drawMembers = async () => {
    const rows = await api(`/api/reports/${rid}/members`);
    if (!rows.length) {
      fill(members, h("p", { class: "meta", text: "No one has joined with a person token yet." }));
      return;
    }
    const scopeText = (s) => (s === "draft" ? "draft" : s === "latest" ? "latest version" : `version ${s}`);
    fill(members, h("div", { class: "table-wrap" }, h("table", { class: "rows members" },
      h("thead", {}, h("tr", {}, ["Person", "Role", "Data", "Sees", "Joined", ""].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, rows.map((m) => h("tr", {},
        h("td", {}, m.name, m.shared_with ? h("span", { class: "meta", text: ` (link shared with ${m.shared_with} other${m.shared_with === 1 ? "" : "s"})` }) : null),
        h("td", { text: m.role }), h("td", { text: m.disclosure }), h("td", { text: scopeText(m.scope) }),
        h("td", { text: (m.since || "").slice(0, 10) }),
        h("td", {}, m.current ? h("span", { class: "meta", text: "you" }) : h("button", { class: "quiet", type: "button", text: "Remove",
          onclick: async () => {
            const also = m.shared_with ? ` The link is shared, so ${m.shared_with} other${m.shared_with === 1 ? "" : "s"} will lose that link too.` : "";
            if (!confirm(`Remove ${m.name} from this report? Their links are revoked and they cannot rejoin with them.${also}`)) return;
            try {
              const res = await api(`/api/reports/${rid}/members/${m.person_id}`, { method: "DELETE" });
              status.textContent = res.also_removed.length ? `Removed ${m.name}; ${res.also_removed.length} other member(s) lost that link too.` : `Removed ${m.name}.`;
              await Promise.all([drawMembers(), drawList()]);
            } catch (err) { status.textContent = err.message; }
          } }))))))));
  };

  const versions = state.report.versions.map((v) => v.number);
  const form = h("form", { class: "compose share-form" },
    h("label", { class: "field" }, "Whose link is it?", h("input", { name: "name", required: true, placeholder: "e.g. Finance team" })),
    h("div", { class: "row" },
      h("label", { class: "field" }, "Role", h("select", { name: "role" },
        [["reader", "Reader"], ["commenter", "Commenter (can challenge)"], ["suggester", "Suggester (sees draft)"],
          ["editor", "Editor (sees draft)"], ["admin", "Admin"]].map(([v, t]) => h("option", { value: v, text: t })))),
      h("label", { class: "field" }, "Data", h("select", { name: "disclosure" },
        [["full", "All rows"], ["cited", "Cited rows only"], ["metadata", "No data values"]].map(([v, t]) => h("option", { value: v, text: t })))),
      h("label", { class: "field" }, "Sees", h("select", { name: "scope" },
        h("option", { value: "latest", text: "Latest version" }),
        versions.map((n) => h("option", { value: String(n), text: `Version ${n} only` })))),
      h("label", { class: "field" }, "Expires", h("input", { name: "expires", type: "date" }))),
    status,
    h("div", { class: "actions" }, h("button", { class: "toggle primary", type: "submit", text: "Create link" })));
  const roleSelect = form.querySelector('select[name="role"]');
  roleSelect.addEventListener("change", () => {
    // Suggesters and up work on the draft, so a version scope does not apply.
    form.querySelector('select[name="scope"]').disabled = ROLES.indexOf(roleSelect.value) >= ROLES.indexOf("suggester");
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const expires = f.get("expires");
    try {
      const link = await api(`/api/reports/${rid}/links`, { method: "POST", body: {
        name: f.get("name"), role: f.get("role"), disclosure: f.get("disclosure"),
        scope: f.get("scope") ?? "latest", expires_at: expires ? `${expires}T23:59:59Z` : undefined,
      } });
      const url = `${location.origin}/view#key=${encodeURIComponent(link.secret)}`;
      const input = h("input", { class: "secret", readonly: true, value: url, "aria-label": "New link" });
      fill(created, h("div", { class: "created" },
        h("p", {}, h("strong", { text: `Link for ${f.get("name")}` }), " — copy it now; it is not shown again."),
        input,
        h("button", { class: "toggle", type: "button", text: "Copy", onclick: async (ev) => {
          try { await navigator.clipboard.writeText(url); ev.target.textContent = "Copied"; }
          catch { input.select(); }
        } })));
      form.reset();
      status.textContent = "";
      await drawList();
    } catch (err) {
      status.textContent = err.message;
    }
  });
  fill($("panel-body"), h("p", { class: "meta", text: "Anyone holding a link can open this report with that link's role. Revoke a link to cut it off." }),
    form, created, h("h3", { text: "Links" }), list, h("h3", { text: "Members" }), members);
  try { await Promise.all([drawList(), drawMembers()]); } catch (err) { status.textContent = err.message; }
}
