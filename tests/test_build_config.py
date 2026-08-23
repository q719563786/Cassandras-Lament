import json
import subprocess
import unittest
from pathlib import Path

from yuanjian_app import __version__


class BuildConfigTests(unittest.TestCase):
    project = Path(__file__).resolve().parents[1]

    def test_package_reports_version_100(self):
        # v1.0：行动雷达首页 + 登高望远方法论规则引擎 + 信源分级。
        self.assertEqual(__version__, "1.0.0")

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
