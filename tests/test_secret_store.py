"""DpapiSecretStore 与 DPAPI 底层封装的对抗性行为测试。

覆盖目标：`src/yuanjian_app/secret_store.py` 行覆盖率 ≥85%（基线 48%）。

测试纪律：
- 只改测试，不改 src。
- DPAPI 真实往返在 Windows 上必须真跑（不 mock），否则等于没测。
- 失败路径用注入的假 `windll` / 假 `unprotect` 触发，断言异常类型与文案，
  不为了让测试通过而放宽断言。
"""

import ctypes
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from yuanjian_app.secret_store import (
    _HEADER,
    DpapiSecretStore,
    _blob,
    _dpapi_protect,
    _dpapi_unprotect,
)

IS_WINDOWS = os.name == "nt"
WINDOWS_ONLY = unittest.skipUnless(IS_WINDOWS, "DPAPI 只在 Windows 上可用")


def _xor(value: bytes) -> bytes:
    """可逆的假加密：保证磁盘上不出现明文，且往返一致。"""
    return bytes(byte ^ 0xA5 for byte in value)


def _make_store(path, **overrides):
    kwargs = {"protect": _xor, "unprotect": _xor}
    kwargs.update(overrides)
    return DpapiSecretStore(path, **kwargs)


def _fake_windll(protect_result=1, unprotect_result=1):
    """构造假的 ctypes.windll，用来驱赶 Crypt*Data 的失败分支。

    返回 0 表示 Win32 调用失败 —— 真实 DPAPI 几乎无法稳定复现这个场景，
    所以必须靠注入。
    """
    return types.SimpleNamespace(
        crypt32=types.SimpleNamespace(
            CryptProtectData=mock.Mock(return_value=protect_result),
            CryptUnprotectData=mock.Mock(return_value=unprotect_result),
        ),
        kernel32=types.SimpleNamespace(LocalFree=mock.Mock(return_value=1)),
    )


class DpapiPrimitiveTests(unittest.TestCase):
    """底层 `_dpapi_*` 与 `_blob` 的直接测试。"""

    @WINDOWS_ONLY
    def test_real_dpapi_round_trip(self):
        """真实 DPAPI 往返：密文不含明文，解回来必须逐字节一致。"""
        # 刻意不用形似真实凭据的字符串：tools/privacy_scan.py 会把
        # "sk-" + 20 位以上字符合法的串判为泄漏，导致发布闸门自检失败。
        payload = b"unit-test-dpapi-payload-0123456789"

        protected = _dpapi_protect(payload)

        self.assertIsInstance(protected, bytes)
        self.assertTrue(protected, "CryptProtectData 返回了空密文")
        self.assertNotEqual(protected, payload)
        self.assertNotIn(payload, protected)
        self.assertEqual(_dpapi_unprotect(protected), payload)

    @WINDOWS_ONLY
    def test_real_dpapi_round_trip_handles_empty_payload(self):
        self.assertEqual(_dpapi_unprotect(_dpapi_protect(b"")), b"")

    @WINDOWS_ONLY
    def test_real_dpapi_rejects_tampered_ciphertext(self):
        """篡改密文必须被 DPAPI 拒绝，不能静默返回垃圾。"""
        protected = _dpapi_protect(b"tamper-me")
        broken = bytes([protected[0] ^ 0xFF]) + protected[1:]
        with self.assertRaises(OSError):
            _dpapi_unprotect(broken)

    def test_non_windows_branches_raise_runtime_error(self):
        """os.name != "nt" 时两个函数都必须立刻抛 RuntimeError。"""
        with mock.patch.object(os, "name", "posix"):
            with self.assertRaises(RuntimeError) as protect_ctx:
                _dpapi_protect(b"payload")
            self.assertEqual(str(protect_ctx.exception), "DPAPI只在Windows可用")

            with self.assertRaises(RuntimeError) as unprotect_ctx:
                _dpapi_unprotect(b"payload")
            self.assertEqual(str(unprotect_ctx.exception), "DPAPI只在Windows可用")

    @WINDOWS_ONLY
    def test_protect_failure_raises_win_error(self):
        fake = _fake_windll(protect_result=0)
        with mock.patch.object(ctypes, "windll", fake, create=True):
            with self.assertRaises(OSError):
                _dpapi_protect(b"payload")
        fake.crypt32.CryptProtectData.assert_called_once()

    @WINDOWS_ONLY
    def test_unprotect_failure_raises_win_error(self):
        fake = _fake_windll(unprotect_result=0)
        with mock.patch.object(ctypes, "windll", fake, create=True):
            with self.assertRaises(OSError):
                _dpapi_unprotect(b"payload")
        fake.crypt32.CryptUnprotectData.assert_called_once()

    def test_blob_wraps_bytes_with_length(self):
        value, buffer = _blob(b"abcd")

        self.assertEqual(value.cbData, 4)
        self.assertTrue(buffer)
        self.assertEqual(ctypes.string_at(value.pbData, value.cbData), b"abcd")


class SecretStoreTests(unittest.TestCase):
    """`DpapiSecretStore` 落盘、读取、清理的行为测试。"""

    def test_encrypted_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secrets" / "ai-token.dpapi"
            store = DpapiSecretStore(
                path,
                protect=lambda value: bytes(byte ^ 0xA5 for byte in value),
                unprotect=lambda value: bytes(byte ^ 0xA5 for byte in value),
            )

            store.save("plain-secret-token")

            self.assertNotIn(b"plain-secret-token", path.read_bytes())
            self.assertEqual(store.load(), "plain-secret-token")
            store.clear()
            self.assertEqual(store.load(), "")
            self.assertFalse(path.exists())

    def test_empty_token_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = DpapiSecretStore(path, protect=lambda value: value, unprotect=lambda value: value)

            store.save("   ")

            self.assertFalse(path.exists())

    def test_save_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deep" / "nested" / "ai-token.dpapi"
            store = _make_store(path)
            self.assertFalse(path.parent.exists())

            store.save("token-value")

            self.assertTrue(path.parent.is_dir())
            self.assertEqual(store.load(), "token-value")

    def test_save_writes_header_before_ciphertext(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            store.save("token-value")

            payload = path.read_bytes()
            self.assertTrue(payload.startswith(_HEADER))
            self.assertEqual(payload, _HEADER + _xor(b"token-value"))

    def test_save_uses_tmp_then_atomic_replace(self):
        """写入必须经过 <path>.tmp + os.replace，且不留下 .tmp 残留。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)
            seen = {}
            real_replace = os.replace

            def spying_replace(source, destination):
                seen["source"] = str(source)
                seen["destination"] = str(destination)
                seen["tmp_existed_at_replace"] = Path(source).exists()
                seen["target_existed_at_replace"] = Path(destination).exists()
                return real_replace(source, destination)

            with mock.patch("yuanjian_app.secret_store.os.replace", side_effect=spying_replace):
                store.save("token-value")

            self.assertEqual(seen["source"], str(path) + ".tmp")
            self.assertEqual(seen["destination"], str(path))
            self.assertTrue(seen["tmp_existed_at_replace"])
            self.assertFalse(seen["target_existed_at_replace"])
            self.assertFalse(Path(seen["source"]).exists(), "残留了 .tmp 文件")
            self.assertEqual(
                sorted(item.name for item in Path(temporary).iterdir()),
                ["ai-token.dpapi"],
            )

    def test_save_overwrites_previous_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            store.save("first-token")
            store.save("second-token")

            self.assertEqual(store.load(), "second-token")
            self.assertNotIn(b"first-token", path.read_bytes())

    def test_save_strips_surrounding_whitespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            store.save("  padded-token\n")

            self.assertEqual(store.load(), "padded-token")

    def test_save_blank_variants_clear_existing_secret(self):
        """save("") / save("   ") 走 clear() 分支：不写文件，并抹掉旧密钥。"""
        for blank in ("", "   ", "\t\n "):
            with self.subTest(blank=repr(blank)):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "ai-token.dpapi"
                    store = _make_store(path)
                    store.save("existing-token")
                    self.assertTrue(path.exists())

                    store.save(blank)

                    self.assertFalse(path.exists(), "空白 token 不应留下密钥文件")
                    self.assertEqual(store.load(), "")

    def test_blank_save_does_not_create_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing" / "ai-token.dpapi"
            store = _make_store(path)

            store.save("   ")

            self.assertFalse(path.parent.exists())

    def test_clear_is_idempotent_on_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            store.clear()
            store.clear()

            self.assertFalse(path.exists())
            self.assertEqual(store.load(), "")

    def test_load_missing_file_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            self.assertEqual(store.load(), "")

    def test_load_rejects_missing_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)
            path.write_bytes(_xor(b"plain-token"))

            with self.assertRaises(ValueError) as ctx:
                store.load()
            self.assertEqual(str(ctx.exception), "密钥文件格式无效")

    def test_load_rejects_empty_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)
            path.write_bytes(b"")

            with self.assertRaises(ValueError) as ctx:
                store.load()
            self.assertEqual(str(ctx.exception), "密钥文件格式无效")

    def test_load_rejects_truncated_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)
            path.write_bytes(_HEADER[:4])

            with self.assertRaises(ValueError):
                store.load()

    def test_load_decodes_utf8_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = _make_store(path)

            store.save("令牌-密钥-测试")

            self.assertEqual(store.load(), "令牌-密钥-测试")

    def test_load_strips_header_before_calling_unprotect(self):
        """unprotect 必须只收到去掉文件头的密文。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            received = []

            def spy_unprotect(value):
                received.append(value)
                return _xor(value)

            store = DpapiSecretStore(path, protect=_xor, unprotect=spy_unprotect)
            store.save("token-value")

            self.assertEqual(store.load(), "token-value")
            self.assertEqual(received, [_xor(b"token-value")])

    @WINDOWS_ONLY
    def test_default_protect_and_unprotect_are_real_dpapi(self):
        """不注入时，store 必须绑定真实的 DPAPI 函数。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai-token.dpapi"
            store = DpapiSecretStore(path)

            self.assertIs(store.protect, _dpapi_protect)
            self.assertIs(store.unprotect, _dpapi_unprotect)

            store.save("real-token-on-disk")

            self.assertNotIn(b"real-token-on-disk", path.read_bytes())
            self.assertEqual(store.load(), "real-token-on-disk")

    def test_path_is_normalized_to_pathlib(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = os.path.join(temporary, "ai-token.dpapi")
            store = _make_store(raw)

            self.assertIsInstance(store.path, Path)
            self.assertEqual(store.path, Path(raw))


if __name__ == "__main__":
    unittest.main()
