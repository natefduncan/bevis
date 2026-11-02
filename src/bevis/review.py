"""Which human edits need a second look (SPEC §6.2).

An edit is **near a claim** when it changes the text of a paragraph that holds a claim span, or
creates, changes, or unlinks a claim. The unit is the paragraph, as Pandoc parses it (a
paragraph, a list item's text, a heading, a table cell), so no sentence splitter is needed and the
test reads the Markdown exactly as the validator does.
"""

from __future__ import annotations

from typing import Any

from .markdown import MarkdownError, pandoc_json, stringify

LEAF = frozenset({"Para", "Plain", "Header"})


def _leaves(node: Any, out: list) -> None:
    if isinstance(node, list):
        for n in node:
            _leaves(n, out)
    elif isinstance(node, dict):
        if node.get("t") in LEAF:
            out.append(node)
        elif "c" in node:
            _leaves(node["c"], out)


def _has_span(node: Any, ids: frozenset[str]) -> bool:
    if isinstance(node, list):
        return any(_has_span(n, ids) for n in node)
    if isinstance(node, dict):
        if node.get("t") == "Span" and node["c"][0][0] in ids:
            return True
        return _has_span(node.get("c"), ids)
    return False


def claim_units(markdown: str, claim_ids: frozenset[str]) -> list[str]:
    """The text of each paragraph that holds a span of one of these claims, in order."""
    if not claim_ids:
        return []
    leaves: list = []
    _leaves(pandoc_json(markdown)["blocks"], leaves)
    return [stringify(leaf) for leaf in leaves if _has_span(leaf, claim_ids)]


def _units(block: dict | None, claims: list[dict]) -> list[str]:
    if block is None or block["type"] != "text":
        return []
    ids = frozenset(c["id"] for c in claims if c["block_id"] == block["id"])
    return claim_units(block["text"], ids)


def text_near_claim(before: dict | None, before_claims: list[dict], after: dict | None,
                    after_claims: list[dict]) -> bool:
    """Did the text of a paragraph with a claim span change, appear, or go?

    A Markdown error counts as near: the validator will reject the edit, but if it is let through
    (a suggestion is validated again on accept) the safe answer is to ask for a review.
    """
    try:
        return _units(before, before_claims) != _units(after, after_claims)
    except MarkdownError:
        return True
