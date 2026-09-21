"""
Use the information gathered from the captured pdf_downloads and record_que
to be able to correctly add individuals to the actionbuilder api.

I believe checking their ids may be the correct route to avoid any conflicts
with creating duplicate file so maybe we make the check for them that way prior
to any creation of a member.

The lookup is now implemented in action_builder_lookup.py and runs before POST.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from action_builder_lookup import ActionBuilderConfig, ActionBuilderLookup, LookupResult
from record_queue import (
    DEFAULT_QUEUE,
    RECORD_FOLDERS,
    STATE_NAME,
    QueueError,
    RecordQueue,
)


# Learning helper; runtime settings now come from ActionBuilderConfig.
def require_environment_variable(name: str) -> str:
    """The function that is used for the purpose of capturing a .env file
    variable."""
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(f"Required environment variable is missing: " f"{name}")
    return value


def load_approved_person(input_path: str | Path) -> dict[str, Any]:
    """This gets the information of the json we load in through the specfic
    path that we want. This would want to be a search later for an automation"""
    path = Path(input_path)
    if not path.is_file():
        raise FileNotFoundError(f"Approved JSON file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("The approved record must be a JSON object.")
    return data


def require_string(record: dict[str, Any], field_name: str) -> str:
    """This is expecting a dictionary and uses the values of that dictnarhy
    of to get a str value. We check the value obtained here and return it."""
    value = record.get(field_name)

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    value = value.strip()

    if not value:
        raise ValueError(f"{field_name} cannot be blank.")
    return value


def build_actionbuilder_payload(record: dict[str, Any]) -> dict[str, Any]:
    """
    Expects a dictionary of the approved person loaded for the entry.

    Construct only the fields displayed in the Action Builder
    Create Person screens. This could be expanded or reduced depending on wants
    and needs for entry.

    We input a dictionary(record) here that is derived from the json dictionary
    that we pass into the record parameter.

    That should be the dictionary from load_approved_person. The json file.

    This takes those values and then pluges them into the acitonbuilders
    related fileds for the POST through the api(the web).

    Those values correlate to the fileds correspondingly of the api
    expectations in conjunction with its form.
    """

    # Only these approved contact fields may enter the API payload.
    allowed_input_fields = {
        "given_name",
        "family_name",
        # Middle name, preferred name, and suffix handling still need a policy.
        "additional_name",
        "email",
        "phone",
        "address_line_1",
        "locality",
        "region",
        "postal_code",
    }

    unexpected_fields = set(record) - allowed_input_fields
    if unexpected_fields:
        raise ValueError(
            "Unapproved fields were found in the local "
            f"record: {sorted(unexpected_fields)}"
        )

    person: dict[str, Any] = {
        "action_builder:entity_type": "Person",
        "given_name": require_string(
            record,
            "given_name",
        ),
        "family_name": require_string(
            record,
            "family_name",
        ),
    }

    # Leave optional values out when they are blank or unavailable.
    additional_name = record.get("additional_name")

    if isinstance(additional_name, str):
        additional_name = additional_name.strip()
        if additional_name:
            person["additional_name"] = additional_name

    email = record.get("email")

    if isinstance(email, str):
        email = email.strip().lower()

        if email:
            person["email_addresses"] = [
                {
                    "address": email,
                    "address_type": "home",
                }
            ]
    phone = require_string(
        record,
        "phone",
    )

    # Extraction supplies the phone as digits, including its country code.
    if not phone.isdigit():
        raise ValueError(
            "Action Builder phone numbers must contain " "numeric characters only."
        )
    person["phone_numbers"] = [
        {
            "number": phone,
            "number_type": "Mobile",
        }
    ]
    # The current workflow handles one US mailing address per person.
    person["postal_addresses"] = [
        {
            "address_lines": [
                require_string(
                    record,
                    "address_line_1",
                )
            ],
            "locality": require_string(
                record,
                "locality",
            ),
            "region": require_string(
                record,
                "region",
            ).upper(),
            "postal_code": require_string(
                record,
                "postal_code",
            ),
            "country": "US",
            "address_type": "physical",
        }
    ]
    return {"person": person}


def submit_to_actionbuilder(
    payload: dict[str, Any], *, config: ActionBuilderConfig | None = None,
) -> dict[str, Any]:
    """This takes in the payload(the formed payload from the approved person)
    to then submit that data provided the .env exist with the correct
    credentials and the connection allows the sending of the data.
    Gives us a json file of the response that was returned from the api."""
    # Use the same campaign and credentials for lookup and submission.
    if config is None:
        config = ActionBuilderConfig.from_environment()

    response = requests.post(
        config.people_url,
        headers=config.headers,
        json=payload,
        # Connect and read timeouts; this is not a total request time limit.
        timeout=(5, 30),
        # Keep credentials and contact data on the validated endpoint.
        allow_redirects=False,
    )

    # Requests does not treat redirects as HTTP errors automatically.
    if 300 <= response.status_code < 400:
        raise RuntimeError(
            "Action Builder returned an unexpected "
            f"redirect: HTTP {response.status_code}"
        )

    response.raise_for_status()

    # A successful HTTP status still needs a usable JSON receipt.
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise RuntimeError(
            "Action Builder returned a successful status "
            "but the response was not valid JSON."
        ) from error
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected Action Builder response format.")
    # A successful status alone is not proof that a person was created.
    person = result.get("person")
    identifiers = person.get("identifiers") if isinstance(person, dict) else None
    if (
        "error" in result or "errors" in result
        or not isinstance(identifiers, list)
        or any(not isinstance(value, str) or not value for value in identifiers)
    ):
        raise RuntimeError("Action Builder did not return a confirmed person receipt. Review the outcome before retrying.")
    native_ids = [value for value in identifiers if value.startswith("action_builder:")]
    if len(native_ids) != 1 or not re.fullmatch(
        r"action_builder:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        native_ids[0],
    ):
        raise RuntimeError("Action Builder did not return one valid person ID. Review the outcome before retrying.")
    return result


def show_success(result: dict[str, Any]) -> None:
    """Show a short receipt without dumping the entire API response."""
    print("Action Builder submission succeeded.")
    person = result.get("person", {})
    if not isinstance(person, dict):
        return
    identifiers = person.get("identifiers", [])
    if (
        isinstance(identifiers, list)
        and identifiers
        and isinstance(identifiers[0], str)
    ):
        print(f"Identifier: {identifiers[0]}")
    if isinstance(person.get("browser_url"), str):
        print(f"Action Builder record: {person['browser_url']}")


def show_lookup_result(result: LookupResult) -> None:
    """Show IDs and differing field names, without dumping remote contact data."""
    print(f"Action Builder lookup: {result.reason}")
    for candidate in result.candidates:
        print("Candidate identifier(s): " + ", ".join(candidate["identifiers"]))
        if candidate["differing_fields"]:
            print("Fields to review: " + ", ".join(candidate["differing_fields"]))


def queue_for_input(input_file: Path, configured_queue: Path) -> Path | None:
    """Recognize managed files even when someone supplies their path directly."""
    resolved = input_file.resolve()
    # A custom queue remains managed when --queue-dir is omitted on a later run.
    for directory in resolved.parents:
        marker = directory / STATE_NAME
        if marker.exists() or marker.is_symlink():
            return directory
    for directory in (configured_queue.resolve(), DEFAULT_QUEUE.resolve()):
        if resolved.is_relative_to(directory):
            return directory
    # A missing history file must not turn a generated record into a legacy file.
    if re.fullmatch(r"person-[0-9a-f]{64}\.json", resolved.name):
        # Status subfolders belong to the same queue and share its root history.
        if resolved.parent.name in RECORD_FOLDERS:
            return resolved.parent.parent
        return resolved.parent
    return None


def send_queue(directory: Path, input_file: Path | None, submit: bool,
               *, check_only: bool = False) -> int:
    """Preview or send only tracked pending records, with one queue lock held."""
    with RecordQueue(directory) as queue:
        labels = {
            "pending": "pending",
            "review": "need review",
            "deferred": "waiting for download",
            "sending": "send in progress",
            "sent": "already sent",
            "uncertain": "need Action Builder check",
            "discarded": "discarded after review",
        }
        counts = queue.status_counts()
        if counts:
            print(
                "Download groups: "
                + "; ".join(
                    f"{count} {labels[status]}"
                    for status, count in sorted(counts.items())
                )
            )
        items = (
            [queue.item_for_path(input_file)]
            if input_file is not None
            else queue.pending_records()
        )
        if not items:
            print(
                "No pending records. Any waiting or review groups listed above still need attention."
            )
            return 0

        # Validate the whole selected batch before the first network request.
        prepared = [
            (item, build_actionbuilder_payload(load_approved_person(item.path)))
            for item in items
        ]
        if not submit and not check_only:
            print("Preview only. No API request was made.")
            for item, payload in prepared:
                print(f"\nPending record: {item.path.relative_to(queue.directory)}")
                print(json.dumps(payload, indent=4, ensure_ascii=False))
            return 0

        # A GET checks for matches without consuming a record's send attempt.
        # Reuse one client so its request pacing applies throughout this batch.
        config = ActionBuilderConfig.from_environment()
        lookup_client = ActionBuilderLookup(config)
        checked = cleared = held = sent = 0
        previous_post = False
        for item, payload in prepared:
            # A prior lookup can hold related versions in this prepared batch.
            # Re-read eligibility rather than trusting the original list.
            if item.id not in {current.id for current in queue.pending_records()}:
                print(f"Related record held for review: {item.path.name}")
                held += 1
                continue
            if previous_post:
                time.sleep(0.3)  # The GET client cannot see the preceding POST.
                previous_post = False
            result = lookup_client.check(payload)
            queue.record_lookup(item, result.as_history(config))
            checked += 1
            print(f"Checked record: {item.path.name}")
            show_lookup_result(result)
            # Only two successful searches with no candidates allow creation.
            if result.outcome != "not_found":
                held += 1
                print("Moved to review. No person was created or updated.")
                continue
            cleared += 1
            if check_only:
                print("Passed email and phone checks. Kept pending for submission.")
                continue
            # The lookup client spaces its GETs; leave a gap before the POST too.
            time.sleep(0.3)
            queue.begin_send(item)  # Save 'sending' before the POST starts.
            try:
                result = submit_to_actionbuilder(payload, config=config)
                queue.finish_send(item, result)
            except BaseException:
                # Even a timeout may mean the server created the person already.
                # A keyboard interruption or local save failure is also uncertain.
                try:
                    queue.mark_uncertain(
                        item,
                        "The sending attempt did not finish reliably. Check Action Builder before retrying.",
                    )
                except (OSError, QueueError):
                    # A persisted 'sending' claim also blocks later automatic retries.
                    pass
                print(
                    "Sending stopped. This record needs review in Action Builder; it will not be retried automatically.",
                    file=sys.stderr,
                )
                raise
            show_success(result)
            sent += 1
            previous_post = True
        print(f"Checked {checked} record(s); {cleared} passed; {held} held for review.")
        if check_only:
            print("GET checks only. No people were created or updated. Use --submit to send pending records after fresh checks.")
        else:
            print(f"Successfully submitted {sent} record(s). Sent records are in: {queue.directory / 'sent'}")
        return 1 if held else 0


def check_queue(directory: Path, input_file: Path | None = None) -> int:
    """Save GET evidence: unmatched records stay pending; matches move to review."""
    return send_queue(directory, input_file, submit=False, check_only=True)


def main() -> int:
    """With no file argument, use the extractor's local pool of approved JSON."""
    parser = argparse.ArgumentParser(
        description="Preview pending records, or submit them after fresh email and phone checks."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        type=Path,
        help="Optional single JSON file; otherwise use the pending queue.",
    )
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=DEFAULT_QUEUE,
        help="Queue root containing pending/sent/review; defaults to composed_info.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Create pending people only after fresh email and phone checks find no match. Without this option, preview only.",
    )
    arguments = parser.parse_args()
    try:
        if arguments.input_file is None:
            return send_queue(arguments.queue_dir, None, arguments.submit)
        managed_queue = queue_for_input(arguments.input_file, arguments.queue_dir)
        if managed_queue is not None:
            return send_queue(managed_queue, arguments.input_file, arguments.submit)

        # Keep the original explicit-file workflow for older, unmanaged JSONs.
        payload = build_actionbuilder_payload(
            load_approved_person(arguments.input_file)
        )
        if not arguments.submit:
            print("Preview only. No API request was made.")
            print(json.dumps(payload, indent=4, ensure_ascii=False))
            return 0
        # An older standalone JSON must pass the same fresh lookup gate.
        config = ActionBuilderConfig.from_environment()
        result = ActionBuilderLookup(config).check(payload)
        show_lookup_result(result)
        if result.outcome != "not_found":
            print("Manual review required. Nothing was created or updated.")
            return 1
        time.sleep(0.3)
        show_success(submit_to_actionbuilder(payload, config=config))
        return 0
    except KeyboardInterrupt:
        print(
            "Sending was interrupted. Check the last attempt in Action Builder before retrying.",
            file=sys.stderr,
        )
        return 130
    except (
        QueueError,
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        requests.RequestException,
    ) as error:
        # Network exceptions may contain server details; keep the user message short.
        message = (
            str(error)
            if isinstance(error, (QueueError, ValueError, TypeError, RuntimeError))
            else "Check the files, connection settings, and internet connection."
        )
        print(f"Sending stopped: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
