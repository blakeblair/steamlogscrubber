from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WINDOWS_LAUNCH_MARKER = "--steamlogscrubber"
TEXT_LOG_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".log",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
LOG_DIRECTORY_NAMES = {"log", "logs"}
LOG_FILENAME_HINTS = {"console", "debug", "error", "errors", "log", "output"}


@dataclass(frozen=True)
class WindowsGameLog:
    app_id: str
    install_dir: Path
    path: Path

    @property
    def relative_path(self) -> Path:
        return self.path.relative_to(self.install_dir)


def tokenize_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0

    while index < len(text):
        character = text[index]

        if character.isspace():
            index += 1
            continue

        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline == -1 else newline + 1
            continue

        if character in "{}":
            tokens.append(character)
            index += 1
            continue

        if character == '"':
            index += 1
            value: list[str] = []

            while index < len(text):
                character = text[index]

                if character == '"':
                    index += 1
                    break

                if character == "\\" and index + 1 < len(text):
                    following = text[index + 1]
                    if following in {'"', "\\"}:
                        value.append(following)
                        index += 2
                        continue

                value.append(character)
                index += 1

            tokens.append("".join(value))
            continue

        start = index
        while index < len(text) and not text[index].isspace() and text[index] not in "{}":
            index += 1
        tokens.append(text[start:index])

    return tokens


def parse_vdf_object(tokens: list[str], index: int = 0) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}

    while index < len(tokens):
        if tokens[index] == "}":
            return result, index + 1

        key = tokens[index]
        index += 1

        if index >= len(tokens):
            result[key] = ""
            break

        if tokens[index] == "{":
            value, index = parse_vdf_object(tokens, index + 1)
        else:
            value = tokens[index]
            index += 1

        result[key] = value

    return result, index


def parse_vdf(text: str) -> dict[str, Any]:
    parsed, _ = parse_vdf_object(tokenize_vdf(text.lstrip("\ufeff")))
    return parsed


def load_vdf(path: Path) -> dict[str, Any]:
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def get_case_insensitive(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict):
        return None

    wanted = key.casefold()
    for current_key, value in mapping.items():
        if str(current_key).casefold() == wanted:
            return value

    return None


def get_nested_case_insensitive(mapping: Any, keys: Iterable[str]) -> Any:
    current = mapping

    for key in keys:
        current = get_case_insensitive(current, key)
        if current is None:
            return None

    return current


def launch_options_include_marker(options: str) -> bool:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(WINDOWS_LAUNCH_MARKER)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    return bool(pattern.search(options))


def find_opted_in_app_ids(steam_install_dirs: Iterable[Path]) -> set[str]:
    app_ids: set[str] = set()

    for steam_install_dir in steam_install_dirs:
        userdata_dir = steam_install_dir / "userdata"
        if not userdata_dir.is_dir():
            continue

        for local_config in userdata_dir.glob("*/config/localconfig.vdf"):
            data = load_vdf(local_config)
            apps = get_nested_case_insensitive(
                data,
                ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"),
            )

            if not isinstance(apps, dict):
                continue

            for app_id, settings in apps.items():
                if not str(app_id).isdigit():
                    continue

                launch_options = get_case_insensitive(settings, "LaunchOptions")
                if isinstance(launch_options, str) and launch_options_include_marker(
                    launch_options
                ):
                    app_ids.add(str(app_id))

    return app_ids


def find_steamapps_dirs(steam_install_dirs: Iterable[Path]) -> list[Path]:
    candidates: list[Path] = []

    for steam_install_dir in steam_install_dirs:
        primary_steamapps = steam_install_dir / "steamapps"
        candidates.append(primary_steamapps)

        library_data = load_vdf(primary_steamapps / "libraryfolders.vdf")
        libraries = get_case_insensitive(library_data, "libraryfolders")

        if not isinstance(libraries, dict):
            continue

        for value in libraries.values():
            if isinstance(value, dict):
                library_path = get_case_insensitive(value, "path")
            else:
                library_path = value

            if isinstance(library_path, str) and library_path.strip():
                candidates.append(Path(library_path.strip()) / "steamapps")

    found: list[Path] = []
    seen: set[Path] = set()

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue

        if resolved.is_dir() and resolved not in seen:
            found.append(resolved)
            seen.add(resolved)

    return found


def find_game_install_dirs(
    app_ids: Iterable[str],
    steam_install_dirs: Iterable[Path],
) -> dict[str, Path]:
    remaining = {str(app_id) for app_id in app_ids}
    found: dict[str, Path] = {}

    for steamapps_dir in find_steamapps_dirs(steam_install_dirs):
        for app_id in tuple(remaining):
            manifest = steamapps_dir / f"appmanifest_{app_id}.acf"
            if not manifest.is_file():
                continue

            data = load_vdf(manifest)
            app_state = get_case_insensitive(data, "AppState")
            install_name = get_case_insensitive(app_state, "installdir")

            if not isinstance(install_name, str) or not install_name.strip():
                continue

            common_dir = (steamapps_dir / "common").resolve()
            install_dir = (common_dir / install_name.strip()).resolve()

            try:
                install_dir.relative_to(common_dir)
            except ValueError:
                continue

            if install_dir.is_dir():
                found[app_id] = install_dir
                remaining.remove(app_id)

    return found


def is_probably_text(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            sample = file.read(8192)
    except OSError:
        return False

    return b"\x00" not in sample


def is_game_log_path(path: Path, install_dir: Path) -> bool:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.suffix.casefold() not in TEXT_LOG_SUFFIXES
    ):
        return False

    relative = path.relative_to(install_dir)
    parent_names = {part.casefold() for part in relative.parts[:-1]}
    stem = path.stem.casefold()

    if path.suffix.casefold() == ".log":
        return is_probably_text(path)

    if parent_names.intersection(LOG_DIRECTORY_NAMES):
        return is_probably_text(path)

    if stem in LOG_FILENAME_HINTS or stem.endswith("_log") or stem.endswith("-log"):
        return is_probably_text(path)

    return False


def find_game_logs(app_id: str, install_dir: Path) -> list[WindowsGameLog]:
    found: list[WindowsGameLog] = []

    for root, _, filenames in os.walk(install_dir):
        root_path = Path(root)

        for filename in filenames:
            path = root_path / filename
            if is_game_log_path(path, install_dir):
                found.append(
                    WindowsGameLog(
                        app_id=app_id,
                        install_dir=install_dir,
                        path=path,
                    )
                )

    return sorted(found, key=lambda item: str(item.path).casefold())


def find_opted_in_game_logs(steam_install_dirs: Iterable[Path]) -> list[WindowsGameLog]:
    install_dirs = list(steam_install_dirs)
    app_ids = find_opted_in_app_ids(install_dirs)
    game_dirs = find_game_install_dirs(app_ids, install_dirs)
    found: list[WindowsGameLog] = []

    for app_id in sorted(game_dirs, key=int):
        found.extend(find_game_logs(app_id, game_dirs[app_id]))

    return found
