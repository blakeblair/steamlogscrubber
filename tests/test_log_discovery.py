from __future__ import annotations

import argparse
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steamlogscrubber import cli


class LogDiscoveryTests(unittest.TestCase):
    def test_discovers_proton_logs_for_any_app_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            proton_log_dir = root / "configured-proton-logs"
            home.mkdir()
            proton_log_dir.mkdir()

            expected = {
                home / "steam-10.log",
                home / "steam-553850.log",
                proton_log_dir / "steam-9999999.log",
            }

            for path in expected:
                path.write_text("test log", encoding="utf-8")

            (home / "not-a-proton-log.txt").write_text("ignored", encoding="utf-8")

            with patch("steamlogscrubber.cli.Path.home", return_value=home):
                with patch.dict(
                    "steamlogscrubber.cli.os.environ",
                    {"PROTON_LOG_DIR": str(proton_log_dir)},
                    clear=True,
                ):
                    found = set(cli.find_proton_log_files())

            self.assertEqual({path.resolve() for path in expected}, found)

    def test_windows_steam_log_directory_uses_detected_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            steam_install = Path(temporary_directory) / "Steam"
            logs = steam_install / "logs"
            logs.mkdir(parents=True)

            found = cli.find_default_steam_log_dirs(
                platform_name="nt",
                windows_install_dirs=[steam_install, steam_install],
            )

            self.assertEqual([logs.resolve()], found)

    def test_auto_bundle_copies_every_discovered_game_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            steam_logs = root / "Steam" / "logs"
            proton_logs = root / "proton"
            steam_logs.mkdir(parents=True)
            proton_logs.mkdir()

            (steam_logs / "content_log.txt").write_text("client", encoding="utf-8")
            game_logs = [
                proton_logs / "steam-10.log",
                proton_logs / "steam-553850.log",
                proton_logs / "steam-9999999.log",
            ]

            for path in game_logs:
                path.write_text(path.name, encoding="utf-8")

            with patch(
                "steamlogscrubber.cli.find_default_steam_log_dirs",
                return_value=[steam_logs],
            ):
                with patch(
                    "steamlogscrubber.cli.find_proton_log_files",
                    return_value=game_logs,
                ):
                    bundle, cleanup = cli.build_auto_input_bundle()

            try:
                self.assertTrue((bundle / "steam-logs-1" / "content_log.txt").is_file())
                self.assertEqual(
                    {path.name for path in game_logs},
                    {path.name for path in (bundle / "proton-logs").iterdir()},
                )
            finally:
                shutil.rmtree(cleanup, ignore_errors=True)

    def test_windows_defaults_to_zip_archive(self) -> None:
        args = argparse.Namespace(zip=False, zip_only=False)
        self.assertEqual((False, True), cli.select_archive_types(args, "nt"))

    def test_linux_defaults_to_tar_xz_archive(self) -> None:
        args = argparse.Namespace(zip=False, zip_only=False)
        self.assertEqual((True, False), cli.select_archive_types(args, "posix"))

    def test_explicit_zip_option_creates_both_archive_types(self) -> None:
        args = argparse.Namespace(zip=True, zip_only=False)
        self.assertEqual((True, True), cli.select_archive_types(args, "nt"))


if __name__ == "__main__":
    unittest.main()
