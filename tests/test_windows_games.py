from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from steamlogscrubber import cli
from steamlogscrubber.windows_games import (
    WINDOWS_LAUNCH_MARKER,
    find_opted_in_app_ids,
    find_opted_in_game_logs,
    launch_options_include_marker,
    parse_vdf,
)


def write_local_config(steam_install: Path) -> None:
    config = steam_install / "userdata" / "12345" / "config" / "localconfig.vdf"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'''"UserLocalConfigStore"
{{
    "Software"
    {{
        "Valve"
        {{
            "Steam"
            {{
                "apps"
                {{
                    "10"
                    {{
                        "LaunchOptions" "{WINDOWS_LAUNCH_MARKER}"
                    }}
                    "553850"
                    {{
                        "LaunchOptions" "-novid {WINDOWS_LAUNCH_MARKER}"
                    }}
                    "9999999"
                    {{
                        "LaunchOptions" "-windowed"
                    }}
                }}
            }}
        }}
    }}
}}
''',
        encoding="utf-8",
    )


def write_manifest(steamapps: Path, app_id: str, install_name: str) -> Path:
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / f"appmanifest_{app_id}.acf").write_text(
        f'''"AppState"
{{
    "appid" "{app_id}"
    "installdir" "{install_name}"
}}
''',
        encoding="utf-8",
    )
    install_dir = steamapps / "common" / install_name
    install_dir.mkdir(parents=True)
    return install_dir


class WindowsGameLogTests(unittest.TestCase):
    def test_vdf_parser_handles_nested_steam_settings(self) -> None:
        parsed = parse_vdf('"root" { "child" { "value" "yes" } }')
        self.assertEqual("yes", parsed["root"]["child"]["value"])

    def test_vdf_parser_decodes_windows_library_paths(self) -> None:
        parsed = parse_vdf(r'"library" { "path" "D:\\SteamLibrary" }')
        self.assertEqual(r"D:\SteamLibrary", parsed["library"]["path"])

    def test_marker_match_is_exact(self) -> None:
        self.assertTrue(launch_options_include_marker(WINDOWS_LAUNCH_MARKER))
        self.assertTrue(
            launch_options_include_marker(f'-novid "{WINDOWS_LAUNCH_MARKER}"')
        )
        self.assertFalse(launch_options_include_marker("--steamlogscrubber-disabled"))

    def test_finds_every_marked_app_id_without_an_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            steam_install = Path(temporary_directory) / "Steam"
            write_local_config(steam_install)

            self.assertEqual(
                {"10", "553850"},
                find_opted_in_app_ids([steam_install]),
            )

    def test_collects_text_logs_only_from_marked_games(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            steam_install = root / "Steam"
            second_library = root / "SteamLibrary"
            write_local_config(steam_install)

            primary_steamapps = steam_install / "steamapps"
            primary_steamapps.mkdir(parents=True)
            (primary_steamapps / "libraryfolders.vdf").write_text(
                f'''"libraryfolders"
{{
    "0" {{ "path" "{steam_install}" }}
    "1" {{ "path" "{second_library}" }}
}}
''',
                encoding="utf-8",
            )

            game_10 = write_manifest(primary_steamapps, "10", "Game Ten")
            game_553850 = write_manifest(
                second_library / "steamapps",
                "553850",
                "Helldivers 2",
            )
            game_9999999 = write_manifest(
                primary_steamapps,
                "9999999",
                "Unmarked Game",
            )

            (game_10 / "game.log").write_text("game ten", encoding="utf-8")
            (game_553850 / "Logs").mkdir()
            (game_553850 / "Logs" / "output.txt").write_text(
                "helldivers",
                encoding="utf-8",
            )
            (game_553850 / "notes.txt").write_text("not a log", encoding="utf-8")
            (game_553850 / "binary.log").write_bytes(b"binary\x00content")
            (game_9999999 / "unmarked.log").write_text("ignore", encoding="utf-8")

            logs = find_opted_in_game_logs([steam_install])

            self.assertEqual(
                {("10", "game.log"), ("553850", "Logs/output.txt")},
                {
                    (log.app_id, log.relative_path.as_posix())
                    for log in logs
                },
            )

            destination = root / "bundle"
            copied = cli.copy_windows_opted_in_game_logs(
                destination,
                steam_install_dirs=[steam_install],
            )

            self.assertEqual(2, copied)
            self.assertTrue(
                (destination / "windows-game-logs" / "app-10" / "game.log").is_file()
            )
            self.assertTrue(
                (
                    destination
                    / "windows-game-logs"
                    / "app-553850"
                    / "Logs"
                    / "output.txt"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
