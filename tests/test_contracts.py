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


if __name__ == "__main__":
    unittest.main()
