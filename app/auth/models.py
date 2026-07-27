from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"
ROLE_AUDITOR = "auditor"

ALL_ROLES = (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    ROLE_AUDITOR,
)

# Capability matrix used by dependency helpers and tests.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(
        {
            "jobs:write",
            "review:write",
            "export:read",
            "users:admin",
            "audit:read",
            "read",
        }
    ),
    ROLE_OPERATOR: frozenset({"jobs:write", "export:read", "read"}),
    ROLE_REVIEWER: frozenset({"review:write", "export:read", "read"}),
    ROLE_VIEWER: frozenset({"read", "export:read"}),
    ROLE_AUDITOR: frozenset({"read", "audit:read", "export:read"}),
}


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    roles: tuple[str, ...]
    session_id: str
    is_active: bool = True

    def has_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)

    def has_capability(self, capability: str) -> bool:
        return any(capability in ROLE_CAPABILITIES.get(role, frozenset()) for role in self.roles)

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        for role in self.roles:
            caps.update(ROLE_CAPABILITIES.get(role, frozenset()))
        return frozenset(caps)


def normalize_roles(roles: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for role in roles:
        if role not in ALL_ROLES:
            raise ValueError(f"未知角色: {role}")
        if role not in ordered:
            ordered.append(role)
    if not ordered:
        raise ValueError("至少需要一个角色")
    return tuple(ordered)
