# PDF to Action Builder

Import contact information from completed **New Member Checklist / LPX Data Entry** PDFs into Action Builder. The program finds downloaded forms, lets you preview the extracted details, checks for existing people by email and phone, and submits eligible records. An interactive review command handles records that need attention.

It reads first name, last name, middle initial, email, phone, street address, city, state, and ZIP code. All nine contact fields are required. It also recognizes the selected classification on the supported CW/CE sheet. The supported format is the checklist template with US contact details; scanned images and unrelated PDF layouts require review. Other form information, including Social Security numbers and beneficiary details, is excluded.

[Local browser app](#local-browser-app) · [Setup](#setup) · [Daily commands](#daily-commands) · [Manual review](#manual-review) · [Folder options](#folder-options) · [Help](#help) · [License](#license)

## Local browser app

Member Intake provides the import, batch preview, duplicate checks, submission results, and individual review workflow in your browser. Python runs on your computer at `127.0.0.1`; nothing is hosted. Contact files and credentials are stored in this installation. Python sends approved contact fields to Action Builder when you submit. Optional Census county lookup sends only address fields to the U.S. Census Bureau when enabled in Settings. No extra GUI packages or Node.js are required.

The interface uses warm amber accents. Red labels identify held records, uncertain attempts, and errors; green labels and row highlights identify confirmed submissions. Status text accompanies the colors.

### Windows: one-time setup

1. Install Git and Python 3.10 or newer (include the Python launcher).
2. Clone this repository into a folder you intend to keep. A clone alone does not create a desktop icon.
3. Double-click **Setup Windows.cmd**. It creates the virtual environment, installs the Python requirements, and adds **Member Intake** to your Windows desktop.
4. Open the desktop shortcut. On first launch the app creates `.env` if it is missing and prompts for the API key, organization subdomain, and campaign ID. Choose your actual Downloads folder if Windows redirects it elsewhere.
5. Enter the constant **Residence local** to enable member tags and assessment 1. Use **Test connection**, then **Save settings**. The connection test searches with a synthetic email and checks the CE/CW and configured local tag definitions; it does not create a person or verify permission to create people.

From a new PowerShell window, with Git and Python installed:

```powershell
git clone https://github.com/WYSblue-dev/ab-auto-fill.git ab_automation
cd ab_automation
& ".\Setup Windows.cmd"
& ".\Start Member Intake.cmd"
```

The setup command waits for a keypress when finished. Later, use the **Member Intake** desktop shortcut. No virtual-environment activation or manual `.env` copy is needed for the GUI.

An existing `.env` is preserved. **Settings** is always available to replace incorrect or expired credentials. A blank API key field keeps the saved key. The app reads its own `.env` directly, so old environment variables do not override changes made in Settings. The file is excluded from Git and contains the key in plain text; keep it private.

The shortcut opens your default browser and keeps a minimized Python console running. Use **Close app** to stop the local service when finished. Closing only the tab leaves Python running; reopening the shortcut reconnects to that running installation. A port occupied by another installation or app is reported instead of reused. Do not close the console during a submission.

### macOS: launch locally

With Git and Python 3.10 or newer installed, open Terminal:

```sh
git clone https://github.com/WYSblue-dev/ab-auto-fill.git ab_automation
cd ab_automation
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python local_app.py
```

First launch opens credential setup and creates `.env` if needed. Enter the API key, subdomain, campaign ID, residence-local constant, and Downloads folder. Use **Test connection**, then **Save settings**. Later, double-click **Start Member Intake.command** in the project folder, or rerun the last command. macOS setup currently does not create a desktop shortcut.

The Terminal window stays open while the app runs. Use **Close app** in the browser when finished. If macOS blocks opening the launcher, run the Python command from Terminal.

### Daily workflow

1. **Import → Find paperwork:** Scan the folder selected in Settings for the supported checklist PDFs.
2. **Batch preview:** Review the names and contact details. **Check only** performs GET lookups without submitting. **Check and submit batch** shows the destination and names for confirmation, then performs fresh checks before creating eligible people.
3. **Results:** See confirmed submissions and their Action Builder person IDs.
4. **Needs review:** Inspect candidate IDs, edit contact details, or discard an import with a reason. **Check revised details** is required before approving a separate person. Approval expires after ten minutes; a fresh lookup during submission must agree with the result you reviewed. Earlier uncertain attempts require checking Action Builder manually, and related successful submissions block another creation.

The GUI uses the existing `composed_info` queue and history. The CLI still works, but do not run it against the same queue while a GUI operation is running. Folder or parsing issues appear in Import; a record that cannot be extracted may require correcting the PDF and importing again.

### Updating an installation

Close the app, then run from the existing project folder:

```powershell
git pull --ff-only
```

Run **Setup Windows.cmd** again if dependencies changed or the project folder moved. Setup preserves `.env`, `composed_info`, and sending history. Keep the existing installation rather than replacing it with a fresh clone that has no history.

The Python workflows and local HTTP interface are tested on macOS. The Windows setup script still needs an end-to-end check on a Windows computer.

## Setup

You need Python 3.10 or newer, Git, and authorized Action Builder API settings. The program has been tested on macOS with Python 3.14.7. Windows commands use PowerShell.

A **terminal** is the window where you enter commands. Run each command from the project folder unless a step says otherwise.

### 1. Get the project

Clone this repository:

```sh
git clone https://github.com/WYSblue-dev/ab-auto-fill.git ab_automation
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

### Member tags and assessment

Set `ACTION_BUILDER_RESIDENCE_LOCAL` in your own `.env` to the exact local-number response you want assigned to every newly submitted member. The example file leaves it blank; no local number is hard-coded. The local browser app exposes this same setting as **Residence local**.

When configured, submissions include:

- **Fourth District Workers → Classification - 4D:** the approved PDF classification. Every CW/CE level maps to **CE/CW**; the explicitly selected JOURNEYMAN option maps to **Journeyman**.
- **Fourth District Workers → Local Jurisdiction by Zip (Residence) - 4D:** the configured constant, regardless of residence ZIP.
- **Assessment 1.**

The tag responses must already exist in the campaign. The sender checks their exact section and field before creation, then reads back both tags and assessment 1 before marking the submission complete. If the person was created but these values cannot be confirmed, the record stays held: correct the existing Action Builder entry and reconcile it, rather than sending the person again.

Classification recognition is deliberately limited to the verified CW/CE form and its selected mark. Missing or ambiguous classifications are held when checking or sending with member automation enabled. Use **Change → Classification (field 10)** in `review_person.py`, or the classification dropdown in the local GUI, to choose from the approved list. An existing queue record keeps its history; no manual JSON edits or queue reset are needed. All allowed options are defined in `member_classification.py`; an unknown value never silently becomes **None**.

A blank residence-local setting retains the legacy contact-only workflow for older records without classification. Newly classified records require the setting before sending. Preview JSON shows the extracted classification tag; the residence-local tag and assessment are added from the configured destination settings during checking/sending.

### County reference in the local browser app

**County reference** is available beside each address in Batch preview, Results, and Needs review. Enter the residence county manually, or enable **Automatic Census lookup on import** in Settings. The default is manual (`MEMBER_INTAKE_COUNTY_LOOKUP=manual`). Enabling `census` sends each pending or held record's street address, city, state, and ZIP to the [U.S. Census geocoder](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html); names, email, phone, classification, credentials, and union information are excluded. Saved county entries are reused. **Look up county** retries an individual address. Review the returned matched address and correct the county manually when needed. Missing/ambiguous matches and connection errors leave county review unresolved and do not stop person submission.

The initial reference profile is for Local **1105**, using its [published county coverage](https://www.ibewlocal1105.org/about/) and the [IBEW Ohio inside-construction map](https://ibew.org/wp-content/uploads/2024/10/OH_Inside_Final-2018_v2-corrected-erroneous-things.pdf), revised February 2018:

- Coshocton, Guernsey, Licking, Muskingum, and Perry counties: inside by county reference.
- Knox and Tuscarawas: split counties; check the residence address against the map.
- Other entered or matched counties: outside by rough county reference, with a copyable suggested note.

This is an operator reference, not exact address-boundary verification, outside-construction jurisdiction, or a membership eligibility decision. Other configured locals show the national map index and “not configured” until a corresponding county profile is added; they never inherit Local 1105's coverage. The residence-local tag remains the configured constant.

**Notes require manual entry in Action Builder.** The documented [signup helper](https://www.actionbuilder.org/docs/v1/person_signup_helper.html) does not expose the general Notes box. The app prepares text for **Copy note**; paste it into the existing person's Notes box. A successful person receipt or copying text does not confirm that a note was saved. County review does not change an existing Action Builder record or retry a submission.

County annotations are saved separately in `composed_info/.residence-reviews.json`, bound to the local record and its address. Correcting a record invalidates its old annotation. Preserve this file with the rest of the queue when updating or moving an installation. The CLI submission behavior is unchanged; county review is available through the browser app.

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
| **Reconcile (`r`)** | For an uncertain submission you already found in Action Builder: fresh GET searches must find one exact match in the saved campaign. After confirmation, moves the local record to `sent` without creating or updating anyone. |
| **Keep** | Leaves the record for another review session. |
| **Discard** | Confirms removal of the local contact file. History remains to prevent the same record returning on another import. |
| **Quit** | Ends the session. Unfinished records stay held. |

Pressing Enter at a confirmation means **No**; Enter at the action menu means **Keep**. Sending and discarding require confirmation that you checked the person and paperwork.

**Send creates the reviewed person directly after approval.** It does not return them to pending. A related record already marked sent blocks another creation. Changes edit the local contact; they do not update an existing Action Builder person. Discarding leaves source PDFs and remote records untouched.

If a submission receives a successful HTTP response but an unrecognized JSON receipt, the sender now checks Action Builder again using GET requests. One exact email/phone search result with matching contact details confirms the person is present; missing, differing, ambiguous, or failed results remain held for review. The POST is never repeated by this recovery. This verifies the resulting record, not the reason the original receipt differed.

For an earlier held submission you verified manually, run `review_person.py`, confirm that you checked the person, choose **r**, and confirm the fresh exact match. Do not choose Send for a person who was already created. Keep the record held if reconciliation cannot establish one exact match. The local GUI provides **Mark already created** for uncertain records.

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
