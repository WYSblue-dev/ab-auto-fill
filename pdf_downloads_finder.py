"""Find completed checklist downloads without changing the original files.

This module only chooses and fingerprints files. extract_person.py still
opens the PDFs and validates their contents before anything is queued.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time


DEFAULT_DOWNLOADS = Path.home() / "Downloads"
# This is the literal suffix in the supplied example. --marker can change it.
DEFAULT_MARKER = "ORGANIZED"
COPY_SUFFIX = re.compile(r"\s*\(([0-9]+)\)$")
PARTIAL_SUFFIXES = (".crdownload", ".part", ".download", ".tmp")


@dataclass(frozen=True)
class Download:
    path: Path
    family: str
    sha256: str
    mtime_ns: int
    copy_number: int


@dataclass(frozen=True)
class DownloadGroup:
    # A family is a filename group, NOT proof that two records are one person.
    family: str
    files: tuple[Download, ...]


@dataclass(frozen=True)
class Discovery:
    groups: tuple[DownloadGroup, ...]
    # A not-yet-ready copy blocks its entire family, including an older PDF.
    deferred: dict[str, tuple[Path, ...]]
    errors: tuple[str, ...]


class DownloadChangedError(ValueError):
    """The file changed while it was being checked; try again after download."""


def filename_family(path: str | Path) -> tuple[str, int]:
    """Remove browser copy suffixes, while preserving the rest of the name."""
    stem = Path(path).stem.strip()
    copy_number = 0
    # Also recognize names such as 'file (1) (1).pdf'.
    while match := COPY_SUFFIX.search(stem):
        copy_number = max(copy_number, int(match.group(1)))
        stem = stem[:match.start()].rstrip()
    # Letter case and repeated spaces do not create a separate filename group.
    return " ".join(stem.split()).casefold(), copy_number


def matches_marker(path: str | Path, marker: str = DEFAULT_MARKER) -> bool:
    """Match a whole trailing marker, not an accidental substring."""
    if not marker.strip():
        raise ValueError("The filename marker cannot be blank.")
    family, _ = filename_family(path)
    # A name must precede the marker. UNORGANIZED does not match ORGANIZED.
    pattern = rf".+[-_\s]{re.escape(marker.strip())}$"
    return re.fullmatch(pattern, family, flags=re.IGNORECASE) is not None


def file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    """Size, modification time, and identity tell us whether a file changed."""
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def fingerprint_pdf(path: str | Path, min_age_seconds: float = 2.0) -> Download:
    """Hash a stable regular file; no PDF answers are read or logged here."""
    path = Path(path)
    if not math.isfinite(min_age_seconds) or min_age_seconds < 0:
        raise ValueError("The minimum download age must be a nonnegative number.")
    before = path.lstat()  # lstat lets us reject links instead of following them.
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
        raise DownloadChangedError("The download is empty or is not a regular file.")
    if time.time_ns() - before.st_mtime_ns < min_age_seconds * 1_000_000_000:
        raise DownloadChangedError("The download is too recent to process yet.")

    digest = hashlib.sha256()
    with path.open("rb") as source:
        # Recheck identity after opening, in case a browser replaced the file.
        if file_signature(os.fstat(source.fileno())) != file_signature(before):
            raise DownloadChangedError("The download changed before it could be read.")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after_read = os.fstat(source.fileno())
    after = path.lstat()
    if (file_signature(before) != file_signature(after_read)
            or file_signature(before) != file_signature(after)
            or not stat.S_ISREG(after.st_mode)):
        raise DownloadChangedError("The download changed while it was being read.")

    family, copy_number = filename_family(path)
    return Download(path, family, digest.hexdigest(), before.st_mtime_ns, copy_number)


def discover_pdfs(
    downloads_dir: str | Path = DEFAULT_DOWNLOADS,
    marker: str = DEFAULT_MARKER,
    min_age_seconds: float = 2.0,
) -> Discovery:
    """Take one snapshot of matching Downloads files, grouped by base name."""
    folder = Path(downloads_dir).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Downloads folder does not exist: {folder}")
    if not marker.strip():
        raise ValueError("The filename marker cannot be blank.")
    if not math.isfinite(min_age_seconds) or min_age_seconds < 0:
        raise ValueError("The minimum download age must be a nonnegative number.")

    grouped: dict[str, list[Download]] = {}
    deferred: dict[str, list[Path]] = {}
    errors: list[str] = []
    for path in sorted(folder.iterdir(), key=lambda value: value.name.casefold()):
        if path.name.startswith(".") or path.is_symlink():
            continue
        name = path.name
        # Browsers sometimes leave file.pdf.crdownload or file.pdf.part behind.
        partial = next((suffix for suffix in PARTIAL_SUFFIXES
                        if name.casefold().endswith(suffix)), None)
        candidate = Path(name[:-len(partial)]) if partial else path
        if candidate.suffix.casefold() != ".pdf" or not matches_marker(candidate, marker):
            continue
        family, _ = filename_family(candidate)
        if partial:
            deferred.setdefault(family, []).append(path)
            continue
        if not path.is_file():
            continue
        try:
            download = fingerprint_pdf(path, min_age_seconds)
        except DownloadChangedError:
            deferred.setdefault(family, []).append(path)
        except OSError:
            # Do not silently fall back to an older copy after a read failure.
            deferred.setdefault(family, []).append(path)
            errors.append(f"Could not read download: {path.name}")
        else:
            grouped.setdefault(family, []).append(download)

    ready = []
    for family, files in sorted(grouped.items()):
        if family in deferred:
            deferred[family].extend(file.path for file in files)
            continue
        files.sort(key=lambda file: (file.mtime_ns, file.copy_number, file.path.name.casefold()))
        ready.append(DownloadGroup(family, tuple(files)))
    return Discovery(
        tuple(ready),
        {family: tuple(paths) for family, paths in sorted(deferred.items())},
        tuple(errors),
    )
