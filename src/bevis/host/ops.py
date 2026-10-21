"""Edit operations on a draft (SPEC §6).

A draft is `{"title", "blocks", "claims", "non_data"}`. Direct edits and
suggestions are both lists of operations, applied atomically. This module
is pure: it changes a copy of the draft and says what changed.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

BLOCK_FIELDS = {
    "heading": {"level", "text"},
    "text": {"text"},
    "chart": {"spec", "evidence", "caption"},
}
CLAIM_FIELDS = {"id", "block_id", "evidence", "numbers", "derivation"}
CLAIM_ID = re.compile(r"c[0-9]+")
BLOCK_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")

OPS = (
    "set_title", "upsert_block", "move_block", "delete_block",
    "link_claim", "unlink_claim", "repin", "mark_non_data", "unmark_non_data",
)


class OpError(Exception):
    def __init__(self, index: int, message: str):
        super().__init__(f"op {index}: {message}")
        self.index = index


@dataclass
class Change:
    action: str
    before: Any
    after: Any


@dataclass
class Applied:
    draft: dict
    changes: list[Change] = field(default_factory=list)


def empty_draft(title: str) -> dict:
    return {"title": title, "blocks": [], "claims": [], "non_data": []}


def apply(draft: dict, ops: list[dict]) -> Applied:
    if not isinstance(ops, list) or not ops:
        raise OpError(0, "ops must be a non-empty list")
    state = Applied(copy.deepcopy(draft))
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise OpError(i, f"unknown op {op.get('op') if isinstance(op, dict) else op!r}; one of {', '.join(OPS)}")
        try:
            _HANDLERS[op["op"]](state, op)
        except OpError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise OpError(i, f"{op['op']}: malformed ({e})") from e
        except _Fail as e:
            raise OpError(i, f"{op['op']}: {e}") from e
    return state


class _Fail(Exception):
    pass


def _check_fields(op: dict, allowed: set[str]) -> None:
    extra = set(op) - allowed - {"op"}
    if extra:
        raise _Fail(f"unexpected field(s): {', '.join(sorted(extra))}")


def _block_index(draft: dict, block_id: str) -> int:
    for i, b in enumerate(draft["blocks"]):
        if b["id"] == block_id:
            return i
    raise _Fail(f"no block {block_id!r}")


def _position(draft: dict, position: dict | None, default: int) -> int:
    if position is None:
        return default
    if not isinstance(position, dict) or len(position) != 1:
        raise _Fail('position is {"after": id}, {"before": id}, or {"index": n}')
    (how, where), = position.items()
    if how == "after":
        return _block_index(draft, where) + 1
    if how == "before":
        return _block_index(draft, where)
    if how == "index" and isinstance(where, int) and 0 <= where <= len(draft["blocks"]):
        return where
    raise _Fail(f"bad position {position!r}")


def next_block_id(draft: dict) -> str:
    """One past the highest bN, so ids are not reused while the draft holds a higher one."""
    nums = [int(b["id"][1:]) for b in draft["blocks"] if re.fullmatch(r"b[0-9]+", b["id"])]
    return f"b{max(nums, default=0) + 1}"


def next_claim_id(draft: dict) -> str:
    nums = [int(c["id"][1:]) for c in draft["claims"] if CLAIM_ID.fullmatch(c["id"])]
    return f"c{max(nums, default=0) + 1}"


# -- operations -----------------------------------------------------------------


def _set_title(state: Applied, op: dict) -> None:
    _check_fields(op, {"title"})
    title = op["title"]
    if not isinstance(title, str) or not title.strip():
        raise _Fail("title must be a non-empty string")
    state.changes.append(Change("set_title", state.draft["title"], title))
    state.draft["title"] = title


def _upsert_block(state: Applied, op: dict) -> None:
    _check_fields(op, {"block", "position"})
    block = dict(op["block"])
    kind = block.get("type")
    if kind == "image":
        raise _Fail("image blocks are not supported by the host yet")
    if kind not in BLOCK_FIELDS:
        raise _Fail(f"block type must be one of {', '.join(BLOCK_FIELDS)}")
    extra = set(block) - BLOCK_FIELDS[kind] - {"id", "type"}
    if extra:
        raise _Fail(f"a {kind} block has no field(s) {', '.join(sorted(extra))}")
    draft = state.draft
    block_id = block.get("id")
    if block_id is not None and not (isinstance(block_id, str) and BLOCK_ID.fullmatch(block_id)):
        raise _Fail(f"bad block id {block_id!r}")
    existing = next((i for i, b in enumerate(draft["blocks"]) if b["id"] == block_id), None)
    if existing is None:
        block["id"] = block_id or next_block_id(draft)
        at = _position(draft, op.get("position"), len(draft["blocks"]))
        draft["blocks"].insert(at, _ordered(block))
        state.changes.append(Change("add_block", None, draft["blocks"][at]))
    else:
        before = draft["blocks"].pop(existing)
        at = _position(draft, op.get("position"), existing)
        draft["blocks"].insert(at, _ordered(block))
        state.changes.append(Change("update_block", before, draft["blocks"][at]))


def _ordered(block: dict) -> dict:
    """id and type first, for readable JSON."""
    return {"id": block["id"], "type": block["type"], **{k: v for k, v in block.items() if k not in ("id", "type")}}


def _move_block(state: Applied, op: dict) -> None:
    _check_fields(op, {"id", "position"})
    draft = state.draft
    i = _block_index(draft, op["id"])
    block = draft["blocks"].pop(i)
    at = _position(draft, op["position"], i)
    draft["blocks"].insert(at, block)
    state.changes.append(Change("move_block", {"id": block["id"], "index": i}, {"id": block["id"], "index": at}))


def _delete_block(state: Applied, op: dict) -> None:
    """Deletes the block, its claims, and its non-data markings."""
    _check_fields(op, {"id"})
    draft = state.draft
    block = draft["blocks"].pop(_block_index(draft, op["id"]))
    for claim in [c for c in draft["claims"] if c["block_id"] == block["id"]]:
        draft["claims"].remove(claim)
        state.changes.append(Change("unlink_claim", claim, None))
    for nd in [n for n in draft["non_data"] if n["block_id"] == block["id"]]:
        draft["non_data"].remove(nd)
        state.changes.append(Change("unmark_non_data", nd, None))
    state.changes.append(Change("delete_block", block, None))


def _link_claim(state: Applied, op: dict) -> None:
    _check_fields(op, {"claim"})
    claim = dict(op["claim"])
    extra = set(claim) - CLAIM_FIELDS - {"status"}
    if extra:
        raise _Fail(f"a claim has no field(s) {', '.join(sorted(extra))}")
    if not isinstance(claim.get("id"), str) or not CLAIM_ID.fullmatch(claim["id"]):
        raise _Fail("claim id must look like c1, c2, ... and match its span {#cN}")
    _block_index(state.draft, claim["block_id"])
    claim.setdefault("evidence", [])
    claim.setdefault("numbers", [])
    numbers = []
    for n in claim["numbers"]:
        n = {k: v for k, v in dict(n).items() if k not in ("status", "value", "message")}
        n["status"] = "ok"  # the validator sets statuses and values
        if n.get("tier") == "computed":
            n["value"] = None
        numbers.append(n)
    claim["numbers"] = numbers
    claim["status"] = "ok"
    claim = {k: claim[k] for k in ("id", "block_id", "evidence", "numbers", "derivation", "status") if k in claim}
    claims = state.draft["claims"]
    for i, c in enumerate(claims):
        if c["id"] == claim["id"]:
            claims[i] = claim
            state.changes.append(Change("update_claim", c, claim))
            return
    claims.append(claim)
    state.changes.append(Change("link_claim", None, claim))


def _unlink_claim(state: Applied, op: dict) -> None:
    _check_fields(op, {"id"})
    claims = state.draft["claims"]
    for c in claims:
        if c["id"] == op["id"]:
            claims.remove(c)
            state.changes.append(Change("unlink_claim", c, None))
            return
    raise _Fail(f"no claim {op['id']!r}")


def _repin(state: Applied, op: dict) -> None:
    """Point claims and charts at another version of one evidence id."""
    _check_fields(op, {"evidence_id", "version", "claims", "blocks"})
    eid, version = op["evidence_id"], op["version"]
    if not isinstance(version, int) or version < 1:
        raise _Fail("version must be a positive integer")
    only_claims, only_blocks = op.get("claims"), op.get("blocks")
    everything = only_claims is None and only_blocks is None

    def wanted(ids: list | None, item_id: str) -> bool:
        return everything or (ids is not None and item_id in ids)

    touched = 0
    for claim in state.draft["claims"]:
        if not wanted(only_claims, claim["id"]) or not any(p["id"] == eid for p in claim["evidence"]):
            continue
        before = copy.deepcopy(claim)
        for p in claim["evidence"]:
            if p["id"] == eid:
                p["version"] = version
        for n in claim["numbers"]:
            if n.get("tier") == "direct" and n["cell"]["evidence"] == eid:
                n["cell"]["version"] = version
        if claim != before:
            touched += 1
            state.changes.append(Change("repin_claim", before, copy.deepcopy(claim)))
    for block in state.draft["blocks"]:
        if block["type"] != "chart" or not wanted(only_blocks, block["id"]):
            continue
        if not any(p["id"] == eid for p in block["evidence"]):
            continue
        before = copy.deepcopy(block)
        for p in block["evidence"]:
            if p["id"] == eid:
                p["version"] = version
        _repin_spec(block["spec"], eid, version)
        if block != before:
            touched += 1
            state.changes.append(Change("repin_block", before, copy.deepcopy(block)))
    if not touched and not everything:
        raise _Fail(f"nothing named there pins {eid}")


def _repin_spec(node: Any, eid: str, version: int) -> None:
    if isinstance(node, dict):
        ref = node.get("evidence") if set(node) == {"evidence"} else None
        if isinstance(ref, dict) and ref.get("id") == eid:
            ref["version"] = version
        for v in node.values():
            _repin_spec(v, eid, version)
    elif isinstance(node, list):
        for v in node:
            _repin_spec(v, eid, version)


def _mark_non_data(state: Applied, op: dict) -> None:
    _check_fields(op, {"block_id", "text", "reason"})
    _block_index(state.draft, op["block_id"])
    if not isinstance(op["reason"], str) or not op["reason"].strip():
        raise _Fail("a non-data marking needs a reason")
    entry = {"block_id": op["block_id"], "text": op["text"], "reason": op["reason"]}
    marks = state.draft["non_data"]
    for i, m in enumerate(marks):
        if m["block_id"] == entry["block_id"] and m["text"] == entry["text"]:
            marks[i] = entry
            state.changes.append(Change("mark_non_data", m, entry))
            return
    marks.append(entry)
    state.changes.append(Change("mark_non_data", None, entry))


def _unmark_non_data(state: Applied, op: dict) -> None:
    _check_fields(op, {"block_id", "text"})
    marks = state.draft["non_data"]
    for m in marks:
        if m["block_id"] == op["block_id"] and m["text"] == op["text"]:
            marks.remove(m)
            state.changes.append(Change("unmark_non_data", m, None))
            return
    raise _Fail(f"no non-data marking for {op['text']!r} in {op['block_id']}")


_HANDLERS = {
    "set_title": _set_title,
    "upsert_block": _upsert_block,
    "move_block": _move_block,
    "delete_block": _delete_block,
    "link_claim": _link_claim,
    "unlink_claim": _unlink_claim,
    "repin": _repin,
    "mark_non_data": _mark_non_data,
    "unmark_non_data": _unmark_non_data,
}
