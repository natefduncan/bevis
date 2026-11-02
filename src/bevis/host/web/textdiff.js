// Text helpers for the block editor (SPEC §6.2): where a block's claim spans are,
// a word diff to show what changed, and a three-way merge by paragraph. Pure
// functions with no DOM, so node can test them.

// -- claim spans ---------------------------------------------------------------------------

// The claim spans in Markdown source: [text]{#c1}. Only ids in `ids` count as claims; any other
// span stays plain text, and the server's validator reports it. A span's text may hold brackets
// (a link, say), so the opening bracket is found by counting back from the closing one.
export function findSpans(text, ids) {
  const found = [];
  const closer = /\]\{#(c[0-9]+)\}/g;
  let lastEnd = 0;
  for (let m = closer.exec(text); m; m = closer.exec(text)) {
    if (!ids.has(m[1])) continue;
    let depth = 0;
    let start = -1;
    for (let i = m.index - 1; i >= lastEnd; i--) {
      if (text[i - 1] === "\\") continue;
      if (text[i] === "]") depth++;
      else if (text[i] === "[") {
        if (depth === 0) { start = i; break; }
        depth--;
      }
    }
    if (start < 0) continue;
    const end = m.index + m[0].length;
    found.push({ id: m[1], start, end, source: text.slice(start, end), inner: text.slice(start + 1, m.index) });
    lastEnd = end;
  }
  return found;
}

// The text cut into plain strings and claim spans, in order.
export function splitSpans(text, ids) {
  const parts = [];
  let at = 0;
  for (const s of findSpans(text, ids)) {
    if (s.start > at) parts.push({ text: text.slice(at, s.start) });
    parts.push({ span: s });
    at = s.end;
  }
  if (at < text.length) parts.push({ text: text.slice(at) });
  return parts;
}

// -- diff ----------------------------------------------------------------------------------

const TOO_BIG = 4_000_000; // cells in the LCS table; past this a diff is a whole replacement

function lcsTable(a, b) {
  const w = b.length + 1;
  const t = new Uint32Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      t[i * w + j] = a[i] === b[j] ? t[(i + 1) * w + j + 1] + 1 : Math.max(t[(i + 1) * w + j], t[i * w + j + 1]);
    }
  }
  return t;
}

// What changed going from a to b, as hunks: a[a0:a1] was replaced by b[b0:b1].
export function hunks(a, b) {
  if (a.length * b.length > TOO_BIG) return a.join("\u0000") === b.join("\u0000") ? [] : [{ a0: 0, a1: a.length, b0: 0, b1: b.length }];
  const w = b.length + 1;
  const t = lcsTable(a, b);
  const out = [];
  let i = 0;
  let j = 0;
  let open = null;
  const close = () => { if (open) { open.a1 = i; open.b1 = j; out.push(open); open = null; } };
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { close(); i++; j++; continue; }
    open = open || { a0: i, a1: i, b0: j, b1: j };
    if (j < b.length && (i === a.length || t[i * w + j + 1] >= t[(i + 1) * w + j])) j++;
    else i++;
  }
  close();
  return out;
}

const words = (text) => text.split(/(\s+)/).filter((s) => s !== "");

// A word diff as [{type: "eq" | "del" | "ins", text}], for showing what changed.
export function diffWords(a, b) {
  const x = words(a);
  const y = words(b);
  const out = [];
  const push = (type, tokens) => {
    if (!tokens.length) return;
    const text = tokens.join("");
    const last = out[out.length - 1];
    if (last && last.type === type) last.text += text;
    else out.push({ type, text });
  };
  let i = 0;
  for (const h of hunks(x, y)) {
    push("eq", x.slice(i, h.a0));
    push("del", x.slice(h.a0, h.a1));
    push("ins", y.slice(h.b0, h.b1));
    i = h.a1;
  }
  push("eq", x.slice(i));
  return out;
}

// -- merge ---------------------------------------------------------------------------------

export const paragraphs = (text) => (text.trim() === "" ? [] : text.split(/\n[ \t]*\n/));

const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

// Do two changes to the same base touch the same place? Ranges that merely sit side by side do
// not. Two insertions at one point do.
function collide(p, q) {
  if (p.a0 === p.a1 && q.a0 === q.a1) return p.a0 === q.a0;
  return p.a0 < q.a1 && q.a0 < p.a1;
}

// Merge `mine` and `theirs`, both edits of `base`, by paragraph. Where both changed the same
// place differently, the result takes theirs and the place is listed in `conflicts` with both
// versions, so nothing is lost silently.
export function merge3(base, mine, theirs) {
  const b = paragraphs(base);
  const m = paragraphs(mine);
  const t = paragraphs(theirs);
  const mine_ = hunks(b, m).map((h) => ({ ...h, side: "mine", lines: m.slice(h.b0, h.b1) }));
  const theirs_ = hunks(b, t).map((h) => ({ ...h, side: "theirs", lines: t.slice(h.b0, h.b1) }));
  const all = [...mine_, ...theirs_].sort((p, q) => p.a0 - q.a0 || p.a1 - q.a1);
  const out = [];
  const conflicts = [];
  let at = 0;
  for (let k = 0; k < all.length;) {
    // A cluster: this change and every later one that collides with something already in it.
    const cluster = [all[k]];
    let end = all[k].a1;
    let n = k + 1;
    while (n < all.length && cluster.some((c) => collide(c, all[n]))) {
      cluster.push(all[n]);
      end = Math.max(end, all[n].a1);
      n++;
    }
    const start = Math.min(...cluster.map((c) => c.a0));
    out.push(...b.slice(at, start));
    const ours = cluster.filter((c) => c.side === "mine");
    const theirs2 = cluster.filter((c) => c.side === "theirs");
    const replacement = (side) => {
      // The base range [start, end) with this side's changes applied.
      const r = [];
      let cursor = start;
      for (const c of side) { r.push(...b.slice(cursor, c.a0), ...c.lines); cursor = c.a1; }
      r.push(...b.slice(cursor, end));
      return r;
    };
    if (!ours.length) out.push(...replacement(theirs2));
    else if (!theirs2.length) out.push(...replacement(ours));
    else {
      const a = replacement(ours);
      const z = replacement(theirs2);
      if (!same(a, z)) conflicts.push({ base: b.slice(start, end), mine: a, theirs: z });
      out.push(...z);
    }
    at = end;
    k = n;
  }
  out.push(...b.slice(at));
  return { text: out.join("\n\n"), conflicts };
}
