#!/usr/bin/env python3
"""
steamlogscrub main CLI.

Expected project layout:

steam-log-scrubber/
├── main.py
├── scrub.py
├── archive.py
└── rules/
    ├── default.steamlogscrub.json
    ├── strict.steamlogscrub.json
    └── custom.template.steamlogscrub.json
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import tempfile
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "steamlogscrub"
RULE_EXT = ".steamlogscrub.json"

APP_DIR = Path(__file__).resolve().parent
RULES_DIR = APP_DIR / "rules"

DEFAULT_RULES = RULES_DIR / f"default{RULE_EXT}"
STRICT_RULES = RULES_DIR / f"strict{RULE_EXT}"
RELAXED_RULES = RULES_DIR / f"relaxed{RULE_EXT}"
TEMPLATE_RULES = RULES_DIR / f"custom.template{RULE_EXT}"

def find_documents_dir() -> Path:
    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer)
            if result == 0 and buffer.value:
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass

    return Path.home() / "Documents"


DOCUMENTS_DIR = find_documents_dir()
APP_DOCUMENTS_DIR = DOCUMENTS_DIR / "steamlogscrubber"
SCRUBBED_LOGS_DIR = APP_DOCUMENTS_DIR / "scrubbedlogs"
USER_CONFIG_DIR = APP_DOCUMENTS_DIR / "custom_scrubrules"
DEFAULT_OUTPUT_HELP = APP_DOCUMENTS_DIR / "scrubbedlogs" / "<datetime>" / "unarchived_<datetime>"



@dataclass(frozen=True)
class CliPaths:
    input_dir: Path
    output_dir: Path
    rules_path: Path
    cleanup_dir: Path | None = None


def safe_ruleset_name(name: str) -> str:
    """
    Convert a user-provided ruleset name into a safe filename stem.

    Examples:
        "Helldivers 2" -> "helldivers2"
        "my custom rules" -> "my_custom_rules"
    """
    cleaned = name.strip().lower()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[^a-z0-9_.-]", "", cleaned)
    cleaned = cleaned.strip("._-")

    if not cleaned:
        raise ValueError("Ruleset name cannot be empty.")

    if cleaned in {"default", "strict", "custom", "template"}:
        raise ValueError(f"'{cleaned}' is reserved. Choose another name.")

    return cleaned


def find_windows_steam_install_dirs() -> list[Path]:
    if os.name != "nt":
        return []

    candidates: list[Path] = []

    try:
        import winreg

        registry_locations = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
        ]
        registry_views = {
            0,
            getattr(winreg, "KEY_WOW64_32KEY", 0),
            getattr(winreg, "KEY_WOW64_64KEY", 0),
        }

        for root, key_name, value_name in registry_locations:
            for registry_view in registry_views:
                try:
                    access = winreg.KEY_READ | registry_view
                    with winreg.OpenKey(root, key_name, 0, access) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue

                if isinstance(value, str) and value.strip():
                    candidates.append(Path(value.strip()))
    except ImportError:
        pass

    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value) / "Steam")

    seen: set[Path] = set()
    found: list[Path] = []

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue

        if resolved not in seen:
            found.append(resolved)
            seen.add(resolved)

    return found


def find_default_steam_log_dirs(
    platform_name: str | None = None,
    windows_install_dirs: Iterable[Path] | None = None,
) -> list[Path]:
    platform = os.name if platform_name is None else platform_name

    if platform == "nt":
        install_dirs = (
            list(windows_install_dirs)
            if windows_install_dirs is not None
            else find_windows_steam_install_dirs()
        )
        candidates = [install_dir / "logs" for install_dir in install_dirs]
    else:
        candidates = [
            Path.home() / ".local" / "share" / "Steam" / "logs",
            Path.home() / ".steam" / "steam" / "logs",
        ]

    seen: set[Path] = set()
    found: list[Path] = []

    for candidate in candidates:
        if candidate.is_dir():
            resolved = candidate.resolve()
            if resolved not in seen:
                found.append(resolved)
                seen.add(resolved)

    return found


def find_default_steam_logs() -> Path | None:
    dirs = find_default_steam_log_dirs()
    return dirs[0] if dirs else None


def find_proton_log_files() -> list[Path]:
    roots = [Path.home()]

    proton_log_dir = os.environ.get("PROTON_LOG_DIR")
    if proton_log_dir:
        roots.append(Path(proton_log_dir).expanduser())

    seen: set[Path] = set()
    found: list[Path] = []

    for root in roots:
        if not root.is_dir():
            continue

        for pattern in ("steam-*.log", "Steam-*.log"):
            for candidate in root.glob(pattern):
                if not candidate.is_file():
                    continue

                resolved = candidate.resolve()
                if resolved not in seen:
                    found.append(resolved)
                    seen.add(resolved)

    return found


def select_archive_types(
    args: argparse.Namespace,
    platform_name: str | None = None,
) -> tuple[bool, bool]:
    platform = os.name if platform_name is None else platform_name
    windows_default_zip = platform == "nt" and not args.zip and not args.zip_only
    make_tar = not args.zip_only and not windows_default_zip
    make_zip = args.zip or args.zip_only or windows_default_zip
    return make_tar, make_zip


def copy_tree_contents(source: Path, destination: Path) -> int:
    copied = 0

    for path in source.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(source)
        out_path = destination / relative
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(path, out_path)
        except OSError:
            continue

        copied += 1

    return copied


def copy_single_file_unique(source: Path, destination_dir: Path) -> bool:
    destination_dir.mkdir(parents=True, exist_ok=True)

    destination = destination_dir / source.name
    if destination.exists():
        stem = source.stem
        suffix = source.suffix
        counter = 2

        while destination.exists():
            destination = destination_dir / f"{stem}-{counter}{suffix}"
            counter += 1

    try:
        shutil.copy2(source, destination)
    except OSError:
        return False

    return True


def copy_windows_opted_in_game_logs(
    destination: Path,
    steam_install_dirs: Iterable[Path] | None = None,
) -> int:
    from .windows_games import find_opted_in_game_logs

    install_dirs = (
        list(steam_install_dirs)
        if steam_install_dirs is not None
        else find_windows_steam_install_dirs()
    )
    copied = 0

    for game_log in find_opted_in_game_logs(install_dirs):
        output = (
            destination
            / "windows-game-logs"
            / f"app-{game_log.app_id}"
            / game_log.relative_path
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(game_log.path, output)
        except OSError:
            continue

        copied += 1

    return copied


def build_auto_input_bundle() -> tuple[Path, Path]:
    bundle = Path(tempfile.mkdtemp(prefix="steamlogscrub-input-"))
    copied = 0

    for index, logs_dir in enumerate(find_default_steam_log_dirs(), start=1):
        copied += copy_tree_contents(logs_dir, bundle / f"steam-logs-{index}")

    proton_dest = bundle / "proton-logs"
    for proton_log in find_proton_log_files():
        if copy_single_file_unique(proton_log, proton_dest):
            copied += 1

    if os.name == "nt":
        copied += copy_windows_opted_in_game_logs(bundle)

    if copied == 0:
        shutil.rmtree(bundle, ignore_errors=True)
        raise FileNotFoundError(
            "Could not auto-detect Steam or Proton logs. "
            "Provide an input folder, for example: steamlogscrub ~/.local/share/Steam/logs"
        )

    return bundle, bundle

def require_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def resolve_rules_path(args: argparse.Namespace) -> Path:
    if args.rules:
        return require_file(Path(args.rules).expanduser().resolve(), "custom rules file")

    if args.strict:
        return require_file(STRICT_RULES, "strict rules file")

    if args.relaxed:
        return require_file(RELAXED_RULES, "relaxed rules file")

    return require_file(DEFAULT_RULES, "default rules file")


def ensure_app_folders() -> None:
    APP_DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    SCRUBBED_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def make_run_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def make_default_run_dir() -> Path:
    stamp = make_run_stamp()
    candidate = SCRUBBED_LOGS_DIR / stamp
    suffix = 2

    while candidate.exists():
        candidate = SCRUBBED_LOGS_DIR / f"{stamp}-{suffix}"
        suffix += 1

    return candidate


def make_default_output_dir() -> Path:
    run_dir = make_default_run_dir()
    return run_dir / f"unarchived_{run_dir.name}"


def resolve_paths(args: argparse.Namespace) -> CliPaths:
    ensure_app_folders()

    cleanup_dir: Path | None = None

    if args.input:
        input_dir = Path(args.input).expanduser().resolve()
    else:
        input_dir, cleanup_dir = build_auto_input_bundle()
        input_dir = input_dir.resolve()

    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input folder does not exist: {input_dir}")

    output_dir = Path(args.output).expanduser().resolve() if args.output else make_default_output_dir().resolve()
    if args.output:
        output_dir.parent.mkdir(parents=True, exist_ok=True)

    if input_dir == output_dir:
        raise ValueError("Refusing to use the input folder as the output folder.")

    rules_path = resolve_rules_path(args)

    return CliPaths(
        input_dir=input_dir,
        output_dir=output_dir,
        rules_path=rules_path,
        cleanup_dir=cleanup_dir,
    )


def create_new_ruleset(name: str | None) -> int:
    ensure_app_folders()

    require_file(TEMPLATE_RULES, "custom template rules file")

    if not name:
        print("Name the ruleset:")
        try:
            name = input("> ")
        except EOFError:
            print("No ruleset name provided.", file=sys.stderr)
            return 2

    try:
        safe_name = safe_ruleset_name(name)
    except ValueError as exc:
        print(f"Invalid ruleset name: {exc}", file=sys.stderr)
        return 2

    destination = USER_CONFIG_DIR / f"{safe_name}{RULE_EXT}"

    if destination.exists():
        print(f"Ruleset already exists: {destination}", file=sys.stderr)
        print("Edit that file directly or choose a different name.", file=sys.stderr)
        return 2

    shutil.copy2(TEMPLATE_RULES, destination)

    print(f"Template copied at {destination}")
    return 0


def print_result(result: object, archive_paths: list[Path] | None, dry_run: bool) -> None:
    """
    Keep this intentionally tolerant.

    scrub.py can return a dataclass later with fields like:
        files_scanned
        files_changed
        redactions
        warnings
        leftovers

    Until that API is finalized, this prints known attributes if present.
    """
    print()

    if dry_run:
        print("Dry run complete. No files were written.")
    else:
        print("Scrub complete.")

    for attr, label in [
        ("files_scanned", "Files scanned"),
        ("files_changed", "Files changed"),
        ("redactions", "Redactions"),
        ("account_names_detected", "Auto-detected Steam account/persona names"),
    ]:
        if hasattr(result, attr):
            print(f"{label}: {getattr(result, attr)}")

    warnings = getattr(result, "warnings", None)
    if warnings:
        print()
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    leftovers = getattr(result, "leftovers", None)
    if leftovers:
        print()
        print("Possible leftovers:")
        for leftover in leftovers:
            print(f"- {leftover}")
        print()
        print("Review possible-leftover files before sharing.")

    if archive_paths:
        print()
        print("Created archive files:")
        for path in archive_paths:
            print(f"- {path}")


def run_scrub(args: argparse.Namespace) -> int:
    paths: CliPaths | None = None

    try:
        paths = resolve_paths(args)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        from .scrub import load_rules, scrub_folder
        from .archive import create_archives
    except ImportError as exc:
        print(f"Error: missing project module: {exc}", file=sys.stderr)
        print("Expected scrub.py and archive.py beside main.py.", file=sys.stderr)
        return 2

    try:
        rules = load_rules(paths.rules_path)

        def emit_json_progress(current: int, total: int, relative_path: str) -> None:
            if not args.json_progress:
                return

            print(
                json.dumps(
                    {
                        "type": "progress",
                        "current": current,
                        "total": total,
                        "file": relative_path,
                    }
                ),
                flush=True,
            )

        result = scrub_folder(
            input_dir=paths.input_dir,
            output_dir=paths.output_dir,
            rules=rules,
            dry_run=args.dry_run,
            force=args.force,
            progress_callback=emit_json_progress if args.json_progress else None,
        )

        archive_paths: list[Path] = []

        if not args.dry_run and not args.no_archive:
            make_tar, make_zip = select_archive_types(args)
            archive_paths = create_archives(
                source_dir=paths.output_dir,
                make_tar=make_tar,
                make_zip=make_zip,
                output_dir=paths.output_dir.parent,
                backup_existing=True,
            )

            if not args.output:
                renamed_archive_paths = []
                archive_base = paths.output_dir.parent.name

                for archive_path in archive_paths:
                    archive_path = Path(archive_path)
                    new_name = archive_path.name.replace(paths.output_dir.name, archive_base, 1)
                    new_path = archive_path.with_name(new_name)

                    if new_path != archive_path:
                        archive_path.replace(new_path)

                    renamed_archive_paths.append(new_path)

                archive_paths = renamed_archive_paths

        print(f"Input:  {paths.input_dir}")
        print(f"Output: {paths.output_dir}")
        print(f"Rules:  {paths.rules_path}")

        print_result(result, archive_paths, args.dry_run)

        leftovers = getattr(result, "leftovers", None)
        return 1 if leftovers else 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if paths is not None and paths.cleanup_dir is not None:
            shutil.rmtree(paths.cleanup_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            "Scrub Steam and Proton logs before sharing them with support. "
            "By default, auto-detects Steam logs and writes a scrubbed archive."
        ),
    )

    parser.add_argument(
        "input",
        nargs="?",
        help="Input log folder. Defaults to the detected Steam logs folder.",
    )

    parser.add_argument(
        "-o",
        "--output",
        help=f"Output folder for scrubbed logs. Default: {DEFAULT_OUTPUT_HELP}",
    )

    parser.add_argument(
        "-s",
        "--strict",
        action="store_true",
        help="Use the strict built-in ruleset instead of default.",
    )

    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Use the relaxed built-in ruleset for troubleshooting. Preserves private LAN IPs while keeping identity and credential redactions.",
    )

    parser.add_argument(
        "-r",
        "--rules",
        help="Use a custom .steamlogscrub.json rules file.",
    )

    parser.add_argument(
        "-n",
        "--new",
        nargs="?",
        const="__PROMPT__",
        metavar="NAME",
        help=(
            "Create a new custom ruleset from the template. "
            "With no NAME, prompts for one."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report what would be scrubbed, but do not write output files.",
    )

    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Allow replacing the existing output folder. archive.py should still back up existing archives.",
    )

    parser.add_argument(
        "--zip",
        action="store_true",
        help="Also create a .zip archive.",
    )

    parser.add_argument(
        "--zip-only",
        action="store_true",
        help="Create only a .zip archive, not .tar.xz. This is the default on Windows.",
    )

    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Write the scrubbed folder but do not create an archive.",
    )

    parser.add_argument(
        "--json-progress",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} 0.3.0",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_app_folders()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.strict and args.relaxed:
        parser.error("Use either --strict or --relaxed, not both.")

    if args.rules and (args.strict or args.relaxed):
        parser.error("Use --rules by itself; do not combine it with --strict or --relaxed.")

    if args.zip_only and args.no_archive:
        parser.error("Use either --zip-only or --no-archive, not both.")

    if args.new is not None:
        name = None if args.new == "__PROMPT__" else args.new
        return create_new_ruleset(name)

    return run_scrub(args)


if __name__ == "__main__":
    raise SystemExit(main())
