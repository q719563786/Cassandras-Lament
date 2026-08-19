"""前后端契约测试：把"按书施工"的接线修复用机器验收钉死。

- A1：远程 AI 设置表单提交体字段名必须逐一对齐 AiSettingsService.save() 的读取键；
      且前端"留空 token = 不修改已存密钥"的语义必须由 save() 保证。
- A3：前端九档概率必须全部落在后端 ALLOWED_PROBABILITIES 白名单内（双向）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ALLOWED_PROBABILITIES
from yuanjian_app.remote_ai import AiSettingsService


class _MemorySecretStore:
    """不依赖 Windows DPAPI 的内存密钥桩，只验证契约行为。"""

    def __init__(self):
        self._value = ""

    def save(self, token):
        self._value = str(token or "")

    def load(self):
        return self._value


def _db():
    directory = tempfile.TemporaryDirectory()
    database = Database(Path(directory.name) / "yuanjian.db")
    database.initialize()
    return directory, database


class AiSettingsContractTests(unittest.TestCase):
    def test_save_accepts_frontend_contract_keys(self):
        directory, database = _db()
        store = _MemorySecretStore()
        service = AiSettingsService(database, store)
        # 前端提交体（settings.js）：enabled / endpoint / model / token
        result = service.save({
            "enabled": True,
            "endpoint": "https://api.example.com/v1",
            "model": "gpt-4o",
            "token": "secret-123",
        })
        self.assertTrue(result["enabled"])
        self.assertEqual(result["model"], "gpt-4o")
        # runtime_state 落盘 ai_settings 且 enabled=true
        with database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='ai_settings'"
            ).fetchone()
        self.assertIsNotNone(row, "save 后 runtime_state 应有 ai_settings 记录")
        stored = json.loads(row["value_json"])
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["model"], "gpt-4o")
        self.assertEqual(stored["endpoint"], "https://api.example.com/v1")
        # token 只在 secret_store，绝不进 runtime_state
        self.assertNotIn("token", stored)
        self.assertEqual(store.load(), "secret-123")
        directory.cleanup()

    def test_omit_token_keeps_existing_secret(self):
        directory, database = _db()
        store = _MemorySecretStore()
        store.save("existing-secret")
        service = AiSettingsService(database, store)
        # 前端留空 token：save() 不应覆盖已存密钥
        service.save({
            "enabled": True,
            "endpoint": "https://api.openai.com/v1/responses",
            "model": "gpt-4o",
        })
        self.assertEqual(store.load(), "existing-secret")
        directory.cleanup()


class ProbabilityTierContractTests(unittest.TestCase):
    # 前端九档（today.js / calib.js 修改后必须与后端白名单一致）
    FRONTEND_TIERS = [95, 90, 80, 65, 50, 35, 20, 10, 5]

    def test_frontend_tiers_subset_of_backend_whitelist(self):
        self.assertEqual(len(self.FRONTEND_TIERS), 9)
        for tier in self.FRONTEND_TIERS:
            self.assertIn(
                round(tier / 100, 2),
                ALLOWED_PROBABILITIES,
                f"前端档位 {tier}% 不在后端白名单，确认会被 400 拒绝",
            )

    def test_backend_whitelist_expressible_by_frontend(self):
        for probability in ALLOWED_PROBABILITIES:
            self.assertIn(
                round(probability * 100),
                self.FRONTEND_TIERS,
                f"后端白名单 {probability} 无法被前端档位表达",
            )


class GywV2ContractTests(unittest.TestCase):
    """B3 施工契约：gyw v2 九键 schema + 防幻觉互斥规则 + C2 缺陷修复。"""

    def _base_result(self, gyw):
        return {
            "fact_summary": "事件X", "actors": ["A"], "causal_chain": ["a"],
            "uncertainties": ["u"], "horizons": ["h"],
            "probability_low": 0.6, "probability_high": 0.8, "confidence": 0.7,
            "supporting_source_ids": ["s1"], "counter_source_ids": [],
            "up_triggers": ["u"], "down_triggers": ["d"],
            "impact_categories": ["finance"], "gyw": gyw,
        }

    def _base_gyw(self, **overrides):
        g = {
            "stakeholders": "推/阻", "constraints": "c",
            "least_resistance_path": "p", "counter_evidence": "ce",
            "leading_indicators": "li",
            "beneficiaries": [{"subject": "甲", "gain": "g", "evidence_refs": ["s1"]}],
            "cost_bearers": [{"subject": "[推断]乙", "cost": "k", "evidence_refs": []}],
            "historical_parallel": None,
            "observable_signals": ["信号1", "信号2"],
        }
        g.update(overrides)
        return g

    def test_local_template_passes_v2_with_backfilled_keys(self):
        # 本地模板路径必须过 v2 校验（_gyw_for 出口补齐四键）
        from yuanjian_app.judgments import LocalHeuristicProvider, EvidenceBundle, EvidenceItem
        provider = LocalHeuristicProvider()
        bundle = EvidenceBundle(
            "c1", "标题", "摘要", "E2", ("policy",),
            (EvidenceItem("s1", "t", "sum", "d.com", "https://d.com", "2026-01-01"),),
        )
        result = provider.analyze(bundle)
        self.assertEqual(len(result.gyw), 9)
        self.assertEqual(result.gyw["beneficiaries"], [])
        self.assertIsNone(result.gyw["historical_parallel"])
        self.assertGreaterEqual(len(result.gyw["observable_signals"]), 2)

    def test_remote_schema_requires_nine_gyw_keys(self):
        from yuanjian_app.remote_ai import _result_schema
        schema = _result_schema()
        gyw_props = schema["properties"]["gyw"]["properties"]
        for key in ("beneficiaries", "cost_bearers", "historical_parallel", "observable_signals"):
            self.assertIn(key, gyw_props, f"schema 缺 v2 键 {key}")
        required = schema["properties"]["gyw"]["required"]
        self.assertEqual(len(required), 9)
        # historical_parallel 允许 null（type 联合）
        self.assertEqual(
            schema["properties"]["gyw"]["properties"]["historical_parallel"]["type"],
            ["string", "null"],
        )

    def test_historical_parallel_normalization_writes_back(self):
        """C2 缺陷修复钉死：空串/纯空白必须归一化为 None 并写回 gyw 字典，
        不能只改局部变量。三场景验证。"""
        from yuanjian_app.judgments import validate_judgment
        allowed = {"s1", "s2"}
        # 场景1: 空字符串 → None
        g1 = self._base_gyw(historical_parallel="")
        r1 = validate_judgment(self._base_result(g1), allowed)
        self.assertIsNone(r1.gyw["historical_parallel"], "空串必须归一化为 None 写回")
        # 场景2: 纯空白字符串 → None
        g2 = self._base_gyw(historical_parallel="   ")
        r2 = validate_judgment(self._base_result(g2), allowed)
        self.assertIsNone(r2.gyw["historical_parallel"], "纯空白必须归一化为 None 写回")
        # 场景3: 正常值 → strip 后保留
        g3 = self._base_gyw(historical_parallel="  2015降息周期  ")
        r3 = validate_judgment(self._base_result(g3), allowed)
        self.assertEqual(r3.gyw["historical_parallel"], "2015降息周期", "正常值应 strip 后写回")
        # 场景4: 显式 None → None
        g4 = self._base_gyw(historical_parallel=None)
        r4 = validate_judgment(self._base_result(g4), allowed)
        self.assertIsNone(r4.gyw["historical_parallel"])

    def test_anti_hallucination_rejects_unref_without_inferred_tag(self):
        from yuanjian_app.judgments import validate_judgment, InvalidJudgmentError
        allowed = {"s1", "s2"}
        # 无引用但主体没标 [推断] → 拒绝
        bad = self._base_gyw(cost_bearers=[{"subject": "乙", "cost": "k", "evidence_refs": []}])
        with self.assertRaises(InvalidJudgmentError):
            validate_judgment(self._base_result(bad), allowed)

    def test_anti_hallucination_rejects_out_of_bundle_refs(self):
        from yuanjian_app.judgments import validate_judgment, InvalidJudgmentError
        allowed = {"s1", "s2"}
        # 引用证据包外来源 → 拒绝
        bad = self._base_gyw(beneficiaries=[{"subject": "甲", "gain": "g", "evidence_refs": ["zzz"]}])
        with self.assertRaises(InvalidJudgmentError):
            validate_judgment(self._base_result(bad), allowed)

    def test_anti_hallucination_rejects_inferred_with_refs(self):
        from yuanjian_app.judgments import validate_judgment, InvalidJudgmentError
        allowed = {"s1", "s2"}
        # 标了 [推断] 却又带引用 → 拒绝（矛盾）
        bad = self._base_gyw(beneficiaries=[{"subject": "[推断]甲", "gain": "g", "evidence_refs": ["s1"]}])
        with self.assertRaises(InvalidJudgmentError):
            validate_judgment(self._base_result(bad), allowed)


class SourceBadgeContractTests(unittest.TestCase):
    """C3 标注：前端 sourceBadge 必须由 judgments.provider 驱动，三态对应。"""

    def test_source_badge_three_states(self):
        from pathlib import Path
        static = Path(__file__).resolve().parent.parent / "src" / "yuanjian_app" / "static"
        ui_core = (static / "js" / "ui_core.js").read_text(encoding="utf-8")
        # 结构性断言：函数存在且三个 tone 分支都在
        self.assertIn("export function sourceBadge", ui_core)
        self.assertIn("tone: 'remote'", ui_core)
        self.assertIn("tone: 'template'", ui_core)
        self.assertIn("tone: 'muted'", ui_core)
        # today.js 必须调用 sourceBadge（不再自写来源文案）
        today = (static / "js" / "views" / "today.js").read_text(encoding="utf-8")
        self.assertIn("sourceBadge", today)
        self.assertNotIn("'后端 judgment 引擎'", today, "旧脱钩文案应已移除")
        self.assertNotIn("'UI 兜底模板'", today, "旧脱钩文案应已移除")
        # CSS 三 tone 徽标
        css = (static / "css" / "components.css").read_text(encoding="utf-8")
        for tone in ("remote", "template", "muted"):
            self.assertIn(f"judgment-{tone}", css, f"CSS 缺 judgment-{tone} 徽标样式")


if __name__ == "__main__":
    unittest.main()
