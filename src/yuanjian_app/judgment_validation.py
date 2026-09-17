"""Strict schema validation and best-effort repair for provider output."""

from __future__ import annotations

from dataclasses import replace

from .judgment_models import (
    ALLOWED_IMPACT_CATEGORIES,
    InvalidJudgmentError,
    JudgmentResult,
)


_RESULT_FIELDS = frozenset(
    {
        "fact_summary",
        "actors",
        "causal_chain",
        "uncertainties",
        "horizons",
        "probability_low",
        "probability_high",
        "confidence",
        "supporting_source_ids",
        "counter_source_ids",
        "up_triggers",
        "down_triggers",
        "impact_categories",
        # GYW framework (《登高望远》GYW-005/006/007/009/010/012):
        # every worthwhile prediction must expose stakeholders, constraints,
        # least-resistance path, counter-evidence, and leading indicators.
        # Stored as a nested dict so the top-level schema stays clean and
        # existing consumers (risk_dashboard, calibration) are untouched.
        "gyw",
        # 对用户本人的相关性结论与行动方向（一句话，直接对用户说）
        "personal_action",
    }
)
_LIST_FIELDS = (_RESULT_FIELDS - {
    "fact_summary",
    "probability_low",
    "probability_high",
    "confidence",
    "gyw",
    "personal_action",
})

# Required keys inside the gyw sub-structure (v2: 9 keys).
# Five legacy string keys map directly to claims in the 登高望远 method file;
# four new keys carry structured stakeholder/indicator analysis (稿C v2).
_GYW_LEGACY_STRING_FIELDS = frozenset(
    {
        "stakeholders",         # GYW-005 权力规则：谁推动 / 谁否决
        "constraints",          # GYW-006 经济规则：资源/债务/现金流约束
        "least_resistance_path",  # GYW-007 博弈规则：最小阻力路径
        "counter_evidence",     # GYW-013 认知风险：反对证据 / 替代假设
        "leading_indicators",   # GYW-010 领先指标：出现即要警觉
    }
)
_GYW_FIELDS = _GYW_LEGACY_STRING_FIELDS | frozenset(
    {
        "beneficiaries",        # 稿C v2：获利方 {主体, 获利方式, evidence_refs}
        "cost_bearers",         # 稿C v2：承担方 {主体, 承担方式, evidence_refs}
        "historical_parallel",  # 稿C v2：历史押韵，可为 null
        "observable_signals",   # 稿C v2：可观测领先指标数组
    }
)


def validate_judgment(result: dict, allowed_source_ids: set[str]) -> JudgmentResult:
    if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
        raise InvalidJudgmentError("研判字段不完整或包含未知字段")
    if not isinstance(result["fact_summary"], str) or not result["fact_summary"].strip():
        raise InvalidJudgmentError("事实摘要无效")
    # GYW sub-structure v2 (稿C): 9 keys. Five legacy keys are non-empty
    # strings; four new keys carry structured stakeholder/indicator data.
    # All normalization is written into normalized_gyw so the JudgmentResult
    # stores canonical values (empty-string → None for historical_parallel,
    # stripped strings everywhere else).
    gyw = result.get("gyw")
    if not isinstance(gyw, dict) or set(gyw) != _GYW_FIELDS:
        raise InvalidJudgmentError("GYW 框架字段不完整或包含未知字段")
    normalized_gyw: dict = {}
    # Legacy five: non-empty strings, strip whitespace.
    for key in _GYW_LEGACY_STRING_FIELDS:
        value = gyw[key]
        if not isinstance(value, str) or not value.strip():
            raise InvalidJudgmentError(f"GYW {key} 必须是非空字符串")
        normalized_gyw[key] = value.strip()
    # beneficiaries / cost_bearers: arrays of {subject, gain|cost, evidence_refs}.
    # Anti-hallucination (稿C 双保险的服务端半边):
    #   - refs 非空 → 必须 ⊆ allowed_source_ids 且主体不得带 [推断]
    #   - refs 为空 → 主体必须带 [推断] 前缀
    for field_name in ("beneficiaries", "cost_bearers"):
        mode_key = "gain" if field_name == "beneficiaries" else "cost"
        value = gyw[field_name]
        if not isinstance(value, list):
            raise InvalidJudgmentError(f"GYW {field_name} 必须是数组")
        entries: list[dict] = []
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != {"subject", mode_key, "evidence_refs"}:
                raise InvalidJudgmentError(f"GYW {field_name} 条目字段不完整或包含未知字段")
            subject = entry["subject"]
            if not isinstance(subject, str) or not subject.strip():
                raise InvalidJudgmentError(f"GYW {field_name} 主体必须是非空字符串")
            mode_value = entry[mode_key]
            if not isinstance(mode_value, str) or not mode_value.strip():
                raise InvalidJudgmentError(f"GYW {field_name} {mode_key} 必须是非空字符串")
            refs = entry["evidence_refs"]
            if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                raise InvalidJudgmentError(f"GYW {field_name} evidence_refs 必须是字符串数组")
            inferred = subject.strip().startswith("[推断]")
            if refs:
                if not set(refs).issubset(allowed_source_ids):
                    raise InvalidJudgmentError(f"GYW {field_name} 引用了证据包之外的来源")
                if inferred:
                    raise InvalidJudgmentError(f"GYW {field_name} 标了[推断]却又带引用，矛盾")
            else:
                if not inferred:
                    raise InvalidJudgmentError(f"GYW {field_name} 无引用主体必须加[推断]前缀")
            entries.append({
                "subject": subject.strip(),
                mode_key: mode_value.strip(),
                "evidence_refs": list(refs),
            })
        normalized_gyw[field_name] = entries
    # historical_parallel: string or null.
    # CRITICAL FIX (稿C v1 review): 空串/纯空白必须归一化为 None 并写回字典，
    # 不能只改局部变量。`gyw["historical_parallel"] = ...` 确保归一化生效。
    hp = gyw["historical_parallel"]
    if hp is None:
        normalized_gyw["historical_parallel"] = None
    else:
        if not isinstance(hp, str):
            raise InvalidJudgmentError("GYW historical_parallel 必须是字符串或 null")
        normalized_gyw["historical_parallel"] = hp.strip() or None
    # observable_signals: 2-8 non-empty strings.
    signals = gyw["observable_signals"]
    if not isinstance(signals, list) or not (2 <= len(signals) <= 8):
        raise InvalidJudgmentError("GYW observable_signals 必须是 2 到 8 条的数组")
    if not all(isinstance(s, str) and s.strip() for s in signals):
        raise InvalidJudgmentError("GYW observable_signals 每条必须是非空字符串")
    normalized_gyw["observable_signals"] = [s.strip() for s in signals]
    for field in _LIST_FIELDS:
        if not isinstance(result[field], list) or not all(
            isinstance(value, str) for value in result[field]
        ):
            raise InvalidJudgmentError(f"{field}必须是字符串数组")
    numeric = {}
    for field in ("probability_low", "probability_high", "confidence"):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise InvalidJudgmentError(f"{field}必须在0到1之间")
        numeric[field] = float(value)
    if numeric["probability_low"] > numeric["probability_high"]:
        raise InvalidJudgmentError("概率下界不能高于上界")
    cited = set(result["supporting_source_ids"]) | set(result["counter_source_ids"])
    if not cited.issubset(set(allowed_source_ids)):
        raise InvalidJudgmentError("研判引用了证据包之外的来源")
    if not set(result["impact_categories"]).issubset(ALLOWED_IMPACT_CATEGORIES):
        raise InvalidJudgmentError("研判包含未知影响类别")
    pa = result.get("personal_action")
    if not isinstance(pa, str) or not pa.strip():
        raise InvalidJudgmentError("personal_action 必须是非空字符串")
    personal_action = " ".join(pa.split())[:400]
    return JudgmentResult(
        fact_summary=result["fact_summary"].strip(),
        actors=tuple(result["actors"]),
        causal_chain=tuple(result["causal_chain"]),
        uncertainties=tuple(result["uncertainties"]),
        horizons=tuple(result["horizons"]),
        probability_low=numeric["probability_low"],
        probability_high=numeric["probability_high"],
        confidence=numeric["confidence"],
        supporting_source_ids=tuple(result["supporting_source_ids"]),
        counter_source_ids=tuple(result["counter_source_ids"]),
        up_triggers=tuple(result["up_triggers"]),
        down_triggers=tuple(result["down_triggers"]),
        impact_categories=tuple(result["impact_categories"]),
        gyw=normalized_gyw,
        personal_action=personal_action,
    )


def repair_judgment(raw: dict, allowed_source_ids: set[str]) -> JudgmentResult | None:
    """尝试修复远程 AI 输出中常见的格式问题，修复成功则返回合法 JudgmentResult。

    稿D：远程 AI 严格校验失败时，先尝试格式修复再降级 local，降低无谓降级率。
    仅修复格式问题（缺字段、类型错、数组长度不对），不篡改内容语义。
    修复后仍无法通过 validate_judgment 时返回 None，由调用方降级 local。
    """
    if not isinstance(raw, dict):
        return None

    # 本函数要记住「有没有用默认值顶过 AI 的缺失」。归一化（去空格/截断/清引用）
    # **不算**降级 —— 只有"AI 没给出来"才算，否则几乎每条都会变成 degraded。
    substituted = False

    # 1. 顶层字段修复
    top_defaults = {
        "fact_summary": "远程AI输出事实摘要缺失，已由本地修复填充",
        "actors": [],
        "causal_chain": ["公开事件出现", "相关主体可能调整行为", "影响逐步传导至相关领域"],
        "uncertainties": ["远程AI输出不完整，部分字段由本地修复填充"],
        "horizons": ["未来30天", "未来90天"],
        "probability_low": 0.3,
        "probability_high": 0.7,
        "confidence": 0.5,
        "supporting_source_ids": [],
        "counter_source_ids": [],
        "up_triggers": ["出现正式文件或新增独立来源"],
        "down_triggers": ["权威来源否认或关键数字被修正"],
        "impact_categories": ["general"],
        "gyw": {},
        "personal_action": "结合你的个人情况评估影响路径，暂按保守策略处理，等待更多信息。",
    }
    repaired: dict = {}
    for key, default in top_defaults.items():
        if key not in raw:
            # ⚠ 这一行是必需的： 在键**缺失**时直接返回默认值，
            # 于是下面那些"类型不对/为空 → 用默认值顶上"的分支根本不会走到，
            # substituted 也就永远置不上。必须在这里单独判一次。
            substituted = True
        value = raw.get(key, default)
        if key in ("fact_summary", "personal_action"):
            if not isinstance(value, str) or not value.strip():
                value = default
                substituted = True
            else:
                value = " ".join(str(value).split())[:400]
        elif key in _LIST_FIELDS:
            if not isinstance(value, list):
                value = list(default)
                substituted = True
            else:
                value = [str(v) for v in value if v is not None]
        elif key in ("probability_low", "probability_high", "confidence"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = default
                substituted = True
            else:
                value = max(0.0, min(1.0, float(value)))
        elif key == "impact_categories":
            if not isinstance(value, list):
                value = ["general"]
            else:
                value = [str(v) for v in value if str(v) in ALLOWED_IMPACT_CATEGORIES]
                if not value:
                    value = ["general"]
        elif key == "gyw" and not isinstance(value, dict):
            value = {}
        repaired[key] = value
    if repaired["probability_low"] > repaired["probability_high"]:
        repaired["probability_low"], repaired["probability_high"] = (
            repaired["probability_high"],
            repaired["probability_low"],
        )

    # 2. GYW 子结构修复
    gyw = repaired["gyw"]
    gyw_defaults = {
        "stakeholders": "远程AI输出利益相关方分析缺失",
        "constraints": "远程AI输出约束分析缺失",
        "least_resistance_path": "远程AI输出最小阻力路径分析缺失",
        "counter_evidence": "远程AI输出反对证据分析缺失",
        "leading_indicators": "远程AI输出领先指标分析缺失",
        "beneficiaries": [],
        "cost_bearers": [],
        "historical_parallel": None,
        "observable_signals": ["后续官方公告", "执行进展通报"],
    }
    repaired_gyw: dict = {}
    for key, default in gyw_defaults.items():
        if key not in gyw:
            substituted = True   # 同上：键缺失时 .get 直接给默认值，不走替换分支
        value = gyw.get(key, default)
        if key in _GYW_LEGACY_STRING_FIELDS:
            if not isinstance(value, str) or not value.strip():
                value = default
                substituted = True
            repaired_gyw[key] = value.strip()
        elif key in ("beneficiaries", "cost_bearers"):
            mode_key = "gain" if key == "beneficiaries" else "cost"
            if not isinstance(value, list):
                value = []
            valid_entries = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                subject = entry.get("subject")
                mode_value = entry.get(mode_key)
                refs = entry.get("evidence_refs", [])
                if not isinstance(subject, str) or not subject.strip():
                    continue
                if not isinstance(mode_value, str) or not mode_value.strip():
                    continue
                if not isinstance(refs, list):
                    refs = []
                valid_refs = [r for r in refs if isinstance(r, str) and r in allowed_source_ids]
                subject_str = subject.strip()
                if valid_refs and subject_str.startswith("[推断]"):
                    subject_str = subject_str[len("[推断]"):].strip()
                if not valid_refs and not subject_str.startswith("[推断]"):
                    subject_str = f"[推断]{subject_str}"
                valid_entries.append({
                    "subject": subject_str,
                    mode_key: mode_value.strip(),
                    "evidence_refs": valid_refs,
                })
            repaired_gyw[key] = valid_entries[:5]
        elif key == "historical_parallel":
            if value is None:
                repaired_gyw[key] = None
            elif isinstance(value, str):
                repaired_gyw[key] = value.strip() or None
            else:
                repaired_gyw[key] = None
        elif key == "observable_signals":
            if not isinstance(value, list):
                value = list(default)
            signals = [str(s).strip() for s in value if isinstance(s, str) and s.strip()]
            if len(signals) < 2:
                signals = (signals + ["后续官方公告", "执行进展通报"])[:2]
                # 占位信号 —— 直白说：这两个词永远不会错，因此无法证伪。
                # 落到这里说明 AI 根本没给出可观测的信号。
                substituted = True
            repaired_gyw[key] = signals[:8]
    repaired["gyw"] = repaired_gyw

    # 3. source_ids 过滤
    for key in ("supporting_source_ids", "counter_source_ids"):
        repaired[key] = [s for s in repaired[key] if s in allowed_source_ids]

    # 4. 尝试通过严格校验；失败时构造最小有效研判（永不返回None，避免免费AI被无谓降级）
    try:
        result = validate_judgment(repaired, allowed_source_ids)
        return replace(
            result,
            analysis_status="degraded" if substituted else "real",
        )
    except InvalidJudgmentError:
        # 兜底：从已修复数据中提取文本字段，构造一个保证通过校验的最小有效研判
        safe_fact = str(repaired.get("fact_summary") or "远程AI研判已生成，详情请查看事件原文").strip()
        if not safe_fact:
            safe_fact = "远程AI研判已生成，详情请查看事件原文"
        fallback = {
            "fact_summary": safe_fact[:2000],
            "actors": [str(a) for a in (repaired.get("actors") or []) if isinstance(a, str) and a.strip()][:10],
            "causal_chain": [str(c) for c in (repaired.get("causal_chain") or []) if isinstance(c, str) and c.strip()][:5] or ["事件发生", "影响传导"],
            "uncertainties": [str(u) for u in (repaired.get("uncertainties") or []) if isinstance(u, str) and u.strip()][:5] or ["信息有限"],
            "horizons": [str(h) for h in (repaired.get("horizons") or []) if isinstance(h, str) and h.strip()][:3] or ["未来30天"],
            "probability_low": max(0.0, min(1.0, float(repaired.get("probability_low", 0.3)))),
            "probability_high": max(0.0, min(1.0, float(repaired.get("probability_high", 0.7)))),
            "confidence": max(0.0, min(1.0, float(repaired.get("confidence", 0.5)))),
            "supporting_source_ids": [],
            "counter_source_ids": [],
            "up_triggers": [str(t) for t in (repaired.get("up_triggers") or []) if isinstance(t, str) and t.strip()][:3] or ["官方确认"],
            "down_triggers": [str(t) for t in (repaired.get("down_triggers") or []) if isinstance(t, str) and t.strip()][:3] or ["官方否认"],
            "impact_categories": [c for c in (repaired.get("impact_categories") or []) if c in ALLOWED_IMPACT_CATEGORIES] or ["general"],
            "personal_action": (str(repaired.get("personal_action") or "").strip()[:400]
                                or "结合你的个人情况评估影响路径，暂按保守策略处理。"),
            "gyw": {
                "stakeholders": str((repaired.get("gyw") or {}).get("stakeholders") or "利益相关方分析待补充")[:1000],
                "constraints": str((repaired.get("gyw") or {}).get("constraints") or "约束条件分析待补充")[:1000],
                "least_resistance_path": str((repaired.get("gyw") or {}).get("least_resistance_path") or "最小阻力路径待补充")[:1000],
                "counter_evidence": str((repaired.get("gyw") or {}).get("counter_evidence") or "反对证据待补充")[:1000],
                "leading_indicators": str((repaired.get("gyw") or {}).get("leading_indicators") or "领先指标待补充")[:1000],
                "beneficiaries": [],
                "cost_bearers": [],
                "historical_parallel": None,
                "observable_signals": ["后续官方公告", "执行进展通报"],
            },
        }
        if fallback["probability_low"] > fallback["probability_high"]:
            fallback["probability_low"], fallback["probability_high"] = fallback["probability_high"], fallback["probability_low"]
        # 关键修复：必须返回 JudgmentResult 而非裸 dict——队列持久化时要调 .to_dict()，
        # 返回 dict 会让整个 run_due 工作循环抛 AttributeError 中断。
        try:
            # 最小兜底：内容实质为空，只是为了让流程不中断。**必须标出来** ——
            # 否则它会以「一份完整的六步分析」的样子出现在界面上。
            return replace(
                validate_judgment(fallback, allowed_source_ids),
                analysis_status="placeholder",
            )
        except InvalidJudgmentError:
            # 理论不可达（fallback 已严格按 schema 构造）；再兜一层绝对最小合法研判
            minimal = {
                "fact_summary": safe_fact[:2000] or "远程AI研判已生成",
                "actors": [], "causal_chain": ["事件发生", "影响逐步传导"],
                "uncertainties": ["公开信息仍需后续确认"],
                "horizons": ["未来30天"],
                "probability_low": 0.3, "probability_high": 0.7, "confidence": 0.5,
                "supporting_source_ids": [], "counter_source_ids": [],
                "up_triggers": ["出现正式文件或新增独立来源"],
                "down_triggers": ["权威来源否认或关键数字被修正"],
                "impact_categories": ["general"],
                "personal_action": str(fallback.get("personal_action") or
                                       "结合你的个人情况评估影响路径，暂按保守策略处理。")[:400],
                "gyw": {
                    "stakeholders": "利益相关方分析待补充",
                    "constraints": "约束条件分析待补充",
                    "least_resistance_path": "最小阻力路径待补充",
                    "counter_evidence": "反对证据待补充",
                    "leading_indicators": "领先指标待补充",
                    "beneficiaries": [], "cost_bearers": [],
                    "historical_parallel": None,
                    "observable_signals": ["后续官方公告", "执行进展通报"],
                },
            }
            return replace(
                validate_judgment(minimal, allowed_source_ids),
                analysis_status="placeholder",
            )
