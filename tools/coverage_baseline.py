"""覆盖率基线：用标准库 trace 统计 src/ 下的代码覆盖，输出可复现的数字。

为什么不用 coverage 包：项目刻意保持零额外依赖。标准库的 trace 虽然慢，
但足够拿到基线，而且不需要改任何环境。

用法（在仓库根目录执行，需 PYTHONPATH=src）：

    set PYTHONPATH=src
    python tools/coverage_baseline.py

注意 trace 的两个坑：
1. 必须同时给 `--count` 和 `--missing`：只给 `--count` 时 .cover 文件里
   不会标记未执行行，算出来永远是 100%。
2. `--summary` 打印的百分比不可信（实测全为 100%），因此本脚本自己解析
   .cover 文件来统计，不看 summary。

基线（2026-09-14，269 项测试）：总体 80.4%，低于 80% 的模块 8 个。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COVER_DIR = ROOT / "build-artifacts" / "coverage"
NOT_EXECUTED = re.compile(r"^\s*>>>>>>")
EXECUTED = re.compile(r"^\s*(\d+):")

RUNNER = '''\
import os, sys, unittest
suite = unittest.TestLoader().discover("tests")
os.makedirs("build-artifacts", exist_ok=True)
with open("build-artifacts/coverage-tests.log", "w", encoding="utf-8") as log:
    result = unittest.TextTestRunner(verbosity=0, stream=log).run(suite)
print("tests_run=%d failures=%d errors=%d" % (
    result.testsRun, len(result.failures), len(result.errors)))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def measure_cover(path):
    total = hit = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if NOT_EXECUTED.match(raw):
                total += 1
            elif EXECUTED.match(raw):
                total += 1
                hit += 1
    return total, hit


def main():
    shutil.rmtree(COVER_DIR, ignore_errors=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "coverage_runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        command = [
            sys.executable, "-m", "trace",
            "--count", "--missing",
            "--coverdir", str(COVER_DIR),
            "--ignore-dir", str(Path(sys.executable).parent),
            str(runner),
        ]
        print("运行测试并统计覆盖（用 trace，可能需要 1 分钟左右）…")
        completed = subprocess.run(command, cwd=str(ROOT))
        if completed.returncode != 0:
            print("测试未全部通过，覆盖率数字仅供参考")

    rows = []
    for path in sorted(COVER_DIR.glob("*.cover")):
        if "yuanjian_app" not in path.name:
            continue
        total, hit = measure_cover(path)
        if total:
            rows.append((hit, total, path.name.replace(".cover", "")))
    if not rows:
        print("没有生成任何覆盖率文件，检查 trace 是否可用")
        return 1

    hits = sum(row[0] for row in rows)
    totals = sum(row[1] for row in rows)
    print()
    print("=== 覆盖率基线 ===")
    print("  模块 %d 个，可执行行 %d，已执行 %d，总体 %.1f%%"
          % (len(rows), totals, hits, hits * 100.0 / totals))
    print()
    rows.sort(key=lambda row: row[0] / row[1])
    print("  覆盖最低的 10 个:")
    for hit, total, name in rows[:10]:
        print("    %5.1f%%  %4d/%-4d  %s" % (hit * 100.0 / total, hit, total, name))
    low = [row for row in rows if row[0] / row[1] < 0.8]
    print()
    print("  低于 80%% 的模块: %d / %d" % (len(low), len(rows)))
    print("  明细见 %s" % COVER_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
