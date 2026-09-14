import tempfile
import unittest
from pathlib import Path

from yuanjian_app.config import DATA_DIR_MARKER_NAME, AppPaths, read_data_dir_marker


class AppPathsTests(unittest.TestCase):
    def test_environment_override_keeps_runtime_data_outside_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            private = Path(temp_dir) / "private"

            paths = AppPaths.from_environment({"YUANJIAN_DATA_DIR": str(private)})
            paths.ensure_directories()

            self.assertEqual(paths.database, private / "data" / "yuanjian.db")
            self.assertEqual(paths.logs, private / "logs")
            self.assertTrue(paths.database.parent.is_dir())

    def test_marker_beside_the_program_selects_the_data_directory(self):
        """程序旁边放 data-dir.txt 就能把数据放到别的盘。

        打包后的程序是双击启动的，用户没有地方设置环境变量，所以需要一个
        看得见、改得动的办法来指定数据目录（例如放到 D 盘）。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            program = Path(temp_dir) / "program"
            data = Path(temp_dir) / "elsewhere" / "数据"
            program.mkdir(parents=True)
            (program / DATA_DIR_MARKER_NAME).write_text(str(data), encoding="utf-8")

            paths = AppPaths.from_environment(
                {"LOCALAPPDATA": str(Path(temp_dir) / "localappdata")},
                application_dir=program,
            )

            self.assertEqual(paths.root, data.resolve())
            self.assertEqual(paths.database, data.resolve() / "data" / "yuanjian.db")

    def test_environment_variable_still_wins_over_the_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            program = Path(temp_dir) / "program"
            program.mkdir(parents=True)
            (program / DATA_DIR_MARKER_NAME).write_text(
                str(Path(temp_dir) / "from-marker"), encoding="utf-8"
            )
            from_env = Path(temp_dir) / "from-env"

            paths = AppPaths.from_environment(
                {"YUANJIAN_DATA_DIR": str(from_env), "LOCALAPPDATA": str(temp_dir)},
                application_dir=program,
            )

            self.assertEqual(paths.root, from_env.resolve())

    def test_relative_marker_path_resolves_beside_the_program(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            program = Path(temp_dir) / "program"
            program.mkdir(parents=True)
            (program / DATA_DIR_MARKER_NAME).write_text("数据", encoding="utf-8")

            paths = AppPaths.from_environment(
                {"LOCALAPPDATA": str(Path(temp_dir) / "localappdata")},
                application_dir=program,
            )

            self.assertEqual(paths.root, (program / "数据").resolve())

    def test_blank_or_missing_marker_falls_back_to_local_app_data(self):
        local_app_data = None
        for write_marker in (False, True):
            with tempfile.TemporaryDirectory() as temp_dir:
                program = Path(temp_dir) / "program"
                program.mkdir(parents=True)
                if write_marker:
                    # 只有空白内容的标记文件不应生效，否则会静默把数据放到奇怪的地方。
                    (program / DATA_DIR_MARKER_NAME).write_text("   \n", encoding="utf-8")
                local_app_data = Path(temp_dir) / "localappdata"

                paths = AppPaths.from_environment(
                    {"LOCALAPPDATA": str(local_app_data)}, application_dir=program
                )

                self.assertEqual(paths.root, (local_app_data / "YuanJian").resolve())

    def test_marker_reader_reports_nothing_for_unreadable_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertIsNone(read_data_dir_marker(root / "does-not-exist.txt"))
            self.assertIsNone(read_data_dir_marker(root))
            empty = root / DATA_DIR_MARKER_NAME
            empty.write_text("", encoding="utf-8")
            self.assertIsNone(read_data_dir_marker(empty))


if __name__ == "__main__":
    unittest.main()
