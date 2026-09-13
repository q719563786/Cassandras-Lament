import json
import re
import subprocess
import unittest
from pathlib import Path

from yuanjian_app import __version__


class BuildConfigTests(unittest.TestCase):
    project = Path(__file__).resolve().parents[1]

    def test_package_reports_version_100(self):
        # v1.0：行动雷达首页 + 登高望远方法论规则引擎 + 信源分级。
        self.assertEqual(__version__, "1.0.0")

    def test_source_version_labels_match_the_package_version(self):
        """源码里的「远见 vX.Y」各处必须与 __version__ 一致。

        历史事故：`__version__` 在 2026-08-22 定为 1.0.0，但此后陆续有文件头被写成
        v1.1 和 v1.5，长期无人察觉；整理版本时只搜了当时的 v0.9，因此漏掉一批。
        这里把字样钉死，避免再次漂移。

        只扫 `src/`：`docs/` 下的实施计划与设计文档记录的是当时那个版本，
        带着 v0.6 / v0.9 字样是正确的，不参与比对。
        """
        expected = ".".join(__version__.split(".")[:2])
        pattern = re.compile(r"远见 v(\d+\.\d+)")
        found = {}
        scanned = 0
        for path in sorted((self.project / "src").rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".js", ".css", ".html", ".py"}:
                continue
            scanned += 1
            for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                found.setdefault(match.group(1), []).append(
                    path.relative_to(self.project).as_posix()
                )

        # 扫描逻辑本身要能被发现失效：找不到任何字样说明正则或路径写错了。
        self.assertGreater(scanned, 0, "没有扫描到任何源文件")
        self.assertTrue(found, "源码里一处版本字样都没找到，扫描逻辑可能已失效")
        self.assertEqual(
            sorted(found),
            [expected],
            f"源码中的版本字样与 __version__（{__version__}）不一致：{found}",
        )

    def powershell_json(self, script, *arguments):
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.project / script),
                *arguments,
            ],
            text=True,
            encoding="utf-8-sig",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_windows_powershell_build_script_is_ascii_safe(self):
        project = Path(__file__).resolve().parents[1]
        script = (project / "build" / "build_windows.ps1").read_text(encoding="utf-8")
        script.encode("ascii")
        spec = (project / "build" / "yuanjian.spec").read_text(encoding="utf-8")
        self.assertIn('name="YuanJian"', spec)

    def test_packaged_smoke_script_does_not_shadow_home(self):
        project = Path(__file__).resolve().parents[1]
        script = (project / "tools" / "smoke_packaged.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"(?i)\$home\b")

    def test_windows_build_describes_exact_desktop_dependencies(self):
        contract = self.powershell_json("build/build_windows.ps1", "-Describe")

        self.assertEqual(
            contract["Dependencies"],
            [
                "pyinstaller==6.21.0",
                "pywebview==6.2.1",
                "pystray==0.19.5",
                "Pillow==12.3.0",
            ],
        )
        self.assertEqual(contract["Gui"], "edgechromium")

    def test_packaged_smoke_uses_only_the_explicit_headless_mode(self):
        contract = self.powershell_json("tools/smoke_packaged.ps1", "-Describe")

        self.assertEqual(contract["HeadlessEnvironment"], "YUANJIAN_HEADLESS")
        self.assertEqual(contract["HeadlessValue"], "1")
        self.assertEqual(contract["DefaultView"], "today")
        self.assertEqual(contract["ModuleEntry"], "/js/app.js")


if __name__ == "__main__":
    unittest.main()
