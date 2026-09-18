"""
Use the information gathered from the captured pdf_downloads and record_que
to be able to correctly add individuals to the actionbuilder api.

I believe checking their ids may be the correct route to avoid any conflicts
with creating duplicate file so maybe we make the check for them that way prior
to any creation of a member.

The lookup is now implemented in action_builder_lookup.py and runs before POST.
"""

from __future__ import annotations

# for the purpose of command line args
import argparse

# for the purpose of json oriented actions
import json

# operating system needed for environ.get() capturuing
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


# Retained as an annotated learning example. Runtime configuration now comes
# from ActionBuilderConfig, shared by the GET lookup and POST submission.
# checks environment variables and grabs them with os.environ.get()
def require_environment_variable(name: str) -> str:
    """The function that is used for the purpose of capturing a .env file
    variable."""
    # Get the value of the .env desired. Default empty.
    value = os.environ.get(name, "").strip()  # remove whitespace

    # If no value/empty then the name of the .env desired doesn't exist.
    if not value:
        # raise the error with the information related.
        raise RuntimeError(f"Required environment variable is missing: " f"{name}")
    # returns the value of the variable since exist.
    return value


# load the information based on the file path(arg in command line)
# raise errors if not found or not a dict
def load_approved_person(input_path: str | Path) -> dict[str, Any]:
    """This gets the information of the json we load in through the specfic
    path that we want. This would want to be a search later for an automation"""
    # take the path(json) and create the Path related obj.
    path = Path(input_path)
    # check that it's a file
    if not path.is_file():
        # raise error if not found(would mean file process didn't work)
        raise FileNotFoundError(f"Approved JSON file not found: {path}")
    # loads json file since FileNotFoundError wasn't raised. Read the text
    # with the utf-8 to decode to a python object dictionary.
    data = json.loads(path.read_text(encoding="utf-8"))
    # must be a dict type object(json)
    if not isinstance(data, dict):
        raise TypeError("The approved record must be a JSON object.")
    # return the dict data
    return data


# used in the build_actionbuilder_payload for the input_fields. Needed to check
# that the values are strs. Uses the data from person we want to enter.
def require_string(record: dict[str, Any], field_name: str) -> str:
    """This is expecting a dictionary and uses the values of that dictnarhy
    of to get a str value. We check the value obtained here and return it."""
    # value is the name variable to using .get(). the .get uses the field_name
    # parameter and that is a key that will get the value associated
    value = record.get(field_name)

    # if the value isn't of of a str type then raise valueerror
    if not isinstance(value, str):
        # helpful error message.
        raise ValueError(f"{field_name} must be a string.")

    # strip the whitespace.
    value = value.strip()

    # if no value must be entered.
    if not value:
        # raise error if blank(empty str)
        raise ValueError(f"{field_name} cannot be blank.")
    # return the str value that is desired.
    return value


# compose the payload that is going to be used for action builder
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

    # these are the fields that we desire based on our forms of action builder
    # if we want to expand or reduce we would do that here.

    # syntactially this is a set. Means no duplicates are possible
    allowed_input_fields = {
        "given_name",
        "family_name",
        # middle name, preferred name, suffix??? How or do we handle these?
        "additional_name",
        "email",
        "phone",
        "address_line_1",
        "locality",
        "region",
        "postal_code",
    }

    # Use set function removes duplicates of keys from record dict. We subtract
    # the fields(keys) we anticipate. Leaves fileds(keys) that are unexpected.
    unexpected_fields = set(record) - allowed_input_fields
    # raise and error if those fileds exist.
    if unexpected_fields:
        raise ValueError(
            "Unapproved fields were found in the local "
            f"record: {sorted(unexpected_fields)}"
        )

    # Dictionary that uses use require_string, is_digits, and default
    # values. Populated with 3 keys and values to start out before more added.
    # Raises errors if need be and builds the dictionary correctly.
    person: dict[str, Any] = {
        "action_builder:entity_type": "Person",
        # set the field_name "given_name" to the value of a str
        "given_name": require_string(
            # dictionary
            record,
            # .get with the key
            "given_name",
        ),
        "family_name": require_string(
            record,
            "family_name",
        ),
    }

    # if not additional_name then equates to None and ommited because not added
    additional_name = record.get("additional_name")

    if isinstance(additional_name, str):
        additional_name = additional_name.strip()
        if additional_name:
            person["additional_name"] = additional_name

    email = record.get("email")

    # check that is of str type
    if isinstance(email, str):
        # lower and strip out the whitespace
        email = email.strip().lower()

        # if str
        if email:
            # set the email_addresses fields to equal the information given.
            # address_type is set to default of "home"
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

    # validation that the entry is number(int) related in the str format
    if not phone.isdigit():
        raise ValueError(
            "Action Builder phone numbers must contain " "numeric characters only."
        )
    # accesss the dictionary we're plugging the data into
    person["phone_numbers"] = [
        {
            # assign number to the value
            "number": phone,
            # set the default type to mobile
            "number_type": "Mobile",
        }
    ]
    # postal address assignment to the dictionary we are creating
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
    # return the dictinoary that is used for the purpose of entry into
    # actionbuilder
    return {"person": person}


# The api call to actionbuilder that actually sends the data over the
# api provided
def submit_to_actionbuilder(
    payload: dict[str, Any], *, config: ActionBuilderConfig | None = None,
) -> dict[str, Any]:
    """This takes in the payload(the formed payload from the approved person)
    to then submit that data provided the .env exist with the correct
    credentials and the connection allows the sending of the data.
    Gives us a json file of the response that was returned from the api."""
    # The sending workflow passes the same validated settings used for GET.
    # This prevents checking one campaign and then creating in another.
    if config is None:
        config = ActionBuilderConfig.from_environment()

    # Physical POST(creation) request sent to the web api with the data we have
    # formed. The .post of the json data we are creating.
    response = requests.post(
        # url to point to api creation.
        config.people_url,
        # passing the headers that need specified.
        headers=config.headers,
        # pass the dictionary of json data(build_actionbuilder_payload)
        # JSON-encodes it to what the api is expecting. Think UTF-8
        json=payload,
        # timeout constraints.
        # The read timeout concerns how long Requests waits without receiving
        # response data. It is not necessarily a total wall-clock limit for
        # the entire request.
        timeout=(5, 30),
        # don't allow redirects for the purpose of containing data
        # Stop at the initial HTTP response rather than automatically
        # requesting the redirect destination.
        allow_redirects=False,
    )

    # Raise a runtime error based on the conditions of codes falling within
    # certain status codes. Essentially if the code falls between 300 and 400
    if 300 <= response.status_code < 400:
        raise RuntimeError(
            # message with the status code.
            "Action Builder returned an unexpected "
            f"redirect: HTTP {response.status_code}"
        )

    # raises the http error if one occures. A means to express the info
    # isn't working correclty per the status. HTTPError for unsuccessful HTTP
    # status codes
    response.raise_for_status()

    # error handling
    try:
        # sets the name variable result to the json type. Goes through decoder
        # Then becomes a python object.
        result = response.json()
    # If what returned wasn't json then raise the error
    except requests.exceptions.JSONDecodeError as error:
        # error if response isn't json
        raise RuntimeError(
            "Action Builder returned a successful status "
            "but the response was not valid JSON."
        ) from error
    # if not a dictionary(should be as json type) raise an error
    if not isinstance(result, dict):
        # raise error.
        raise RuntimeError("Unexpected Action Builder response format.")
    # return the dictionary that api returns to see what was submitted
    # this will be used for viewing what was returned which should be what was
    # submitted.
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
            "pending": "ready",
            "review": "need review",
            "deferred": "waiting for download",
            "sending": "send in progress",
            "sent": "already sent",
            "uncertain": "need Action Builder check",
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
        checked = held = sent = 0
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
            if result.outcome != "not_found":
                held += 1
                print("Moved to review. No person was created or updated.")
                continue
            if check_only:
                # A clean check stays pending. Submission will check again.
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
        print(f"Checked {checked} record(s); {held} record(s) held for review.")
        if check_only:
            print("GET checks only. No people were created or updated. Clear records remain pending.")
        else:
            print(f"Successfully submitted {sent} record(s). Sent records are in: {queue.directory / 'sent'}")
        return 1 if held else 0


def check_queue(directory: Path, input_file: Path | None = None) -> int:
    """Check pending records with GET only, saving matches in the review folder."""
    return send_queue(directory, input_file, submit=False, check_only=True)


def main() -> int:
    """With no file argument, use the extractor's local pool of approved JSON."""
    parser = argparse.ArgumentParser(
        description="Preview or send approved Action Builder records."
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
        help="Actually send records. Without this option, only preview them.",
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
            print("Possible existing person. Review Action Builder; nothing was created or updated.")
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
