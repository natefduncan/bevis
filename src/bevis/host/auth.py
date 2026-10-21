"""Credentials and roles (SPEC §6, §7).

Two kinds of secret authenticate a caller:

- An **agent key** (`bvk_…`) names an agent across the host. It can create
  reports. On a report it created it is an editor; on any other report it
  is a suggester.
- A **link token** (`bvl_…`) is a capability link for one report, with a
  role, a disclosure level, and a scope (the draft, the latest published
  version, or one version number).

Only SHA-256 hashes of secrets are stored.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

ROLES = ("reader", "commenter", "suggester", "editor", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
DISCLOSURES = ("full", "cited", "metadata")


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    credential_id: str
    kind: str  # agent_key | link
    name: str
    author_kind: str  # agent | human
    report_id: str | None = None  # links only
    role: str | None = None  # links only
    disclosure: str = "full"
    scope: str | None = None  # links only: draft | latest | <number>

    def author(self) -> dict:
        return {"kind": self.author_kind, "name": self.name, "verified": False}

    def role_on(self, report_id: str, creator_key: str | None) -> str | None:
        if self.kind == "agent_key":
            return "editor" if creator_key == self.credential_id else "suggester"
        return self.role if self.report_id == report_id else None

    def sees_draft(self) -> bool:
        return self.kind == "agent_key" or self.scope == "draft"
