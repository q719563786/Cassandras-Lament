"""发布前隐私闸门：拒绝运行产物与可识别的凭据进入发布物。

三种用法：

    python tools/privacy_scan.py                  # 扫描当前工作目录
    python tools/privacy_scan.py <root>           # 扫描指定目录
    python tools/privacy_scan.py --committed      # 只扫描 git 已跟踪的文件（发布树）

发布前的正式检查应当使用 `--committed`：工作目录里含有 `.venv-build/`
等构建产物，直接扫工作目录会得到大量与发布无关的 blocked 结果，
真正的泄漏反而被噪声淹没。
"""

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BLOCKED_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".log", ".env", ".bak"}
BLOCKED_NAMES = {"cache", "backups", "__pycache__"}
SCANNED_EXTENSIONS = {".py", ".md", ".txt", ".json", ".html", ".css", ".js", ".cmd", ".ps1"}

SECRET_PATTERNS = (
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # Windows 绝对路径。反斜杠与正斜杠都要覆盖：此前只写了反斜杠一种形式，
    # 而真实泄漏用的是正斜杠写法，因此从闸门里漏了过去。
    re.compile(r"C:[\\/]Users[\\/][^\r\n]+", re.IGNORECASE),
    re.compile(r"C:[\\/]Documents and Settings[\\/][^\r\n]+", re.IGNORECASE),
)


@dataclass(frozen=True)
class ScanReport:
    safe: bool = True
    blocked_files: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


def scan_tree(root):
    """Reject runtime artifacts and recognizable credentials from a source tree."""
    root = Path(root)
    blocked_files = []
    findings = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(part.lower() in BLOCKED_NAMES for part in path.relative_to(root).parts):
            if path.is_file():
                blocked_files.append(relative)
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in BLOCKED_EXTENSIONS:
            blocked_files.append(relative)
            continue
        if path.suffix.lower() not in SCANNED_EXTENSIONS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{relative}:敏感内容模式")
                break
    return ScanReport(
        safe=not blocked_files and not findings,
        blocked_files=blocked_files,
        findings=findings,
    )


def export_committed_tree(destination, repository):
    """Materialize `git ls-files` into a directory so it can be scanned as published."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repository),
        capture_output=True,
        check=True,
    ).stdout
    names = [name.decode("utf-8") for name in listed.split(b"\x00") if name]
    root = Path(destination)
    for name in names:
        target = root / name.replace("/", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(repository) / name, target)
    return len(names)


def scan_committed(repository):
    repository = Path(repository).resolve()
    with tempfile.TemporaryDirectory(prefix="yuanjian-privacy-") as staged:
        count = export_committed_tree(staged, repository)
        report = scan_tree(staged)
    return count, report


def main(argv):
    if len(argv) > 1 and argv[1] == "--committed":
        repository = argv[2] if len(argv) > 2 else "."
        count, report = scan_committed(repository)
        print(f"committed_files={count}")
    else:
        report = scan_tree(argv[1] if len(argv) > 1 else ".")
    print(f"safe={report.safe} blocked={len(report.blocked_files)} findings={len(report.findings)}")
    for item in [*report.blocked_files, *report.findings]:
        print(item)
    return 0 if report.safe else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
