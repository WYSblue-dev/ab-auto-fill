# Add people to Action Builder from downloaded PDFs

This tool reads contact information from completed Jotform PDFs so you do not have to type it into Action Builder by hand.

**Available now:** it finds matching PDFs in your Downloads folder, checks their contact details, compares repeated downloads, and puts the results in a local waiting list. You can preview that list and check for existing people in the configured Action Builder campaign before sending.

**Still planned:** a file on your desktop that starts the process when you double-click it. For now, the person setting this up runs the commands below. The existing-person check is implemented, but its matching rules and the remaining issues in the code review still need validation before rollout.

Before rollout, read the [detailed code walkthrough and review](CODE_REVIEW.md). It covers every function, current test evidence, confirmed defects, and a weekend validation plan.

## What happens to a downloaded PDF?

1. **Find it.** The tool looks in the current user's Downloads folder for filenames with the configured ending. The default ending is `ORGANIZED.pdf`, based on the example supplied for this project.
2. **Read it.** It finds the **New Member Checklist / LPX Data Entry** page and checks the expected contact fields.
3. **Compare copies.** Filenames ending in `(1).pdf`, `(2).pdf`, and similar numbers are checked together with the original. Different contact details are held for review.
4. **Add the result to the waiting list.** Each accepted contact record is saved as a small `.json` file inside `composed_info/pending`. JSON is simply a text format the sending program can read.
5. **Preview.** The sending program shows the pending records without using the API.
6. **Check Action Builder.** `action_builder_lookup.py` searches the configured campaign using GET requests. Existing people and possible matches move to `review`; records without a match remain `pending`.
7. **Send.** Sending requires the separate `--submit` option. The sender always performs a fresh existing-person check before each creation attempt, even if you already ran the separate check.

The original PDFs stay in Downloads, unchanged. **`composed_info` stores contact JSON files and processing history.** No new PDFs are generated, and the PDFs themselves are not sent to Action Builder.

## Where are the results stored?

The tool creates this folder beside the Python scripts:

```text
composed_info/
    pending/             Contact records waiting for preview and API checks
    sent/                Contact records the sender has recorded as successful
    review/              Contact records held for attention
    .queue-state.json    The tool's history of files and sending attempts
```

The tool manages these folders for you. After a send is accepted as successful by the current response checks, the contact file moves to `sent`. Those checks still need strengthening, as described in the code review; during validation, verify the resulting person in Action Builder. Unsent records held because of corrected or unfinished paperwork, or an existing-person match, move to `review`. A record also stays in `review` while its send is in progress or if the result of that attempt is uncertain.

**An empty `review` folder does not mean there is nothing to review.** An invalid PDF or conflicting first-time copies may never produce a usable contact JSON. Read the scan messages and check the source PDFs; the history still records the affected download group.

The hidden `.queue-state.json` file is the record of what can be sent. Moving a file from `sent` or `review` into `pending` by hand does not reset that history or make it eligible to send again.

## Which information is included?

- First and last name, plus the middle initial entered on the form.
- Phone number and email address.
- Street address, city, state, and ZIP code.

All nine contact fields are required by the current extractor because this source form marks them required. It supports the verified checklist layout, US phone numbers, and US addresses. It does not read every kind of PDF or scanned image.

Social Security number, birth date, ethnicity, beneficiary information, classification, employer, membership or organizing status, hours, and organizer name are excluded from the contact record sent to Action Builder. Organizer text in a filename can help find files; it is not imported as a contact field.

## What if the paperwork was downloaded or filled out again?

The number in `(1)` does not prove that a file is a correction. The tool checks the contents instead:

| What it finds | What happens |
|---|---|
| The original and numbered copies produce the same nine contact values | One contact record is queued. |
| The copies produce different contact values | The group is held for review; the older version is not left pending. |
| A related download is empty, incomplete, too recent, or changes while being read | The whole group waits until the download is ready. |
| The same files have already been handled | Their local history is retained instead of adding them again. |
| A correction arrives after a related record was sent or attempted | The group needs review in Action Builder; it is not automatically sent as a new person. |

These comparisons concern the nine contact fields only. A change to excluded information, such as a beneficiary, does not create another contact record.

The waiting list remembers successful sends on this computer. The separate API lookup also checks for people entered in the configured campaign by other means. It does not search the entire organization or coordinate separate computers. Two computers could both check before either creates the person, so use one sending computer while a shared sending workflow is still being designed.

## If something goes wrong

- **No files were found:** Check the Downloads folder and filename ending with your setup person. A different ending can be configured without editing Python code.
- **Waiting for downloads:** Let the browser finish downloading, then run extraction again. By default, a file must be at least two seconds past its last modification before it can be read.
- **Needs review:** Check the identified PDFs and, for an existing-person match, the corresponding Action Builder record. The program does not automatically choose which version is correct, update an existing person, or delete anything.
- **Lookup stopped:** The search did not complete reliably. The affected record stays pending and is not sent; fix the reported problem and check again. Earlier completed holds or sends remain recorded.
- **Preview only:** Nothing was sent. This lets you compare the extracted details with the paperwork.
- **A send failed or its outcome is uncertain:** Have the setup person check Action Builder. The tool holds that attempted record instead of automatically retrying it.
- **The queue is locked:** Another run may still be active. If a previous run crashed, the setup person must confirm it has stopped and review its state. Do not remove files to force another send.

Keep the contents of `composed_info` together, including its three folders and hidden history file. Do not delete the history, edit queued JSON, or move contact files between folders by hand to retry a record.

## For the person setting this up

Everyday users should not need this section once the desktop launcher is available.

### One-time setup

Set up and validate one sending computer first. Each computer has its own Downloads folder and queue history; cloning this repository does not share those records or prevent two computers from creating the same person.

You need:

- **Python 3.10 or newer.** The current dependency versions require at least 3.10. The recorded regression run used **Python 3.14.7 on macOS**; the minimum version and Windows instructions have not been independently tested as supported deployment targets.
- **Git**, if you will clone the repository. Alternatively, obtain a copy of the project files and open that folder.
- An internet connection for package installation and API operations, plus authorized Action Builder API settings for the intended campaign.
- A completed PDF using the supported checklist template. Extraction and offline preview do not need an API key.

A **terminal** is the window where you type commands: use Terminal on macOS or PowerShell on Windows. Run each command from the project folder unless a step says otherwise. A **virtual environment** is a private folder containing this project's Python packages; it does not replace the computer's other software.

**1. Get the project.** Replace `YOUR_REPOSITORY_URL` with this repository's clone URL. Run these commands in the parent folder where you want to keep the project:

```sh
git clone "YOUR_REPOSITORY_URL" ab_automation
cd ab_automation
```

If the project has already been copied or cloned, open a terminal in that existing folder instead. Do not create a second copy of a working queue as a way to retry sending.

**2. Create the virtual environment and install packages.** Choose the commands for your computer.

macOS:

```sh
python3 --version
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Check the version shown by the first command before continuing. If `python3` or `py` is not found, Python installation needs attention. Some Windows installations provide `python` instead of `py`; the setup person can use that command after checking that it refers to a compatible Python 3 installation.

The commands below call the environment's Python directly. You do not need to activate the environment or change PowerShell's script execution policy. Create a new `.venv` on each computer; do not copy one from another computer.

**3. Configure the destination.** For a new installation without an existing `.env`, copy the example file:

macOS:

```sh
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Open `.env` in a text editor and replace all three example values:

| Setting | What belongs here |
|---|---|
| `ACTION_BUILDER_API_KEY` | The API key supplied by the organization's administrator. |
| `ACTION_BUILDER_SUBDOMAIN` | Only the name before `.actionbuilder.org`, such as `example`; do not enter `https://` or the full website address. |
| `ACTION_BUILDER_CAMPAIGN_ID` | The identifier of the destination campaign supplied by the administrator. |

The `.env` file is plain text and contains credentials. Keep it private. Do not overwrite an existing configuration with the example when updating the project. Environment variables already set on the computer take precedence over values in `.env`, so the setup person should check for conflicting settings if the destination is unexpected.

Use a dedicated test campaign during validation. Keep each queue associated with one destination; its send history is not currently bound to the configuration, so changing campaign settings does not create a clean, independent history.

**4. Check the download location and filename ending.** The default folder is the current user's home folder followed by `Downloads`. This is a fixed path, not a lookup of the browser's actual download setting or a redirected Windows folder. Supply `--downloads-dir` if they differ. Only that folder's immediate files are scanned, not its subfolders.

The supplied example ends with **`ORGANIZED`**, so that is the default marker. If the actual ending is an organizer's name in capitals, supply that literal ending with `--marker`, as shown below.

**5. Run the local regression checks before using the installation.** These checks use temporary files and mocked API responses; they do not contact your campaign.

macOS:

```sh
./.venv/bin/python -m unittest discover -s tests -v
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

A passing test run does not establish that the live campaign's API responses or every PDF export match the assumptions. Use the validation plan in [CODE_REVIEW.md](CODE_REVIEW.md) before rollout.

### Everyday command reference

Run the preparation and checking commands in order, inspect the messages and preview, and use the separate sending command only when ready to create records in the configured campaign.

| Step | macOS | Windows PowerShell | Effect |
|---|---|---|---|
| 1. Extract downloaded PDFs | `./.venv/bin/python extract_person.py` | `.\.venv\Scripts\python.exe extract_person.py` | Reads local PDFs and changes the local queue. |
| 2. Preview pending records | `./.venv/bin/python send_person.py` | `.\.venv\Scripts\python.exe send_person.py` | Prints the proposed payloads without API requests. |
| 3. Check existing people | `./.venv/bin/python action_builder_lookup.py` | `.\.venv\Scripts\python.exe action_builder_lookup.py` | Reads the API with GET and saves local decisions or holds. |
| 4. Send after review | `./.venv/bin/python send_person.py --submit` | `.\.venv\Scripts\python.exe send_person.py --submit` | Rechecks the API and may create people with POST. |

The detailed examples below use the macOS interpreter path. On Windows, replace `./.venv/bin/python` with `.\.venv\Scripts\python.exe`; keep the script name and options the same, and use Windows paths for your folders. Quote any path containing spaces. For example:

```powershell
.\.venv\Scripts\python.exe extract_person.py --downloads-dir "C:\Users\YourName\Downloads" --marker "NATE-CORDER"
```

### Find PDFs and build the waiting list

```sh
./.venv/bin/python extract_person.py
```

This scans the current user's Downloads folder and creates `composed_info` and its three subfolders beside the scripts if needed. Accepted contact records go into `pending`. It scans **all matching completed downloads, including older ones**. Check the first preview carefully before submitting.

The scan reports how many groups were queued, already handled, held for review, or waiting for downloads. “Already handled” can include records that are still pending; it does not mean every record has been sent. This step makes no API requests.

For another download folder:

```sh
./.venv/bin/python extract_person.py --downloads-dir "/full/path/to/downloads"
```

For filenames such as `Mo-Smit-NATE-CORDER.pdf` and `Mo-Smit-NATE-CORDER (1).pdf`:

```sh
./.venv/bin/python extract_person.py --marker "NATE-CORDER"
```

The marker must be the filename's ending before any browser copy number and `.pdf`. Matching ignores letter case. A hyphen, underscore, or space must separate the person's filename prefix from the marker. It does not accept an unrelated filename just because it contains capitals.

Use `--queue-dir "/full/path/to/composed_info"` to choose another queue location. **Point to the main folder containing the history and all three subfolders, not to `pending`, `sent`, or `review`.** Pass that same option to extraction, lookup, and sending. Continue using the same queue so its sending history stays available.

### Preview the pending records

```sh
./.venv/bin/python send_person.py
```

This previews tracked records from `composed_info/pending` without making an API request. Compare their names and contact details with the source PDFs. Records held for review and previously sent records are not pending sends.

### Check whether pending people already exist

```sh
./.venv/bin/python action_builder_lookup.py
```

For another queue:

```sh
./.venv/bin/python action_builder_lookup.py --queue-dir "/full/path/to/composed_info"
```

This requires the same connection settings as sending. It makes **GET requests only**: it reads Action Builder and saves the decisions locally. It never creates, updates, or deletes a remote person.

The check searches email and phone independently, then searches the surname for possible matches with the same first name or street address. It compares the nine contact fields with limited formatting normalization. One person with all matching fields is recorded as `existing`; differing details or several candidates are `needs_review`. Both outcomes move the local record to `review`. `not_found` leaves it pending.

The history saves the check time, campaign, candidate IDs, and names of matching/differing fields. It does not copy the candidate's personal values or your API key into that receipt. The program retains the contact JSON and does not delete source PDFs, so you can compare the paperwork with Action Builder.

**There is not yet a command to release an API-related hold after review.** A fresh PDF import or `--resolve` cannot bypass that hold, including through connected filename aliases. Have the setup person review Action Builder and the recorded reason; do not delete history or move files to force a submission. The operator reconciliation workflow is still to be implemented.

A successful no-match check is a result for that moment and that campaign, not proof that the latest PDF has been extracted. Run extraction again after corrections arrive, compare the paperwork, and address its scan messages. A person with changed names and contact details can also escape these matching rules.

### Send the pending records

```sh
./.venv/bin/python send_person.py --submit
```

The `--submit` option first makes fresh GET checks and then sends POST requests for records with no detected match, one pending record at a time. Matches are held for review. An incomplete or failed lookup stops the run before a creation attempt for that record. It requires the connection settings and an internet connection. The tool moves each claimed record into `review` while sending, then into `sent` when the send succeeds. Successful sends are recorded in the history so the next queue run does not send them again.

If a request is attempted but the outcome cannot be confirmed, that record remains in `review` with an uncertain status in the history. A timeout does not prove that Action Builder rejected the person. Check Action Builder before any recovery; deleting the queue history is not a retry procedure.

Run extraction again after new or corrected PDFs arrive, then preview before submitting. Sending reads the stored contact records; it does not rescan Downloads for new corrections.

### Choose the correct version of an unsent PDF

If a group is held because its copies contain different details, open the PDFs and decide which one is correct. Then explicitly choose that file:

```sh
./.venv/bin/python extract_person.py "/full/path/to/Mo-Smit-ORGANIZED (1).pdf" --resolve
```

Include the same `--downloads-dir`, `--marker`, and `--queue-dir` options used for the original scan if you changed them. The tool remembers the related copies present during this choice so the next scan does not reopen the same conflict. A new changed copy can still trigger review.

`--resolve` is for a checked, unsent PDF correction. It does not clear an API-related hold or authorize sending another person after a related record has already been sent or attempted. Those cases require checking the existing Action Builder record.

### Upgrading an older queue

An older queue may have stored all contact JSON files alongside `.queue-state.json` in one folder. When the updated tool opens that folder, it converts the layout in place, putting the records into `pending`, `sent`, or `review` while retaining the existing history.

For an older custom location, continue passing that same root folder with `--queue-dir` to extraction, lookup, and sending. Do not copy only its contact files into a fresh queue: the history is needed to preserve sent and held statuses. This project's default location is now `composed_info`; any move from an older location such as `senders_pdfs` must keep its history with its records. If the old default folder still contains queue data, the tool stops for setup attention rather than silently starting a separate default queue.

### Read one file or keep the standalone workflow

To add one named PDF to the queue, while checking related copies in the configured Downloads folder:

```sh
./.venv/bin/python extract_person.py "/full/path/to/downloaded.pdf"
```

To create a separate JSON file instead of using the queue, supply `--output` explicitly:

```sh
./.venv/bin/python extract_person.py "/full/path/to/downloaded.pdf" --output "/full/path/to/approved.json"
./.venv/bin/python send_person.py "/full/path/to/approved.json"
```

Appending `--submit` to the second command performs the same fresh API lookup and submits only if no match is detected. That lookup requires usable names, email, US phone, and address even for a standalone file. **Standalone files outside the queue do not have the queue's sending history.** If extraction fails, an older output may still exist; do not send that older file accidentally. The no-argument workflow no longer writes `pending_person.json`.

### Understanding the code in the morning

Start at `main()` near the bottom of `extract_person.py`, then follow the modules in this order:

1. **`pdf_downloads_finder.py`** finds matching filenames, groups numbered copies, and computes file fingerprints. A fingerprint lets it compare file contents without relying on the filename or date alone.
2. **`extract_person.py`** reads the checklist and compares the nine extracted contact values across copies. `import_downloads()` coordinates the scan; `extract_approved_person()` handles one PDF.
3. **`record_queue.py`** stores contact JSON under `composed_info/pending`, `sent`, or `review`, with `.queue-state.json` at the root. The history tracks pending, review, sending, sent, and uncertain statuses; the folders follow those statuses. A lock prevents two runs from changing the same queue at once.
4. **`action_builder_lookup.py`** checks the destination settings, searches Action Builder, validates complete search results, and classifies possible matches. Its standalone command checks pending records and saves local review decisions.
5. **`send_person.py`** builds the Action Builder request, previews offline, or calls the lookup before submitting with `--submit`. `check_queue()` reuses its queue workflow for GET checks without a POST.

Inside extraction, `parse_approved_person()` verifies the page size and printed labels before reading answers. `FIELDS` contains answer areas; `LABEL_AREAS` checks that the template still matches. PDF coordinates start at the bottom-left. `read_page_fragments()` combines the PDF's text and page transformations to find the displayed text positions.

The `normalize_*()` functions check contact formatting; they do not verify that a person or address exists. A missing answer stays missing: the extractor never substitutes a neighboring answer or a value from another page. Up to two separately positioned address lines are supported; ambiguous fragments need review. More representative exports should be checked before unattended use.

Run the regression checks from the project folder:

```sh
./.venv/bin/python -m unittest discover -s tests -v
```

The checks use temporary files and invented contact details. They cover extraction, filename discovery, repeated downloads, changed copies, queue history, existing-person lookup, and sending behavior without making live API requests.

### What belongs in Git?

Git is for the program, tests, example configuration, and documentation. Each installation keeps its credentials and contact data locally.

- Commit the Python files, `requirements.txt`, tests, documentation, `.env.example` with placeholders, and the empty queue folders' `.gitkeep` files.
- Do not commit `.env`, downloaded forms, real contact JSON, `.queue-state.json`, or terminal output containing personal information. Previews print contact details, so check screenshots and copied output before sharing them.
- The existing `.gitignore` excludes the default queue data, `.env`, `.venv`, `pending_person.json`, and files under `senders_pdfs`. It does **not** protect every PDF or every custom JSON output path. Keep custom outputs outside the repository or arrange an appropriate ignore rule before saving them there.
- Review `git status --short` and `git diff --cached` before committing. An ignore rule does not remove sensitive files already tracked by Git, and an empty queue on another computer does not know what this computer has already sent.

When updating an installation, stop running scripts first, retain the complete local queue and configuration, install the required packages into that computer's environment, and rerun the tests. Do not replace the queue with the repository's empty folder structure. A deliberate backup should keep the history and all contact folders together in a private location.

For the current changes, see the suggested commit title and body in [COMMIT_MESSAGE.md](COMMIT_MESSAGE.md). [CODE_REVIEW.md](CODE_REVIEW.md) contains the detailed review and release checks. Read the proposed message, review the exact staged files, and finish validation before committing or pushing.

### Still needed for the desktop version

- A desktop file that launches the correct Python environment and coordinates extraction, review, and sending.
- Validation of the existing-person matching rules against a controlled test campaign, and coordination of creation across computers.
- A clear review and recovery interface for corrected paperwork and uncertain sends.
- Progress and completion messages that remain visible for everyday users.

The intended everyday workflow remains: download the paperwork, double-click the desktop file, and read the result.

## License and permitted use

Copyright © 2026 William Jerrells iii. All rights reserved. Recipients other than the copyright holder need express written permission to use this project; authorized use is limited to the internal purposes and computers covered by that permission. Outside use or redistribution requires additional written permission. Read [license.md](license.md) for the terms and warranty and liability disclaimers. Third-party packages retain their own licenses. These terms are not a guarantee of legal protection; section 21 of [CODE_REVIEW.md](CODE_REVIEW.md#21-private-use-license-ownership-and-practical-limits) explains the remaining limits and the need for legal review before relying on them.
