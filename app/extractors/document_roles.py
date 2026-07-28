"""Document role extraction: joint issuance, forwarding, signatures."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


FORWARD_PATTERNS = [
    re.compile(r"(联合印发|联合发文|联合转发)"),
    re.compile(r"(批转|转发)"),
    re.compile(r"(办公室.{0,6}转发|转发.{0,12}办公室)"),
    re.compile(r"(经区政府同意|经区委同意|经市政府同意)"),
]


@dataclass
class DocumentRoles:
    public_body: str = ""
    upload_body: str = ""
    primary_issuer: str = ""
    joint_issuers: list[str] = field(default_factory=list)
    forwarding_body: str = ""
    original_issuer: str = ""
    signature_bodies: list[str] = field(default_factory=list)
    relation_type: str = ""  # joint | forward | joint_forward | direct | unknown
    relation_evidence: str = ""
    opening_excerpt: str = ""
    closing_excerpt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _split_org_names(text: str) -> list[str]:
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return []
    parts = re.split(r"[、,，;；及和与/]", text)
    return [part for part in parts if len(part) >= 4]


def _opening_block(body: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    return "\n".join(lines[:12])[:limit]


def _closing_block(body: str, limit: int = 500) -> str:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    return "\n".join(lines[-15:])[:limit]


ORG_LINE_RE = re.compile(
    r"(中共[\u4e00-\u9fff]{2,30}(?:委员会|委办公室|办公室)|"
    r"上海市[\u4e00-\u9fff]{2,30}(?:人民政府办公室|人民政府|办事处|委员会|局|办公室)|"
    r"[\u4e00-\u9fff]{2,20}(?:街道办事处|镇人民政府|区委办公室|区政府办公室))"
)


def extract_document_roles(
    *,
    public_body: str = "",
    title: str = "",
    body_text: str = "",
    upload_body: str = "",
) -> DocumentRoles:
    roles = DocumentRoles(public_body=public_body or "", upload_body=upload_body or "")
    opening = _opening_block(body_text)
    closing = _closing_block(body_text)
    roles.opening_excerpt = opening
    roles.closing_excerpt = closing

    relation = "direct"
    evidence_bits: list[str] = []
    for pattern in FORWARD_PATTERNS:
        match = pattern.search(opening) or pattern.search(title or "") or pattern.search(body_text[:800] or "")
        if match:
            token = match.group(1)
            evidence_bits.append(token)
            if "联合" in token and ("转发" in token or "印发" in token or "发文" in token):
                relation = "joint_forward" if "转发" in token else "joint"
            elif "转发" in token or "批转" in token:
                relation = "forward" if relation == "direct" else relation
            elif "同意" in token:
                evidence_bits.append(token)

    # Explicit "A B 转发" opening pattern common in party-government office docs.
    forward_open = re.search(
        r"(.{4,40})(?:、|和|与)?(.{4,40})转发",
        re.sub(r"\s+", "", opening[:120]),
    )
    if forward_open:
        relation = "joint_forward" if relation in {"joint", "joint_forward"} or "联合" in opening[:80] else "forward"
        left, right = forward_open.group(1), forward_open.group(2)
        for name in _split_org_names(left) + _split_org_names(right):
            if name not in roles.joint_issuers:
                roles.joint_issuers.append(name)
        roles.forwarding_body = "、".join(roles.joint_issuers[:2]) or left
        evidence_bits.append(forward_open.group(0)[:80])

    # Collect organization-like lines from opening
    opening_orgs = ORG_LINE_RE.findall(re.sub(r"\s+", "", opening))
    for name in opening_orgs:
        if name not in roles.joint_issuers:
            roles.joint_issuers.append(name)

    closing_orgs = ORG_LINE_RE.findall(re.sub(r"\s+", "", closing))
    roles.signature_bodies = list(dict.fromkeys(closing_orgs))

    if roles.joint_issuers:
        # Prefer party office / government office as primary when joint forward
        preferred = None
        for name in roles.joint_issuers:
            if "委办公室" in name or "区委办" in name:
                preferred = name
                break
        if not preferred:
            for name in roles.joint_issuers:
                if "人民政府办公室" in name or "政府办公室" in name:
                    preferred = name
                    break
        roles.primary_issuer = preferred or roles.joint_issuers[0]
        if len(roles.joint_issuers) > 1 and relation == "direct":
            relation = "joint"
    elif public_body:
        roles.primary_issuer = public_body
    else:
        roles.primary_issuer = roles.signature_bodies[0] if roles.signature_bodies else ""

    # Original issuer: often after 转发《...》 or 原文件
    original = re.search(
        r"转发.{0,20}?(中共[\u4e00-\u9fff]{2,30}|上海市[\u4e00-\u9fff]{2,30}(?:人民政府|委员会|局))",
        re.sub(r"\s+", "", body_text[:600]),
    )
    if original:
        roles.original_issuer = original.group(1)

    roles.relation_type = relation if evidence_bits or roles.joint_issuers else (
        "direct" if roles.primary_issuer else "unknown"
    )
    roles.relation_evidence = "；".join(dict.fromkeys(evidence_bits))[:300]
    return roles
