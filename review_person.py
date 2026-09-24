"""Review held people one at a time, edit locally, or explicitly approve creation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any

import requests

from action_builder_lookup import ActionBuilderConfig, ActionBuilderLookup, LookupResult
from member_classification import CLASSIFICATIONS, normalize_classification
from member_automation import MemberAutomationClient, prepare_member_payload
from extract_person import (
    clean_text,
    normalize_email,
    normalize_state,
    normalize_us_phone,
    normalize_zip,
)
from record_queue import CONTACT_FIELDS, DEFAULT_QUEUE, QueueError, QueueItem, RecordQueue
from send_person import (
    build_actionbuilder_payload,
    load_approved_person,
    show_lookup_result,
    show_success,
    submit_to_actionbuilder,
)


REVIEW_FIELDS = (
    ("given_name", "First name"),
    ("family_name", "Last name"),
    ("additional_name", "Middle initial"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("address_line_1", "Street address"),
    ("locality", "City"),
    ("region", "State"),
    ("postal_code", "ZIP code"),
    ("classification", "Classification"),
)


def confirm(prompt: str) -> bool:
    """Only an explicit yes permits a send, override, or deletion."""
    while True:
        answer = input(f"\n{prompt} [y/N]: ").strip().casefold()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Enter yes or no; pressing Enter means no.")


def normalize_review_field(field: str, value: str) -> str:
    """Apply the same contact rules used when reading the source PDF."""
    value = clean_text(value)
    if field == "classification":
        return normalize_classification(value)
    if not value:
        raise ValueError("All nine contact fields are required; this value cannot be blank.")
    normalizers = {
        "email": normalize_email,
        "phone": normalize_us_phone,
        "region": normalize_state,
        "postal_code": normalize_zip,
    }
    if field in normalizers:
        return normalizers[field](value)
    if field in {"given_name", "family_name"}:
        if not any(character.isalpha() for character in value) or any(
            character.isdigit() for character in value
        ):
            raise ValueError("Names must contain a letter and cannot contain digits.")
    elif field == "additional_name":
        initial = value.removesuffix(".")
        if len(initial) != 1 or not initial.isalpha():
            raise ValueError("Middle initial must be one letter, optionally followed by a period.")
    elif field == "locality" and not any(character.isalpha() for character in value):
        raise ValueError("City must contain a name.")
    elif field not in CONTACT_FIELDS:
        raise ValueError("This field is not part of the approved contact record.")
    return value


def normalize_review_record(record: dict[str, Any]) -> dict[str, str]:
    """Keep the review editor within the nine approved, nonempty contact fields."""
    if not isinstance(record, dict) or set(record) not in (CONTACT_FIELDS, CONTACT_FIELDS | {"classification"}):
        raise ValueError("The record must contain the nine approved contact fields and only the optional classification.")
    if any(not isinstance(value, str) for value in record.values()):
        raise ValueError("Every contact field must contain text.")
    return {field: normalize_review_field(field, record[field]) for field, _ in REVIEW_FIELDS
            if field != "classification" or record.get(field, "").strip()}


def show_person(record: dict[str, Any]) -> None:
    for field, label in REVIEW_FIELDS:
        if field in {"email", "address_line_1"}:
            print()
        print(f"  {label + ':':<16} {record.get(field) or 'Not set'}")


def show_review(queue: RecordQueue, item: QueueItem, record: dict[str, Any]) -> None:
    details = queue.review_details(item)
    print("\n" + "=" * 64)
    print(f"Reviewing: {record['given_name']} {record['family_name']}")
    print("=" * 64)
    print("\nLocal contact details\n")
    show_person(record)
    status = {"review": "Needs manual review", "uncertain": "Previous submission needs checking"}
    print("\nWhy this record is held\n")
    print(f"  Status: {status.get(details['status'], details['status'])}")
    print(f"  Reason: {details['reason']}")
    print("\nSource PDFs\n")
    for source in details["source_names"]:
        print(f"  {source}")
    lookup = details.get("lookup")
    if lookup:
        show_lookup_result(
            LookupResult(lookup["outcome"], lookup["reason"], tuple(lookup["candidates"])),
            saved=True,
        )
        destination = lookup["destination"]
        print(f"  Saved check time: {lookup['checked_at']}")
        print(f"  Organization: {destination['subdomain']}.actionbuilder.org")
        print(f"  Campaign ID: {destination['campaign_id']}")
    else:
        print("\nAction Builder lookup\n\n  No saved lookup is available for these local details.")
    print()
    if details["related_sent"]:
        print("A related record was already sent. Creating another person here is blocked.")
    elif details["related_uncertain"]:
        print("An earlier related send may have succeeded. Check that attempt in Action Builder before retrying.")


def edit_record(
    queue: RecordQueue, item: QueueItem, record: dict[str, Any]
) -> tuple[QueueItem, dict[str, str]]:
    """Collect corrections, validate, and let the queue replace the hashed record."""
    edited = dict(record)
    print("\nEdit local contact details")
    print("Edits change the local contact record, not the source PDF or an existing Action Builder person.")
    while True:
        print()
        for number, (field, label) in enumerate(REVIEW_FIELDS, start=1):
            print(f"  {number}. {label + ':':<16} {edited.get(field) or 'Not set'}")
        selection = input("\nField number to change (Enter or done to finish): ").strip().casefold()
        if selection in {"", "done"}:
            try:
                normalized = normalize_review_record(edited)
            except ValueError as error:
                print(f"Cannot save yet: {error}")
                continue
            if normalized != record:
                item = queue.save_review_edit(item, normalized)
                print("Changes saved. This record remains held for review until you approve sending.")
            else:
                print("No changes were needed.")
            return item, normalized
        if not selection.isdigit() or not 1 <= int(selection) <= len(REVIEW_FIELDS):
            print(f"Choose a field number from 1 to {len(REVIEW_FIELDS)}, or press Enter to finish.")
            continue
        field, label = REVIEW_FIELDS[int(selection) - 1]
        print(f"\nOld {label}: {edited.get(field) or 'Not set'}")
        if field == "classification":
            print("Approved classifications: " + "; ".join(CLASSIFICATIONS))
        while True:
            replacement = input("New value (Enter keeps the old value): ")
            if not replacement.strip():
                break
            try:
                edited[field] = normalize_review_field(field, replacement)
                break
            except ValueError as error:
                print(f"Value was not changed: {error}")


def review_send(
    queue: RecordQueue, item: QueueItem, record: dict[str, Any], *, checked: bool
) -> bool:
    """Use fresh GET evidence and explicit human approval before a single POST."""
    if not checked:
        print("Kept for review. Check Action Builder and the paperwork before approving a send.")
        return False
    details = queue.review_details(item)
    if details["related_sent"]:
        print("Not sent: a related record already has a successful sending receipt.")
        return False
    normalize_review_record(record)
    payload = build_actionbuilder_payload(record)
    config = ActionBuilderConfig.from_environment()
    payload = prepare_member_payload(payload, config)
    print(f"\nChecking {config.subdomain}.actionbuilder.org, campaign {config.campaign_id}...")
    result = ActionBuilderLookup(config).check(payload)
    queue.record_review_lookup(item, result.as_history(config))
    show_lookup_result(result)

    if details["related_uncertain"] and not confirm(
        "Have you checked the earlier attempt in this campaign and confirmed this person was NOT already created?"
    ):
        print("Kept for review; the earlier sending outcome is still unresolved.")
        return False
    if result.candidates and not confirm(
        "Have you checked these candidate IDs and confirmed they are NOT the same person, so a NEW person should be created?"
    ):
        print("Kept for review. No person was created or changed.")
        return False
    if result.outcome != "not_found" and not result.candidates:
        print("Kept for review: this lookup did not establish a usable result.")
        return False

    reason = "Manual review approved creation after fresh email and phone searches found no matches."
    if result.candidates or details["related_uncertain"]:
        reason = input("\nReason for approving creation despite this warning (required; Enter cancels): ").strip()
        if not reason:
            print("Kept for review. Approval needs a reason.")
            return False
        if len(reason) > 500:
            print("Kept for review. Use an approval reason of 500 characters or fewer.")
            return False
    name = f"{record['given_name']} {record['family_name']}"
    if not confirm(
        f"Create {name} as a NEW person in {config.subdomain}.actionbuilder.org campaign {config.campaign_id}?"
    ):
        print("Kept for review. Nothing was sent.")
        return False

    if payload.get("add_tags"):
        MemberAutomationClient(config).preflight(payload)
    time.sleep(0.3)  # Leave room between the final GET and POST in the API rate limit.
    queue.begin_review_send(item, config_destination=config.destination, reason=reason)
    try:
        options = {"member_preflight": True} if payload.get("add_tags") else {}
        response = submit_to_actionbuilder(payload, config=config, **options)
        queue.finish_send(item, response)
    except BaseException:
        # A timeout, interruption, or failed local receipt write can follow creation.
        try:
            queue.mark_uncertain(
                item,
                "The manual sending attempt did not finish reliably. Check Action Builder before retrying.",
            )
        except (OSError, QueueError):
            pass  # The saved 'sending' claim still prevents an automatic retry.
        print(
            "Sending stopped. This person may already exist in Action Builder. The record is held; check the attempt before retrying.",
            file=sys.stderr,
        )
        raise
    show_success(response)
    time.sleep(0.3)  # A new review starts with a separate GET client's rate clock.
    return True


def reconcile_person(queue: RecordQueue, item: QueueItem, record: dict[str, Any], *, checked: bool) -> bool:
    """Confirm an earlier creation and update only the local queue."""
    details = queue.review_details(item)
    if not checked or details["status"] != "uncertain":
        print("Kept for review. Check the earlier uncertain submission in Action Builder first.")
        return False
    config = ActionBuilderConfig.from_environment()
    if not details.get("lookup") or details["lookup"]["destination"] != config.destination:
        print("Kept for review. Configure the same campaign used for the saved submission check.")
        return False
    payload = prepare_member_payload(build_actionbuilder_payload(record), config)
    result = ActionBuilderLookup(config).check(payload)
    show_lookup_result(result)
    if result.outcome != "existing":
        print("Kept for review. The fresh searches did not find one exact matching person. Nothing was sent.")
        return False
    MemberAutomationClient(config).verify(payload, {"person": {"identifiers": result.candidates[0]["identifiers"]}})
    print(f"\nVerified in {config.subdomain}.actionbuilder.org, campaign {config.campaign_id}.")
    if not confirm("Mark this earlier submission complete locally using the matching person ID? No person will be created or changed"):
        print("Kept for later review.")
        return False
    queue.reconcile_sent(item, lookup=result.as_history(config),
                         reason="Earlier submission verified by the operator and fresh exact email and phone matches; reconciled without sending again.")
    print("Marked complete and moved to sent. No POST or update was made to Action Builder.")
    return True


def run_review(directory: Path) -> int:
    """Hold the queue lock while the operator works through a stable review list."""
    with RecordQueue(directory) as queue:
        items = queue.review_records()
        if not items:
            print("No contact records need manual review.")
            return 0
        print(f"\n{len(items)} record(s) need review.")
        print("Enter keeps a record; no sending or deletion is automatic.")
        for original in items:
            # Earlier decisions can retire a related version from this snapshot.
            current = {item.id: item for item in queue.review_records()}
            item = current.get(original.id)
            if item is None:
                continue
            record = load_approved_person(item.path)
            show_review(queue, item, record)
            checked = confirm("Have you checked this person in Action Builder and the paperwork?")
            while True:
                print("\nChoose an action\n")
                print("  s  Send after checks and confirmation")
                print("  r  Reconcile an earlier submission already in Action Builder (no sending)")
                print("  c  Change local contact details")
                print("  k  Keep for later review")
                print("  d  Discard this local record")
                print("  q  Quit this review session")
                action = input("\nAction [keep]: ").strip().casefold()
                if action in {"q", "quit"}:
                    print("Review stopped. Unfinished records remain held.")
                    return 0
                if action in {"", "k", "keep"}:
                    print("Kept for later review.")
                    break
                if action in {"s", "send"}:
                    review_send(queue, item, record, checked=checked)
                    break
                if action in {"r", "reconcile"}:
                    reconcile_person(queue, item, record, checked=checked)
                    break
                if action in {"c", "change", "edit"}:
                    previous = record
                    item, record = edit_record(queue, item, record)
                    if record != previous:
                        checked = False  # Approval of old details does not approve corrections.
                    print("\nPerson after editing:")
                    show_review(queue, item, record)
                    if confirm("Send this revised person now?"):
                        if not checked:
                            checked = confirm("Have you now checked this revised person in Action Builder and the paperwork?")
                        review_send(queue, item, record, checked=checked)
                    else:
                        print("Changes retained for later review.")
                    break
                if action in {"d", "discard"}:
                    if not checked:
                        print("Kept for review. Check the person and paperwork before discarding this record.")
                        break
                    print("Discarding removes this contact JSON from review. Minimal history is retained; source PDFs are not deleted.")
                    if confirm("Discard this reviewed local contact record?"):
                        queue.discard_review(item, "Discarded after manual review.")
                        print("Discarded from review.")
                    else:
                        print("Kept for later review.")
                    break
                print("Choose send, reconcile, change, keep, discard, or quit.")
        print("\nReview pass complete. Any records kept for later remain held.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review held people one at a time; sending and discarding require explicit confirmation."
    )
    parser.add_argument(
        "--queue-dir", type=Path, default=DEFAULT_QUEUE,
        help="Queue root containing pending/sent/review; defaults to composed_info.",
    )
    arguments = parser.parse_args()
    try:
        return run_review(arguments.queue_dir)
    except (KeyboardInterrupt, EOFError):
        print("Review interrupted. No further action was taken; unfinished records remain held.", file=sys.stderr)
        return 130
    except (QueueError, OSError, ValueError, TypeError, RuntimeError, requests.RequestException) as error:
        message = (
            str(error)
            if isinstance(error, (QueueError, ValueError, TypeError, RuntimeError))
            else "Check the queue files, connection settings, and internet connection."
        )
        print(f"Review stopped: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
