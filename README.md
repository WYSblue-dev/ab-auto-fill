# Add people to Action Builder from downloaded PDFs

This tool reads contact information from completed Jotform PDFs so you do not have to type it into Action Builder by hand.

**Available now:** it finds matching PDFs in your Downloads folder, checks their contact details, compares repeated downloads, and puts the results in a local waiting list. You can preview that list before sending it to Action Builder.

**Still planned:** a file on your desktop that starts the process when you double-click it. For now, the person setting this up runs the commands below. The tool also does not yet search Action Builder to check whether a person already exists there.

## What happens to a downloaded PDF?

1. **Find it.** The tool looks in the current user's Downloads folder for filenames with the configured ending. The default ending is `ORGANIZED.pdf`, based on the example supplied for this project.
2. **Read it.** It finds the **New Member Checklist / LPX Data Entry** page and checks the expected contact fields.
3. **Compare copies.** Filenames ending in `(1).pdf`, `(2).pdf`, and similar numbers are checked together with the original. Different contact details are held for review.
4. **Add the result to the waiting list.** Each accepted contact record is saved as a small `.json` file inside `senders_pdfs`. JSON is simply a text format the sending program can read.
5. **Preview, then send.** The sending program shows the pending records. Sending requires the separate `--submit` option.

The original PDFs stay in Downloads, unchanged. Despite its name, **`senders_pdfs` stores contact JSON files and processing history, not copies of the PDFs.** No new PDFs are generated, and the PDFs themselves are not sent to Action Builder.

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

The waiting list remembers successful sends on this computer. **That is not an existing-person search in Action Builder**, and it does not coordinate separate computers. A person entered manually, entered elsewhere, or submitted with changed contact details may still need an existing-person check.

## If something goes wrong

- **No files were found:** Check the Downloads folder and filename ending with your setup person. A different ending can be configured without editing Python code.
- **Waiting for downloads:** Let the browser finish downloading, then run extraction again. By default, a file must be at least two seconds past its last modification before it can be read.
- **Needs review:** Check the identified PDFs. Missing fields, unexpected form layouts, and conflicting copies need attention before that group can be sent.
- **Preview only:** Nothing was sent. This lets you compare the extracted details with the paperwork.
- **A send failed or its outcome is uncertain:** Have the setup person check Action Builder. The tool holds that attempted record instead of automatically retrying it.
- **The queue is locked:** Another run may still be active. If a previous run crashed, the setup person must confirm it has stopped and review its state. Do not remove files to force another send.

Keep the contents of `senders_pdfs` together. Its hidden history file remembers what was sent and which corrections are held. Do not delete the history, edit queued JSON, or move contact files into the folder by hand to retry a record.

## For the person setting this up

Everyday users should not need this section once the desktop launcher is available.

### One-time setup

1. Copy or clone the project onto the user's computer.
2. Install Python and the packages in `requirements.txt`, preferably in a dedicated Python virtual environment. With that environment active, run `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` in the project folder and fill in `ACTION_BUILDER_API_KEY`, `ACTION_BUILDER_SUBDOMAIN`, and `ACTION_BUILDER_CAMPAIGN_ID`. These select the organization's credentials and destination campaign. The `.env` file is plain text; keep the API key private.
4. Check the filename ending used by the downloaded forms. The supplied example ends with **`ORGANIZED`**, so that is the default. If your actual ending is an organizer's name in capitals, supply that literal ending with `--marker`, as shown below.

Use a terminal—the window where you type commands—opened in the project folder, with the Python environment active. The examples use `python`; your installation may use `python3` or `py` instead.

### Find PDFs and build the waiting list

```sh
python extract_person.py
```

This scans the current user's Downloads folder and creates `senders_pdfs` beside the scripts if needed. It scans **all matching completed downloads, including older ones**. Check the first preview carefully before submitting.

The scan reports how many groups were queued, already handled, held for review, or waiting for downloads. “Already handled” can include records that are still pending; it does not mean every record has been sent. This step makes no API requests.

For another download folder:

```sh
python extract_person.py --downloads-dir "/full/path/to/downloads"
```

For filenames such as `Mo-Smit-NATE-CORDER.pdf` and `Mo-Smit-NATE-CORDER (1).pdf`:

```sh
python extract_person.py --marker "NATE-CORDER"
```

The marker must be the filename's ending before any browser copy number and `.pdf`. Matching ignores letter case. A hyphen, underscore, or space must separate the person's filename prefix from the marker. It does not accept an unrelated filename just because it contains capitals.

Use `--queue-dir "/full/path/to/waiting-list"` to choose another queue folder. Pass that same option to both extraction and sending. Continue using the same queue so its sending history stays available.

### Preview the pending records

```sh
python send_person.py
```

This previews pending records from `senders_pdfs` without making an API request. Compare their names and contact details with the source PDFs. Review-held and previously sent records are not pending sends.

### Send the pending records

```sh
python send_person.py --submit
```

The `--submit` option sends actual requests to the configured Action Builder campaign, one pending record at a time. It requires the connection settings and an internet connection. Successful sends are recorded locally so the next queue run does not send them again.

If a request is attempted but the outcome cannot be confirmed, that record is held as uncertain. A timeout does not prove that Action Builder rejected the person. Check Action Builder before any recovery; deleting the queue history is not a retry procedure.

Run extraction again after new or corrected PDFs arrive, then preview before submitting. Sending reads the stored contact records; it does not rescan Downloads for new corrections.

### Choose the correct version of an unsent PDF

If a group is held because its copies contain different details, open the PDFs and decide which one is correct. Then explicitly choose that file:

```sh
python extract_person.py "/full/path/to/Mo-Smit-ORGANIZED (1).pdf" --resolve
```

Include the same `--downloads-dir`, `--marker`, and `--queue-dir` options used for the original scan if you changed them. The tool remembers the related copies present during this choice so the next scan does not reopen the same conflict. A new changed copy can still trigger review.

`--resolve` is for a checked, unsent correction. It does not authorize sending another person after a related record has already been sent or attempted. Those cases require checking the existing Action Builder record.

### Read one file or keep the standalone workflow

To add one named PDF to the queue, while checking related copies in the configured Downloads folder:

```sh
python extract_person.py "/full/path/to/downloaded.pdf"
```

To create a separate JSON file instead of using the queue, supply `--output` explicitly:

```sh
python extract_person.py "/full/path/to/downloaded.pdf" --output "/full/path/to/approved.json"
python send_person.py "/full/path/to/approved.json"
```

Appending `--submit` to the second command submits that standalone file. **Standalone files outside the queue do not have the queue's sending history.** If extraction fails, an older output may still exist; do not send that older file accidentally. The no-argument workflow no longer writes `pending_person.json`.

### Understanding the code in the morning

Start at `main()` near the bottom of `extract_person.py`, then follow the modules in this order:

1. **`pdf_downloads_finder.py`** finds matching filenames, groups numbered copies, and computes file fingerprints. A fingerprint lets it compare file contents without relying on the filename or date alone.
2. **`extract_person.py`** reads the checklist and compares the nine extracted contact values across copies. `import_downloads()` coordinates the scan; `extract_approved_person()` handles one PDF.
3. **`record_queue.py`** stores the contact JSON and `.queue-state.json` history in `senders_pdfs`. It tracks pending, review, sending, sent, and uncertain records. A lock prevents two runs from changing the same queue at once.
4. **`send_person.py`** builds the Action Builder request from each pending contact record, previews it, or submits it when `--submit` is supplied.

Inside extraction, `parse_approved_person()` verifies the page size and printed labels before reading answers. `FIELDS` contains answer areas; `LABEL_AREAS` checks that the template still matches. PDF coordinates start at the bottom-left. `read_page_fragments()` combines the PDF's text and page transformations to find the displayed text positions.

The `normalize_*()` functions check contact formatting; they do not verify that a person or address exists. A missing answer stays missing: the extractor never substitutes a neighboring answer or a value from another page. Up to two separately positioned address lines are supported; ambiguous fragments need review. More representative exports should be checked before unattended use.

Run the regression checks from the project folder:

```sh
python -m unittest discover -s tests -v
```

The checks use temporary files and invented contact details. They cover extraction, filename discovery, repeated downloads, changed copies, queue history, and sending behavior without making live API requests.

### Still needed for the desktop version

- A desktop file that launches the correct Python environment and coordinates extraction, review, and sending.
- An Action Builder lookup and matching rules for people already in the destination campaign.
- A clear review and recovery interface for corrected paperwork and uncertain sends.
- Progress and completion messages that remain visible for everyday users.

The intended everyday workflow remains: download the paperwork, double-click the desktop file, and read the result.
