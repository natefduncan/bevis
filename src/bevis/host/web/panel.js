// The side panel: evidence for a claim or chart mark, and the rows behind it.

import { $, FULL_TABLE_ROWS, PAGE_SIZE, STATUS_LABEL, TIER_LABEL, canDiscuss, claimById, evidence, evidenceCsv, fill, h, plural, sameKey, saveBlob, selKey, shortValue, state, writeHash } from "./core.js";
import { challengeSection, toggleCell } from "./challenges.js";

// -- panel -------------------------------------------------------------------------------

export function showPanel(title) {
  $("panel").hidden = false;
  document.querySelector(".layout").classList.add("with-panel");
  $("panel-title").textContent = title;
  fill($("panel-body"), h("p", { class: "meta", text: "Loading…" }));
  $("panel").scrollTop = 0;
}

export function closePanel() {
  state.compose = null;
  $("panel").hidden = true;
  document.querySelector(".layout").classList.remove("with-panel");
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  state.open = null;
  writeHash();
}

export async function openClaim(id, { scroll = false } = {}) {
  const claim = claimById(id);
  if (!claim) return;
  state.compose = null;
  state.open = { kind: "claim", id };
  writeHash();
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  const span = document.querySelector(`span.claim[data-claim="${CSS.escape(id)}"]`);
  span?.classList.add("active");
  if (scroll) span?.scrollIntoView({ behavior: "smooth", block: "center" });

  showPanel(`Claim ${id}`);
  const body = [];
  body.push(h("p", { class: "quote", text: span ? `“${span.textContent}”` : "" }));
  body.push(h("p", {}, h("span", { class: `chip ${claim.status}`, text: STATUS_LABEL[claim.status] || claim.status })));
  const claimFindings = state.report.validation.findings.filter((f) => f.claim === id && f.number === undefined);
  for (const f of claimFindings) {
    body.push(h("p", { class: `message ${f.severity}`, text: f.message }));
  }
  if (claim.derivation) body.push(h("h3", { text: "Derivation" }), h("p", { text: claim.derivation }));
  if (canDiscuss()) body.push(challengeSection({ claim: id }, "claim"));

  if (claim.numbers.length) {
    body.push(h("h3", { text: "Numbers" }));
    body.push(h("ul", { class: "numbers" }, claim.numbers.map((n) => numberItem(n))));
  } else {
    body.push(h("p", { class: "meta", text: "This claim states no numbers, so nothing in it is checked against the data." }));
  }

  const cited = new Map(); // "id@v" -> [{key, column}]
  for (const n of claim.numbers) {
    for (const c of n.cells || []) {
      const k = `${c.evidence}@${c.version}`;
      if (!cited.has(k)) cited.set(k, []);
      cited.get(k).push(c);
    }
  }
  fill($("panel-body"), ...body);
  for (const pin of claim.evidence) {
    const section = h("section", { class: "evidence" });
    $("panel-body").append(section);
    await renderEvidence(section, pin.id, pin.version, cited.get(`${pin.id}@${pin.version}`) || [], null);
  }
}

function numberItem(n) {
  const item = h("li", {},
    h("div", { class: "num-head" },
      h("span", { class: "num-text", text: n.text }),
      h("span", { class: "tier", text: TIER_LABEL[n.tier] || n.tier }),
      h("span", { class: `chip ${n.status}`, text: STATUS_LABEL[n.status] || n.status })));
  if (n.tier === "computed") {
    item.append(h("code", { class: "expr", text: n.expression }));
    item.append(h("div", { class: "value", title: n.value ?? "", text: `= ${shortValue(n.value)}` }));
  } else if (n.tier === "direct") {
    const c = n.cell;
    item.append(h("code", { class: "expr", text: `cell(${[c.evidence, ...c.key.map((k) => JSON.stringify(k)), c.column].join(", ")})` }));
  } else {
    item.append(h("p", { text: n.derivation }));
  }
  if (n.message) item.append(h("div", { class: `message ${n.status}`, text: n.message }));
  return item;
}

export async function openEvidence(id, version, row) {
  state.compose = null;
  state.open = { kind: "evidence", id, version, row };
  writeHash();
  for (const el of document.querySelectorAll(".claim.active")) el.classList.remove("active");
  showPanel(`Evidence ${id} v${version}`);
  const section = h("section", {});
  fill($("panel-body"), section);
  await renderEvidence(section, id, version, [], row);
}

export async function renderEvidence(section, id, version, cells, focusRow, marks = []) {
  let res;
  try {
    res = await evidence(id, version);
  } catch (err) {
    fill(section, h("p", { class: "message", text: `${id} v${version}: ${err.message}` }));
    return;
  }
  const doc = res.evidence;
  const rowsText = doc.partial
    ? `sample of ${doc.row_count.toLocaleString()} of ${doc.source_row_count.toLocaleString()} rows`
    : plural(doc.row_count, "row");
  fill(section,
    h("h4", { text: `${doc.id} v${doc.version}` }),
    h("dl", { class: "facts" },
      h("dt", { text: "Source" }), h("dd", { text: doc.source }),
      h("dt", { text: "Rows" }), h("dd", { text: rowsText }),
      h("dt", { text: "Run at" }), h("dd", { text: `${doc.run_at} (reported by the agent)` }),
      h("dt", { text: "Provenance" }), h("dd", { text: doc.provenance }),
      h("dt", { text: "Data hash" }), h("dd", { class: "mono", text: doc.data_hash }),
      Object.keys(doc.params || {}).length ? [h("dt", { text: "Params" }), h("dd", { class: "mono", text: JSON.stringify(doc.params) })] : null),
    doc.assumptions.length ? [h("h3", { text: "Assumptions" }), h("ul", { class: "assumptions" }, doc.assumptions.map((a) => h("li", { text: a })))] : null,
    h("h3", { text: "SQL" }),
    h("pre", { class: "sql", text: doc.sql }),
    h("h3", { text: "Result" }),
    resultTable(doc, res.row_keys, cells, focusRow, { id, version }, marks),
  );
  section.querySelector("tr.focus")?.scrollIntoView({ block: "nearest" });
}

function resultTable(doc, rowKeys, cells, focusRow, ident, marks = []) {
  if (!doc.rows) {
    return h("p", { class: "meta", text: `Rows are not disclosed at this link's level (${plural(doc.row_count, "row")}).` });
  }
  const cols = doc.columns.map((c) => c.name);
  const numeric = new Set(doc.columns.filter((c) => c.type === "integer" || c.type === "decimal").map((c) => c.name));
  const citedRows = new Map(); // row index -> Set(columns)
  rowKeys.forEach((key, i) => {
    for (const c of cells) {
      if (sameKey(key, c.key)) {
        if (!citedRows.has(i)) citedRows.set(i, new Set());
        citedRows.get(i).add(c.column);
      }
    }
  });
  const withheld = doc.disclosure?.withheld_rows || 0;
  const withheldCells = new Set((doc.disclosure?.withheld_cells || []).map(([i, name]) => `${i}\u0000${name}`));
  const marked = new Set(marks.map(selKey)); // cells a challenge points at
  for (const m of marks) {
    rowKeys.forEach((key, i) => { if (sameKey(key, m.key) && !citedRows.has(i)) citedRows.set(i, new Set()); });
  }
  let citedOnly = citedRows.size > 0 && doc.rows.length > FULL_TABLE_ROWS;
  let page = focusRow !== null && focusRow !== undefined ? Math.floor(focusRow / PAGE_SIZE) : 0;
  if (focusRow !== null && focusRow !== undefined) citedOnly = false;

  const wrap = h("div", {});
  const draw = () => {
    const indexes = citedOnly ? [...citedRows.keys()].sort((a, b) => a - b) : doc.rows.map((_, i) => i);
    const pages = Math.max(1, Math.ceil(indexes.length / PAGE_SIZE));
    page = Math.min(page, pages - 1);
    const shown = indexes.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const tbody = h("tbody", {}, shown.map((i) => {
      const row = doc.rows[i];
      const citedCols = citedRows.get(i);
      return h("tr", { class: [citedCols ? "cited" : "", i === focusRow ? "focus" : ""].join(" ").trim() || null },
        row.map((v, j) => {
          const name = cols[j];
          const cell = { evidence: ident.id, version: ident.version, key: rowKeys[i], column: name };
          const k = selKey(cell);
          const hidden = withheldCells.has(`${i}\u0000${name}`);
          const cls = [numeric.has(name) ? "num" : "", hidden ? "withheld" : v === null ? "null" : "", citedCols?.has(name) ? "cited-cell" : "",
            marked.has(k) ? "challenged-cell" : "", state.compose?.cells.has(k) ? "selected-cell" : ""];
          return h("td", {
            class: cls.join(" ").trim() || null,
            text: hidden ? "withheld" : v === null ? "null" : String(v),
            onclick: state.compose && !hidden ? (e) => toggleCell(e.currentTarget, cell) : null,
          });
        }));
    }));
    const notes = [];
    if (withheld) notes.push(`${plural(withheld, "row")} not disclosed at this level`);
    if (doc.disclosure?.withheld_columns?.length) notes.push(`not disclosed: ${doc.disclosure.withheld_columns.join(", ")}`);
    fill(wrap,
      h("div", { class: "table-tools" },
        h("span", { text: [`${indexes.length.toLocaleString()} of ${doc.row_count.toLocaleString()} rows`, ...notes].join(" · ") }),
        h("button", { class: "quiet", type: "button", text: "Download CSV",
          title: "All rows of this result, as disclosed at this link's level",
          onclick: () => saveBlob(new Blob([evidenceCsv(doc)], { type: "text/csv;charset=utf-8" }), `${ident.id}.v${ident.version}.csv`) }),
        citedRows.size ? h("label", {},
          h("input", { type: "checkbox", checked: citedOnly, onchange: (e) => { citedOnly = e.target.checked; page = 0; draw(); } }),
          "cited rows only") : null),
      h("div", { class: "table-wrap" },
        h("table", { class: "rows" },
          h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col", class: numeric.has(c) ? "num" : null, text: c })))),
          tbody)),
      pages > 1 ? h("div", { class: "pager" },
        h("button", { type: "button", text: "‹", "aria-label": "Previous page", disabled: page === 0, onclick: () => { page -= 1; draw(); } }),
        h("span", { text: `page ${page + 1} of ${pages}` }),
        h("button", { type: "button", text: "›", "aria-label": "Next page", disabled: page >= pages - 1, onclick: () => { page += 1; draw(); } })) : null,
    );
  };
  draw();
  return wrap;
}
