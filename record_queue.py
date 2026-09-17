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


DEFAULT_QUEUE = Path(__file__).resolve().parent / "senders_pdfs"
STATE_NAME = ".queue-state.json"
LOCK_NAME = ".queue.lock"
CONTACT_FIELDS = {
    "given_name", "family_name", "additional_name", "email", "phone",
    "address_line_1", "locality", "region", "postal_code",
}
STATUSES = {"pending", "review", "sending", "sent", "uncertain"}
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


def _record_bytes(record: dict[str, Any]) -> bytes:
    if not isinstance(record, dict) or set(record) != CONTACT_FIELDS:
        raise QueueError("A queued record must contain exactly the nine approved contact fields.")
    if any(not isinstance(value, str) or not value.strip() for value in record.values()):
        raise QueueError("Every queued contact field must contain text.")
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

    def __enter__(self) -> "RecordQueue":
        if self._lock_identity is not None:
            raise QueueError("This queue is already open.")
        if self.directory.is_symlink():
            raise QueueError("Choose a real queue folder, not a symbolic link.")
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
                self._state = {"version": 1, "families": {}, "records": {}}
            self._validate()
            # A crash after a claim may have happened after the server accepted it.
            recovered = False
            for entry in self._state["records"].values():
                if entry["status"] == "sending":
                    entry.update(status="uncertain", reason="A previous sending run did not finish.", updated_at=_now())
                    recovered = True
            if recovered or not state_path.exists():
                self._commit()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._state = None
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
        return self._state

    def _path(self, identifier: str) -> Path:
        # Never trust a stored path: only this computed filename is permitted.
        if not ID_PATTERN.fullmatch(identifier):
            raise QueueError("The queue history contains an invalid record ID.")
        return self.directory / f"person-{identifier}.json"

    def _write_json(self, path: Path, value: Any) -> None:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".queue-", suffix=".tmp", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            # Make the renamed history durable before a POST can start.
            if os.name == "posix":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _commit(self) -> None:
        self._write_json(self.directory / STATE_NAME, self._require_open())

    def _validate(self) -> None:
        state = self._require_open()
        if not isinstance(state, dict) or set(state) != {"version", "families", "records"} or state["version"] != 1:
            raise QueueError("The queue history format is unsupported or damaged.")
        families, records = state["families"], state["records"]
        if not isinstance(families, dict) or not isinstance(records, dict):
            raise QueueError("The queue history is damaged.")
        expected_files = {STATE_NAME}
        for identifier, entry in records.items():
            path = self._path(identifier)
            expected_files.add(path.name)
            if not isinstance(entry, dict) or not isinstance(entry.get("status"), str) or entry["status"] not in STATUSES:
                raise QueueError("A queue record has an invalid status.")
            if set(entry) - {"status", "created_at", "updated_at", "reason", "result"}:
                raise QueueError("A queue record contains unexpected history fields.")
            if any(not isinstance(entry.get(key), str) for key in ("created_at", "updated_at")):
                raise QueueError("A queue record is missing its history.")
            try:
                digest = hashlib.sha256(_record_bytes(_load_json(path))).hexdigest()
            except (TypeError, ValueError) as error:
                raise QueueError("A queued record was edited or is damaged. Re-extract it through the finder.") from error
            if digest != identifier:
                raise QueueError("A queued record was edited. Re-extract it through the finder; do not send it.")
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
        for path in self.directory.iterdir():
            if path.is_dir() or path.is_symlink():
                raise QueueError("The queue contains an unexpected folder or symbolic link. Review it first.")
            if path.suffix.casefold() == ".json" and path.name not in expected_files:
                raise QueueError("The queue contains an unknown JSON file. Use the extractor to add records.")
        for identifier, entry in records.items():
            if entry["status"] == "pending" and not self._eligible(identifier):
                raise QueueError("The queue history marks a held record as pending. Review its history.")

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

    def _eligible(self, identifier: str) -> bool:
        # Shared identical contact records are sent once, even under two filenames.
        owners = [entry for entry in self._require_open()["families"].values()
                  if entry["current_id"] == identifier]
        return bool(owners) and all(not entry["review"] and not entry["deferred"] and not self._attempted(entry) for entry in owners)

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
            self._write_json(self._path(digest), record)
            records[digest] = {"status": "review", "created_at": _now(), "updated_at": _now()}
        if digest not in entry["record_ids"]:
            entry["record_ids"].append(digest)
        current = records[digest]

        # Once any version was attempted, resolving a changed file cannot create
        # another person. An operator must reconcile it with Action Builder.
        if (changed or entry["review"]) and attempted:
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

    def item_for_path(self, path: str | Path) -> QueueItem:
        """Explicitly naming a queue file must not bypass its sending history."""
        candidate = Path(path).resolve()
        for item in self.pending_records():
            if item.path == candidate:
                return item
        raise QueueError("This queue file is not pending. It may be held, already sent, or unknown.")

    def _entry_for(self, item: QueueItem) -> dict[str, Any]:
        if not isinstance(item, QueueItem) or item.path != self._path(item.id):
            raise QueueError("The sender selected an invalid queue item.")
        entry = self._require_open()["records"].get(item.id)
        if entry is None:
            raise QueueError("The sender selected an unknown queue item.")
        return entry

    def begin_send(self, item: QueueItem) -> None:
        """Persist the claim before the first network request can occur."""
        self._validate()
        entry = self._entry_for(item)
        if entry["status"] != "pending" or not self._eligible(item.id):
            raise QueueError("This record is no longer pending. Nothing was sent.")
        entry.update(status="sending", updated_at=_now())
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
