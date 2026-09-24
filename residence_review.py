"""Keep local residence annotations separate from approved contact records.

Callers hold the queue lock and validate that the row is a current queue record.
An annotation applies only to its record ID and the exact address reviewed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


ADDRESS_FIELDS = ("address_line_1", "locality", "region", "postal_code")
STATE_NAME = ".residence-reviews.json"
_REVIEW_FIELDS = {"county", "region", "source", "checked_at"}
_CORRUPT = "The residence review file is invalid or unsupported. Restore or review it before saving residence details."


def _plain_text(value, limit):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(unicodedata.category(character) in {"Cc", "Cf"} for character in value))


def _annotation(values, *, saved=False):
    if not isinstance(values, dict):
        raise ValueError("Enter valid residence review details.")
    if saved and (set(values) not in (_REVIEW_FIELDS, _REVIEW_FIELDS | {"matched_address"})):
        raise ValueError(_CORRUPT)
    if not _plain_text(values.get("county"), 100):
        raise ValueError("Enter a county name of 1 to 100 characters without control characters.")
    region = values.get("region")
    if not isinstance(region, str) or not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("Enter a two-letter uppercase state abbreviation.")
    if values.get("source") not in ("manual", "census"):
        raise ValueError("The residence review source must be manual or census.")
    result = {"county": values["county"].strip(), "region": region, "source": values["source"]}
    if "matched_address" in values:
        if not _plain_text(values["matched_address"], 1000):
            raise ValueError("The matched address must contain text without control characters.")
        result["matched_address"] = values["matched_address"].strip()
    if saved:
        stamp = values.get("checked_at")
        try:
            checked_at = datetime.fromisoformat(stamp)
            if checked_at.utcoffset() is None or checked_at.utcoffset().total_seconds() != 0:
                raise ValueError(_CORRUPT)
        except (TypeError, ValueError):
            raise ValueError(_CORRUPT) from None
        result["checked_at"] = stamp
    else:
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _identity(row):
    if not isinstance(row, dict) or not _plain_text(row.get("id"), 256):
        raise ValueError("Select a valid record before reviewing its residence.")
    person = row.get("person")
    if not isinstance(person, dict) or any(not isinstance(person.get(field), str) for field in ADDRESS_FIELDS):
        raise ValueError("The record must contain its four address fields before reviewing its residence.")
    address = [person[field] for field in ADDRESS_FIELDS]
    encoded = json.dumps(address, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return row["id"], hashlib.sha256(encoded).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_CORRUPT)
        result[key] = value
    return result


class ResidenceReviews:
    """Read and save private address-bound annotations under the queue lock."""

    def __init__(self, queue_dir):
        self.path = Path(queue_dir) / STATE_NAME

    def _load(self):
        if self.path.is_symlink():
            raise ValueError("The residence review file must not be a symbolic link.")
        try:
            contents = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "records": {}}
        except (OSError, UnicodeError):
            raise ValueError("The residence review file could not be read. Restore or review it before saving residence details.") from None
        try:
            data = json.loads(contents, object_pairs_hook=_unique_pairs)
            if (not isinstance(data, dict) or set(data) != {"version", "records"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["records"], dict)):
                raise ValueError(_CORRUPT)
            for record_id, entry in data["records"].items():
                if (not _plain_text(record_id, 256) or not isinstance(entry, dict)
                        or set(entry) != {"address_digest", "review"}
                        or not isinstance(entry["address_digest"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["address_digest"])):
                    raise ValueError(_CORRUPT)
                entry["review"] = _annotation(entry["review"], saved=True)
            return data
        except (ValueError, TypeError, OverflowError):
            raise ValueError(_CORRUPT) from None

    def get(self, row):
        record_id, address_digest = _identity(row)
        entry = self._load()["records"].get(record_id)
        if entry is None or entry["address_digest"] != address_digest:
            return {}
        return dict(entry["review"])

    def save(self, row, values):
        record_id, address_digest = _identity(row)
        review = _annotation(values)
        data = self._load()  # Never replace unreadable or unsupported history.
        data["records"][record_id] = {"address_digest": address_digest, "review": review}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".queue-residence-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return dict(review)
