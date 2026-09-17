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
# 常规聚类窗口：超过它就不再算"同一个时间受限事件"。
CLUSTER_WINDOW_HOURS = 72
# 势头延续窗口（v1.3）：**只对近重复**放行，见 should_merge 的注释。
CONTINUITY_WINDOW_HOURS = 30 * 24
# 近重复判据（两条同时看，比常规路径严格得多）
CONTINUITY_MIN_SCORE = 0.85
CONTINUITY_NUMBER_SCORE = 0.70

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


def _worth_extended_check(left: ClusterText, right: ClusterText) -> bool:
    """超过 72 小时时用的廉价预筛：两个标题至少要有 4 个共同汉字。

    目的是保住性能：没有它，每个新条目都要对全部活跃簇跑一次分词，
    而活跃簇数量随时间增长。预筛只是"不值得细算"的快速否定，
    通过预筛的仍要过严格的近重复判据。
    """
    left_title = left.title or ""
    right_title = right.title or ""
    if len(left_title) < 4 or len(right_title) < 4:
        return False
    pool = set(right_title)
    return sum(1 for ch in set(left_title) if ch in pool) >= 4


def should_merge(
    left: ClusterText,
    right: ClusterText,
    threshold: float = 0.55,
) -> MergeDecision:
    """Decide whether two observations describe the same time-bounded event.

    v1.3：72 小时之外不再无条件切断。超过 72 小时但**互为近重复**的，
    仍判为同一件事的后续报道（`continued_subject`）—— 否则同一件事会被切成
    多个独立事件、各自进账本，"势头"在数据层就断了。
    门槛刻意定得很高（近重复），避免把"话题相近"当成"同一件事"。
    """
    hours_apart = abs((left.observed_at - right.observed_at).total_seconds()) / 3600
    if hours_apart > CONTINUITY_WINDOW_HOURS:
        return MergeDecision(False, 0.0, (), (), "outside_time_window")

    extended = hours_apart > CLUSTER_WINDOW_HOURS
    if extended and not _worth_extended_check(left, right):
        # 便宜的预筛：标题几乎不重合就不必算相似度。
        # 没有这一步，活跃簇数量 × 每个新条目 都要跑一次分词——那是真会被感觉到的卡顿。
        return MergeDecision(False, 0.0, (), (), "outside_time_window")

    score = similarity(left.combined, right.combined)
    shared_numbers = _normalized_numbers(left.combined) & _normalized_numbers(
        right.combined
    )
    shared_entities = _shared_subject_features(left.combined, right.combined)

    if extended:
        merge = score >= CONTINUITY_MIN_SCORE or (
            bool(shared_numbers) and score >= CONTINUITY_NUMBER_SCORE
        )
        reason = "continued_subject" if merge else "outside_time_window"
    elif score >= threshold:
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
