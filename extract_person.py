"""Read contact details from downloaded Jotform checklist PDFs into a JSON queue.

Start reading at main() near the bottom, then follow its function calls.
This file reads local PDFs and queues validated contact JSON records. It never
contacts Action Builder. send_person.py still handles previewing and sending.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError

from pdf_downloads_finder import (
    DEFAULT_DOWNLOADS,
    DEFAULT_MARKER,
    DownloadChangedError,
    DownloadGroup,
    discover_pdfs,
    filename_family,
    fingerprint_pdf,
)
from record_queue import DEFAULT_QUEUE, QueueError, RecordQueue


# These measurements describe the checklist in the supplied PDF.
# A different form layout needs a separately verified set of measurements.
TEMPLATE_NAME = "New Member Checklist / LPX Data Entry"
PAGE_BOX = (0.0, 0.0, 612.0, 792.0)  # US Letter, measured in PDF points.


class ExtractionError(ValueError):
    """A document or field needs review before a record can be created."""


@dataclass(frozen=True)
class Box:
    """A rectangle on the PDF page; frozen=True prevents accidental edits."""

    # PDF coordinates begin at the bottom-left. Larger y means higher up.
    left: float
    bottom: float
    right: float
    top: float

    def contains(self, x: float, y: float) -> bool:
        # We select text by its starting position, not its drawing order.
        return self.left <= x < self.right and self.bottom <= y < self.top


@dataclass(frozen=True)
class TextFragment:
    """One piece of text and its starting position on a page."""

    text: str
    x: float
    y: float
    horizontal: bool = True


@dataclass(frozen=True)
class FieldSpec:
    """The label, answer area, and allowed line count for one contact field."""

    label: str
    area: Box
    max_lines: int = 1


# The two title lines distinguish this page from the other six documents.
HEADING_AREA = Box(180, 707, 450, 755)
HEADING_TEXT = "New Member Checklist LPX Data Entry"

# Each printed label must still be in its expected location.
# Otherwise we could apply correct coordinates to the wrong form revision.
LABEL_AREAS = {
    "Last Name": Box(70, 651, 127, 658),
    "First Name": Box(70, 622, 128, 629),
    "MI": Box(430, 622, 446, 629),
    "Address": Box(70, 593, 112, 600),
    "City": Box(70, 564, 94, 571),
    "State": Box(322, 564, 352, 571),
    "Zip": Box(430, 564, 449, 571),
    "Phone": Box(70, 535, 105, 542),
    "Email": Box(70, 360, 102, 367),
}

# Dictionary keys match the JSON fields already understood by send_person.py.
# Answer areas exclude the printed labels, SSN, birth date, and other answers.
FIELDS = {
    "given_name": FieldSpec("First Name", Box(129, 617, 425, 643)),
    "family_name": FieldSpec("Last Name", Box(129, 645, 425, 677)),
    "additional_name": FieldSpec("Middle Initial", Box(448, 617, 480, 643)),
    "email": FieldSpec("Email", Box(103, 354, 545, 385)),
    "phone": FieldSpec("Phone", Box(106, 527, 545, 555)),
    "address_line_1": FieldSpec("Address", Box(114, 583, 540, 614), max_lines=2),
    "locality": FieldSpec("City", Box(96, 556, 318, 583)),
    "region": FieldSpec("State", Box(353, 556, 429, 583)),
    "postal_code": FieldSpec("Zip", Box(450, 556, 545, 583)),
}

ZIP_PATTERN = re.compile(r"[0-9]{5}(?:-[0-9]{4})?")
EMAIL_PATTERN = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")

# A set of two-letter combinations would incorrectly accept values like ZZ.
# This mapping accepts both full state names and their postal abbreviations.
STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
    "AMERICAN SAMOA": "AS", "GUAM": "GU", "NORTHERN MARIANA ISLANDS": "MP",
    "PUERTO RICO": "PR", "US VIRGIN ISLANDS": "VI",
}
STATE_CODES = set(STATE_NAMES.values())


def clean_text(value: str) -> str:
    """Normalize harmless spacing without removing meaningful punctuation."""
    # Decode exported HTML entities such as &amp;.
    value = html.unescape(value)
    value = value.replace(r"\@", "@")
    # split/join also handles tabs and nonbreaking spaces.
    return " ".join(value.split())


def label_key(value: str) -> str:
    """Ignore the decorative underscores and punctuation in printed labels."""
    # This is for template labels only. Never apply it to a person's name.
    return re.sub(r"[\W_]+", "", value.casefold())


def read_page_fragments(page: PageObject) -> list[TextFragment]:
    """Collect positioned text for this template, without keeping SSN text."""
    fragments: list[TextFragment] = []
    relevant_areas = [HEADING_AREA, *LABEL_AREAS.values()]
    relevant_areas.extend(field.area for field in FIELDS.values())

    def visit(text, cm, tm, font, font_size):
        # Combine text placement (tm) with the page's scaling and flips (cm).
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        if not text.strip() or not any(area.contains(x, y) for area in relevant_areas):
            return

        # A rotated/skewed answer cannot be interpreted by these rectangles.
        a = tm[0] * cm[0] + tm[1] * cm[2]
        b = tm[0] * cm[1] + tm[1] * cm[3]
        c = tm[2] * cm[0] + tm[3] * cm[2]
        d = tm[2] * cm[1] + tm[3] * cm[3]
        horizontal = a > 0 and d > 0 and abs(b) < 0.001 and abs(c) < 0.001
        fragments.append(TextFragment(text, float(x), float(y), horizontal))

    # The returned whole-page text is deliberately not printed or saved.
    page.extract_text(visitor_text=visit)
    return fragments


def fragments_in(area: Box, fragments: list[TextFragment]) -> list[TextFragment]:
    """Select a rectangle, then order its pieces from top to bottom."""
    return sorted(
        (fragment for fragment in fragments if area.contains(fragment.x, fragment.y)),
        key=lambda fragment: (-fragment.y, fragment.x),
    )


def is_checklist(fragments: list[TextFragment]) -> bool:
    heading = " ".join(fragment.text for fragment in fragments_in(HEADING_AREA, fragments))
    return label_key(heading) == label_key(HEADING_TEXT)


def validate_layout(page: PageObject, fragments: list[TextFragment]) -> None:
    """Stop if the page's size, orientation, or printed labels have changed."""
    # Comparing all four numbers checks the origin as well as width/height.
    for box in (page.mediabox, page.cropbox):
        if not all(
            math.isclose(float(value), expected, abs_tol=0.1)
            for value, expected in zip(box, PAGE_BOX)
        ):
            raise ExtractionError(
                "The checklist page size or crop has changed. Review its layout."
            )
    if page.rotation % 360 or float(page.get("/UserUnit", 1)) != 1:
        raise ExtractionError(
            "The checklist is rotated or uses an unsupported scale. Review its layout."
        )

    for label, area in LABEL_AREAS.items():
        pieces = fragments_in(area, fragments)
        found = label_key(" ".join(piece.text for piece in pieces))
        if found != label_key(label) or not all(piece.horizontal for piece in pieces):
            raise ExtractionError(
                f"The checklist's {label} label moved or is missing. Review its layout."
            )


def read_answer(spec: FieldSpec, fragments: list[TextFragment]) -> str:
    """Read one field independently so missing answers cannot shift others."""
    pieces = fragments_in(spec.area, fragments)
    if not pieces:
        raise ExtractionError(f"{spec.label} is missing from the checklist. Review the PDF.")

    rows: list[list[TextFragment]] = []
    for piece in pieces:
        if not piece.horizontal:
            raise ExtractionError(
                f"{spec.label} has unsupported text positioning. Review the PDF."
            )
        # A trailing newline is normal. Multiple lines in one callback lack
        # separate coordinates, so this version cannot safely place them.
        if len([line for line in piece.text.splitlines() if line.strip()]) > 1:
            raise ExtractionError(f"{spec.label} has an unsupported text layout. Review the PDF.")
        # Baselines within two points count as the same printed line.
        if rows and abs(rows[-1][0].y - piece.y) <= 2:
            rows[-1].append(piece)
        else:
            rows.append([piece])

    # Multiple fragments on one line could be split characters, overlapping
    # answers, or duplicate text. Do not guess where spaces should go.
    if any(len(row) != 1 for row in rows) or len(rows) > spec.max_lines:
        raise ExtractionError(
            f"{spec.label} has multiple or ambiguous text pieces. Review the PDF."
        )

    # A two-line street address is kept together in the existing JSON field.
    value = " ".join(clean_text(row[0].text) for row in rows)
    if not value or not any(character.isalnum() for character in value):
        raise ExtractionError(
            f"{spec.label} is blank or contains only a placeholder. Review the PDF."
        )
    return value


def digits_only(value: str) -> str:
    """Keep only ASCII digits, as text."""
    return re.sub(r"[^0-9]", "", value)


def normalize_us_phone(value: str) -> str:
    # Reject letters/extensions instead of quietly deleting useful information.
    if not re.fullmatch(r"\+?[0-9().\s-]+", value.strip()):
        raise ExtractionError("Phone has an unsupported format or extension. Review the PDF.")
    digits = digits_only(value)
    if len(digits) == 10:
        return "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    raise ExtractionError("Phone must contain 10 US digits or 11 digits beginning with 1.")


def normalize_state(value: str) -> str:
    state = clean_text(value).upper()
    if state in STATE_CODES:
        return state
    if state in STATE_NAMES:
        return STATE_NAMES[state]
    raise ExtractionError("State is not a supported US state or territory name/abbreviation.")


def normalize_zip(value: str) -> str:
    postal_code = value.strip()
    if not ZIP_PATTERN.fullmatch(postal_code):
        raise ExtractionError("Zip must contain five digits or a ZIP+4 such as 01234-5678.")
    # Never use int(): it would remove a leading zero.
    return postal_code


def normalize_email(value: str) -> str:
    email = clean_text(value).lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ExtractionError(
            "Email is missing or does not look like an email address. Review the PDF."
        )
    # This checks basic formatting; it cannot prove that a mailbox exists.
    return email


def parse_approved_person(page: PageObject) -> dict[str, str]:
    """Parse one verified checklist page; plain text alone loses positions."""
    fragments = read_page_fragments(page)
    if not is_checklist(fragments):
        raise ExtractionError(f"The expected {TEMPLATE_NAME} heading was not found.")
    validate_layout(page, fragments)

    # A later edit to the rectangles must never assign one piece to two fields.
    for fragment in fragments:
        owners = sum(spec.area.contains(fragment.x, fragment.y) for spec in FIELDS.values())
        if owners > 1:
            raise ExtractionError("Two contact areas overlap. The extraction layout needs review.")

    # All nine fields are required for this particular source form.
    # In particular, a missing MI or email is not silently treated as optional.
    person = {key: read_answer(spec, fragments) for key, spec in FIELDS.items()}
    for key in ("given_name", "family_name"):
        value = person[key]
        # Accept accented letters and normal punctuation; do not force ASCII.
        has_letters = any(character.isalpha() for character in value)
        has_digits = any(character.isdigit() for character in value)
        if not has_letters or has_digits:
            raise ExtractionError(
                f"{FIELDS[key].label} does not look like a name. Review the PDF."
            )
    # City names can contain numbers (for example, '29 Palms').
    if not any(character.isalpha() for character in person["locality"]):
        raise ExtractionError("City must contain a name. Review the PDF.")
    initial = person["additional_name"].removesuffix(".")
    if len(initial) != 1 or not initial.isalpha():
        raise ExtractionError(
            "Middle Initial must be one letter, optionally followed by a period."
        )

    person["phone"] = normalize_us_phone(person["phone"])
    person["region"] = normalize_state(person["region"])
    person["postal_code"] = normalize_zip(person["postal_code"])
    person["email"] = normalize_email(person["email"])
    # Only these nine allowed fields can reach the JSON output.
    return person


def extract_approved_person(pdf_path: str | Path) -> dict[str, str]:
    """Find exactly one checklist anywhere in a PDF, then read its answers."""
    with Path(pdf_path).open("rb") as source:
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ExtractionError(
                "The PDF is password-protected. Use an unlocked downloaded copy."
            )

        matches = []
        for page in reader.pages:
            # Read headings instead of relying on a filename or page number.
            if is_checklist(read_page_fragments(page)):
                matches.append(page)
        if not matches:
            raise ExtractionError(f"No supported {TEMPLATE_NAME} page was found.")
        if len(matches) != 1:
            raise ExtractionError(
                "More than one checklist was found. Review each submission separately."
            )

        # Do not search another page to fill missing checklist answers.
        return parse_approved_person(matches[0])


def save_approved_person(person: dict[str, str], output_path: str | Path) -> None:
    """Replace the output only after a complete new record has been written."""
    path = Path(output_path)
    if set(person) != set(FIELDS):
        raise ExtractionError(
            "The output record must contain exactly the nine approved contact fields."
        )

    # A temporary file in the same folder lets os.replace finish atomically.
    # Readers see the old complete file or the new complete file, not half a file.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".person-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            # NamedTemporaryFile starts private on POSIX; keep that explicit.
            if os.name == "posix":
                os.chmod(temporary_path, 0o600)
            json.dump(person, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        # Only remove our own unfinished temporary file, never the old record.
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@dataclass
class ImportSummary:
    """Counts and explanations for one scan; nothing here sends an API request."""

    queued: int = 0
    unchanged: int = 0
    review: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _import_group(
    group: DownloadGroup,
    queue: RecordQueue,
    summary: ImportSummary,
    selected_path: Path | None = None,
    resolve: bool = False,
) -> str:
    """Compare versions before permitting one contact record into the queue."""
    hashes = {download.sha256 for download in group.files}
    names = [download.path.name for download in group.files]
    if queue.known_sources(group.family, hashes) and not resolve:
        status = queue.status_for(group.family)
        if status in {"review", "sending", "uncertain"}:
            summary.messages.append(f"Still needs review: {group.family}")
            return "review"
        return "unchanged"

    # An explicit resolution chooses one file, but remembers the siblings too.
    # That prevents next morning's scan from reopening the same resolved group.
    chosen = group.files
    if selected_path is not None:
        chosen = tuple(file for file in group.files
                       if file.path.resolve() == selected_path.resolve())
        if len(chosen) != 1:
            raise ExtractionError("The selected PDF could not be identified in its download group.")
        names = [chosen[0].path.name] + [name for name in names if name != chosen[0].path.name]

    records: dict[str, dict[str, str]] = {}
    seen_bytes: set[str] = set()
    try:
        for download in chosen:
            # Exact byte copies only need to be parsed once.
            if download.sha256 in seen_bytes:
                continue
            seen_bytes.add(download.sha256)
            record = extract_approved_person(download.path)
            # PDF metadata can change while the approved contact details stay equal.
            record_key = json.dumps(record, sort_keys=True, ensure_ascii=False)
            records[record_key] = record
        for download in group.files:
            # Recheck EVERY sibling, even if an identical copy was not parsed.
            if fingerprint_pdf(download.path, min_age_seconds=0).sha256 != download.sha256:
                raise DownloadChangedError("A download changed during extraction.")
    except (DownloadChangedError, OSError):
        queue.defer(group.family, names, "A download changed or became unreadable during extraction.")
        summary.messages.append(f"Download not ready: {group.family}")
        return "deferred"
    except (ExtractionError, PdfReadError) as error:
        reason = str(error) if isinstance(error, ExtractionError) else "A PDF could not be read."
        queue.hold(group.family, hashes, names, reason)
        summary.errors.append(f"{group.family}: {reason}")
        return "review"

    if len(records) != 1:
        reason = "Copies contain different contact details. Choose the correct PDF with --resolve."
        queue.hold(group.family, hashes, names, reason)
        summary.messages.append(f"Needs review: {group.family}. {reason}")
        return "review"

    status = queue.enqueue(group.family, next(iter(records.values())), hashes, names, resolve=resolve)
    if status == "review":
        summary.messages.append(
            f"Needs review: {group.family}. A changed or previously attempted record needs attention."
        )
    return "unchanged" if status in {"unchanged", "already_sent"} else status


def _count_results(summary: ImportSummary, statuses: dict[str, str]) -> ImportSummary:
    for status in statuses.values():
        setattr(summary, status, getattr(summary, status) + 1)
    return summary


def import_downloads(
    downloads_dir: str | Path = DEFAULT_DOWNLOADS,
    queue_dir: str | Path = DEFAULT_QUEUE,
    marker: str = DEFAULT_MARKER,
    min_age_seconds: float = 2.0,
) -> ImportSummary:
    """Find downloads, compare copies, and queue only validated contact JSON."""
    summary = ImportSummary()
    statuses: dict[str, str] = {}
    # The shared queue lock prevents sending halfway through a scan/correction.
    with RecordQueue(queue_dir) as queue:
        found = discover_pdfs(downloads_dir, marker, min_age_seconds)
        summary.errors.extend(found.errors)
        for family, paths in found.deferred.items():
            queue.defer(family, [path.name for path in paths], "A download is incomplete or too recent.")
            statuses[family] = "deferred"
            summary.messages.append(f"Wait for the download to finish: {family}")
        for group in found.groups:
            statuses[group.family] = _import_group(group, queue, summary)

        # A correction may arrive while older files are being parsed. Re-scan
        # before releasing the lock so that an older queued version is held.
        latest = discover_pdfs(downloads_dir, marker, min_age_seconds)
        summary.errors.extend(error for error in latest.errors if error not in summary.errors)
        latest_groups = {group.family: group for group in latest.groups}
        for group in found.groups:
            current = latest_groups.get(group.family)
            before = {(file.path.name, file.sha256) for file in group.files}
            after = {(file.path.name, file.sha256) for file in current.files} if current else set()
            if before != after:
                queue.defer(group.family, [file.path.name for file in group.files],
                            "Downloads changed during the scan. Run extraction again.")
                statuses[group.family] = "deferred"
        for family, paths in latest.deferred.items():
            queue.defer(family, [path.name for path in paths], "A download is incomplete or too recent.")
            statuses[family] = "deferred"
    return _count_results(summary, statuses)


def import_selected_pdf(
    pdf_path: str | Path,
    queue_dir: str | Path = DEFAULT_QUEUE,
    downloads_dir: str | Path = DEFAULT_DOWNLOADS,
    marker: str = DEFAULT_MARKER,
    min_age_seconds: float = 2.0,
    resolve: bool = False,
) -> ImportSummary:
    """Queue an explicit PDF, optionally resolving a group of corrected copies."""
    path = Path(pdf_path).expanduser()
    # The filename identifies which earlier record to pause even if reading fails.
    family, _ = filename_family(path)
    summary = ImportSummary()

    def selected_group(selected, discovery):
        # Explicit PDFs may live outside Downloads; always include the chosen file.
        siblings = [selected]
        for group in discovery.groups:
            if group.family == family:
                siblings.extend(file for file in group.files
                                if file.path.resolve() != path.resolve())
        return DownloadGroup(family, tuple(siblings))

    def snapshot(group):
        # Full paths distinguish an external PDF from a same-named Downloads copy.
        return {(file.path.resolve(), file.sha256) for file in group.files}

    # Take the lock before reading: the sender must not send an older version
    # while this command is checking the user's selected correction.
    with RecordQueue(queue_dir) as queue:
        try:
            selected = fingerprint_pdf(path, min_age_seconds)
        except (DownloadChangedError, OSError) as error:
            queue.defer(family, [path.name], "The selected PDF is not ready or could not be read.")
            summary.deferred = 1
            summary.messages.append("The selected PDF is not ready. Check it and run extraction again.")
            if isinstance(error, OSError):
                summary.errors.append("The selected PDF could not be read. Check its path and permissions.")
            return summary

        # Include siblings so a manual choice is remembered across future scans.
        try:
            found = discover_pdfs(downloads_dir, marker, min_age_seconds)
        except (OSError, ValueError):
            queue.defer(family, [path.name], "Related downloads could not be checked.")
            raise
        summary.errors.extend(found.errors)
        if family in found.deferred:
            paths = found.deferred[family]
            queue.defer(family, [item.name for item in paths], "A related download is not ready.")
            summary.deferred = 1
            summary.messages.append("A related download is still incomplete. Try again after it finishes.")
            return summary
        group = selected_group(selected, found)
        status = _import_group(group, queue, summary,
                               selected_path=selected.path if resolve else None, resolve=resolve)

        # Recheck even when _import_group recognized an already-handled PDF.
        # Rehashing old files alone would miss a new numbered copy arriving now.
        try:
            current_selected = fingerprint_pdf(path, min_age_seconds)
            latest = discover_pdfs(downloads_dir, marker, min_age_seconds)
        except (OSError, ValueError) as error:
            queue.defer(family, [file.path.name for file in group.files],
                        "The selected PDF or its related downloads changed during extraction.")
            status = "deferred"
            summary.messages.append("Downloads could not be rechecked. Run extraction again.")
            if not isinstance(error, DownloadChangedError):
                summary.errors.append("The PDF or Downloads folder could not be rechecked. Check the files and folder.")
        else:
            summary.errors.extend(error for error in latest.errors if error not in summary.errors)
            current_group = selected_group(current_selected, latest)
            if family in latest.deferred or snapshot(group) != snapshot(current_group):
                names = [file.path.name for file in current_group.files]
                names.extend(item.name for item in latest.deferred.get(family, ()))
                queue.defer(family, names, "Related downloads changed during extraction. Run it again.")
                status = "deferred"
                summary.messages.append("Related downloads changed. Run extraction again before sending.")
        return _count_results(summary, {group.family: status})


def main() -> int:
    """No filename means auto-find; an explicit --output keeps manual mode."""
    parser = argparse.ArgumentParser(
        description="Find downloaded checklist PDFs and queue contact JSON. Nothing is sent."
    )
    parser.add_argument("pdf_path", nargs="?", type=Path,
                        help="Optional PDF to read; otherwise scan Downloads.")
    parser.add_argument("--downloads-dir", type=Path, default=DEFAULT_DOWNLOADS,
                        help="Folder to scan; defaults to this user's Downloads.")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE,
                        help="Queue root containing pending/sent/review; defaults to composed_info.")
    parser.add_argument("--marker", default=DEFAULT_MARKER,
                        help="Literal filename ending before .pdf, ignoring numbered copies.")
    parser.add_argument("--min-age-seconds", type=float, default=2.0,
                        help="Wait this long after modification before processing a download.")
    parser.add_argument("--resolve", action="store_true",
                        help="Explicitly choose this PDF over conflicting, unsent versions.")
    parser.add_argument("--output", type=Path,
                        help="Write one standalone JSON instead of using the queue; requires a PDF path.")
    arguments = parser.parse_args()
    if arguments.output is not None and (arguments.pdf_path is None or arguments.resolve):
        parser.error("--output requires a PDF path and cannot be combined with --resolve.")
    if arguments.resolve and arguments.pdf_path is None:
        parser.error("--resolve requires the path of the PDF you have checked.")

    try:
        if arguments.output is not None:
            same_file = arguments.pdf_path.resolve() == arguments.output.resolve()
            if same_file or arguments.output.suffix.lower() != ".json":
                raise ExtractionError("Choose a separate .json output file; the source PDF must stay unchanged.")
            approved_person = extract_approved_person(arguments.pdf_path)
            save_approved_person(approved_person, arguments.output)
            print(f"Approved local record created: {arguments.output.resolve()}")
            print("Check it against the PDF before using send_person.py. Nothing was sent.")
            return 0

        if arguments.pdf_path is not None:
            summary = import_selected_pdf(
                arguments.pdf_path, arguments.queue_dir, arguments.downloads_dir,
                arguments.marker, arguments.min_age_seconds, arguments.resolve,
            )
        else:
            summary = import_downloads(
                arguments.downloads_dir, arguments.queue_dir,
                arguments.marker, arguments.min_age_seconds,
            )
        for message in summary.messages + summary.errors:
            print(message)
        print(f"Queued: {summary.queued}; already handled: {summary.unchanged}; "
              f"needs review: {summary.review}; waiting for downloads: {summary.deferred}.")
        print(f"Composed information: {arguments.queue_dir.resolve()}.")
        print("Ready records are in pending; held records are in review. Nothing was sent.")
        return 1 if summary.review or summary.deferred or summary.errors else 0
    except (ValueError, QueueError, OSError, PdfReadError) as error:
        # Field errors explain what needs review without dumping personal data.
        if isinstance(error, (ValueError, QueueError)):
            message = str(error)
        else:
            message = "A file could not be read or written. Check the paths and permissions."
        print(f"Extraction stopped: {message}", file=sys.stderr)
        if arguments.output is not None:
            print("No new record was written. Do not submit an older pending_person.json after this error.",
                  file=sys.stderr)
        else:
            print("Review the queue before sending; the scan did not complete.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
