"""Keep approved contact records and their local sending history together.

This folder contains JSON contact records, never copies of the source PDFs.
The history prevents automatic repeat sends on this computer. It is not a
replacement for checking duplicates across several computers or campaigns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from member_classification import normalize_classification


DEFAULT_QUEUE = Path(__file__).resolve().parent / "composed_info"
LEGACY_QUEUE = Path(__file__).resolve().parent / "senders_pdfs"
STATE_NAME = ".queue-state.json"
LOCK_NAME = ".queue.lock"
# Folders make progress visible. The history, not the folder, decides status.
RECORD_FOLDERS = ("pending", "sent", "review")
STATUS_FOLDERS = {"pending": "pending", "sent": "sent", "review": "review",
                  "sending": "review", "uncertain": "review", "discarded": "review"}
CONTACT_FIELDS = {
    "given_name", "family_name", "additional_name", "email", "phone",
    "address_line_1", "locality", "region", "postal_code",
}
STATUSES = {"pending", "review", "sending", "sent", "uncertain", "discarded"}
ATTEMPTED = {"sending", "sent", "uncertain"}
ID_PATTERN = re.compile(r"[0-9a-f]{64}")


class QueueError(ValueError):
    """The local queue needs attention before processing can continue."""


@dataclass(frozen=True)
class QueueItem:
    """The sender receives a tracked file, not an arbitrary folder entry."""

    id: str
    path: Path
    family: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_queue_data(directory: Path) -> bool:
    """Empty tracked folders are harmless; an older history must not be ignored."""
    waiting = [directory]
    while waiting:
        path = waiting.pop()
        if path.is_symlink():
            return True
        if path.is_dir():
            waiting.extend(path.iterdir())
        elif path.is_file():
            if path.name == ".DS_Store" or (path.name == ".gitkeep" and path.stat().st_size == 0):
                continue
            return True
        elif path.exists():
            return True
    return False


def _record_bytes(record: dict[str, Any]) -> bytes:
    if not isinstance(record, dict) or set(record) not in (CONTACT_FIELDS, CONTACT_FIELDS | {"classification"}):
        raise QueueError("A queued record must contain the nine approved contact fields and only the optional classification.")
    if any(not isinstance(value, str) or not value.strip() for value in record.values()):
        raise QueueError("Every queued contact field must contain text.")
    if "classification" in record and normalize_classification(record["classification"]) != record["classification"]:
        raise QueueError("The classification must use its approved tag name.")
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_json(path: Path) -> Any:
    # Duplicate JSON keys are suspicious; ordinary json.loads would hide them.
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QueueError("A queue file contains duplicate JSON keys. Review the queue.")
            result[key] = value
        return result

    if path.is_symlink() or not path.is_file():
        raise QueueError("A queue file is missing or is a symbolic link. Review the queue.")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QueueError("A queue file cannot be read. Restore or review the queue history.") from error


class RecordQueue:
    """Lock the folder while finding, updating, previewing, or sending records."""

    def __init__(self, directory: str | Path = DEFAULT_QUEUE):
        self.directory = Path(directory).absolute()
        self._state: dict[str, Any] | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._review_checks: set[str] = set()
        self._needs_reopen = False

    def __enter__(self) -> "RecordQueue":
        if self._lock_identity is not None:
            raise QueueError("This queue is already open.")
        if self.directory.is_symlink():
            raise QueueError("Choose a real queue folder, not a symbolic link.")
        for parent in self.directory.resolve().parents:
            marker = parent / STATE_NAME
            if marker.exists() or marker.is_symlink():
                raise QueueError("Choose the queue's main folder, not one of its pending, sent, or review subfolders.")
        if self.directory.resolve() == DEFAULT_QUEUE.resolve():
            if (LEGACY_QUEUE.exists() or LEGACY_QUEUE.is_symlink()) and _has_queue_data(LEGACY_QUEUE):
                raise QueueError("An older senders_pdfs queue exists. Move the whole folder to composed_info, "
                                 "or use --queue-dir senders_pdfs, so its sending history is preserved.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = self.directory.resolve()
        lock = self.directory / LOCK_NAME
        try:
            # O_EXCL means only one process can create this lock file.
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise QueueError(
                "The queue is locked by another run. If a run crashed, have the setup person "
                "confirm it has stopped before removing .queue.lock."
            ) from error
        try:
            stat = os.fstat(descriptor)
            self._lock_identity = (stat.st_dev, stat.st_ino)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"Process {os.getpid()} started {_now()}\n")
                stream.flush()
                os.fsync(stream.fileno())
            state_path = self.directory / STATE_NAME
            if state_path.exists() or state_path.is_symlink():
                self._state = _load_json(state_path)
            else:
                self._state = {"version": 3, "families": {}, "records": {}}
            # Validate every file before migration or recovery moves anything.
            self._validate(reconcile=False)
            migrated = self._state["version"] != 3
            self._state["version"] = 3
            # A crash after a claim may have happened after the server accepted it.
            recovered = False
            for entry in self._state["records"].values():
                if entry["status"] == "sending":
                    entry.update(status="uncertain", reason="A previous sending run did not finish.", updated_at=_now())
                    recovered = True
            if migrated or recovered or not state_path.exists():
                self._commit()
            else:
                self._reconcile_records()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._state = None
        self._review_checks.clear()
        self._needs_reopen = False
        if self._lock_identity is not None:
            lock = self.directory / LOCK_NAME
            try:
                stat = lock.lstat()
                if (stat.st_dev, stat.st_ino) == self._lock_identity:
                    lock.unlink()
            finally:
                self._lock_identity = None

    def _require_open(self) -> dict[str, Any]:
        if self._state is None or self._lock_identity is None:
            raise QueueError("Open RecordQueue with a 'with' statement before using it.")
        if self._needs_reopen:
            raise QueueError("A queue update failed. Close and reopen the queue before continuing.")
        return self._state

    def _filename(self, identifier: str) -> str:
        # Never trust a stored path: only this computed filename is permitted.
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
            raise QueueError("The queue history contains an invalid record ID.")
        return f"person-{identifier}.json"

    def _possible_paths(self, identifier: str) -> tuple[Path, ...]:
        filename = self._filename(identifier)
        # The flat path is included only to recover an old or interrupted move.
        return (self.directory / filename,
                *(self.directory / folder / filename for folder in RECORD_FOLDERS))

    def _path(self, identifier: str, status: str | None = None) -> Path:
        filename = self._filename(identifier)
        if status is None:
            entry = self._require_open()["records"].get(identifier)
            if entry is None:
                raise QueueError("The queue history refers to an unknown contact record.")
            status = entry["status"]
        if status not in STATUS_FOLDERS:
            raise QueueError("The queue history contains an invalid status.")
        return self.directory / STATUS_FOLDERS[status] / filename

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        # Saving a file and saving its directory entry are separate on POSIX.
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _ensure_folders(self) -> None:
        for name in RECORD_FOLDERS:
            path = self.directory / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise QueueError("A queue status folder is not a real directory. Review the queue.")
            path.mkdir(exist_ok=True, mode=0o700)
        self._sync_directory(self.directory)

    def _write_json(self, path: Path, value: Any) -> None:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".queue-", suffix=".tmp", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            # Make the renamed history durable before a POST can start.
            self._sync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _commit(self) -> None:
        # Check first, save the status second, and move the file last.
        # A stopped rename can then be recovered using the saved status.
        try:
            self._validate(reconcile=False)
            self._write_json(self.directory / STATE_NAME, self._require_open())
            self._reconcile_records()
        except BaseException:
            # Never reconcile unsaved in-memory decisions after a failed write.
            self._needs_reopen = True
            raise

    def _validate(self, *, reconcile: bool = True) -> None:
        state = self._require_open()
        if not isinstance(state, dict) or set(state) != {"version", "families", "records"} or type(state["version"]) is not int or state["version"] not in {1, 2, 3}:
            raise QueueError("The queue history format is unsupported or damaged.")
        families, records = state["families"], state["records"]
        if not isinstance(families, dict) or not isinstance(records, dict):
            raise QueueError("The queue history is damaged.")
        for identifier, entry in records.items():
            self._filename(identifier)
            if not isinstance(entry, dict) or not isinstance(entry.get("status"), str) or entry["status"] not in STATUSES:
                raise QueueError("A queue record has an invalid status.")
            if set(entry) - {"status", "created_at", "updated_at", "reason", "result", "lookup", "decisions", "replacement_id"}:
                raise QueueError("A queue record contains unexpected history fields.")
            if any(not isinstance(entry.get(key), str) for key in ("created_at", "updated_at")):
                raise QueueError("A queue record is missing its history.")
            if "lookup" in entry:
                self._validate_lookup(entry["lookup"])
            self._validate_decisions(identifier, entry, records)
            if state["version"] < 3 and (entry["status"] == "discarded" or "decisions" in entry or "replacement_id" in entry):
                raise QueueError("Manual review history requires queue version 3.")
        referenced = set()
        for family, entry in families.items():
            if not isinstance(family, str) or not family or not isinstance(entry, dict):
                raise QueueError("A filename group in the queue history is damaged.")
            if set(entry) != {"current_id", "record_ids", "source_hashes", "source_names", "review", "deferred", "reason"}:
                raise QueueError("A filename group has unexpected or missing history fields.")
            if not isinstance(entry["review"], bool) or not isinstance(entry["deferred"], bool) or not isinstance(entry["reason"], str):
                raise QueueError("A filename group's review status is damaged.")
            for key in ("record_ids", "source_hashes", "source_names"):
                values = entry[key]
                if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values) or len(values) != len(set(values)):
                    raise QueueError("A filename group's history is damaged.")
            if any(x not in records for x in entry["record_ids"]):
                raise QueueError("The queue history refers to an unknown contact record.")
            if entry["current_id"] is not None and entry["current_id"] not in entry["record_ids"]:
                raise QueueError("A filename group's selected record is missing.")
            self._source_details(family, set(entry["source_hashes"]), entry["source_names"])
            referenced.update(entry["record_ids"])
        if referenced != set(records):
            raise QueueError("The queue contains an untracked contact record.")
        for identifier, entry in records.items():
            if entry["status"] == "pending" and not self._eligible(identifier):
                raise QueueError("The queue history marks a held record as pending. Review its history.")
        locations = self._scan_records()
        if reconcile:
            self._reconcile_records(locations)

    def _scan_records(self) -> dict[str, Path]:
        """Find one intact copy of every tracked record, without trusting folders."""
        records = self._require_open()["records"]
        names = {self._filename(identifier): identifier for identifier in records}
        locations: dict[str, Path] = {}
        folders = [self.directory]
        for folder in folders:
            for path in folder.iterdir():
                if path.is_symlink():
                    raise QueueError("The queue contains a symbolic link. Review it before continuing.")
                if path.is_dir():
                    if folder == self.directory and path.name in RECORD_FOLDERS:
                        folders.append(path)
                        continue
                    raise QueueError("The queue contains an unexpected subfolder. Review it before continuing.")
                if not path.is_file():
                    raise QueueError("The queue contains an unsupported filesystem entry.")
                if folder == self.directory and path.name in {STATE_NAME, LOCK_NAME}:
                    continue
                # Finder creates this harmless metadata file when viewing folders.
                if path.name == ".DS_Store":
                    continue
                # Git needs an empty placeholder to include an otherwise empty folder.
                if path.name == ".gitkeep" and path.stat().st_size == 0:
                    continue
                # An interrupted atomic write can leave our private temp file.
                if re.fullmatch(r"\.queue-[A-Za-z0-9_-]+\.tmp", path.name):
                    continue
                identifier = names.get(path.name)
                if identifier is None:
                    raise QueueError("The queue contains an unknown file. Use the extractor to add records.")
                if identifier in locations:
                    raise QueueError("The queue contains duplicate copies of a record. Review them before continuing.")
                try:
                    digest = hashlib.sha256(_record_bytes(_load_json(path))).hexdigest()
                except (TypeError, ValueError) as error:
                    raise QueueError("A queued record was edited or is damaged. Re-extract it through the finder.") from error
                if digest != identifier:
                    raise QueueError("A queued record was edited. Re-extract it through the finder; do not send it.")
                locations[identifier] = path
        required = {identifier for identifier, entry in records.items() if entry["status"] != "discarded"}
        if not required <= set(locations):
            raise QueueError("A tracked contact record is missing. Restore it before continuing.")
        return locations

    def _move_record(self, source: Path, destination: Path) -> None:
        """Rename one checked file; never replace another copy."""
        if destination.exists() or destination.is_symlink():
            raise QueueError("A record already exists at the destination. Review duplicate copies.")
        os.replace(source, destination)
        self._sync_directory(source.parent)
        self._sync_directory(destination.parent)

    def _reconcile_records(self, locations: dict[str, Path] | None = None) -> None:
        """Finish interrupted moves using history, never using the folder as status."""
        if locations is None:
            locations = self._scan_records()
        self._ensure_folders()
        for identifier, source in sorted(locations.items()):
            if self._require_open()["records"][identifier]["status"] == "discarded":
                # The durable tombstone must exist before its contact file goes away.
                self._remove_discarded_file(identifier, source)
                continue
            destination = self._path(identifier)
            if source != destination:
                self._move_record(source, destination)

    def _remove_discarded_file(self, identifier: str, source: Path) -> None:
        if source not in self._possible_paths(identifier):
            raise QueueError("A discarded record has an invalid file location.")
        if hashlib.sha256(_record_bytes(_load_json(source))).hexdigest() != identifier:
            raise QueueError("A discarded file was changed. Review it before deleting anything.")
        source.unlink()
        self._sync_directory(source.parent)

    @staticmethod
    def _source_details(family: str, hashes: set[str], names: list[str]) -> None:
        if not isinstance(family, str) or not family.strip():
            raise QueueError("A source filename group is required.")
        if not isinstance(hashes, set) or any(not isinstance(x, str) or not x for x in hashes):
            raise QueueError("Source fingerprints must be nonempty strings.")
        if not isinstance(names, list) or any(not isinstance(x, str) or not x or x in {".", ".."} or "/" in x or "\\" in x for x in names):
            raise QueueError("Store source filenames only, not paths, in the queue history.")

    def _family(self, family: str, hashes: set[str], names: list[str]) -> dict[str, Any]:
        self._source_details(family, hashes, names)
        families = self._require_open()["families"]
        entry = families.setdefault(family, {
            "current_id": None, "record_ids": [], "source_hashes": [], "source_names": [],
            "review": False, "deferred": False, "reason": "",
        })
        entry["source_hashes"] = sorted(set(entry["source_hashes"]) | hashes)
        entry["source_names"] = sorted(set(entry["source_names"]) | set(names))
        return entry

    def _attempted(self, family: dict[str, Any]) -> bool:
        records = self._require_open()["records"]
        return any(records[identifier]["status"] in ATTEMPTED for identifier in family["record_ids"])

    @staticmethod
    def _validate_decisions(identifier: str, entry: dict, records: dict) -> None:
        decisions = entry.get("decisions", [])
        if not isinstance(decisions, list) or ("decisions" in entry and not decisions):
            raise QueueError("A record has invalid manual review decisions.")
        for decision in decisions:
            required = {"action", "at", "reason", "previous_status"}
            if not isinstance(decision, dict) or not required <= set(decision):
                raise QueueError("A manual review decision is damaged.")
            action = decision["action"]
            if not isinstance(action, str):
                raise QueueError("A manual review decision has an invalid action.")
            extra = {"related_id"} if action == "edited" else {"destination"} if action in {"checked", "approved_send"} else set()
            if action not in {"edited", "discarded", "checked", "approved_send"} or set(decision) != required | extra:
                raise QueueError("A manual review decision contains invalid fields.")
            if any(not isinstance(decision[key], str) or not decision[key].strip() for key in ("at", "reason")):
                raise QueueError("A manual review decision needs a date and explanation.")
            if (len(decision["reason"]) > 500 or not isinstance(decision["previous_status"], str)
                    or decision["previous_status"] not in {"review", "uncertain"}):
                raise QueueError("A manual review decision has invalid history.")
            if action == "edited" and (not isinstance(decision["related_id"], str)
                                        or decision["related_id"] not in records or decision["related_id"] == identifier):
                raise QueueError("An edited record is missing its replacement.")
            if action in {"checked", "approved_send"}:
                RecordQueue._validate_destination(decision["destination"])
        replacement = entry.get("replacement_id")
        if "replacement_id" in entry and (not isinstance(replacement, str) or replacement not in records or replacement == identifier):
            raise QueueError("A record has an invalid replacement link.")
        if entry["status"] == "discarded":
            if not decisions or decisions[-1]["action"] not in {"edited", "discarded"}:
                raise QueueError("A discarded record is missing its review decision.")
            if decisions[-1]["action"] == "edited":
                if replacement != decisions[-1]["related_id"]:
                    raise QueueError("An edited record has conflicting replacement history.")
            elif replacement is not None:
                raise QueueError("A discarded record has an unexpected replacement.")
        elif replacement is not None or (decisions and decisions[-1]["action"] in {"edited", "discarded"}):
            raise QueueError("A retired record cannot become active again.")

    @staticmethod
    def _validate_destination(destination: Any) -> None:
        if (not isinstance(destination, dict) or set(destination) != {"subdomain", "campaign_id"}
                or any(not isinstance(value, str) or not value.strip() for value in destination.values())):
            raise QueueError("An Action Builder lookup is missing its destination.")

    @staticmethod
    def _validate_lookup(lookup: Any) -> None:
        """Keep a small, understandable receipt for the read-only API check."""
        if not isinstance(lookup, dict) or set(lookup) != {
            "outcome", "reason", "candidates", "destination", "checked_at",
        }:
            raise QueueError("A record's Action Builder lookup history is damaged.")
        if not isinstance(lookup["outcome"], str) or lookup["outcome"] not in {"not_found", "existing", "needs_review"}:
            raise QueueError("A record has an invalid Action Builder lookup outcome.")
        if any(not isinstance(lookup[key], str) or not lookup[key].strip()
               for key in ("reason", "checked_at")):
            raise QueueError("An Action Builder lookup is missing its explanation or date.")
        RecordQueue._validate_destination(lookup["destination"])
        candidates = lookup["candidates"]
        if not isinstance(candidates, list):
            raise QueueError("An Action Builder lookup has invalid matching records.")
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "identifiers", "matching_fields", "differing_fields",
            }:
                raise QueueError("An Action Builder lookup has an invalid matching record.")
            for values in candidate.values():
                if (not isinstance(values, list)
                        or any(not isinstance(value, str) or not value.strip() for value in values)):
                    raise QueueError("An Action Builder lookup has invalid matching details.")
            if not candidate["identifiers"]:
                raise QueueError("An Action Builder match is missing its identifier.")
        if lookup["outcome"] == "not_found" and candidates:
            raise QueueError("A lookup cannot report no match while containing matching people.")
        # Keep older empty-candidate review holds readable; never release them here.
        if lookup["outcome"] == "existing" and not candidates:
            raise QueueError("An existing-person lookup is missing its matching people.")
        if lookup["outcome"] == "existing" and (
            len(candidates) != 1 or candidates[0]["differing_fields"]
        ):
            raise QueueError("An exact existing-person lookup has conflicting matching details.")

    def _related_ids(self, record_ids: list[str]) -> set[str]:
        """Follow identical records shared across differently named downloads."""
        state = self._require_open()
        related_ids = set(record_ids)
        unchecked = list(state["families"].values())
        changed = True
        while changed:
            changed = False
            remaining = []
            for related_family in unchecked:
                if related_ids.intersection(related_family["record_ids"]):
                    related_ids.update(related_family["record_ids"])
                    changed = True
                else:
                    remaining.append(related_family)
            unchecked = remaining
        return related_ids

    def _manual_held(self, family: dict[str, Any]) -> bool:
        records = self._require_open()["records"]
        return any(records[identifier].get("decisions") for identifier in self._related_ids(family["record_ids"]))

    def _lookup_held(self, family: dict[str, Any]) -> bool:
        """PDF resolution cannot clear an unresolved API precheck.

        Identical records can connect several filename groups. Follow those
        connections so choosing a different filename or version cannot bypass
        a hold. A new GET check is required before any future POST; a saved
        'not_found' receipt is never permanent permission to create someone.
        """
        state = self._require_open()
        related_ids = self._related_ids(family["record_ids"])
        return any(state["records"][identifier].get("lookup", {}).get("outcome")
                   in {"existing", "needs_review"} for identifier in related_ids)

    def _eligible(self, identifier: str) -> bool:
        # Shared identical contact records are sent once, even under two filenames.
        owners = [entry for entry in self._require_open()["families"].values()
                  if entry["current_id"] == identifier]
        return bool(owners) and all(not entry["review"] and not entry["deferred"]
                                   and not self._attempted(entry) and not self._lookup_held(entry)
                                   and not self._manual_held(entry)
                                   for entry in owners)

    def _hold_related(self, identifier: str, reason: str) -> None:
        related = self._related_ids([identifier])
        for family in self._require_open()["families"].values():
            if related.intersection(family["record_ids"]):
                self._revoke(family)
                family.update(review=True, deferred=False, reason=reason[:500])

    def _hold_lookup_families(self, reason: str) -> None:
        """Apply an API review hold to every connected filename group."""
        for family in self._require_open()["families"].values():
            if self._lookup_held(family):
                self._revoke(family)
                family.update(review=True, deferred=False, reason=reason[:500])

    def _revoke(self, family: dict[str, Any]) -> None:
        for identifier in family["record_ids"]:
            record = self._require_open()["records"][identifier]
            if record["status"] == "pending":
                record.update(status="review", updated_at=_now(), reason="A related download needs review.")

    def known_sources(self, family: str, hashes: set[str]) -> bool:
        self._source_details(family, hashes, [])
        entry = self._require_open()["families"].get(family)
        return bool(entry is not None and not entry["deferred"] and hashes and hashes <= set(entry["source_hashes"]))

    def status_for(self, family: str) -> str | None:
        """Let the finder describe a known group without opening its record."""
        entry = self._require_open()["families"].get(family)
        if entry is None:
            return None
        current = self._require_open()["records"].get(entry["current_id"], {})
        if current.get("status") in {"sent", "discarded"}:
            return current["status"]
        if entry["review"]:
            return "review"
        if entry["deferred"]:
            return "deferred"
        if entry["current_id"] is None:
            return None
        return self._require_open()["records"][entry["current_id"]]["status"]

    def status_counts(self) -> dict[str, int]:
        """Count filename groups so a quiet queue cannot hide review work."""
        counts: dict[str, int] = {}
        for family in self._require_open()["families"]:
            status = self.status_for(family)
            if status is not None:
                counts[status] = counts.get(status, 0) + 1
        return counts

    def defer(self, family: str, source_names: list[str], reason: str) -> None:
        """Pause this group while a browser download is still incomplete."""
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("A deferral reason is required.")
        entry = self._family(family, set(), source_names)
        self._revoke(entry)
        # Keep a prior review hold; an incomplete download cannot clear it.
        entry.update(deferred=True, reason=reason[:500])
        self._commit()

    def hold(self, family: str, source_hashes: set[str], source_names: list[str], reason: str) -> None:
        """A changed or unreadable download removes that group's pending send."""
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("A review reason is required.")
        entry = self._family(family, source_hashes, source_names)
        self._revoke(entry)
        entry.update(review=True, deferred=False, reason=reason[:500])
        self._commit()

    def enqueue(self, family: str, record: dict, source_hashes: set[str], source_names: list[str],
                resolve: bool = False) -> str:
        """Save one validated record, holding changed versions for review."""
        digest = hashlib.sha256(_record_bytes(record)).hexdigest()
        entry = self._family(family, source_hashes, source_names)
        entry["deferred"] = False
        records = self._require_open()["records"]
        old_id = entry["current_id"]
        changed = old_id is not None and old_id != digest
        attempted = self._attempted(entry)
        already_exists = digest in records
        if not already_exists:
            # An interruption before the history commit leaves an unknown file;
            # the next run stops for review instead of guessing its status.
            if any(path.exists() or path.is_symlink() for path in self._possible_paths(digest)):
                raise QueueError("An untracked copy of this record already exists. Review the queue.")
            self._write_json(self._path(digest, "review"), record)
            records[digest] = {"status": "review", "created_at": _now(), "updated_at": _now()}
        if digest not in entry["record_ids"]:
            entry["record_ids"].append(digest)
        current = records[digest]

        if current["status"] == "discarded":
            # A discarded hash is a tombstone, even under a new filename.
            if entry["current_id"] is None:
                entry["current_id"] = digest
            self._hold_related(digest, "A related record was discarded or replaced during manual review.")
            self._commit()
            return "unchanged"

        # Choosing a PDF cannot clear an API hold or erase an earlier send attempt.
        if self._manual_held(entry):
            self._hold_related(digest, "Related records have manual review history. Use the review command.")
            entry["current_id"] = digest
            outcome = "already_sent" if current["status"] == "sent" else "review"
        elif self._lookup_held(entry):
            # This new version may connect two previously separate groups.
            self._hold_lookup_families("A related record may already exist in Action Builder. Review its lookup history.")
            entry.update(current_id=digest, review=True,
                         reason="A related record may already exist in Action Builder. Review its lookup history.")
            outcome = "review"
        elif (changed or entry["review"]) and attempted:
            self._revoke(entry)
            entry.update(current_id=digest, review=True, reason="A related record was already sent or attempted. Check Action Builder.")
            outcome = "review"
        elif (changed or entry["review"]) and not resolve:
            self._revoke(entry)
            entry.update(current_id=digest, review=True, reason="Related downloads contain different details. Choose the correct PDF explicitly.")
            outcome = "review"
        else:
            if resolve:
                self._revoke(entry)
            entry.update(current_id=digest, review=False, reason="")
            if current["status"] == "sent":
                outcome = "already_sent"
            elif current["status"] in {"sending", "uncertain"}:
                entry.update(review=True, reason="A previous send has an uncertain outcome. Check Action Builder.")
                outcome = "review"
            elif self._eligible(digest):
                was_pending = current["status"] == "pending"
                current.update(status="pending", updated_at=_now())
                current.pop("reason", None)
                outcome = "unchanged" if was_pending else "queued"
            else:
                entry.update(review=True, reason="Another related download still needs review.")
                outcome = "review"
        self._commit()
        return outcome

    def pending_records(self) -> list[QueueItem]:
        self._validate()
        state = self._require_open()
        result = []
        for identifier, entry in state["records"].items():
            if entry["status"] == "pending":
                family = sorted(name for name, data in state["families"].items() if data["current_id"] == identifier)[0]
                result.append(QueueItem(identifier, self._path(identifier), family))
        return sorted(result, key=lambda item: (item.family, item.id))

    def review_records(self) -> list[QueueItem]:
        """Return each held contact once, including uncertain send attempts."""
        self._validate()
        state = self._require_open()
        result = []
        for identifier, entry in state["records"].items():
            if entry["status"] not in {"review", "uncertain"}:
                continue
            owners = sorted(name for name, family in state["families"].items()
                            if identifier in family["record_ids"])
            result.append(QueueItem(identifier, self._path(identifier), owners[0]))
        return sorted(result, key=lambda item: (item.family, item.id))

    def review_details(self, item: QueueItem) -> dict[str, Any]:
        """Explain a hold without exposing unrelated records or stored secrets."""
        self._validate()
        entry = self._entry_for(item)
        state = self._require_open()
        related = self._related_ids([item.id])
        families = {name: family for name, family in state["families"].items()
                    if related.intersection(family["record_ids"])}
        records = [state["records"][identifier] for identifier in related]
        reasons = sorted({family["reason"] for family in families.values() if family["reason"]})
        return {
            "status": entry["status"],
            "reason": entry.get("reason") or "; ".join(reasons) or "This record needs manual review.",
            "lookup": json.loads(json.dumps(entry.get("lookup"))),
            "source_names": sorted({name for family in families.values() for name in family["source_names"]}),
            "families": sorted(families),
            "related_sent": any(record["status"] == "sent" or "result" in record for record in records),
            "related_uncertain": any(record["status"] in {"sending", "uncertain"}
                                     or any(decision["previous_status"] == "uncertain"
                                            for decision in record.get("decisions", []))
                                     for record in records),
        }

    @staticmethod
    def _review_reason(reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("A manual review decision needs an explanation.")
        return reason.strip()[:500]

    @staticmethod
    def _require_review(entry: dict) -> None:
        if entry["status"] not in {"review", "uncertain"}:
            raise QueueError("This record is no longer awaiting manual review.")

    def save_review_edit(self, item: QueueItem, record: dict[str, Any]) -> QueueItem:
        """Replace a reviewed contact with a new hash, preserving its old hold."""
        self._validate()
        entry = self._entry_for(item)
        self._require_review(entry)
        digest = hashlib.sha256(_record_bytes(record)).hexdigest()
        if digest == item.id:
            return QueueItem(item.id, self._path(item.id), item.family)
        state = self._require_open()
        if digest in state["records"] or any(path.exists() or path.is_symlink() for path in self._possible_paths(digest)):
            raise QueueError("Those details already exist in queue history; cannot overwrite or restore that record here.")
        # Write the replacement first. A failed history save never loses the old contact.
        self._write_json(self._path(digest, "review"), record)
        timestamp = _now()
        state["records"][digest] = {
            "status": "review", "created_at": timestamp, "updated_at": timestamp,
            "reason": "Contact details were edited during manual review. Review the corrected record before sending.",
        }
        entry.setdefault("decisions", []).append({
            "action": "edited", "at": timestamp, "reason": "Contact details corrected during manual review.",
            "previous_status": entry["status"], "related_id": digest,
        })
        entry.update(status="discarded", replacement_id=digest, updated_at=timestamp,
                     reason="Replaced by a corrected record during manual review.")
        for family in state["families"].values():
            if item.id in family["record_ids"]:
                family["record_ids"].append(digest)
                if family["current_id"] == item.id:
                    family["current_id"] = digest
        self._review_checks.discard(item.id)
        self._hold_related(digest, "A related record was edited during manual review. Review before sending.")
        self._commit()
        return QueueItem(digest, self._path(digest), item.family)

    def discard_review(self, item: QueueItem, reason: str) -> None:
        """Save a permanent disposition before deleting the managed contact file."""
        self._validate()
        entry = self._entry_for(item)
        reason = self._review_reason(reason)
        if entry["status"] == "discarded":
            return
        self._require_review(entry)
        timestamp = _now()
        entry.setdefault("decisions", []).append({
            "action": "discarded", "at": timestamp, "reason": reason,
            "previous_status": entry["status"],
        })
        entry.update(status="discarded", updated_at=timestamp, reason=reason)
        self._review_checks.discard(item.id)
        self._hold_related(item.id, "A related record was discarded during manual review.")
        self._commit()

    def record_review_lookup(self, item: QueueItem, lookup: dict[str, Any]) -> None:
        """Save a fresh GET result without releasing the manual review hold."""
        self._validate()
        self._review_checks.discard(item.id)
        self._validate_lookup(lookup)
        entry = self._entry_for(item)
        self._require_review(entry)
        timestamp = _now()
        entry.update(lookup=json.loads(json.dumps(lookup)), updated_at=timestamp)
        entry.setdefault("decisions", []).append({
            "action": "checked", "at": timestamp, "reason": "Refreshed API lookup; manual review is still required.",
            "previous_status": entry["status"], "destination": dict(lookup["destination"]),
        })
        self._hold_related(item.id, "Manual review is still required after this Action Builder check.")
        self._commit()
        self._review_checks.add(item.id)

    def reconcile_sent(self, item: QueueItem, *, lookup: dict[str, Any], reason: str) -> None:
        """Resolve an uncertain attempt using a newly verified exact match, no POST."""
        self._validate()
        entry = self._entry_for(item)
        if entry["status"] != "uncertain":
            raise QueueError("Only an uncertain submission can be marked already created here.")
        reason = self._review_reason(reason)
        self._validate_lookup(lookup)
        prior = entry.get("lookup")
        if not prior or prior["destination"] != lookup["destination"]:
            raise QueueError("The verification campaign must match the saved submission lookup.")
        if lookup["outcome"] != "existing" or len(lookup["candidates"]) != 1:
            raise QueueError("Reconciliation requires one exact matching Action Builder person.")
        candidate = lookup["candidates"][0]
        identifiers = candidate["identifiers"]
        if (candidate["differing_fields"] or len(identifiers) != 1 or not re.fullmatch(
                r"action_builder:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", identifiers[0])):
            raise QueueError("Reconciliation requires one valid Action Builder person ID and matching details.")
        # Retain a checked decision in the existing history format. This does
        # not record a new approved_send decision or consume a sending attempt.
        self.record_review_lookup(item, lookup)
        entry["decisions"][-1]["reason"] = reason
        entry.update(status="sent", updated_at=_now(), reason=reason,
                     result={"identifiers": list(identifiers)})
        self._review_checks.discard(item.id)
        self._commit()

    def begin_review_send(self, item: QueueItem, *, config_destination: dict[str, str], reason: str) -> None:
        """Claim one explicitly approved send; connected alternatives stay held."""
        self._validate()
        entry = self._entry_for(item)
        self._require_review(entry)
        reason = self._review_reason(reason)
        self._validate_destination(config_destination)
        if self.review_details(item)["related_sent"]:
            raise QueueError("A related record was already sent. Do not create this person again.")
        if any(self._require_open()["records"][identifier]["status"] == "sending"
               for identifier in self._related_ids([item.id])):
            raise QueueError("A related send is still in progress. Wait for its recorded outcome.")
        if item.id not in self._review_checks or entry.get("lookup", {}).get("destination") != config_destination:
            raise QueueError("Run a fresh review lookup for this destination before approving a send.")
        timestamp = _now()
        entry.setdefault("decisions", []).append({
            "action": "approved_send", "at": timestamp, "reason": reason,
            "previous_status": entry["status"], "destination": dict(config_destination),
        })
        self._hold_related(item.id, "A related record was manually approved for sending. Check its history.")
        for family in self._require_open()["families"].values():
            if item.id in family["record_ids"]:
                family["current_id"] = item.id
        entry.update(status="sending", updated_at=timestamp, reason="Manually approved for one submission.")
        self._review_checks.discard(item.id)
        self._commit()

    def item_for_path(self, path: str | Path) -> QueueItem:
        """Explicitly naming a queue file must not bypass its sending history."""
        candidate = Path(path).resolve()
        for item in self.pending_records():
            # Opening an older queue may have just moved its flat file.
            if candidate in self._possible_paths(item.id):
                return item
        raise QueueError("This queue file is not pending. It may be held, already sent, or unknown.")

    def _entry_for(self, item: QueueItem) -> dict[str, Any]:
        # An issued item's pending path becomes stale after begin_send moves it.
        # Accept its original controlled path; still require the same tracked ID.
        if not isinstance(item, QueueItem) or item.path not in self._possible_paths(item.id):
            raise QueueError("The sender selected an invalid queue item.")
        entry = self._require_open()["records"].get(item.id)
        if entry is None:
            raise QueueError("The sender selected an unknown queue item.")
        family = self._require_open()["families"].get(item.family)
        if family is None or item.id not in family["record_ids"]:
            raise QueueError("The sender selected an item from the wrong filename group.")
        return entry

    def begin_send(self, item: QueueItem) -> None:
        """Persist the claim before POST; earlier GET checks do not claim a send."""
        self._validate()
        entry = self._entry_for(item)
        if entry["status"] != "pending" or not self._eligible(item.id):
            raise QueueError("This record is no longer pending. Nothing was sent.")
        entry.update(status="sending", updated_at=_now())
        self._commit()

    def record_lookup(self, item: QueueItem, lookup: dict[str, Any]) -> None:
        """Save GET evidence; keep not_found pending and hold review outcomes."""
        self._validate()
        self._validate_lookup(lookup)
        entry = self._entry_for(item)
        if entry["status"] != "pending" or not self._eligible(item.id):
            raise QueueError("This record is no longer pending. Its lookup was not saved.")
        # Copy the receipt so later caller changes cannot alter queue history.
        entry.update(lookup=json.loads(json.dumps(lookup)), updated_at=_now())
        if lookup["outcome"] != "not_found":
            # Keep every unresolved version on hold, including filename aliases.
            self._hold_lookup_families(lookup["reason"])
            entry.update(status="review", reason=lookup["reason"][:500])
        self._commit()

    def finish_send(self, item: QueueItem, result: dict) -> None:
        entry = self._entry_for(item)
        if entry["status"] != "sending":
            raise QueueError("Only a claimed record can be marked sent.")
        # Retain a receipt, not the API's potentially much larger person record.
        person = result.get("person", {}) if isinstance(result, dict) else {}
        receipt = {}
        if isinstance(person, dict):
            if isinstance(person.get("browser_url"), str):
                receipt["browser_url"] = person["browser_url"]
            identifiers = person.get("identifiers")
            if isinstance(identifiers, list) and all(isinstance(value, str) for value in identifiers):
                receipt["identifiers"] = identifiers
        entry.update(status="sent", updated_at=_now(), result=receipt)
        self._commit()

    def mark_uncertain(self, item: QueueItem, reason: str) -> None:
        entry = self._entry_for(item)
        if entry["status"] not in {"sending", "uncertain", "sent"}:
            raise QueueError("Only an attempted send can have an uncertain outcome.")
        entry.update(status="uncertain", updated_at=_now(), reason=str(reason)[:500])
        self._commit()
