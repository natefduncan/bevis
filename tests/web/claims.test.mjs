// Run with: node --test tests/web   (tests/test_web.py runs it under pytest when node is installed)
import test from "node:test";
import assert from "node:assert/strict";

import { cellExpression, claimForOps, derivePins, detectNumbers, expressionSlugs, isComplete, keyLiteral, moveOverWord, nextClaimId }
  from "../../src/bevis/host/web/claims.js";

test("detectNumbers finds each number a span states", () => {
  assert.deepEqual(detectNumbers("Revenue fell 12.0%, from $4.21M to $3.71M"), ["12.0%", "$4.21M", "$3.71M"]);
  assert.deepEqual(detectNumbers("142 accounts churned, taking $1.02M"), ["142", "$1.02M"]);
  assert.deepEqual(detectNumbers("up 1,234 units, down -5.5%"), ["1,234", "-5.5%"]);
});

test("a year or a date is not a number to cover", () => {
  assert.deepEqual(detectNumbers("In 2025, revenue fell 12%"), ["12%"]);
  assert.deepEqual(detectNumbers("Since 2024-03-01 we sold 40 units"), ["40"]);
  assert.deepEqual(detectNumbers("On March 3 and 3 March 2025 we sold 7"), ["7"]);
  assert.deepEqual(detectNumbers("Sept. 14 sales hit $9K"), ["$9K"]);
});

test("a four-digit quantity with a unit or separator still counts", () => {
  assert.deepEqual(detectNumbers("2,025 customers and 2025% growth"), ["2,025", "2025%"]);
});

test("a token the validator cannot parse is left out", () => {
  assert.deepEqual(detectNumbers("the 3rd quarter, 4.2bn dollars, and 10am"), []);
});

test("no numbers, no tokens", () => {
  assert.deepEqual(detectNumbers("The decline was broad"), []);
});

test("key literals follow the key column's type", () => {
  assert.equal(keyLiteral(2025, "integer"), "2025");
  assert.equal(keyLiteral("12.50", "decimal"), "12.50");
  assert.equal(keyLiteral("Mid-market", "string"), '"Mid-market"');
  assert.equal(keyLiteral('say "hi"', "string"), '"say \\"hi\\""');
  assert.equal(keyLiteral("2025-03-01", "date"), '"2025-03-01"');
  assert.equal(keyLiteral(true, "boolean"), "true");
  assert.equal(keyLiteral(null, "string"), "null");
});

test("a cell expression quotes a column that is not an identifier", () => {
  assert.equal(cellExpression("q-rev-yoy", [2024], ["integer"], "revenue"), "cell(q-rev-yoy, 2024, revenue)");
  assert.equal(cellExpression("q-seg", ["Mid-market", 2025], ["string", "integer"], "net revenue"),
    'cell(q-seg, "Mid-market", 2025, "net revenue")');
});

test("expressionSlugs lists the evidence an expression reads", () => {
  const e = "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue)) + cell(q-seg-delta, \"x\", y)";
  assert.deepEqual(expressionSlugs(e), ["q-rev-yoy", "q-seg-delta"]);
  assert.deepEqual(expressionSlugs("1 + 2"), []);
});

test("pins come from direct cells and expressions, one version per slug", () => {
  const numbers = [
    { tier: "direct", cell: { evidence: "q-a-aaa", version: 2, key: [1], column: "x" } },
    { tier: "computed", expression: "cell(q-a-aaa, 1, x) + cell(q-b-bbb, 1, y)" },
    { tier: "asserted", derivation: "from the board deck" },
  ];
  assert.deepEqual(derivePins(numbers, { "q-a-aaa": 5, "q-b-bbb": 3 }),
    [{ id: "q-a-aaa", version: 2 }, { id: "q-b-bbb", version: 3 }]);
  assert.deepEqual(derivePins([{ tier: "asserted", derivation: "x" }], {}), []);
});

test("claimForOps drops what the validator sets and keeps what each tier needs", () => {
  const def = {
    id: "c9", status: "error", numbers: [
      { text: "12.0%", tier: "computed", expression: "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))",
        status: "ok", value: "-0.1199", message: "x" },
      { text: "$4.21M", tier: "direct", cell: { evidence: "q-rev-yoy", version: 1, key: [2024], column: "revenue" }, status: "ok" },
      { text: "5", tier: "asserted", derivation: "told by finance" },
    ],
  };
  const out = claimForOps(def, "b2", { "q-rev-yoy": 1 });
  assert.equal(out.block_id, "b2");
  assert.deepEqual(out.evidence, [{ id: "q-rev-yoy", version: 1 }]);
  assert.deepEqual(out.numbers.map((n) => Object.keys(n).sort()),
    [["expression", "text", "tier"], ["cell", "text", "tier"], ["derivation", "text", "tier"]]);
  assert.equal("status" in out, false);
});

test("the next claim id is past every one in use", () => {
  assert.equal(nextClaimId(["c1", "c2", "c7"]), "c8");
  assert.equal(nextClaimId([]), "c1");
  assert.equal(nextClaimId(new Set(["c3"])), "c4");
});

test("a chip moves over one word at a time, spacing intact", () => {
  assert.deepEqual(moveOverWord("Revenue fell ", ". More text.", -1), { before: "Revenue ", after: "fell . More text." });
  assert.deepEqual(moveOverWord("Revenue ", " more text.", 1), { before: "Revenue  more", after: " text." });
  assert.equal(moveOverWord("", "x", -1), null);
  assert.equal(moveOverWord("x", "   ", 1), null);
});

test("a number is complete when its tier has what it needs", () => {
  assert.equal(isComplete({ text: "5", tier: "asserted", derivation: "finance said" }), true);
  assert.equal(isComplete({ text: "5", tier: "asserted", derivation: " " }), false);
  assert.equal(isComplete({ text: "", tier: "asserted", derivation: "x" }), false);
  assert.equal(isComplete({ text: "5", tier: "computed", expression: "1 + 1" }), true);
  assert.equal(isComplete({ text: "5", tier: "computed" }), false);
  assert.equal(isComplete({ text: "5", tier: "direct", cell: { evidence: "q-a-aaa", version: 1, key: [1], column: "x" } }), true);
  assert.equal(isComplete({ text: "5", tier: "direct", cell: { evidence: "q-a-aaa", version: 1, key: [1] } }), false);
  assert.equal(isComplete({ text: "5", tier: "direct" }), false);
});

test("a lenient claim leaves out numbers still being filled in, and pins only what remains", () => {
  const def = { id: "c9", numbers: [
    { text: "12.0%", tier: "computed", expression: "cell(q-a-aaa, 1, x)" },
    { text: "$4M", tier: "direct", cell: { evidence: "q-b-bbb", version: 1, key: [1] } }, // no column yet
  ] };
  const lenient = claimForOps(def, "b2", { "q-a-aaa": 2, "q-b-bbb": 1 }, { lenient: true });
  assert.deepEqual(lenient.numbers.map((n) => n.text), ["12.0%"]);
  assert.deepEqual(lenient.evidence, [{ id: "q-a-aaa", version: 2 }]);
  assert.equal(claimForOps(def, "b2", { "q-a-aaa": 2, "q-b-bbb": 1 }).numbers.length, 2);
});

test("a claim can cite evidence with no number, and a number's own pin wins for the same slug", () => {
  assert.deepEqual(derivePins([], {}, [{ id: "q-a-aaa", version: 2 }]), [{ id: "q-a-aaa", version: 2 }]);
  const numbers = [{ tier: "direct", cell: { evidence: "q-a-aaa", version: 3, key: [1], column: "x" } }];
  assert.deepEqual(derivePins(numbers, {}, [{ id: "q-a-aaa", version: 2 }, { id: "q-b-bbb", version: 1 }]),
    [{ id: "q-a-aaa", version: 3 }, { id: "q-b-bbb", version: 1 }]);
  const out = claimForOps({ id: "c9", numbers: [], extraEvidence: [{ id: "q-a-aaa", version: 1 }] }, "b2", {});
  assert.deepEqual(out.evidence, [{ id: "q-a-aaa", version: 1 }]);
});
