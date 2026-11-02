// Run with: node --test tests/web   (tests/test_web.py runs it under pytest when node is installed)
import test from "node:test";
import assert from "node:assert/strict";

import { diffWords, findSpans, hunks, merge3, paragraphs, splitSpans } from "../../src/bevis/host/web/textdiff.js";

const ids = (...names) => new Set(names);

test("a claim span is found with its source and inner text", () => {
  const text = "Intro. [Revenue fell 12.0%]{#c1} and more.";
  const [s] = findSpans(text, ids("c1"));
  assert.equal(s.id, "c1");
  assert.equal(s.source, "[Revenue fell 12.0%]{#c1}");
  assert.equal(s.inner, "Revenue fell 12.0%");
  assert.equal(text.slice(s.start, s.end), s.source);
});

test("only known claim ids are spans", () => {
  assert.deepEqual(findSpans("[a]{#c1} [b]{#c2}", ids("c2")).map((s) => s.id), ["c2"]);
  assert.deepEqual(findSpans("[a]{#c1}", ids()), []);
  assert.deepEqual(findSpans("no spans here", ids("c1")), []);
});

test("a span may hold brackets, such as a link", () => {
  const text = "x [see [the table](http://e.x) for 12%]{#c1} y";
  const [s] = findSpans(text, ids("c1"));
  assert.equal(s.source, "[see [the table](http://e.x) for 12%]{#c1}");
});

test("a span starts after the one before it", () => {
  const spans = findSpans("[a]{#c1}[b]{#c2}", ids("c1", "c2"));
  assert.deepEqual(spans.map((s) => s.source), ["[a]{#c1}", "[b]{#c2}"]);
});

test("a closer with no opener is skipped", () => {
  assert.deepEqual(findSpans("stray ]{#c1} here", ids("c1")), []);
});

test("splitSpans keeps every character, in order", () => {
  const text = "Intro. [Revenue fell]{#c1} then [rose]{#c2}.";
  const parts = splitSpans(text, ids("c1", "c2"));
  assert.equal(parts.map((p) => p.text ?? p.span.source).join(""), text);
  assert.deepEqual(parts.map((p) => (p.span ? p.span.id : "text")), ["text", "c1", "text", "c2", "text"]);
});

test("hunks say what replaced what", () => {
  assert.deepEqual(hunks(["a", "b", "c"], ["a", "x", "c"]), [{ a0: 1, a1: 2, b0: 1, b1: 2 }]);
  assert.deepEqual(hunks(["a"], ["a", "b"]), [{ a0: 1, a1: 1, b0: 1, b1: 2 }]);
  assert.deepEqual(hunks(["a", "b"], ["a"]), [{ a0: 1, a1: 2, b0: 1, b1: 1 }]);
  assert.deepEqual(hunks(["a"], ["a"]), []);
});

test("a word diff marks what went and what came", () => {
  const d = diffWords("The decline was broad", "The decline was wide and deep");
  assert.deepEqual(d.map((x) => x.type), ["eq", "del", "ins"]);
  assert.equal(d[0].text, "The decline was ");
  assert.equal(d[1].text, "broad");
  assert.equal(d[2].text, "wide and deep");
  assert.deepEqual(diffWords("same", "same"), [{ type: "eq", text: "same" }]);
});

test("paragraphs split on blank lines", () => {
  assert.deepEqual(paragraphs("a\nb\n\nc"), ["a\nb", "c"]);
  assert.deepEqual(paragraphs("  "), []);
});

const base = "One.\n\nTwo.\n\nThree.";

test("changes to different paragraphs merge", () => {
  const r = merge3(base, "One, mine.\n\nTwo.\n\nThree.", "One.\n\nTwo.\n\nThree, theirs.");
  assert.equal(r.text, "One, mine.\n\nTwo.\n\nThree, theirs.");
  assert.deepEqual(r.conflicts, []);
});

test("changes to adjacent paragraphs still merge", () => {
  const r = merge3(base, "One, mine.\n\nTwo.\n\nThree.", "One.\n\nTwo, theirs.\n\nThree.");
  assert.equal(r.text, "One, mine.\n\nTwo, theirs.\n\nThree.");
  assert.deepEqual(r.conflicts, []);
});

test("both changing one paragraph differently is a conflict that takes theirs", () => {
  const r = merge3(base, "One.\n\nTwo, mine.\n\nThree.", "One.\n\nTwo, theirs.\n\nThree.");
  assert.equal(r.text, "One.\n\nTwo, theirs.\n\nThree.");
  assert.equal(r.conflicts.length, 1);
  assert.deepEqual(r.conflicts[0], { base: ["Two."], mine: ["Two, mine."], theirs: ["Two, theirs."] });
});

test("the same change on both sides is not a conflict", () => {
  const r = merge3(base, "One.\n\nTwo, same.\n\nThree.", "One.\n\nTwo, same.\n\nThree.");
  assert.equal(r.text, "One.\n\nTwo, same.\n\nThree.");
  assert.deepEqual(r.conflicts, []);
});

test("an addition on one side and an edit on the other merge", () => {
  const r = merge3(base, "One.\n\nTwo.\n\nThree.\n\nMine added.", "One, theirs.\n\nTwo.\n\nThree.");
  assert.equal(r.text, "One, theirs.\n\nTwo.\n\nThree.\n\nMine added.");
  assert.deepEqual(r.conflicts, []);
});

test("two additions at the same place conflict, and both are reported", () => {
  const r = merge3(base, "One.\n\nTwo.\n\nThree.\n\nMine.", "One.\n\nTwo.\n\nThree.\n\nTheirs.");
  assert.equal(r.conflicts.length, 1);
  assert.deepEqual(r.conflicts[0].mine, ["Mine."]);
  assert.deepEqual(r.conflicts[0].theirs, ["Theirs."]);
  assert.equal(r.text, "One.\n\nTwo.\n\nThree.\n\nTheirs.");
});

test("a deletion on one side and an edit elsewhere merge", () => {
  const r = merge3(base, "One.\n\nThree.", "One.\n\nTwo.\n\nThree, theirs.");
  assert.equal(r.text, "One.\n\nThree, theirs.");
  assert.deepEqual(r.conflicts, []);
});

test("deleting what the other side edited is a conflict", () => {
  const r = merge3(base, "One.\n\nThree.", "One.\n\nTwo, theirs.\n\nThree.");
  assert.equal(r.conflicts.length, 1);
  assert.equal(r.text, "One.\n\nTwo, theirs.\n\nThree.");
});

test("untouched text comes back as it was", () => {
  const r = merge3(base, base, base);
  assert.equal(r.text, base);
  assert.deepEqual(r.conflicts, []);
});

test("a claim span survives a merge that edits other prose", () => {
  const b = "[Revenue fell 12%]{#c1}.\n\nClosing.";
  const r = merge3(b, "[Revenue fell 12%]{#c1}.\n\nClosing, mine.", "[Revenue fell 12%]{#c1}, theirs.\n\nClosing.");
  assert.equal(r.text, "[Revenue fell 12%]{#c1}, theirs.\n\nClosing, mine.");
  assert.deepEqual(r.conflicts, []);
});
