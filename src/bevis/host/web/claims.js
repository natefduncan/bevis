// Helpers for making a claim in the block editor (SPEC §6.2, §4, §5). Pure functions with no
// DOM, so node can test them. The server's validator is the authority; these only save the
// author typing what it will ask for.

// -- numbers in a span -----------------------------------------------------------------------

// The number token the validator reads (src/bevis/numbers.py), as written in text.
const TOKEN = new RegExp(
  "(?<![A-Za-z0-9_.])" + // not glued to a word or a decimal point
    "(?<sign>[+\\-−](?=\\$?[0-9]))?" +
    "(?<cur>\\$)?" +
    "(?<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)" +
    "(?<frac>\\.[0-9]+)?" +
    "(?![0-9])" +
    "(?<suffix>%|[KkMmBb](?![A-Za-z]))?" +
    "(?<tail>[A-Za-z]+)?",
  "g");

const MONTH = "(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)";
const DATE_PARTS = [
  /\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+Z?)?/g, // ISO dates and datetimes
  new RegExp(`\\b${MONTH}\\.?\\s+\\d{1,2}(?:,?\\s+\\d{4})?\\b`, "g"), // March 3, Sept. 14, March 3, 2025
  new RegExp(`\\b\\d{1,2}\\s+${MONTH}\\b\\.?(?:\\s+\\d{4})?`, "g"), // 3 March 2025
];

function dateRanges(text) {
  const out = [];
  for (const re of DATE_PARTS) for (const m of text.matchAll(re)) out.push([m.index, m.index + m[0].length]);
  return out;
}

// The numbers a span's claim has to cover, in order: every token except years and dates, which the
// validator ignores, and tokens it cannot parse ("3rd", "4.2bn").
export function detectNumbers(text) {
  const dates = dateRanges(text);
  const out = [];
  for (const m of text.matchAll(TOKEN)) {
    const g = m.groups;
    if (g.tail) continue;
    const bareInteger = !(g.sign || g.cur || g.frac || g.suffix || g.int.includes(","));
    if (bareInteger && g.int.length === 4 && Number(g.int) >= 1900 && Number(g.int) <= 2100) continue;
    if (dates.some(([a, b]) => m.index >= a && m.index < b)) continue;
    out.push(m[0]);
  }
  return out;
}

// -- cells and expressions ---------------------------------------------------------------------

// A key value as it is written inside cell(...): numbers bare, booleans bare, anything else a string.
export function keyLiteral(value, type) {
  if (value === null || value === undefined) return "null";
  if (type === "boolean") return value === true || value === "true" ? "true" : "false";
  if (type === "integer" || type === "decimal") return String(value);
  return `"${String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;

// cell(slug, key..., column), the expression text for one cell. `keyTypes` are the key columns' types.
export function cellExpression(evidenceId, key, keyTypes, column) {
  const parts = [evidenceId, ...key.map((v, i) => keyLiteral(v, keyTypes[i])),
    IDENTIFIER.test(column) ? column : `"${column.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`];
  return `cell(${parts.join(", ")})`;
}

// The evidence ids an expression reads, in order, without repeats.
export function expressionSlugs(expression) {
  return [...new Set([...String(expression).matchAll(/\bcell\(\s*([a-z][a-z0-9-]{2,63})\s*[,)]/g)].map((m) => m[1]))];
}

// -- the claim a form describes ------------------------------------------------------------------

// The evidence a claim has to pin: each direct cell's own version, and for each slug an expression
// reads, that version if a direct cell pinned it, else the latest. One version per slug.
export function derivePins(numbers, latest, extra = []) {
  const pins = new Map(extra.map((p) => [p.id, p.version])); // evidence a claim cites with no number of its own
  for (const n of numbers) if (n.tier === "direct" && n.cell?.evidence) pins.set(n.cell.evidence, n.cell.version);
  for (const n of numbers) {
    if (n.tier !== "computed") continue;
    for (const slug of expressionSlugs(n.expression || "")) if (!pins.has(slug) && latest[slug]) pins.set(slug, latest[slug]);
  }
  return [...pins].sort(([a], [b]) => (a < b ? -1 : 1)).map(([id, version]) => ({ id, version }));
}

// Is this number filled in enough to send? A form being filled in is not.
export function isComplete(n) {
  if (!String(n.text || "").trim()) return false;
  if (n.tier === "direct") return Boolean(n.cell?.evidence && n.cell.column && Array.isArray(n.cell.key));
  if (n.tier === "computed") return Boolean(String(n.expression || "").trim());
  if (n.tier === "asserted") return Boolean(String(n.derivation || "").trim());
  return false;
}

// A claim as link_claim takes it: no statuses, values, or messages, which the validator sets. With
// `lenient`, numbers still being filled in are left out, so a live check can run on the rest.
export function claimForOps(def, blockId, latest, { lenient = false } = {}) {
  const numbers = def.numbers.filter((n) => !lenient || isComplete(n)).map((n) => {
    const out = { text: n.text, tier: n.tier };
    if (n.tier === "direct" && n.cell) out.cell = { evidence: n.cell.evidence, version: n.cell.version, key: n.cell.key, column: n.cell.column };
    if (n.tier === "computed") out.expression = n.expression || "";
    if (n.tier === "asserted") out.derivation = n.derivation || "";
    return out;
  });
  return {
    id: def.id, block_id: blockId, evidence: derivePins(numbers.map((n) => ({ ...n })), latest, def.extraEvidence || []), numbers,
    ...(def.derivation ? { derivation: def.derivation } : {}),
  };
}

// The next free claim id, c1, c2, ..., past every id in use.
export function nextClaimId(ids) {
  const used = [...ids].map((id) => Number(String(id).slice(1))).filter(Number.isFinite);
  return `c${Math.max(0, ...used) + 1}`;
}

// -- moving a chip -----------------------------------------------------------------------------

// Move a chip one word left or right, given the text just before and just after it. A word is a
// run of non-space characters together with the spaces next to it, so spacing stays as it was.
export function moveOverWord(before, after, direction) {
  if (direction < 0) {
    const m = before.match(/(\S+\s*)$/);
    if (!m) return null;
    return { before: before.slice(0, before.length - m[1].length), after: m[1] + after };
  }
  const m = after.match(/^(\s*\S+)/);
  if (!m) return null;
  return { before: before + m[1], after: after.slice(m[1].length) };
}
