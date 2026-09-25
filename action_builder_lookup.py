"""Check Action Builder before a pending contact can become a new person.

Run this file to check the pending queue using GET requests only. The sender
also uses the same check immediately before each POST; an old search result
is never permission to send later. Searches cover the configured campaign,
not every campaign in the organization and not newer PDFs in Downloads.

Only email and phone filters are used. The live endpoint rejected the
documented name filters. Names and addresses are still compared locally on
returned candidates. Two complete searches with no matches clear the record
for submission. This can miss someone whose email and phone have both changed.

API references:
https://www.actionbuilder.org/docs/v1/index.html
https://www.actionbuilder.org/docs/v1/people.html
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv

from action_builder_http import get_with_retries


class LookupError(RuntimeError):
    """The search was incomplete; this is different from finding no match."""


@dataclass(frozen=True)
class ActionBuilderConfig:
    """Use one checked destination for both the lookup and the eventual POST."""

    api_key: str = field(repr=False)  # Do not expose credentials in repr().
    subdomain: str
    campaign_id: str
    residence_local: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.residence_local, str) or (self.residence_local and not re.fullmatch(r"[0-9]{1,6}", self.residence_local)):
            raise LookupError("ACTION_BUILDER_RESIDENCE_LOCAL must be a local number, or blank for the legacy contact-only workflow.")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise LookupError("ACTION_BUILDER_API_KEY is required.")
        if any(character.isspace() for character in self.api_key):
            raise LookupError("ACTION_BUILDER_API_KEY cannot contain whitespace.")
        # A subdomain is a single DNS label, never a full URL or hostname.
        if not isinstance(self.subdomain, str) or not re.fullmatch(
            r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?",
            self.subdomain,
        ):
            raise LookupError(
                "ACTION_BUILDER_SUBDOMAIN must be the single name before .actionbuilder.org."
            )
        # Excluding URL punctuation prevents a campaign setting changing paths.
        if not isinstance(self.campaign_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]+",
            self.campaign_id,
        ):
            raise LookupError("ACTION_BUILDER_CAMPAIGN_ID must be a single identifier.")
        if any(
            value.startswith("your_actual_")
            for value in (
                self.api_key,
                self.subdomain,
                self.campaign_id,
            )
        ):
            raise LookupError(
                "Replace the example Action Builder settings before checking records."
            )

    @classmethod
    def from_environment(cls) -> "ActionBuilderConfig":
        # This is called only by an explicit API check or submission, not preview.
        load_dotenv()
        return cls(
            *(
                os.environ.get(name, "").strip()
                for name in (
                    "ACTION_BUILDER_API_KEY",
                    "ACTION_BUILDER_SUBDOMAIN",
                    "ACTION_BUILDER_CAMPAIGN_ID",
                    "ACTION_BUILDER_RESIDENCE_LOCAL",
                )
            )
        )

    @property
    def people_url(self) -> str:
        return (
            f"https://{self.subdomain.lower()}.actionbuilder.org/api/rest/v1/"
            f"campaigns/{self.campaign_id}/people"
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"OSDI-API-Token": self.api_key, "Accept": "application/hal+json"}

    @property
    def destination(self) -> dict[str, str]:
        # Save the destination in history, never its API key.
        return {"subdomain": self.subdomain.lower(), "campaign_id": self.campaign_id}


@dataclass(frozen=True)
class LookupResult:
    """A decision plus small candidate summaries, without copying remote PII."""

    outcome: str
    reason: str
    candidates: tuple[dict[str, Any], ...] = ()

    def as_history(self, config: ActionBuilderConfig) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "destination": config.destination,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def _text(value: str) -> str:
    # Whitespace and capitalization do not establish a different person.
    return " ".join(value.split()).casefold()


# Deliberately limited to common primary/standard pairs from USPS Pub. 28 C1:
# https://pe.usps.com/text/pub28/28apc_002.htm
# This is a comparison aid, not address validation or a full postal parser.
_STREET_SUFFIXES = {
    "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "circle": "cir",
    "place": "pl", "parkway": "pkwy", "terrace": "ter", "trail": "trl",
    "highway": "hwy",
}
_STREET_SUFFIXES.update({value: value for value in tuple(_STREET_SUFFIXES.values())})
_DIRECTIONS = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south",
    "east", "west", "northeast", "northwest", "southeast", "southwest",
}
# Freeze the entire secondary-address tail, including identifiers such as
# "UNIT ST". Never mistake those identifiers for street suffixes (USPS C2).
_UNIT_MARKERS = {
    "apartment", "apt", "basement", "bsmt", "building", "bldg", "department",
    "dept", "floor", "fl", "front", "frnt", "hangar", "hngr", "key", "lobby",
    "lbby", "lot", "lower", "lowr", "office", "ofc", "penthouse", "ph",
    "pier", "rear", "room", "rm", "side", "slip", "space", "spc", "stop",
    "suite", "ste", "trailer", "trlr", "unit", "upper", "uppr",
}


def _street_text(value: str) -> str:
    """Compare a numbered street's suffix while preserving all other tokens."""
    text = _text(value)
    tokens = text.split()
    if (len(tokens) < 3 or not re.fullmatch(r"[0-9]+[a-z]?(?:-[0-9]+[a-z]?)?", tokens[0])
            or re.fullmatch(r"[0-9]+/[0-9]+", tokens[1])):
        return text  # PO boxes, rural routes and other formats remain exact.
    street_end = next(
        (index for index, token in enumerate(tokens[1:], 1)
         if token.removesuffix(".") in _UNIT_MARKERS or token.startswith("#")),
        len(tokens),
    )
    unit = tokens[street_end:]
    if unit:
        identifier = (unit[1] if len(unit) == 2 else
                      unit[0][1:] if len(unit) == 1 and unit[0].startswith("#") else "")
        if not (re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier)
                and (len(identifier) == 1 or any(char.isdigit() for char in identifier))):
            # Unknown tails may be part of a street name, e.g. FRONT ROAD.
            # Only recognize a simple unit with a number or single-letter ID.
            return text
    # Locate an optional postdirectional without changing or removing it.
    if tokens[street_end - 1].removesuffix(".") in _DIRECTIONS:
        street_end -= 1
    suffix_index = street_end - 1
    if suffix_index < 2:
        return text  # Require separate house-number and street-name content.
    suffix = _STREET_SUFFIXES.get(tokens[suffix_index].removesuffix("."))
    if suffix is not None:
        name_end = suffix_index
        while name_end > 2 and tokens[name_end - 1].removesuffix(".") in _STREET_SUFFIXES:
            name_end -= 1
        if any(token.removesuffix(".") in _STREET_SUFFIXES for token in tokens[2:name_end]):
            # An earlier suffix separated by other words could start an
            # unsupported unit tail. Leave that ambiguous address exact.
            return text
        # Only this token may change: ST JOHN and MAIN AVENUE COURT retain
        # their street-name words. House/unit numbers and punctuation survive.
        tokens[suffix_index] = suffix
    return " ".join(tokens)


def _phone(value: str) -> str:
    # Compare US formatting variants, without silently stripping letters.
    if not re.fullmatch(r"[0-9+().\s-]+", value):
        return ""
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 10:
        digits = "1" + digits
    return digits if re.fullmatch(r"1[0-9]{10}", digits) else ""


def _identifier(person: dict[str, Any]) -> str:
    identifiers = person.get("identifiers")
    if not isinstance(identifiers, list) or any(
        not isinstance(v, str) for v in identifiers
    ):
        raise LookupError(
            "A search result has no valid identifier list. Review Action Builder."
        )
    native = [v for v in identifiers if v.startswith("action_builder:")]
    if len(native) != 1 or not re.fullmatch(
        r"action_builder:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        native[0],
    ):
        raise LookupError(
            "A search result has no unambiguous Action Builder person ID."
        )
    return native[0]


def _values(person: dict[str, Any], collection: str, key: str) -> list[str]:
    entries = person.get(collection, [])
    if not isinstance(entries, list) or any(not isinstance(v, dict) for v in entries):
        raise LookupError("A search result contains malformed contact fields.")
    values = [v.get(key, "") for v in entries]
    if any(not isinstance(v, str) for v in values):
        raise LookupError("A search result contains malformed contact values.")
    return values


def _validate_person(person: Any) -> None:
    if not isinstance(person, dict):
        raise LookupError("The search returned an invalid person record.")
    _identifier(person)
    for name in (
        "given_name",
        "family_name",
        "additional_name",
        "action_builder:entity_type",
    ):
        if name in person and not isinstance(person[name], str):
            raise LookupError("The search returned an invalid name or entity type.")
    _values(person, "email_addresses", "address")
    _values(person, "phone_numbers", "number")
    for name in ("locality", "region", "postal_code", "country"):
        _values(person, "postal_addresses", name)
    for address in person.get("postal_addresses", []):
        lines = address.get("address_lines", [])
        if not isinstance(lines, list) or any(not isinstance(v, str) for v in lines):
            raise LookupError("The search returned an invalid street address.")


def _comparison(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, bool]:
    matches = {
        "given_name": _text(local["given_name"]) == _text(remote.get("given_name", "")),
        "family_name": _text(local["family_name"])
        == _text(remote.get("family_name", "")),
        "additional_name": _text(local.get("additional_name", "")).rstrip(".")
        == _text(remote.get("additional_name", "")).rstrip("."),
        "email": _text(local["email_addresses"][0]["address"])
        in {
            _text(v)
            for v in _values(remote, "email_addresses", "address")
        },
        "phone": _phone(local["phone_numbers"][0]["number"])
        in {
            _phone(v)
            for v in _values(remote, "phone_numbers", "number")
        },
    }
    local_address = local["postal_addresses"][0]
    # Pick one address to compare. Never combine pieces from different homes.
    address_matches = []
    for address in remote.get("postal_addresses", []):
        address_matches.append(
            {
                "address_line_1": _street_text(" ".join(local_address["address_lines"]))
                == _street_text(" ".join(address.get("address_lines", []))),
                **{
                    name: _text(local_address[name]) == _text(address.get(name, ""))
                    for name in ("locality", "region", "postal_code")
                },
            }
        )
    best_address = max(
        address_matches,
        key=lambda v: sum(v.values()),
        default={
            name: False
            for name in ("address_line_1", "locality", "region", "postal_code")
        },
    )
    matches.update(best_address)
    return matches


class ActionBuilderLookup:
    """Issue read-only, bounded, fully paginated searches for one campaign."""

    MAX_PAGES = 100  # A broad/unstable search must stop for attention.
    REQUEST_INTERVAL = 0.3  # Action Builder documents a four-requests/sec limit.

    def __init__(self, config: ActionBuilderConfig):
        self.config = config
        self._next_request_at = 0.0

    def _get_page(self, expression: str, page: int) -> dict[str, Any]:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            # Requests encodes the filter. Never put personal data in log output.
            response = get_with_retries(
                self.config.people_url,
                headers=self.config.headers,
                params={"filter": expression, "page": page},
                timeout=(5, 30),
                allow_redirects=False,
            )
            if response.status_code != 200:
                search_field = expression.partition(" eq ")[0]
                if search_field not in {"email_address", "phone_number"}:
                    search_field = "person"
                raise LookupError(
                    f"Action Builder {search_field} lookup stopped on page {page}: "
                    f"HTTP {response.status_code}. Nothing was created for this record."
                )
            result = response.json()
        except requests.RequestException:
            # Raw request errors can include the email/phone query and URL.
            raise LookupError(
                "Action Builder lookup failed. Check the connection and settings; no person was created for this record."
            ) from None
        except ValueError:
            raise LookupError(
                "Action Builder lookup did not return valid JSON."
            ) from None
        finally:
            # Also space the next request after a failed one.
            self._next_request_at = time.monotonic() + self.REQUEST_INTERVAL
        if not isinstance(result, dict):
            raise LookupError(
                "Action Builder lookup did not return a collection object."
            )
        return result

    def _search(self, name: str, value: str) -> list[dict[str, Any]]:
        # Apostrophes inside an OData string literal are doubled.
        escaped_value = value.replace("'", "''")
        expression = f"{name} eq '{escaped_value}'"
        people: list[dict[str, Any]] = []
        seen: set[str] = set()
        expected_total = None
        expected_size = None
        page = 1
        while True:
            result = self._get_page(expression, page)
            total = result.get("total_pages")
            per_page = result.get("per_page")
            # Incomplete or changing pages make the search unreliable.
            if (
                type(result.get("page")) is not int
                or result["page"] != page
                or type(total) is not int
                or not 0 <= total <= self.MAX_PAGES
                or type(per_page) is not int
                or not 1 <= per_page <= 25
            ):
                raise LookupError(
                    "The search has missing or invalid pagination; it cannot confirm absence."
                )
            if expected_total is not None and total != expected_total:
                raise LookupError(
                    "Search results changed during pagination. Check again before sending."
                )
            if expected_size is not None and per_page != expected_size:
                raise LookupError(
                    "The search changed its page size. Check again before sending."
                )
            expected_total = total
            expected_size = per_page
            embedded = result.get("_embedded")
            rows = embedded.get("osdi:people") if isinstance(embedded, dict) else None
            # Live Action Builder empty searches omit _embedded entirely.
            # Only an explicit zero-page first response proves this is empty;
            # missing collections on nonempty searches must still fail closed.
            if "_embedded" not in result and total == 0 and page == 1:
                rows = []
            if not isinstance(rows, list) or len(rows) > per_page:
                raise LookupError(
                    "The search did not return the expected people collection."
                )
            if (
                (total == 0 and (page != 1 or rows))
                or (page > 1 and not rows)
                or (page < total and len(rows) != per_page)
            ):
                raise LookupError(
                    "The search returned incomplete pages; it cannot confirm absence."
                )
            links = result.get("_links", {})
            if not isinstance(links, dict) or (
                page >= total and links.get("next") is not None
            ):
                raise LookupError("The search returned inconsistent pagination links.")
            for person in rows:
                _validate_person(person)
                identifier = _identifier(person)
                if identifier in seen:
                    raise LookupError(
                        "The search repeated a person across pages. Check again before sending."
                    )
                seen.add(identifier)
                people.append(person)
            if page >= total:
                return people
            # Construct each page on our validated endpoint with the same filter.
            # Never send the API token to an arbitrary URL from a 'next' link.
            page += 1

    def check(self, payload: dict[str, Any]) -> LookupResult:
        """Hold candidates; clear submission only after both searches find no match."""
        person = payload.get("person") if isinstance(payload, dict) else None
        try:
            # Names and addresses are still needed to compare returned candidates.
            for name in ("given_name", "family_name"):
                if not isinstance(person[name], str) or not person[name].strip():
                    raise ValueError
            email = person["email_addresses"][0]["address"].strip().lower()
            phone = _phone(person["phone_numbers"][0]["number"])
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not phone:
                raise ValueError
            address = person["postal_addresses"][0]
            if (
                not isinstance(address["address_lines"], list)
                or not address["address_lines"]
                or any(
                    not isinstance(v, str) or not v.strip()
                    for v in address["address_lines"]
                )
            ):
                raise ValueError
            for name in ("locality", "region", "postal_code"):
                if not isinstance(address[name], str) or not address[name].strip():
                    raise ValueError
            if not isinstance(person.get("additional_name", ""), str):
                raise ValueError
        except (TypeError, AttributeError, KeyError, IndexError, ValueError):
            raise LookupError(
                "A lookup needs valid names, email, US phone, and address. Review the pending record."
            ) from None

        candidates: dict[str, dict[str, Any]] = {}
        # Only these filters work on the live endpoint; compare names locally.
        for name, value in (
            ("email_address", email),
            ("phone_number", phone),
        ):
            for remote in self._search(name, value):
                # Verify the API actually applied the requested filter.
                matches = _comparison(person, remote)
                filter_matches = {
                    "email_address": matches["email"],
                    "phone_number": matches["phone"],
                }
                if not filter_matches[name]:
                    raise LookupError(
                        "A search returned a person outside its filter. Review the API matching behavior."
                    )
                identifier = _identifier(remote)
                summary = {
                    "identifiers": [identifier],
                    "matching_fields": sorted(
                        key for key, equal in matches.items() if equal
                    ),
                    "differing_fields": sorted(
                        key for key, equal in matches.items() if not equal
                    ),
                }
                if _text(remote.get("action_builder:entity_type", "")) != "person":
                    summary["differing_fields"].append("entity_type")
                # Email and phone searches may return the same person.
                previous = candidates.get(identifier)
                if previous is not None and previous != summary:
                    raise LookupError(
                        "A possible match changed during the lookup. Check again before sending."
                    )
                candidates[identifier] = summary

        summaries = tuple(candidates[key] for key in sorted(candidates))
        if not summaries:
            # Both searches completed successfully; errors never reach this branch.
            return LookupResult(
                "not_found",
                "Email and phone checks found no matches in this campaign.",
            )
        if len(summaries) == 1 and not summaries[0]["differing_fields"]:
            return LookupResult(
                "existing",
                "One existing person has matching contact details. Review instead of creating another person.",
                summaries,
            )
        return LookupResult(
            "needs_review",
            "Possible existing person(s) or differing details found. Review before creating or updating anyone.",
            summaries,
        )


def main() -> int:
    # Import here to keep the lookup client independent of queue/POST machinery.
    from record_queue import DEFAULT_QUEUE
    from send_person import check_queue

    parser = argparse.ArgumentParser(
        description="Check pending contacts by email and phone using GET only; keep unmatched records pending and hold possible duplicates for review."
    )
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=DEFAULT_QUEUE,
        help="Queue root containing pending/sent/review; defaults to composed_info.",
    )
    arguments = parser.parse_args()
    try:
        return check_queue(arguments.queue_dir)
    except KeyboardInterrupt:
        print(
            "Lookup interrupted. No people were created by this check.", file=sys.stderr
        )
        return 130
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        requests.RequestException,
    ) as error:
        message = (
            str(error)
            if isinstance(error, (ValueError, TypeError, RuntimeError))
            else "Check the queue, connection, and settings."
        )
        print(f"Lookup stopped: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
