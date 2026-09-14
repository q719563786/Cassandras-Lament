import sys
from dataclasses import dataclass
from os import environ
from pathlib import Path

DATA_DIR_MARKER_NAME = "data-dir.txt"


def application_directory():
    """程序自身所在目录。

    打包后是 exe 所在目录；源码运行时是仓库根目录。用于安放 data-dir.txt。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def read_data_dir_marker(marker):
    """读取 data-dir.txt 里写的目录，读不到或内容为空时返回 None。

    相对路径按该文件所在目录解析，所以里面既可以写 D:\\远见\\数据，
    也可以只写「数据」。
    """
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    return candidate.resolve()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    logs: Path
    backups: Path
    cache: Path

    @classmethod
    def from_environment(cls, env, application_dir=None):
        """Resolve private runtime paths without placing data beside source code.

        决定数据目录的优先级：

        1. 环境变量 `YUANJIAN_DATA_DIR`
        2. 程序旁边的 `data-dir.txt`（文件内容就是目录路径）
        3. `%LOCALAPPDATA%\\YuanJian`

        之所以支持第 2 条：打包后的程序是双击启动的，用户没有地方设置环境变量。
        想把自己的数据放到别的盘（例如 D 盘），放一个文本文件进去是最省事、
        且对使用者可见可改的办法。

        说明：这个文件能决定私人数据的存放位置。能改写它的人也能替换程序本身，
        因此没有引入新的权限边界。
        """
        configured = env.get("YUANJIAN_DATA_DIR")
        if configured:
            root = Path(configured).expanduser().resolve()
        else:
            base = Path(application_dir) if application_dir else application_directory()
            pointed = read_data_dir_marker(base / DATA_DIR_MARKER_NAME)
            if pointed is not None:
                root = pointed
            else:
                local_app_data = env.get("LOCALAPPDATA") or environ.get("LOCALAPPDATA")
                if not local_app_data:
                    raise RuntimeError("无法确定Windows本地数据目录")
                root = (Path(local_app_data) / "YuanJian").resolve()
        return cls(
            root=root,
            database=root / "data" / "yuanjian.db",
            logs=root / "logs",
            backups=root / "backups",
            cache=root / "cache",
        )

    def ensure_directories(self):
        """Create runtime directories while leaving application source untouched."""
        for directory in (self.database.parent, self.logs, self.backups, self.cache):
            directory.mkdir(parents=True, exist_ok=True)
