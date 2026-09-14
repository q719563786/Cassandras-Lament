"""Privacy-bounded evidence bundles and structured judgment contracts.

本模块是一次职责拆分的**门面**（2026-09-14）：原先 1247 行的实现按职责拆到
四个模块，这里把它们重新导出，保证 `from yuanjian_app.judgments import ...`
这条既有路径一字不变地继续可用。

- `judgment_models`     —— 常量、异常、证据包/研判结果的数据类型与 provider 协议
- `judgment_bundle`     —— 构造交给 provider 的隐私有界证据包
- `judgment_validation` —— 严格 schema 校验与尽力修复
- `judgment_local`      —— 离线启发式 provider（兜底，永不读取个人利益）

拆分的动机是可维护性：一个 1200 行的文件里同时住着"数据类型""隐私裁剪""schema
校验""推演模板"，改动任何一处都要在四种关注点之间来回跳。
"""

from __future__ import annotations

from .judgment_bundle import build_public_bundle
from .judgment_local import LocalHeuristicProvider
from .judgment_models import (
    ALLOWED_IMPACT_CATEGORIES,
    EvidenceBundle,
    EvidenceItem,
    InvalidJudgmentError,
    JudgmentProvider,
    JudgmentResult,
    MAX_BUNDLE_CHARACTERS,
    MAX_EVIDENCE_SOURCES,
    SYSTEM_INSTRUCTION,
)
from .judgment_validation import repair_judgment, validate_judgment

# 拆分前这些私有名也能从 `yuanjian_app.judgments` 直接取到。不放进 `__all__`
# （下划线即"不对外承诺"），但保持可达，避免任何既有引用被静默打断。
from .judgment_bundle import (  # noqa: F401
    _public_text,
    _public_url,
    _serialized_length,
)
from .judgment_validation import (  # noqa: F401
    _GYW_FIELDS,
    _GYW_LEGACY_STRING_FIELDS,
    _LIST_FIELDS,
    _RESULT_FIELDS,
)

__all__ = [
    "MAX_BUNDLE_CHARACTERS",
    "MAX_EVIDENCE_SOURCES",
    "SYSTEM_INSTRUCTION",
    "ALLOWED_IMPACT_CATEGORIES",
    "InvalidJudgmentError",
    "EvidenceItem",
    "EvidenceBundle",
    "JudgmentResult",
    "JudgmentProvider",
    "build_public_bundle",
    "validate_judgment",
    "repair_judgment",
    "LocalHeuristicProvider",
]
