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
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from member_automation import MemberAutomationClient, MemberAutomationError, classification_tag, prepare_member_payload

from action_builder_lookup import ActionBuilderConfig, ActionBuilderLookup, LookupError, LookupResult
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
        "classification",
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
    payload = {"person": person}
    if "classification" in record:
        payload["add_tags"] = [classification_tag(record["classification"])]
    return payload


class SubmissionReceiptError(RuntimeError):
    """An unconfirmed POST, with structural diagnostics safe to display."""


def _receipt_shape(result: Any, status_code: int) -> str:
    """Describe only known fields and types, never remote values or key names."""
    def shape(container: Any, key: str) -> str:
        if not isinstance(container, dict) or key not in container:
            return "missing"
        value = container[key]
        if isinstance(value, list):
            return f"list({len(value)})"
        return type(value).__name__

    person = result.get("person") if isinstance(result, dict) else None
    return (
        f"HTTP {status_code}; response={type(result).__name__}; "
        f"person={shape(result, 'person')}; "
        f"person.identifiers={shape(person, 'identifiers')}; "
        f"top-level identifiers={shape(result, 'identifiers')}; "
        f"error={shape(result, 'error')}; errors={shape(result, 'errors')}"
    )


def _confirm_uncertain_submission(payload, config, explanation):
    """Recover proof using read-only searches, never repeat the creation POST."""
    time.sleep(0.3)
    try:
        checked = ActionBuilderLookup(config).check(payload)
    except (LookupError, requests.RequestException):
        checked = None
    if (checked is not None and checked.outcome == "existing"
            and len(checked.candidates) == 1
            and not checked.candidates[0]["differing_fields"]):
        receipt = {"person": {"identifiers": checked.candidates[0]["identifiers"]},
                   "confirmed_by_lookup": True}
        return _validate_submission_receipt(receipt, 200)
    raise SubmissionReceiptError(
        f"{explanation} Read-only verification did not establish one exact matching person. "
        "Keep the record held; do not send it again."
    ) from None


def submit_to_actionbuilder(
    payload: dict[str, Any], *, config: ActionBuilderConfig | None = None,
    member_preflight: bool = False,
    on_person_receipt: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Send once, save confirmed identity, then verify configured member data."""
    # Use the same campaign and credentials for lookup and submission.
    if config is None:
        config = ActionBuilderConfig.from_environment()
    payload = prepare_member_payload(payload, config)
    member_client = MemberAutomationClient(config)
    if payload.get("add_tags") and not member_preflight:
        member_client.preflight(payload)
        time.sleep(0.3)

    receipt = None
    try:
        response = requests.post(
            config.people_url, headers=config.headers, json=payload,
            timeout=(5, 30), allow_redirects=False,
        )
    except requests.exceptions.SSLError:
        raise  # A certificate problem is not a transient verification failure.
    except (requests.Timeout, requests.ConnectionError):
        receipt = _confirm_uncertain_submission(
            payload, config, "The sending response was interrupted or timed out.")
    if receipt is None:
        # Authentication failures and redirects do not authorize recovery.
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise SubmissionReceiptError(f"Action Builder returned an unexpected HTTP {response.status_code}. Review the outcome before retrying.")
        try:
            result = response.json()
        except ValueError:
            receipt = _confirm_uncertain_submission(
                payload, config, "Action Builder returned a successful status but the response was not valid JSON.")
        else:
            try:
                receipt = _validate_submission_receipt(result, response.status_code)
            except SubmissionReceiptError as error:
                receipt = _confirm_uncertain_submission(payload, config, str(error))
    # Save validated identity before any later read can fail. Callback/storage
    # errors stop here; they must never trigger another POST or a second callback.
    if on_person_receipt is not None:
        on_person_receipt(receipt)
    # A valid person receipt alone does not prove tags or assessment applied.
    member_client.next_request = time.monotonic() + .3
    member_client.verify(payload, receipt)
    return receipt


def _validate_submission_receipt(result: Any, status_code: int) -> dict[str, Any]:
    """Normalize no fields: require one unambiguous native person identifier."""
    if not isinstance(result, dict):
        raise SubmissionReceiptError(
            "Unexpected Action Builder response format. "
            f"Receipt diagnostic: {_receipt_shape(result, status_code)}. "
            "Check Action Builder before retrying."
        )
    # A successful status alone is not proof that a person was created.
    person = result.get("person")
    identifiers = person.get("identifiers") if isinstance(person, dict) else None
    if (
        "error" in result or "errors" in result
        or not isinstance(identifiers, list)
        or any(not isinstance(value, str) or not value for value in identifiers)
    ):
        raise SubmissionReceiptError(
            "Action Builder did not return a confirmed person receipt. "
            f"Receipt diagnostic: {_receipt_shape(result, status_code)}. "
            "Review the outcome before retrying."
        )
    native_ids = [value for value in identifiers if value.startswith("action_builder:")]
    if len(native_ids) != 1 or not re.fullmatch(
        r"action_builder:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        native_ids[0],
    ):
        raise SubmissionReceiptError(
            "Action Builder did not return one valid person ID. "
            f"Receipt diagnostic: {_receipt_shape(result, status_code)}. "
            "Review the outcome before retrying."
        )
    return result


def show_success(result: dict[str, Any]) -> None:
    """Show a short receipt without dumping the entire API response."""
    if result.get("confirmed_by_lookup"):
        print("\nPerson confirmed in Action Builder by fresh email and phone lookups.\n")
    else:
        print("\nAction Builder submission succeeded.\n")
    person = result.get("person", {})
    if not isinstance(person, dict):
        return
    identifiers = person.get("identifiers", [])
    if isinstance(identifiers, list):
        for identifier in identifiers:
            if isinstance(identifier, str) and identifier.startswith("action_builder:"):
                print(f"  Confirmed person ID: {identifier}")
    if isinstance(person.get("browser_url"), str):
        print(f"  Open in Action Builder: {person['browser_url']}")
    print()


def show_lookup_result(result: LookupResult, *, saved: bool = False) -> None:
    """Show IDs and differing field names, without dumping remote contact data."""
    field_labels = {
        "given_name": "First name",
        "family_name": "Last name",
        "additional_name": "Middle initial",
        "email": "Email",
        "phone": "Phone",
        "address_line_1": "Street address",
        "locality": "City",
        "region": "State",
        "postal_code": "ZIP code",
        "entity_type": "Record type (Person)",
    }
    heading = "Saved Action Builder lookup" if saved else "Fresh Action Builder lookup"
    print(f"\n{heading}\n")
    print(f"  Result: {result.reason}")
    if saved:
        print("  This is an earlier check; sending will run fresh searches.")
    if result.candidates:
        print("\n  Check these existing records in Action Builder to confirm who they belong to.")
        print("  Comparisons below use the local details at the time of the check.")
    elif saved:
        print("  No candidate IDs were saved. This result does not release the review hold.")
    for number, candidate in enumerate(result.candidates, start=1):
        print(f"\n  Possible match {number} of {len(result.candidates)} in Action Builder")
        for identifier in candidate["identifiers"]:
            print(f"    Action Builder person ID: {identifier}")
        matching = ", ".join(field_labels.get(field, field) for field in candidate["matching_fields"])
        differing = ", ".join(field_labels.get(field, field) for field in candidate["differing_fields"])
        print(f"    Matching fields: {matching or 'None recorded'}")
        print(f"    Different or missing fields: {differing or 'None; all compared fields match'}")
    print()


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
        if config.residence_local:
            print(f"Member automation: classification from PDF; residence local {config.residence_local}; assessment 1.")
        else:
            print("Member automation is not configured. Set ACTION_BUILDER_RESIDENCE_LOCAL to enable both tags and assessment 1.")
        checked = cleared = held = sent = 0
        previous_post = False
        for item, payload in prepared:
            # A prior lookup can hold related versions in this prepared batch.
            # Re-read eligibility rather than trusting the original list.
            if item.id not in {current.id for current in queue.pending_records()}:
                print(f"Related record held for review: {item.path.name}")
                held += 1
                continue
            try:
                payload = prepare_member_payload(payload, config)
            except MemberAutomationError as error:
                queue.hold(item.family, set(), [], str(error))
                print(f"Held for member information review: {error}")
                held += 1
                continue
            if previous_post:
                time.sleep(0.3)  # The GET client cannot see the preceding POST.
                previous_post = False
            result = lookup_client.check(payload)
            queue.record_lookup(item, result.as_history(config))
            checked += 1
            person = payload["person"]
            print("\n" + "=" * 64)
            print(f"Local person checked: {person['given_name']} {person['family_name']}")
            print(f"Local queue file: {item.path.name}")
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
            if payload.get("add_tags"):
                MemberAutomationClient(config).preflight(payload)
            # The lookup client spaces its GETs; leave a gap before the POST too.
            time.sleep(0.3)
            queue.begin_send(item)  # Save 'sending' before the POST starts.
            try:
                options = {"member_preflight": True} if payload.get("add_tags") else {}
                result = submit_to_actionbuilder(
                    payload, config=config, **options,
                    on_person_receipt=lambda receipt: queue.record_person_receipt(
                        item, receipt, destination=config.destination),
                )
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
        print(f"\nChecked {checked} record(s); {cleared} passed; {held} held for review.")
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
