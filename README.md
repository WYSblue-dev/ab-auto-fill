# PDF to Action Builder

Import contact information from completed **New Member Checklist / LPX Data Entry** PDFs into Action Builder. The program finds downloaded forms, lets you preview the extracted details, checks for existing people by email and phone, and submits eligible records. An interactive review command handles records that need attention.

It reads first name, last name, middle initial, email, phone, street address, city, state, and ZIP code. All nine fields are required. The supported format is the checklist template with US contact details; scanned images and unrelated PDF layouts are not supported. Other form information, including Social Security numbers and beneficiary details, is excluded.

[Setup](#setup) · [Daily commands](#daily-commands) · [Manual review](#manual-review) · [Folder options](#folder-options) · [Help](#help) · [License](#license)

## Setup

You need Python 3.10 or newer, Git, and authorized Action Builder API settings. The program has been tested on macOS with Python 3.14.7. Windows commands use PowerShell.

A **terminal** is the window where you enter commands. Run each command from the project folder unless a step says otherwise.

### 1. Get the project

Replace `YOUR_REPOSITORY_URL` with this repository's clone URL:

```sh
git clone "YOUR_REPOSITORY_URL" ab_automation
cd ab_automation
```

If you already have the project, open a terminal in that folder instead.

### 2. Install the Python packages

On macOS:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

<details>
<summary>Windows PowerShell commands</summary>

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

</details>

The `.venv` folder keeps this project's packages together. The commands use it directly, so no activation step is needed.

### 3. Add the connection settings

For a **new installation**, copy the example configuration:

```sh
cp .env.example .env
```

On Windows, use `Copy-Item .env.example .env`. Keep an existing `.env` when updating.

Open `.env` in a text editor and replace the three example values:

| Setting | Value to enter |
|---|---|
| `ACTION_BUILDER_API_KEY` | Your authorized Action Builder API key. |
| `ACTION_BUILDER_SUBDOMAIN` | The name before `.actionbuilder.org`, without `https://`. |
| `ACTION_BUILDER_CAMPAIGN_ID` | The ID of the campaign you want to use. |

Keep `.env` private. Use one designated sending computer and keep its queue associated with the same campaign. Each installation has its own history; separate computers can create duplicates if they submit at the same time.

### 4. Check the installation

```sh
./.venv/bin/python -m unittest discover -s tests
```

These tests use temporary data and simulated API responses. They do not contact Action Builder. Check representative PDFs and validate the connection in a test campaign before processing real submissions.

## Daily commands

Download the completed PDFs, then run these commands **one at a time** from the project folder. Read each result before continuing.

| Step | macOS command | What it does |
|---|---|---|
| **1. Extract** | `./.venv/bin/python extract_person.py` | Finds matching PDFs in Downloads and prepares local contact records. |
| **2. Preview** | `./.venv/bin/python send_person.py` | Displays pending contact information without contacting Action Builder. |
| **3. Check** | `./.venv/bin/python action_builder_lookup.py` | Searches Action Builder by email and phone. Possible matches move to review. |
| **4. Submit** | `./.venv/bin/python send_person.py --submit` | Checks again and creates people when both searches finish with no matches. |
| **Review held records** | `./.venv/bin/python review_person.py` | Lets you edit, keep, discard, or approve individual records. |

On Windows, replace `./.venv/bin/python` with `.\.venv\Scripts\python.exe` in these and the remaining commands. Script names and options stay the same.

The default scan reads matching files directly inside `~/Downloads`, including older downloads. Filenames must end in `ORGANIZED.pdf` or a numbered copy such as `ORGANIZED (1).pdf`. Use the [folder options](#folder-options) for a different ending or location.

Compare the preview with the latest paperwork. Renamed copies, late corrections, or PDF errors can leave older details pending. Run extraction again when corrected PDFs arrive; sending does not rescan Downloads.

**Pending means waiting to be sent.** It can include unchecked records or records whose lookup failed. Submission always performs fresh checks. Searches cover the configured campaign, and someone whose email and phone have both changed can be missed.

Run the commands separately rather than joining them with `&&`: a command can return a nonzero status when records need review. Read its message to distinguish a review hold from an error.

### Where records are stored

The program creates and manages these folders beside the scripts:

```text
composed_info/
    pending/          Contact records waiting to be sent
    sent/             Contact records with confirmed submissions
    review/           Contact records needing attention
    .queue-state.json Processing and submission history
```

These are contact data files, not PDFs. Source PDFs remain unchanged in Downloads and are not uploaded to Action Builder. Keep the whole `composed_info` folder together, including its hidden history file. Let the commands manage these files; moving or deleting them by hand does not reset a sending attempt.

## Manual review

```sh
./.venv/bin/python review_person.py
```

The display separates local contact details, the reason for review, source PDFs, and Action Builder matches. **Saved** results come from an earlier check; **fresh** results come from searches just performed. Numbered matches show existing Action Builder person IDs and which fields match, differ, or are missing. Open those records in Action Builder to check the person.

| Choice | What happens |
|---|---|
| **Change** | Select a field, see its old value, and enter a correction. The updated person is displayed before you decide about sending. |
| **Send** | Runs fresh checks and asks for confirmation before creating this person. Candidate matches and uncertain earlier attempts require additional confirmation and a written reason. |
| **Keep** | Leaves the record for another review session. |
| **Discard** | Confirms removal of the local contact file. History remains to prevent the same record returning on another import. |
| **Quit** | Ends the session. Unfinished records stay held. |

Pressing Enter at a confirmation means **No**; Enter at the action menu means **Keep**. Sending and discarding require confirmation that you checked the person and paperwork.

**Send creates the reviewed person directly after approval.** It does not return them to pending. A related record already marked sent blocks another creation. Changes edit the local contact; they do not update an existing Action Builder person. Discarding leaves source PDFs and remote records untouched.

You can review before or after submitting the pending batch. Exit the review session before running another queue command, because the session holds the queue lock.

## Folder options

For PDFs ending in an organizer's name, such as `Mo-Smit-NATE-CORDER (1).pdf`:

```sh
./.venv/bin/python extract_person.py --marker "NATE-CORDER"
```

The marker is the literal ending before any copy number and `.pdf`; matching ignores letter case.

For a different download folder:

```sh
./.venv/bin/python extract_person.py --downloads-dir "/full/path/to/downloads"
```

For a different queue, add `--queue-dir "/full/path/to/composed_info"` to **every** extraction, preview, lookup, submit, and review command. Point to the folder containing all three subfolders and the history file. Quote paths containing spaces.

<details>
<summary>Choose the correct version of conflicting PDF copies</summary>

After checking which unsent PDF is correct:

```sh
./.venv/bin/python extract_person.py "/full/path/to/Mo-Smit-ORGANIZED (1).pdf" --resolve
```

Include any custom folder or marker options used for the original import. This resolves a source-PDF choice; it does not clear an Action Builder hold or approve resending a previous attempt. Use the review command for those records.

</details>

## Help

| Message or problem | What to do |
|---|---|
| **No matching PDFs** | Check the download folder and filename ending. Use `--downloads-dir` or `--marker` if needed. |
| **Waiting for download** | Let the download finish, wait a few seconds, and run extraction again. |
| **Needs review** | Run `review_person.py`. If extraction failed before creating a contact record, inspect and correct the source PDF first. |
| **Lookup stopped** | Read the error, check the connection/settings, and retry the lookup. Failed checks do not authorize sending. |
| **Send failed or outcome uncertain** | Check Action Builder before retrying: the person may already have been created. |
| **Queue locked** | Exit any active review or other run. After a crash, confirm the earlier process has stopped before attempting recovery. |

When updating the program, stop active runs and back up `.env` and the complete `composed_info` folder privately. Keep both in place, update the code and packages, and rerun the installation checks. Older queue history upgrades automatically; use the updated code afterward.

Keep API keys, downloaded forms, contact records, queue history, and terminal output containing personal information out of Git. The default local configuration and queue folders are ignored; custom output locations need their own protection.

## License

Copyright © 2026 William Jerrells iii. All rights reserved. Use by others requires express written permission. See [license.md](license.md) for permitted use and restrictions.
