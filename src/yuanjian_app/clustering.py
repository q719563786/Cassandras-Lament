"""Deterministic, local-only text similarity for external event clustering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache


_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._+-]*")
_NUMBER = re.compile(
    r"(?<!\d)(?:\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:万|亿|元|美元|人民币)?)(?!\d)"
)
_WEAK_BIGRAMS = frozenset(
    {
        "发布",
        "公布",
        "表示",
        "消息",
        "最新",
        "今日",
        "本月",
        "广东",
        "中国",
        "相关",
        "实施",
        "调整",
    }
)


@dataclass(frozen=True)
class ClusterText:
    title: str
    summary: str
    observed_at: datetime

    @property
    def combined(self) -> str:
        return f"{self.title} {self.summary}".strip()


@dataclass(frozen=True)
class MergeDecision:
    merge: bool
    score: float
    shared_entities: tuple[str, ...]
    shared_numbers: tuple[str, ...]
    reason: str


@lru_cache(maxsize=8192)
def _normalized_numbers(text: str) -> frozenset[str]:
    return frozenset(match.group(0).replace("年", "-").replace("月", "-").rstrip("日") for match in _NUMBER.finditer(text))


@lru_cache(maxsize=8192)
def text_features(text: str) -> frozenset[str]:
    """Return stable tokens without sending or persisting the original text.

    这个函数是纯函数，且是聚类里最贵的一步（三套正则）。它的调用模式天然重复：
    `should_merge()` 每比较一对就要算两次（`similarity` 一次、`_shared_subject_features`
    一次），而每个新条目又会把全部活跃簇重算一遍——同一个簇的标题+摘要会被反复
    分词。加缓存后跨条目、跨配对都只算一次。返回 frozenset 不可变，缓存安全。
    """
    if not text or not text.strip():
        return frozenset()

    features: set[str] = set(_normalized_numbers(text))
    features.update(word.casefold() for word in _LATIN_WORD.findall(text))
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            features.add(run)
        else:
            features.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(features)


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over normalized local text features."""
    left_features = text_features(left)
    right_features = text_features(right)
    if not left_features or not right_features:
        return 0.0
    if left_features == right_features:
        return 1.0
    return len(left_features & right_features) / len(left_features | right_features)


def _shared_subject_features(left: str, right: str) -> frozenset[str]:
    shared = text_features(left) & text_features(right)
    return frozenset(
        token
        for token in shared
        if token not in _WEAK_BIGRAMS
        and not _NUMBER.fullmatch(token)
        and (len(token) >= 2 or token.isascii())
    )


def should_merge(
    left: ClusterText,
    right: ClusterText,
    threshold: float = 0.55,
) -> MergeDecision:
    """Decide whether two observations describe the same time-bounded event."""
    hours_apart = abs((left.observed_at - right.observed_at).total_seconds()) / 3600
    if hours_apart > 72:
        return MergeDecision(False, 0.0, (), (), "outside_time_window")

    score = similarity(left.combined, right.combined)
    shared_numbers = _normalized_numbers(left.combined) & _normalized_numbers(
        right.combined
    )
    shared_entities = _shared_subject_features(left.combined, right.combined)

    if score >= threshold:
        reason = "text_similarity"
        merge = True
    elif shared_numbers and len(shared_entities) >= 3 and score >= 0.25:
        reason = "number_and_subject_agreement"
        merge = True
    else:
        reason = "insufficient_agreement"
        merge = False
    return MergeDecision(
        merge,
        round(score, 6),
        tuple(sorted(shared_entities)),
        tuple(sorted(shared_numbers)),
        reason,
    )
