// Charts: Vega-Lite specs drawn into the report, with a mark per datum.

import { $, evidence, h } from "./core.js";
import { openEvidence } from "./panel.js";

// -- charts ----------------------------------------------------------------------------

function rowObjects(doc, ref) {
  // Values for drawing only: decimals become numbers here, never in the panel.
  // Each datum remembers its record and row, so a click opens the right one.
  const cols = doc.columns;
  return doc.rows.map((row, i) => {
    const o = { __bevis_ref: ref, __bevis_row: i };
    cols.forEach((c, j) => {
      const v = row[j];
      o[c.name] = c.type === "decimal" && v !== null ? Number(v) : v;
    });
    return o;
  });
}

function stripLinks(node) {
  // A chart's href channel would make marks into links; the panel is the only click target.
  if (Array.isArray(node)) node.forEach(stripLinks);
  else if (node && typeof node === "object") {
    delete node.href;
    Object.values(node).forEach(stripLinks);
  }
}

async function inlineData(node, refs) {
  if (Array.isArray(node)) { for (const n of node) await inlineData(n, refs); return; }
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node)) {
    if (k === "data" && v && v.evidence) {
      const res = await evidence(v.evidence.id, v.evidence.version);
      if (!res.evidence.rows) throw new Error("withheld");
      refs.push(v.evidence);
      node.data = { values: rowObjects(res.evidence, refs.length - 1) };
    } else {
      await inlineData(v, refs);
    }
  }
}

export async function drawChart(el, block) {
  const spec = structuredClone(block.spec);
  const refs = [];
  stripLinks(spec); // before inlining: data columns named href are not channels
  try {
    await inlineData(spec, refs);
  } catch (err) {
    el.replaceWith(h("p", { class: "chart-note", text: err.message === "withheld"
      ? "Chart not drawn: its data is not disclosed at this link's level."
      : `Chart not drawn: ${err.message}` }));
    return;
  }
  spec.width = spec.width === undefined ? "container" : spec.width;
  try {
    const result = await vegaEmbed(el, spec, {
      actions: false,
      renderer: "svg",
      ast: true, // CSP-safe: author expressions are interpreted, never compiled with eval
      expr: vega.expressionInterpreter,
      loader: vega.loader({ mode: "http" }),
      config: chartTheme(),
    });
    result.view.addEventListener("click", (_event, item) => {
      // Strict charts (SPEC §3.6) draw one mark per row, so the datum is the row.
      const ref = refs[item?.datum?.__bevis_ref];
      const row = item?.datum?.__bevis_row;
      if (ref && row !== undefined) openEvidence(ref.id, ref.version, row);
    });
  } catch (err) {
    el.replaceWith(h("p", { class: "chart-note", text: `Chart not drawn: ${err.message}` }));
  }
}

function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const text = css.getPropertyValue("--text").trim();
  const muted = css.getPropertyValue("--muted").trim();
  const line = css.getPropertyValue("--line").trim();
  return {
    background: null,
    view: { stroke: null },
    axis: { labelColor: muted, titleColor: text, domainColor: line, gridColor: line, tickColor: line },
    legend: { labelColor: muted, titleColor: text },
    title: { color: text },
  };
}
