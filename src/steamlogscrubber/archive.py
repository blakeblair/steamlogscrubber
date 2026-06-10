#!/usr/bin/env python3
"""
archive.py

Archive creation helpers for steamlogscrub.

Public API expected by main.py:

    create_archives(
        source_dir,
        make_tar=True,
        make_zip=False,
        backup_existing=True,
    )

Additional lower-level API:

    create_archive(
        source_dir,
        archive_format="tar.xz",
        output_path=None,
        output_dir=None,
        backup_existing=True,
        overwrite=False,
    )

Supported formats:
    tar.xz / txz
    tar.gz / tgz
    tar.bz2 / tbz2
    tar
    zip
    7z       optional, requires py7zr
    tar.zst  optional, requires zstandard
    tzst     alias for tar.zst
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_TAR_FORMAT = "tar.xz"

FORMAT_ALIASES = {
    "xz": "tar.xz",
    ".xz": "tar.xz",
    "txz": "tar.xz",
    ".txz": "tar.xz",
    "tarxz": "tar.xz",
    "tar.xz": "tar.xz",
    ".tar.xz": "tar.xz",

    "gz": "tar.gz",
    ".gz": "tar.gz",
    "tgz": "tar.gz",
    ".tgz": "tar.gz",
    "targz": "tar.gz",
    "tar.gz": "tar.gz",
    ".tar.gz": "tar.gz",

    "bz2": "tar.bz2",
    ".bz2": "tar.bz2",
    "tbz2": "tar.bz2",
    ".tbz2": "tar.bz2",
    "tarbz2": "tar.bz2",
    "tar.bz2": "tar.bz2",
    ".tar.bz2": "tar.bz2",

    "tar": "tar",
    ".tar": "tar",

    "zip": "zip",
    ".zip": "zip",

    "7z": "7z",
    ".7z": "7z",

    "zst": "tar.zst",
    ".zst": "tar.zst",
    "tzst": "tar.zst",
    ".tzst": "tar.zst",
    "tarzst": "tar.zst",
    "tar.zst": "tar.zst",
    ".tar.zst": "tar.zst",
}

FORMAT_EXTENSIONS = {
    "tar.xz": ".tar.xz",
    "tar.gz": ".tar.gz",
    "tar.bz2": ".tar.bz2",
    "tar": ".tar",
    "zip": ".zip",
    "7z": ".7z",
    "tar.zst": ".tar.zst",
}

TARFILE_MODES = {
    "tar.xz": "w:xz",
    "tar.gz": "w:gz",
    "tar.bz2": "w:bz2",
    "tar": "w",
}


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    archive_format: str
    backup_path: Path | None = None


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def normalize_format(archive_format: str) -> str:
    normalized = archive_format.strip().lower()

    if normalized not in FORMAT_ALIASES:
        supported = ", ".join(sorted(set(FORMAT_ALIASES.values())))
        raise ValueError(
            f"Unsupported archive format: {archive_format!r}. "
            f"Supported formats: {supported}"
        )

    return FORMAT_ALIASES[normalized]


def archive_extension(archive_format: str) -> str:
    normalized = normalize_format(archive_format)
    return FORMAT_EXTENSIONS[normalized]


def default_archive_path(
    source_dir: str | Path,
    archive_format: str = DEFAULT_TAR_FORMAT,
    output_dir: str | Path | None = None,
) -> Path:
    source = Path(source_dir).expanduser().resolve()
    fmt = normalize_format(archive_format)
    ext = FORMAT_EXTENSIONS[fmt]

    base_dir = Path(output_dir).expanduser().resolve() if output_dir else source.parent
    return base_dir / f"{source.name}{ext}"


def backup_existing_file(path: str | Path) -> Path | None:
    target = Path(path).expanduser().resolve()

    if not target.exists():
        return None

    backup = target.with_name(f"{target.name}.bak-{timestamp()}")
    counter = 2

    while backup.exists():
        backup = target.with_name(f"{target.name}.bak-{timestamp()}-{counter}")
        counter += 1

    target.rename(backup)
    return backup


def prepare_archive_path(
    output_path: Path,
    backup_existing: bool,
    overwrite: bool,
) -> Path | None:
    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return None

    if overwrite:
        output_path.unlink()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return None

    if backup_existing:
        backup_path = backup_existing_file(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return backup_path

    raise FileExistsError(
        f"Archive already exists: {output_path}. "
        "Use backup_existing=True or overwrite=True."
    )


def add_zip_directory(zip_file: zipfile.ZipFile, source_dir: Path, arcname: str) -> None:
    """
    Add source_dir to a zip archive while including the top-level folder.

    zipfile does not preserve Unix metadata as well as tar, but it is the most
    convenient archive type for non-technical users on Windows.
    """
    source_dir = source_dir.resolve()

    # Add the top-level directory entry.
    zip_file.writestr(f"{arcname}/", "")

    for root, dirs, files in os.walk(source_dir):
        root_path = Path(root)

        for directory in dirs:
            directory_path = root_path / directory
            relative = directory_path.relative_to(source_dir)
            zip_file.writestr(f"{arcname}/{relative.as_posix()}/", "")

        for filename in files:
            file_path = root_path / filename
            relative = file_path.relative_to(source_dir)
            archive_name = f"{arcname}/{relative.as_posix()}"
            zip_file.write(file_path, archive_name)


def create_zip_archive(source_dir: Path, output_path: Path) -> None:
    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zip_file:
        add_zip_directory(zip_file, source_dir, source_dir.name)


def create_tar_archive(source_dir: Path, output_path: Path, archive_format: str) -> None:
    mode = TARFILE_MODES[archive_format]

    with tarfile.open(output_path, mode) as tar:
        tar.add(source_dir, arcname=source_dir.name, recursive=True)


def create_7z_archive(source_dir: Path, output_path: Path) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError(
            "7z archive support requires the optional py7zr package. "
            "Install it with: python -m pip install py7zr"
        ) from exc

    with py7zr.SevenZipFile(output_path, "w") as archive:
        archive.writeall(source_dir, arcname=source_dir.name)


def create_tar_zst_archive(source_dir: Path, output_path: Path) -> None:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError(
            "tar.zst archive support requires the optional zstandard package. "
            "Install it with: python -m pip install zstandard"
        ) from exc

    compressor = zstd.ZstdCompressor(level=10)

    with output_path.open("wb") as raw_file:
        with compressor.stream_writer(raw_file) as compressed_file:
            with tarfile.open(fileobj=compressed_file, mode="w|") as tar:
                tar.add(source_dir, arcname=source_dir.name, recursive=True)


def create_archive(
    source_dir: str | Path,
    archive_format: str = DEFAULT_TAR_FORMAT,
    output_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    backup_existing: bool = True,
    overwrite: bool = False,
) -> ArchiveResult:
    """
    Create a single archive from source_dir.

    Args:
        source_dir:
            Folder to archive. The archive includes the top-level folder itself.

        archive_format:
            One of:
                tar.xz, txz, tar.gz, tgz, tar.bz2, tbz2, tar, zip,
                7z, tar.zst, tzst

        output_path:
            Exact archive path. If omitted, source_dir name + extension is used.

        output_dir:
            Directory for the archive when output_path is omitted.

        backup_existing:
            If True, existing archive files are renamed with .bak-TIMESTAMP.

        overwrite:
            If True, existing archive files are deleted instead of backed up.

    Returns:
        ArchiveResult
    """
    source = Path(source_dir).expanduser().resolve()

    if not source.is_dir():
        raise NotADirectoryError(f"Source directory does not exist: {source}")

    fmt = normalize_format(archive_format)

    if output_path:
        target = Path(output_path).expanduser().resolve()
    else:
        target = default_archive_path(source, fmt, output_dir)

    backup_path = prepare_archive_path(
        output_path=target,
        backup_existing=backup_existing,
        overwrite=overwrite,
    )

    if fmt in TARFILE_MODES:
        create_tar_archive(source, target, fmt)
    elif fmt == "zip":
        create_zip_archive(source, target)
    elif fmt == "7z":
        create_7z_archive(source, target)
    elif fmt == "tar.zst":
        create_tar_zst_archive(source, target)
    else:
        raise ValueError(f"Unsupported archive format after normalization: {fmt}")

    return ArchiveResult(
        path=target,
        archive_format=fmt,
        backup_path=backup_path,
    )


def create_many_archives(
    source_dir: str | Path,
    formats: Iterable[str],
    output_dir: str | Path | None = None,
    backup_existing: bool = True,
    overwrite: bool = False,
) -> list[ArchiveResult]:
    results: list[ArchiveResult] = []

    for archive_format in formats:
        results.append(
            create_archive(
                source_dir=source_dir,
                archive_format=archive_format,
                output_dir=output_dir,
                backup_existing=backup_existing,
                overwrite=overwrite,
            )
        )

    return results


def create_archives(
    source_dir: str | Path,
    make_tar: bool = True,
    make_zip: bool = False,
    backup_existing: bool = True,
    formats: list[str] | tuple[str, ...] | None = None,
    output_dir: str | Path | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """
    Compatibility wrapper for main.py.

    By default:
        make_tar=True creates .tar.xz
        make_zip=True also creates .zip

    For advanced usage, pass formats directly:

        create_archives(source_dir, formats=["tar.xz", "zip", "tar.gz"])

    Returns:
        list[Path]
    """
    if formats is None:
        selected_formats: list[str] = []

        if make_tar:
            selected_formats.append(DEFAULT_TAR_FORMAT)

        if make_zip:
            selected_formats.append("zip")

        if not selected_formats:
            return []
    else:
        selected_formats = list(formats)

    results = create_many_archives(
        source_dir=source_dir,
        formats=selected_formats,
        output_dir=output_dir,
        backup_existing=backup_existing,
        overwrite=overwrite,
    )

    return [result.path for result in results]


def list_supported_formats() -> list[str]:
    return sorted(set(FORMAT_ALIASES.values()))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="archive.py",
        description="Create archives from a folder. Mostly intended for steamlogscrub internal use.",
    )

    parser.add_argument("source_dir", help="Folder to archive.")
    parser.add_argument(
        "-f",
        "--format",
        dest="archive_format",
        default=DEFAULT_TAR_FORMAT,
        help=f"Archive format. Default: {DEFAULT_TAR_FORMAT}",
    )
    parser.add_argument("-o", "--output", help="Exact output archive path.")
    parser.add_argument("--output-dir", help="Directory to place the archive in.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing archive.")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not back up existing archive. Raises an error if it exists.",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="List supported archive formats and exit.",
    )

    args = parser.parse_args()

    if args.list_formats:
        for item in list_supported_formats():
            print(item)
        raise SystemExit(0)

    result = create_archive(
        source_dir=args.source_dir,
        archive_format=args.archive_format,
        output_path=args.output,
        output_dir=args.output_dir,
        backup_existing=not args.no_backup,
        overwrite=args.overwrite,
    )

    print(f"Created: {result.path}")
    if result.backup_path:
        print(f"Backed up previous archive: {result.backup_path}")
