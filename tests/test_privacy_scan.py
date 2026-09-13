import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from privacy_scan import scan_committed, scan_tree

PROJECT = Path(__file__).resolve().parents[1]

# 下面两个路径刻意用拼接而不是字面量写出。原因：这些测试文件本身会被
# `privacy_scan.py --committed` 扫到，如果这里直接写下 Windows 绝对路径的
# 字面形式，闸门就会报出测试文件自身，形成"检测规则把检测工具的测试判为泄漏"
# 的自指问题。拼接后源码里不存在连续的可匹配子串，但运行期字符串是真的。
BACKSLASH_PATH = "C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "a.txt"
FORWARD_SLASH_PATH = "C:" + "/" + "Users" + "/" + "someone" + "/a.txt"


class PrivacyScanTests(unittest.TestCase):
    def test_scanner_blocks_private_database_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "forecast.db").write_bytes(b"private")

            report = scan_tree(root)

            self.assertFalse(report.safe)
            self.assertEqual(report.blocked_files, ["forecast.db"])

    def test_scanner_accepts_sanitized_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "README.md").write_text("虚构示例数据", encoding="utf-8")

            report = scan_tree(root)

            self.assertTrue(report.safe)

    def test_scanner_catches_backslash_absolute_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "plan.md").write_text(f"修改 {BACKSLASH_PATH}", encoding="utf-8")

            report = scan_tree(root)

            self.assertFalse(report.safe)
            self.assertEqual(report.findings, ["plan.md:敏感内容模式"])

    def test_scanner_catches_forward_slash_absolute_path(self):
        """回归钉死：正斜杠形式此前不受检测，导致真实泄漏通过闸门。

        历史事故：docs 里出现过正斜杠写法的本机绝对路径，当时的规则只匹配
        反斜杠，于是扫描报告 safe=True，路径却被发布了出去。
        两种分隔符必须都被拦住。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "plan.md").write_text(f"修改 {FORWARD_SLASH_PATH}", encoding="utf-8")

            report = scan_tree(root)

            self.assertFalse(report.safe)
            self.assertEqual(report.findings, ["plan.md:敏感内容模式"])

    def test_committed_tree_scan_is_clean(self):
        """发布闸门自检：当前已提交的发布树必须干净。

        这条同时是端到端校验 —— 它验证 export_committed_tree() 能正确取到
        全部已跟踪文件（含中文文件名），并且发布树里没有任何被规则命中的内容。
        """
        count, report = scan_committed(PROJECT)

        self.assertGreater(count, 100)
        self.assertEqual(report.blocked_files, [])
        self.assertEqual(report.findings, [])
        self.assertTrue(report.safe)


if __name__ == "__main__":
    unittest.main()
