"""Build the privacy-bounded evidence bundle handed to a judgment provider."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from urllib.parse import urlsplit

from .judgment_models import (
    ALLOWED_IMPACT_CATEGORIES,
    EvidenceBundle,
    EvidenceItem,
    MAX_BUNDLE_CHARACTERS,
    MAX_EVIDENCE_SOURCES,
    system_instruction_for,
)


def _public_text(value, limit):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _public_url(value):
    url = _public_text(value, 500)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    return url


def _serialized_length(bundle):
    return len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))


def build_public_bundle(cluster: dict, items: list[dict]) -> EvidenceBundle:
    """Construct the only object that may cross the remote-AI boundary."""
    evidence = []
    for index, item in enumerate(items[:MAX_EVIDENCE_SOURCES], start=1):
        url = _public_url(item.get("canonical_url") or item.get("url"))
        if not url:
            continue
        evidence.append(
            EvidenceItem(
                source_id=_public_text(
                    item.get("source_id") or item.get("item_id") or f"source-{index}",
                    128,
                ),
                title=_public_text(item.get("title"), 300),
                summary=_public_text(item.get("summary"), 900),
                domain=(urlsplit(url).hostname or "")[:253].casefold(),
                url=url,
                published_at=_public_text(
                    item.get("published_at") or item.get("first_seen_at"), 64
                ),
            )
        )
    categories = tuple(
        category
        for category in map(str, cluster.get("categories", ()))
        if category in ALLOWED_IMPACT_CATEGORIES
    ) or ("general",)
    bundle = EvidenceBundle(
        cluster_id=_public_text(cluster.get("cluster_id"), 128),
        title=_public_text(cluster.get("title"), 300),
        summary=_public_text(cluster.get("summary"), 900),
        evidence_level=(
            str(cluster.get("evidence_level"))
            if str(cluster.get("evidence_level")) in {"E1", "E2", "E3", "E4"}
            else "E1"
        ),
        categories=categories,
        items=tuple(evidence),
    )
    # v1.3：认知框架块按事件内容组装（天外实体侧只在相关事件上注入）。
    # 必须在长度裁剪**之前**设定，否则裁剪时算的不是真实体积。
    bundle = replace(
        bundle,
        system_instruction=system_instruction_for(bundle.title, bundle.summary),
    )
    while _serialized_length(bundle) > MAX_BUNDLE_CHARACTERS and bundle.items:
        longest = max(range(len(bundle.items)), key=lambda i: len(bundle.items[i].summary))
        target = bundle.items[longest]
        if len(target.summary) > 80:
            shortened = EvidenceItem(
                target.source_id,
                target.title,
                target.summary[: max(80, int(len(target.summary) * 0.8))],
                target.domain,
                target.url,
                target.published_at,
            )
            values = list(bundle.items)
            values[longest] = shortened
            bundle = EvidenceBundle(
                bundle.cluster_id,
                bundle.title,
                bundle.summary,
                bundle.evidence_level,
                bundle.categories,
                tuple(values),
                bundle.system_instruction,
            )
        else:
            bundle = EvidenceBundle(
                bundle.cluster_id,
                bundle.title,
                bundle.summary,
                bundle.evidence_level,
                bundle.categories,
                bundle.items[:-1],
                bundle.system_instruction,
            )
    return bundle
