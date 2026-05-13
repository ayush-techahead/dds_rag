"""Build a flat, sorted manifest of Markdown paths inside a ZIP (root + nested folders)."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.config import settings
from app.core.exceptions import BadRequestException


@dataclass(frozen=True)
class MarkdownZipManifestEntry:
    """One Markdown member after safety and policy filters (stable `index` is manifest order)."""

    index: int
    path: str
    size_bytes: int


_SKIP_ZIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".svn",
        ".hg",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        "target",
        ".turbo",
    }
)


def is_safe_zip_member_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute():
        return False
    return ".." not in path.parts


def is_under_skipped_directory(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(_SKIP_ZIP_DIR_NAMES.intersection(parts))


def build_markdown_zip_manifest(
    zf: zipfile.ZipFile,
) -> tuple[list[MarkdownZipManifestEntry], list[str]]:
    """Return sorted flat manifest (nested paths normalized to `/`) and collector warnings."""
    warnings: list[str] = []
    raw: list[tuple[str, int]] = []
    total_declared = 0

    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if not is_safe_zip_member_path(name):
            warnings.append(f"Ignored unsafe path: {info.filename}")
            continue
        if is_under_skipped_directory(name):
            continue
        lower = name.lower()
        if not lower.endswith((".md", ".markdown")):
            continue
        if "__MACOSX/" in name or name.split("/")[-1].startswith("."):
            continue

        total_declared += info.file_size
        if total_declared > settings.ZIP_INGEST_MAX_UNCOMPRESSED_BYTES:
            raise BadRequestException(
                "ZIP declares more uncompressed data than ZIP_INGEST_MAX_UNCOMPRESSED_BYTES"
            )

        raw.append((name, int(info.file_size)))
        if len(raw) > settings.ZIP_INGEST_MAX_MARKDOWN_LISTED:
            raise BadRequestException(
                f"ZIP lists more than {settings.ZIP_INGEST_MAX_MARKDOWN_LISTED} Markdown paths "
                "after filtering. Narrow the archive or raise "
                "ZIP_INGEST_MAX_MARKDOWN_LISTED in .env."
            )

    raw.sort(key=lambda item: item[0].lower())
    entries = [
        MarkdownZipManifestEntry(index=i, path=p, size_bytes=sz) for i, (p, sz) in enumerate(raw)
    ]
    return entries, warnings
