# Code walkthrough and rollout review

Reviewed September 18, 2026; lookup behavior, interactive review, and response validation updated September 21. This guide explains every runtime module, the queue and API boundaries, the tested behavior, and the remaining rollout work. It is intended for a detailed code-reading session; it is not a statement that the program is ready for unattended use.

The original review and regression checks used invented data and mocked requests. A later implementation added `action_builder_lookup.py` and integrated its checks with sending and queue history. On September 21, the user reported HTTP 200 for unfiltered, email, and phone GET requests, but rejection of `family_name`, `given_name`, and `name` filters. That is user-reported live evidence, separate from the local tests; it does not establish POST behavior or complete duplicate detection. The lookup uses email and phone only. At the user's request, a check now returns `not_found` when both searches complete successfully without candidates. This keeps a managed record pending and permits creation after a fresh submit-time check. Existing or possible matches still require review; lookup errors never permit a POST.

For a first reading, use sections 1–3 for the workflow and release blockers, sections 6–10B while reading the Python files, and sections 13–19 for validation and acceptance decisions. The commands in this guide are examples for the operator; API commands are explicitly identified and were not run against your organization as part of the review. The [README](README.md) provides installation and everyday command instructions.

**Reading map:** start with the [workflow](#1-what-the-program-actually-does), [current assessment](#2-current-assessment), and [confirmed findings](#3-confirmed-findings-and-rollout-gaps). Then follow the [file responsibilities](#4-files-and-responsibilities) into the module walkthroughs. For the weekend review, use the [acceptance worksheet](#19-acceptance-matrix-and-decisions-before-deployment). The [commit instructions](#20-git-commit-information-for-the-current-changes) and [private-use licensing discussion](#21-private-use-license-ownership-and-practical-limits) are at the end.

**Reading the comments:** short comments explain important workflow decisions and less obvious rules. Function docstrings describe responsibilities. The repetitive Python/Program pairs have been removed at the owner's request; the cleanup changes explanations, not behavior. Use the function references below to navigate the shorter files. Comments do not resolve the findings in section 3.

## 1. What the program actually does

The command sequence is **extract → preview → lookup → submit**. The persistent queue holds contact JSON, while the original PDFs stay in Downloads:

1. You download completed paperwork into Downloads.
2. You run `extract_person.py`.
3. The finder lists matching files in the top level of Downloads and groups related filenames in memory.
4. The extractor reads each relevant PDF, validates the checklist layout and contact fields, and compares copies.
5. A successful extraction creates a contact JSON record in `composed_info/pending` and records its history.
6. You run `send_person.py` to preview the currently pending records.
7. Run `action_builder_lookup.py` to check pending records. It uses GET only and records decisions locally.
8. **Before any possible creation claim, the lookup performs independent email and phone GET searches in the configured campaign.** It compares names and addresses in returned people locally; it does not issue remote name searches. A failed/incomplete lookup stops the batch, leaving the affected record pending and unclaimed.
9. An existing or possible match saves `existing` or `needs_review` and moves the record to `review`. If both searches complete successfully without candidates, save `not_found` and leave the record pending.
10. Run `send_person.py --submit` to recheck each remaining pending record. A fresh `not_found` allows the send claim and POST; matches are held and errors stop the run. A successful response must include a person with a valid native Action Builder ID before the record is marked sent.
11. Run `review_person.py` for the held contact records. Work through each person, inspect the source and remote candidates, then keep, edit, discard, or explicitly approve one creation. This is an interactive branch of the workflow, not an automatic batch release.

The standalone lookup command performs GET checks only and updates the local queue. **Pending alone does not prove a completed check:** newly imported records, failed checks, and later records after a batch error can also be pending. Every submission performs a fresh lookup; saved receipts cannot bypass it. The needs-review folder remains `composed_info/review`, with no rename to `need_review`. Existing API holds, including historical empty-candidate holds, remain blocked. Neither upgrading nor PDF `--resolve` releases them. `review_person.py` now provides explicit per-record editing, keeping, discarding, and approved submission without globally releasing related holds.

There is **no separate persistent queue of PDFs waiting for extraction**. The PDFs stay in Downloads. The persistent queue stores the extracted contact information. The initial file groups exist only while extraction runs.

```mermaid
flowchart TD
    A[Downloaded PDFs] --> B[Finder: filter filenames and group copies]
    B --> C[Extractor: verify layout and read contact fields]
    C --> D{Copies valid and consistent?}
    D -->|Yes| E[Validated JSON in pending]
    D -->|No| F[Hold group in history; retain source PDFs]
    F --> G[Previously queued unsent JSON moves to review]
    E --> H[Sender preview]
    H --> I[GET-check command or sender with --submit]
    E --> I
    I --> N[Email and phone searches in configured campaign]
    N -->|Existing or possible match| O[Save evidence; hold JSON in review]
    N -->|Both searches complete and empty| Q[Save not_found]
    Q --> S{Run mode}
    S -->|Check only| T[Leave pending for later fresh check]
    S -->|Submit| U[Claim send; POST person]
    U -->|Accepted response| V[Record sent]
    U -->|Failed or uncertain attempt| W[Hold in review; stop batch]
    N -->|Failed or incomplete lookup| P[Stop; record stays pending; no POST]
    O --> R[Interactive review_person.py]
    G --> R
    W --> R
    R -->|Edit or keep| O
    R -->|Confirm discard| X[Delete contact file; retain history]
    R -->|Check and approve creation| U
```

The arrow through preview is optional in the actual code. Normal pending submission does not require a saved human preview approval; interactive review decisions are recorded separately. A user can run `--submit` without previewing first. Functions named `extract_approved_person` and `load_approved_person` use “approved” to mean accepted by program rules, not signed off by a person.

The finder does not watch Downloads continuously. Extraction and sending are separate commands. The API lookup compares stored contact data against Action Builder; **it does not check Downloads for newer paperwork or establish which PDF is correct**. New or corrected PDFs still require extraction and review before submission. F2–F5 describe local issues that this remote check does not repair.

## 2. Current assessment

The project has a useful separation of responsibilities and meaningful regression tests. It is suitable for studying the workflow and testing extraction and previews with controlled examples.

**I would resolve the confirmed findings below before unattended or multi-computer rollout.** Passing the current tests establishes that the tested cases work; it does not establish complete duplicate prevention, correct handling of every exported PDF, or successful integration with your organization’s live campaign.

The current policy holds email/phone candidates and allows submission when both supported searches complete with no candidates. This is the requested operating rule, not proof of unique identity: a person with both contact details changed can be missed. Submission repeats the lookup instead of relying on the pending folder or an older receipt. Filename aliases, late corrections, and exceptional PDF failures remain separate issues; destination formatting is validated as described in F7. F11 records a historical surname-filter defect whose candidate-discard branch has now been removed. An interactive decision workflow now handles existing contact JSONs; broader remote reconciliation and source failures without JSON remain separate work.

| Area | Implemented now | Evidence or remaining limitation |
|---|---|---|
| Download discovery and template extraction | Yes | Synthetic PDFs and file-race cases tested; representative real exports still need field-by-field comparison |
| Managed pending/sent/review queue | Yes | Integrity, migration, and attempt-history tests pass; alias and recovery findings remain |
| Offline preview | Yes | Tests prohibit HTTP; opening the queue can still perform local recovery/migration |
| Existing-person GET checks | Yes, email/phone only | User reports those GETs succeed and tested name filters fail; mocked tests check local handling, not complete identity coverage |
| Automatic creation through normal CLI | Yes, after a fresh `not_found` | Both searches must complete empty; candidates and errors block automatic POST. Successful JSON must identify a returned person |
| Operator review of held records | Yes, interactive | Edit, keep, discard, or approve one submission after checking remote state; no remote update or automated reconciliation |
| Automatic detection of newer PDFs during sending | No | Run extraction separately; a GET search cannot inspect Downloads |
| Desktop double-click application, installer, or bundled Python | No | Current interface is command-line Python; deployment remains separate work |
| Organization-wide or multi-computer duplicate guarantee | No | Campaign searches and a local queue lock cannot supply that guarantee |

## 3. Confirmed findings and rollout gaps

The terms below describe impact in this project:

- **Before rollout:** could send stale or duplicate contact information, misdirect a request, or incorrectly mark work complete.
- **Operational issue:** can block legitimate work or requires a recovery procedure.
- **Design decision:** behavior is intentional or understandable, but you should explicitly accept it.

### F1. Existing-person checking is implemented; scope and review remain limited

**Status:** GET checks are implemented; the September 21 filter evidence limits them to email/phone. The requested policy allows creation after both searches complete without candidates. Matches require review and incomplete checks block sending.

**Read:** `action_builder_lookup.py`, `send_person.check_queue()`, `send_person.send_queue()`, and `RecordQueue.record_lookup()`.

The original sender posted without a remote search. It now searches the configured campaign by email and phone, combines candidates by Action Builder ID, and compares the nine contact fields, including returned names and addresses. One Person entity matching every comparison field is `existing`; other possible matches are `needs_review`. If both completed searches are empty, the outcome is `not_found`, with an empty candidate list; check-only mode leaves the record pending. A lookup result does not itself update or create a person. Submit mode sends only after its own fresh `not_found` result. A failed search raises an error and leaves the affected record pending instead of recording a completed decision.

Every normal submission performs a fresh lookup before the send claim. The standalone lookup CLI saves evidence for operator review but does not grant durable approval. The local `person-<hash>.json` filename remains a content fingerprint, not a remote ID; candidate Action Builder IDs are saved separately in the lookup receipt.

Searches cover the configured campaign, not the entire organization. If both email and phone have changed, the client can return `not_found` for an existing person. This accepted lookup policy cannot guarantee duplicate prevention. Two independent computers could also check before either creates the person. One designated sender remains the initial operating model to validate; a local queue lock is not a cross-computer identity guarantee.

API-related holds persist across re-imports, PDF `--resolve`, and connected filename aliases. Historical `needs_review` receipts with empty candidate lists are still valid holds and are not automatically requeued by this change. Use `review_person.py` to make an explicit decision after checking the remote record and source information. It can submit one approved record directly; it does not move an entire connected family back to pending. Sections 10A–10B explain lookup decisions and manual review.

### F2. Two filename aliases can eventually send two versions of one previously shared contact

**Type:** confirmed duplicate-prevention gap; before rollout.

**Read:** `RecordQueue._eligible()` at `record_queue.py:517`, `enqueue()` at `record_queue.py:595`, and `begin_send()` at `record_queue.py:839`.

Reproduction using invented records and no HTTP:

1. Queue the same contact data under filename families A and B. They correctly share one record, R1.
2. A receives changed contact data, R2.
3. Explicitly resolve A to R2.
4. Explicitly resolve B to the old R1.
5. Both R1 and R2 can now be pending.
6. Calling the queue’s claim and completion methods for R2 and then R1 succeeds for both.

This matters when A and B are two filenames for the same person’s paperwork. Equality of nine fields is not absolute proof of a person’s identity, but the queue previously treated these two inputs as one shared contact record. Its protection against sending different historical versions is not consistently preserved when the filename groups split.

The underlying issue is that eligibility considers the record’s **current owners**, while the family histories remember earlier shared records differently. API-related holds follow historical alias connections, but do not repair the original alias/attempt model. A remote lookup may catch a later version when contact values overlap an existing person, but it does not repair this queue defect. If both versions pass their email/phone checks, the normal sender can still submit both. The underlying queue defect needs an explicit alias policy and regressions before rollout.

### F3. Shared records can get stuck in review

**Type:** confirmed availability bug; operational issue.

**Read:** `RecordQueue._eligible()` at `record_queue.py:517`, `_revoke()` at `record_queue.py:540`, and `enqueue()` at `record_queue.py:595`.

Reproduction:

1. A and B share the same contact record.
2. Hold A for review.
3. Process an identical record for B under a new source fingerprint.
4. B is also held because the shared record has another held owner.
5. Repeatedly resolving A or B still returns `review`.

Each group sees the other group's hold and reestablishes its own. The existing single-group PDF resolution sequence does not resolve this reproduced state. The new interactive command can explicitly act on one held JSON, but it does not repair the underlying automatic alias-resolution model. Do not edit the history manually to bypass the hold.

### F4. The final automatic rescan can see a correction without holding the old pending record

**Type:** confirmed stale-data gap; before rollout.

**Read:** `extract_person.py:485`, particularly the final comparison beginning near line 507.

Reproduction with synthetic PDFs:

1. Morgan’s original PDF has already produced a pending record.
2. That original PDF is no longer in Downloads when the next scan starts.
3. The scan starts processing another person’s PDF.
4. A corrected Morgan PDF arrives during that extraction.
5. The final discovery sees Morgan’s corrected file as ready.
6. The old Morgan record nevertheless remains pending, with no review or deferral reported for it.

The final comparison iterates over the initially ready groups, rather than all relevant groups in the union of the initial and final snapshots. A newly observed ready family with existing queue history can therefore be missed. The explicit single-file path has additional rechecking and is not identical to this automatic path.

### F5. Some PDF exceptions leave an older version pending

**Type:** confirmed stale-data gap; before rollout.

**Read:** `extract_person.py:191`, `extract_person.py:412`, particularly the exception handling near line 459.

A synthetic corrected PDF with `/UserUnit` set to a nonnumeric value raises a plain `ValueError` during layout validation. `_import_group()` handles the project’s `ExtractionError` and pypdf’s `PdfReadError`, but this plain `ValueError` does not pass through the branch that holds the filename family.

If the family already had a pending record, that older record remains pending. The CLI reports failure, but a later sender command can still select that stale JSON for preview/lookup. A successful no-match lookup can then allow that stale record to be submitted. Remote checks do not repair the extraction error boundary.

The extractor needs a consistent boundary around parser failures so a failed correction cannot leave its predecessor eligible by accident. The solution should preserve useful error information without printing personal document contents.

### F6. Unexpected successful JSON is rejected before completion

**Status:** the earlier completion-validation defect is addressed in the shared POST helper used by normal and reviewed submissions.

Previously, mocked HTTP 200 responses containing `{}`, an error object, or `person: null` could be marked sent with an empty receipt. `submit_to_actionbuilder()` now requires a nested person with an identifier list containing exactly one valid native `action_builder:` UUID. Empty, malformed, ambiguous, and error responses raise instead. A claimed queue attempt then remains uncertain and is not automatically retried.

The expected wrapper and identifier format follow the [official person signup helper response](https://www.actionbuilder.org/docs/v1/person_signup_helper.html). Local tests exercise this boundary with mocked responses; no live creation was used to verify it. A valid receipt does not establish that all remote fields match the intended person or that the response can never be misleading.

### F7. Destination formatting is now validated

**Status:** the originally reproduced hostname-construction defect is addressed by shared configuration validation; live destination ownership and credentials remain unverified.

**Read:** `ActionBuilderConfig.__post_init__()`, `ActionBuilderConfig.people_url`, and `submit_to_actionbuilder()`.

The original preflight accepted any nonempty subdomain. A mocked request with `outside.invalid/` therefore used hostname `outside.invalid` while carrying the API-token header. No traffic was sent there. This historical result showed why rejecting redirects alone did not validate the initial destination.

Both GET and POST now use one checked configuration object. The subdomain must be a single DNS label, the campaign must be one identifier without URL punctuation, and supplied example placeholders are rejected. The URL is constructed under `.actionbuilder.org`. Invalid settings fail before lookup or a send claim. This checks format; it does not verify that the chosen valid-looking organization/campaign is the intended one or that a token has the correct access.

### F8. A queue is not bound to its Action Builder destination

**Type:** design gap; before multi-campaign use.

**Read:** `ActionBuilderConfig.destination` at `action_builder_lookup.py:104`, `send_queue()` at `send_person.py:320`, `RecordQueue.record_lookup()` at `record_queue.py:848`, and `finish_send()` at `record_queue.py:863`.

Each new lookup receipt records its subdomain and campaign, but the queue as a whole is not bound to that destination. Changing settings leaves previously sent records suppressed and API-held records blocked by the same history, while remaining pending records are checked against the new destination. Lookup evidence records where a check happened; it does not implement a queue destination-migration policy.

For rollout, decide whether a queue belongs permanently to one destination. A destination mismatch should be visible and handled explicitly. Preview currently returns before loading credentials, so it does not show or verify the actual destination.

### F9. Recovery requires an operator decision

**Status:** interactive record review is implemented; broader recovery remains incomplete.

A crashed send, timeout, rejected request, bad receipt, or local completion failure remains blocked from automatic retry. `review_person.py` shows held and uncertain contact records and their related history. After checking Action Builder, an operator can keep or discard one, edit local contact values, or explicitly approve a new attempt. Uncertain attempts require confirming that no person was created. A related recorded successful send blocks another creation. The command does not update a remote person or attach an uncertain local attempt to an existing remote ID.

An interrupted process can still leave `.queue.lock`; the next run refuses to continue until the stopped process is verified. An interrupted first record write can leave an unknown JSON file that deliberately blocks opening the queue. The interactive command cannot repair corrupt history or extract missing source data.

Discard decisions are saved before their JSON is removed. Reopening can finish an interrupted deletion. The history remains so the same record cannot be silently re-imported. Deleting the manifest or moving JSON by hand is not a recovery method.

### F10. The standalone path has a different safety contract

**Type:** intentional compatibility path; decide whether to expose it to everyday users.

**Read:** `load_approved_person()` at `send_person.py:47`, `build_actionbuilder_payload()` at `send_person.py:74`, and `main()` at `send_person.py:436`.

An ordinary JSON file outside a managed queue is allowed through the older standalone workflow. In the original review, sending that file twice made two mocked POSTs and created no queue history. The new standalone submission path requires a fresh lookup before each POST; it still has no durable send-attempt history or recovery claim. Managed generated filenames have extra protections, but copying and renaming one into an ordinary standalone file crosses that boundary.

The standalone loader accepts duplicate JSON keys, keeping the last value. Its builder also accepts values rejected by PDF extraction: a one-digit phone, malformed email, unknown state, and invalid ZIP. Python’s `isdigit()` even accepts certain non-ASCII numeric characters such as `²`. Wrong-type optional email and middle-name fields are silently omitted.

The lookup gate additionally requires usable names, email, a US phone, and an address. Standalone `--submit` posts only after both email/phone searches complete without candidates; matches and errors stop it. It does not create managed queue history. These checks reject some inputs that the older payload builder alone accepted, but do not fully replace extraction's state/ZIP and PDF checks. Decide whether standalone submission should remain an expert-only operation, use the same validator, or be retired from the eventual desktop flow.

### F11. Historical surname-and-street candidate exclusion; surname search removed

**Status:** the false-negative behavior was reproduced in the September 18 implementation. Its surname-search/candidate-exclusion branch was removed on September 21 because the API rejected the tested name filters. This is not a replacement implementation of name matching.

**Read:** `_comparison()` and `ActionBuilderLookup.check()` in `action_builder_lookup.py`.

The earlier fallback searched by surname and kept a result if its first name or street also matched. `_comparison()` selected just the remote address with the largest total number of matching components. An invented candidate with a different first name could therefore be discarded when one address matched city/state/ZIP but another matched the street: the selected address hid the qualifying street match. Mocked email/phone searches were empty, so that older check returned `not_found`.

The current check makes no surname query and has no such candidate-discard rule. A person returned by email or phone remains a candidate even when names or addresses differ. Two empty searches now return `not_found` under the requested email/phone policy. A person discoverable only through a name search can therefore be missed; removal of the old candidate-discard defect does not restore name-based identity coverage.

`_comparison()` still chooses one best-scoring complete address for the field-difference summary; equal scores keep the first. It does not merge components from separate homes. Review that behavior when interpreting multiple-address differences. If name/address candidate discovery is introduced later through a supported API capability, test candidate inclusion separately from address-summary selection so this earlier defect is not reintroduced.

## 4. Files and responsibilities

| File or folder | Responsibility | Does it call the API? |
|---|---|---|
| `pdf_downloads_finder.py` | Recognize filenames, group copies, check file stability, compute PDF fingerprints | No |
| `extract_person.py` | Recognize the checklist, extract and normalize nine fields, coordinate import and correction decisions | No |
| `record_queue.py` | Store contact records, maintain history, lock the queue, move records between status folders | No |
| `action_builder_lookup.py` | Validate destination, search and compare candidates, provide a check-only CLI | GET only; CLI also saves local decisions |
| `send_person.py` | Convert contact data to a request, preview offline, coordinate fresh GET checks and submission | Preview makes no requests; submit makes GETs and posts only after fresh no-match results |
| `review_person.py` | Show held contacts individually, validate local corrections, keep/discard, or explicitly approve one creation | Inspect/edit/keep/discard make no requests; choosing send makes fresh GETs and a confirmed POST |
| `composed_info/pending` | JSON for currently eligible pending records | No |
| `composed_info/sent` | JSON for records whose send was recorded as successful | No |
| `composed_info/review` | JSON for held, claimed, or uncertain records | No |
| `composed_info/.queue-state.json` | Versioned history used to determine eligibility | No |
| `composed_info/.queue.lock` | Temporary marker preventing cooperating runs from opening the same queue concurrently | No |
| `tests/` | Synthetic PDF, discovery, queue, migration, lookup, sender, manual-review, and response-receipt regression checks | HTTP is mocked/prohibited |
| `requirements.txt` | Pinned third-party distributions to install | Installation may download packages; it is not the person-submission workflow |
| `.env.example` | Example names/placeholders for the three connection settings | No |
| `.gitignore` | Keeps default contact-data locations, credentials, and Python cache files out of ordinary new Git tracking | No |
| `README.md` | Setup and everyday command reference | No |
| `license.md` | Private-use permission terms, copyright notice, and warranty/liability language; no runtime role | No |

`pending_person.json`, if left from earlier work, is not the current default queue. A failed standalone extraction may leave an older standalone output intact. Do not mistake that old file for the results of a failed new extraction.

## 5. Four different identities to keep separate

| Identity | Example | What equality means | What it does not establish |
|---|---|---|---|
| Filename family | `mo-example-organized` | Filenames match after stripping browser copy numbers and normalizing case/spaces | That two people with abbreviated similar filenames are the same person |
| PDF fingerprint | SHA-256 of all PDF bytes | The source files have identical bytes | That different bytes contain different contact answers |
| Contact record ID | SHA-256 of canonical nine-field JSON | The approved contact dictionaries contain exactly the same values | A stable real-world person ID or an Action Builder identifier |
| Action Builder person ID | `action_builder:<UUID>` returned by the API | Returned candidates refer to the same remote resource | That the local PDF is current, that the person belongs to every campaign, or that matching contact details alone prove identity |

Two PDFs with different metadata can have different PDF fingerprints but produce one shared contact record. A phone correction changes the contact record ID even if it is the same person. First-name capitalization, address wording, or `A` versus `A.` for a middle initial can also change that ID.

The hash is not encryption. It helps identify content and detect changes. The contact JSON itself remains readable text.

## 6. `pdf_downloads_finder.py` in detail

The finder knows about files, not contact fields. It cannot establish that a PDF has the expected checklist until the extractor opens it.

**Defaults and filters.** `DEFAULT_DOWNLOADS` is `Path.home() / 'Downloads'`, evaluated when the module is imported. It is not the script’s working directory. `DEFAULT_MARKER` is the literal `ORGANIZED`, based on your sample. A different organizer-name ending is selected with `--marker`.

Only the top level is scanned. Hidden entries, symlinks, unrelated names, and ordinary subdirectories are ignored. Matching is case-insensitive, even though your normal naming convention uses capitals. A hyphen, underscore, or whitespace separates the person prefix from the configured marker. `UNORGANIZED` does not accidentally match `ORGANIZED`.

| Item | Where to start | Purpose and important behavior |
|---|---|---|
| `Download` | line 27 | Immutable description of a checked source: path, normalized family, byte hash, modification time in nanoseconds, and browser copy number |
| `DownloadGroup` | line 36 | Immutable collection of related completed files; its family is a filename grouping, not verified person identity |
| `Discovery` | line 43 | Carries ready groups, deferred families, and readable error messages |
| `DownloadChangedError` | line 50 | Signals a source that should wait rather than be processed |
| `filename_family()` | line 54 | Removes trailing numbered suffixes repeatedly, such as `(1)` and `(1) (1)`, then collapses whitespace and case-folds the remaining stem |
| `matches_marker()` | line 66 | Checks a whole trailing literal marker; rejects a blank marker; escapes regular-expression characters in the supplied marker |
| `file_signature()` | line 76 | Returns size, modification time, device, and inode so replacement and modification can be detected |
| `fingerprint_pdf()` | line 81 | Checks a regular, nonempty, sufficiently old file; hashes it in chunks; compares file identity before/open/after reading |
| `discover_pdfs()` | line 110 | Lists entries, detects incomplete companions, fingerprints ready files, groups them, and returns one snapshot |

A completed PDF normally must be at least two seconds older than the current time. Future-dated files are also deferred. This interval is a practical stability check, not a proof that every browser has finished writing.

The finder recognizes `.crdownload`, `.part`, `.download`, and `.tmp` companions when the remaining name ends in `.pdf` and matches the marker. An incomplete related copy defers the whole family. It should not silently select an older original while a correction is downloading.

Files within a family are ordered by modification time, browser copy number, and filename. **That ordering does not choose the latest one as authoritative.** The extractor compares their content.

A default scan considers old matching downloads as well as newly downloaded ones. There is no “only today’s PDFs” filter. After history is lost or a fresh queue is used, matching historical paperwork can be considered again.

## 7. `extract_person.py`: reading and validating a PDF

This is a template-specific parser. It uses pypdf’s positioned text; it does not use OCR, an AI model, visual understanding, or a universal PDF form-field mapping.

**The template contract.** The supported checklist is US Letter with a page/crop box `(0, 0, 612, 792)`, no page rotation, and the expected user-unit scale. The heading must identify `New Member Checklist / LPX Data Entry`. Printed labels must still appear in calibrated rectangular areas.

PDF coordinates start at the bottom-left. A point is the measurement unit used by these rectangles. The visitor combines text placement and enclosing page transformations. The program tests text origins against rectangles; it does not measure a full rendered text bounding box.

| Data type or constant | Meaning |
|---|---|
| `Box` | A rectangle; `contains()` includes the lower bounds and excludes the upper bounds |
| `TextFragment` | A text callback’s content, x/y position, and whether its transform is supported as horizontal text |
| `FieldSpec` | A human-readable field label, answer rectangle, and maximum allowed answer lines |
| `HEADING_AREA`, `HEADING_TEXT` | Rules used to identify the checklist page |
| `LABEL_AREAS` | Expected positions of printed labels; these guard against known forms of template drift |
| `FIELDS` | The nine output keys and where their answers are expected |
| `STATE_NAMES`, `STATE_CODES` | Allowed US state/territory names and codes |
| `ZIP_PATTERN`, `EMAIL_PATTERN` | Basic syntax checks, not address/mailbox verification |

| Function | Line | What to inspect |
|---|---:|---|
| `clean_text()` | 137 | Decodes HTML entities, fixes the known escaped `\@` spelling, and collapses whitespace. It preserves meaningful punctuation. |
| `label_key()` | 146 | Removes punctuation/underscores and case differences for template-label comparison only. It is deliberately not a person-name normalizer. |
| `read_page_fragments()` | 152 | Its nested `visit()` computes transformed x/y positions, retains only relevant regions, and records whether rotation/skew is supported. |
| `fragments_in()` | 178 | Selects a rectangle and orders text by descending y, then ascending x. |
| `is_checklist()` | 186 | Compares normalized heading text to the expected heading. |
| `validate_layout()` | 191 | Checks page boxes, rotation, scale, label contents, and label orientation before accepting the template. See F5 for an exception-boundary gap. |
| `read_answer()` | 216 | Rejects missing or ambiguous answers, groups baselines within two points, and joins permitted address lines. |
| `digits_only()` | 254 | Removes everything except ASCII digits while keeping the result a string. |
| `normalize_us_phone()` | 259 | Accepts the supported punctuation and 10 digits, or 11 digits beginning with 1. Produces a country-code-prefixed string. Rejects extensions and alphabetic formats. |
| `normalize_state()` | 271 | Converts a recognized full state name or abbreviation into its postal abbreviation. |
| `normalize_zip()` | 280 | Accepts five digits or ZIP+4 with a hyphen; preserves leading zeroes. |
| `normalize_email()` | 288 | Cleans and lowercases the address, then checks a basic email pattern. |
| `parse_approved_person()` | 298 | Runs template checks, rejects answer-area overlap, reads all fields, applies name/city/initial checks and contact normalizers. |
| `extract_approved_person()` | 340 | Opens the PDF, rejects encrypted input, searches every page for the checklist, requires exactly one matching page, and parses it. |
| `save_approved_person()` | 365 | Supports standalone output: checks the key set, writes a temporary private JSON file, flushes it, then atomically replaces the target. |

`read_answer()` is deliberately strict. It allows one text fragment per baseline. Several fragments on the same line might mean split letters, overlapping text, or duplicate answers, so it rejects them instead of guessing. Most fields permit one line; the street address permits two separately positioned lines. Several nonblank lines arriving in one callback are rejected because they lack distinct positions.

That strictness prevents some incorrect extraction, but may reject legitimate PDFs produced with a different font or export layout. A label-position check also cannot prove that every answer belongs to the right field: it detects certain layout changes, not every possible exporter error.

The PDF library still reads the source document into its parsing machinery. The application filters which text fragments it retains and which fields it writes. “SSN is excluded from JSON” does not mean the underlying PDF parser never encounters the PDF’s SSN content in memory.

### The nine local fields

| JSON key | Checklist field | Current extraction rule |
|---|---|---|
| `given_name` | First Name | Required; must contain a letter and no digits; accents and punctuation can survive |
| `family_name` | Last Name | Same name checks; no splitting or guessing of surname components |
| `additional_name` | Middle Initial | Required; one alphabetic character, optionally followed by a period |
| `email` | Email | Required; whitespace cleaned, lowercased, simple pattern check |
| `phone` | Phone | Required; normalized to 11 digits beginning with 1 for supported US-format input |
| `address_line_1` | Address | Required; up to two positioned lines joined with a space |
| `locality` | City | Required; must contain a letter; numbers can also appear |
| `region` | State | Required; recognized US state/territory code or full name |
| `postal_code` | Zip | Required; five digits or hyphenated ZIP+4 |

The middle-initial validation strips a trailing period only in a temporary variable. The stored value retains it. Thus `A` and `A.` are both valid but different contact values for deduplication. Name capitalization and address abbreviations are also not standardized.

The current field requirements follow the supplied form’s required controls. You should still decide how real people without a middle initial or usable email are handled. That is a business-rule decision; the program currently stops for review.

Example using invented data:

```json
{
  "given_name": "Morgan",
  "family_name": "Example",
  "additional_name": "A",
  "email": "morgan@example.test",
  "phone": "12025550123",
  "address_line_1": "123 Example Street Apt 2",
  "locality": "Example City",
  "region": "MA",
  "postal_code": "01234"
}
```

The extractor does not emit SSN, birth date, ethnicity, gender, beneficiary details, classification, employer, organizing status, hours, organizer name, or document attachments. It also does not verify that the address exists, that the phone belongs to the applicant, or that the email receives mail.

A further normalization assumption deserves a real-export check: `clean_text()` applies HTML decoding to every answer. For example, the literal text `member&copy@example.test` becomes `member©@example.test`. Do not assume that transformation is harmless for every input merely because the helper’s docstring says it normalizes harmless spacing. Likewise, the current phone-length rule accepts `000-000-0000`, and the basic email expression accepts `member@example..test`; these validators check selected formatting rules, not contact truth or complete standards compliance.

## 8. `extract_person.py`: coordinating an import

| Item | Line | Role |
|---|---:|---|
| `ImportSummary` | 401 | Carries counts for queued, unchanged, review, and deferred filename families, plus message and error lists |
| `_import_group()` | 412 | Processes one family and decides whether to enqueue, hold, defer, or skip it |
| `_count_results()` | 479 | Adds final per-family outcomes to the summary |
| `import_downloads()` | 485 | Coordinates an automatic scan while holding the queue lock |
| `import_selected_pdf()` | 524 | Coordinates a specifically selected PDF, including a checked `--resolve` choice |
| `main()` | 605 | Parses options, chooses the mode, prints results, and returns a process exit code |

`ImportSummary` uses `field(default_factory=list)` so each scan gets its own message lists. Otherwise a shared mutable default could mix messages from different runs.

**Inside `_import_group()`:**

1. Collect the source byte hashes and filenames.
2. If all hashes are already remembered and this is not an explicit resolution, report the existing status without re-parsing.
3. Otherwise parse each distinct PDF byte sequence once.
4. Serialize each extracted contact dictionary consistently so equal field values can be compared.
5. Re-fingerprint every known sibling, including byte-identical copies that were not parsed separately.
6. A changed/unreadable download defers the family. A recognized extraction failure holds it for review.
7. More than one distinct contact dictionary causes a conflict hold.
8. Exactly one accepted dictionary is passed to `RecordQueue.enqueue()`.

The known-source shortcut avoids repeated parsing, **not all file reads**: the finder still hashes files on every scan. It also means unchanged PDFs are not automatically re-extracted after you change parser rules. A parser update needs a deliberate reprocessing/version policy.

**Automatic mode:** `import_downloads()` opens the queue first, marks incomplete families deferred, processes ready groups, and takes a second directory snapshot before releasing the lock. This prevents many cases of an older version remaining ready while a known correction is downloading. F4 and F5 explain gaps that remain.

**Explicit mode:** `import_selected_pdf()` also takes the lock before reading. It derives the family from the filename even if the chosen file cannot be read, allowing an earlier record to be paused. The nested `selected_group()` includes the chosen file and same-family siblings from the configured Downloads folder. The nested `snapshot()` compares resolved paths and byte hashes before and after processing.

An explicit PDF may live outside Downloads. Related copies in some other unconfigured folder are not automatically discovered. The configured Downloads folder still needs to be accessible for the sibling check.

**Resolution mode:** `--resolve` chooses the named version for an unsent family and remembers the hashes of the siblings present during that choice. Those existing siblings will not reopen the same conflict on the next normal scan. A new changed copy can trigger review again. This is a deliberate selection, not a “choose the biggest copy number” rule.

**Standalone output:** `--output` requires one explicit PDF and cannot be combined with `--resolve`. It writes a separate JSON rather than using discovery or queue history. The output must have a `.json` suffix and cannot resolve to the source PDF. Its parent directory must already exist. A failed extraction leaves an existing output unchanged; success replaces the old complete file after the new one is fully written.

Automatic/explicit extraction returns 0 for a completed scan without review, deferral, or errors. It returns 1 when those outcomes are present or a handled error stops the operation. Invalid command-line combinations use argparse’s error exit, normally 2. A future launcher must check the exit code rather than simply running the sender after every extraction attempt.

A scan is not one all-or-nothing transaction. Earlier successful families can already be saved if a later family fails. An unrelated valid record may remain pending while another group needs review. This is intentional batch progress, but the operator needs to understand what the summary counts mean.

## 9. `record_queue.py`: the history and state machine

The queue module is the persistence layer. It does not open PDFs or call Action Builder. Both extraction and sending use it so they agree about what is eligible.

```text
composed_info/
    pending/             status: pending
    sent/                status: sent
    review/              statuses: review, sending, uncertain
    .queue-state.json    authoritative versioned history
    .queue.lock          present while a run owns the queue
```

The three folders are views of the recorded status. The folder name is not a command. Moving a sent file into `pending` does not reset its history; opening the queue moves the intact tracked file back to the recorded location. Duplicating it into two folders instead stops the queue for review.

### Two related levels of state

The manifest is a JSON object containing exactly `version`, `families`, and `records`. The current format version is 3. Opening valid version-1 or version-2 history migrates it without turning held records into pending records.

| Part | Fields | Purpose |
|---|---|---|
| `families` entry | `current_id`, `record_ids`, `source_hashes`, `source_names`, `review`, `deferred`, `reason` | Tracks a normalized filename group, its selected record, previous record IDs, source fingerprints, and holds |
| `records` entry | `status`, `created_at`, `updated_at`, optionally `reason`, `result`, `lookup`, `decisions`, and `replacement_id` | Tracks one contact hash, its lookup/attempt, and manual decisions; discarded entries remain after the contact file is removed |
| `lookup` receipt | `outcome`, `reason`, `checked_at`, `destination`, `candidates` | Saves the latest completed check, candidate IDs, and matching/differing field names without duplicating remote contact values |
| `result` receipt | Selected returned identifiers and browser URL | Records a small receipt instead of the entire API response |
| Manual `decisions` entry | `action`, `at`, `reason`, `previous_status`; edits also have `related_id`, checks/approvals have `destination` | Records `edited`, `discarded`, `checked`, or `approved_send`; reasons are nonblank and limited to 500 characters |

The contact fields live in the separate `person-<digest>.json` files. A `discarded` record is a retained history entry, sometimes called a tombstone: its contact JSON is removed, but its hash and decision prevent the same contact from being silently re-imported. There is no `discarded` folder. The manifest includes source filenames and review reasons, so it is still potentially identifying information even without the full contact payload.

`deferred` is a **family flag**, not one of the record status strings. Deferring a previously pending family changes its pending record to `review`. If the related download later finishes with unchanged, acceptable contact details, that record can become pending again. A prior actual review hold is not automatically erased by deferral.

A later correction can make a family need review while an older successfully submitted record remains in `sent`. The old receipt should not disappear just because new paperwork needs attention.

Printed status counts count filename families. The list of pending JSON files counts unique records. Two filename families sharing identical contact data can therefore produce two ready groups but one pending JSON file. That difference is not automatically an error.

### Main transitions

The normal send-attempt rows apply when submit mode receives a fresh `not_found` decision. Check-only mode saves the decision and leaves the record pending without making a send attempt. The separate manual-review rows require an explicit operator decision; a fresh lookup alone cannot clear an existing hold.

| Event | Previous state | Recorded outcome | File location |
|---|---|---|---|
| New valid contact | No record | `pending` | `pending` |
| Same tracked contact again | `pending` | Still `pending` | `pending` |
| Conflicting or invalid related paperwork | `pending` | `review`, family held | `review` |
| Related incomplete download | `pending` | `review`, family deferred | `review` |
| Deferred download finishes consistently | Deferred unsent record | Can return to `pending` | `pending` |
| Explicitly resolved unsent PDF correction | Held unsent family without an API-related hold | Chosen record can become `pending` | `pending` |
| Complete email/phone check without a candidate | `pending` | Still `pending`, with `lookup.outcome=not_found`; submit mode may then claim and POST | `pending` until a send claim |
| Historical empty-candidate API hold after upgrade | `review` | Still held; the new policy does not automatically release it | `review` |
| Existing or possible remote match | `pending` | `review`, lookup evidence saved and connected families held | `review` |
| Incomplete/failed API lookup | `pending` | No new completed lookup result or send claim; batch stops | `pending` |
| Sender claims an eligible record | `pending` | `sending` persisted before POST | `review` |
| Response accepted and completion saved | `sending` | `sent` | `sent` |
| Attempt cannot be reliably completed | `sending` or completion in progress | `uncertain` | `review` |
| Queue reopens with interrupted `sending` | `sending` | `uncertain` | `review` |
| Later changed version after an attempt | Attempted history exists | Review required, no normal automatic new send | Depends on each historical record's status |
| Manual correction saved | `review` or `uncertain` | Old hash becomes `discarded`; new hash is `review`, with a replacement link | Old contact JSON removed; corrected JSON in `review` |
| Manual discard confirmed | `review` or `uncertain` | `discarded` decision saved; related versions remain held | Contact JSON removed; history retained |
| Same discarded contact is imported again | `discarded`, including a new filename alias | Remains discarded; no automatic restoration | No contact JSON restored |
| Fresh GET during interactive review | `review` or `uncertain` | Lookup and `checked` decision saved; hold remains | `review` |
| Explicit manual creation approval | `review` or `uncertain`, freshly checked in this session and destination, with no related sent receipt | One record becomes `sending`; connected alternatives remain held | `review` until completion |

The normal protection for a later changed version after an attempt has the alias gap described in F2. The interactive command's connected-history checks do not repair that separate automatic eligibility defect. Do not generalize tested behavior into a guarantee covering every relationship between filenames.

### Why status is saved before a file moves

An operation can stop between any two filesystem actions. `_commit()` therefore validates the in-memory state and existing record files, writes and flushes the manifest, then reconciles file locations to that saved state. Discarding follows the same ordering: save the disposition first, then unlink the validated contact file. If a commit fails, the in-memory queue is invalidated until it is reopened; later operations cannot accidentally apply an unsaved deletion decision.

If a process stops after `sending` is saved but before its JSON moves out of `pending`, the folder may temporarily look misleading. Reopening uses the manifest, treats the interrupted attempt as uncertain, and relocates the record. It does not use “the file is still in pending” as permission to send.

There is deliberately a small interval where a process can stop after claiming but before making a POST. On restart, the program cannot prove whether the network call happened, so it holds the record. Preventing an accidental second attempt takes priority over automatically retrying this case.

There is no atomic transaction spanning your computer and the remote API. If the server accepts a person and the response or local save fails, the queue needs reconciliation. The durable claim reduces accidental retries; it does not establish exactly-once delivery across both systems.

### Queue function reference

These locations refer to the current source fingerprinted in section 17. Function names remain the most reliable navigation aid after later edits.

| Item | Current line | Responsibility |
|---|---:|---|
| `QueueError` | 38 | Controlled persistence/history error requiring attention |
| `QueueItem` | 43 | Immutable handle carrying a record ID, its currently issued path, and a family name |
| `_now()` | 51 | Generates UTC timestamps for history |
| `_has_queue_data()` | 55 | Detects meaningful old-queue contents while ignoring empty placeholder folders/files and Finder metadata |
| `_record_bytes()` | 73 | Requires exactly nine nonblank string fields and produces deterministic JSON bytes for hashing |
| `_load_json()` | 81 | Rejects missing/symlinked files and duplicate JSON keys; loads readable JSON or raises a controlled error |
| `RecordQueue.__init__()` | 102 | Stores the queue root and initializes unopened state |
| `__enter__()` | 109 | Validates the root choice, guards against abandoning old data, acquires the lock, loads/validates history, upgrades/reconciles files, and recovers interrupted sends |
| `__exit__()` | 164 | Clears in-memory state and removes only the lock with the identity this run created |
| `_require_open()` | 177 | Ensures queue methods run inside the context manager |
| `_filename()` | 184 | Requires a 64-character lowercase hexadecimal ID and computes the generated filename |
| `_possible_paths()` | 190 | Lists the controlled flat and three status-folder paths usable during migration/recovery |
| `_path()` | 196 | Computes the destination from recorded status, never from a stored arbitrary path |
| `_sync_directory()` | 208 | Flushes directory entries on POSIX systems |
| `_ensure_folders()` | 217 | Creates the three status folders and rejects replacement symlinks/non-directories |
| `_write_json()` | 225 | Writes a temporary JSON, flushes it, atomically replaces the target, and flushes its directory |
| `_commit()` | 242 | Validates, saves authoritative history, then moves files to match it |
| `_validate()` | 254 | Checks manifest shape/types/references, record eligibility, and file integrity; can reconcile paths |
| `_scan_records()` | 301 | Requires one intact copy per active or sent record; allows discarded files to be absent; rejects unknown files, duplicates, links, unsupported entries, and unexpected nested folders |
| `_move_record()` | 346 | Moves a checked file without intentionally replacing an existing destination and flushes both directories |
| `_reconcile_records()` | 354 | Finishes moves, reverses manual misplacement, and removes validated files whose discarded status was saved |
| `_source_details()` | 377 | Validates family/source metadata and permits source filenames rather than arbitrary stored paths |
| `_family()` | 385 | Creates/updates a family and accumulates its source names and fingerprints |
| `_attempted()` | 396 | Checks whether any record in a family’s history is sending, sent, or uncertain |
| `_validate_lookup()` | 446 | Validates the shape and consistency of the saved lookup receipt; it is not another API search. |
| `_lookup_held()` | 504 | Follows shared historical record IDs across filename families so API-related holds survive aliases and later PDF resolution. |
| `_eligible()` | 517 | Checks current owners and their PDF/API/manual holds and attempt status; important to F2/F3 |
| `_hold_lookup_families()` | 533 | Revokes pending versions and applies the API review hold to every connected filename family. |
| `_revoke()` | 540 | Changes a family’s pending records to review |
| `known_sources()` | 546 | Tests whether all present hashes are already remembered and the family is not deferred |
| `status_for()` | 551 | Reports a selected sent/discarded disposition first, then review, deferral, or the selected record's other status |
| `status_counts()` | 567 | Counts statuses by filename family |
| `defer()` | 576 | Pauses a group and revokes pending eligibility while a source is not ready |
| `hold()` | 586 | Records a review reason and revokes pending eligibility |
| `enqueue()` | 595 | Calculates a contact ID, stages new JSON, updates family history, and decides pending/review/already-sent behavior |
| `pending_records()` | 664 | Validates the queue and returns a sorted list of tracked pending handles |
| `item_for_path()` | 817 | Allows an explicit managed filename only if its tracked record is pending; accommodates controlled old paths after migration |
| `_entry_for()` | 826 | Validates a handle’s ID, allowed path, and family association; the original path may be stale after a move |
| `begin_send()` | 839 | Rechecks eligibility and durably claims the record before the POST; GET checks now happen earlier |
| `record_lookup()` | 848 | Saves the destination and decision. `not_found` keeps records pending; `existing`/`needs_review` block connected records, including historical empty-candidate holds. |
| `finish_send()` | 863 | Requires a claimed record, saves the selected receipt, and marks it sent |
| `mark_uncertain()` | 879 | Holds an attempted record whose outcome cannot be relied upon |

The manual-review additions use function names here so they remain easy to locate as the queue evolves:

| Function | Responsibility |
|---|---|
| `_remove_discarded_file()` | Rechecks a controlled path and its contact hash before unlinking; never deletes the source PDF |
| `_validate_decisions()` | Checks action names, required metadata, prior statuses, edit replacement links, and destinations; prevents a retired record from silently becoming active |
| `_validate_destination()` | Checks the minimal saved destination shape; runtime configuration supplies the stricter API URL validation |
| `_related_ids()` | Traverses filename histories that share contact hashes to find connected versions |
| `_manual_held()` | Keeps a connected family out of automatic pending eligibility after a manual decision, including a refreshed review lookup |
| `_hold_related()` | Revokes pending status in connected families while preserving unrelated records |
| `review_records()` | Returns each `review` or `uncertain` contact once; excludes pending, sent, and discarded records |
| `review_details()` | Supplies the reason, source names, lookup, connected families, and evidence of a related successful or uncertain attempt |
| `_review_reason()` / `_require_review()` | Require a usable decision reason and a record that is still reviewable |
| `save_review_edit()` | Writes a validated nine-field replacement under its new hash, records the link, and retires the original; rejects collisions with any existing history |
| `discard_review()` | Saves a discarded decision and retains connected holds before removing the contact file |
| `record_review_lookup()` | Saves a completed lookup plus a `checked` decision without releasing the hold; enables one same-session claim |
| `begin_review_send()` | Requires that fresh review lookup for the same destination, rejects related successful sends, records approval, and claims exactly one record |

`not_found` is a lookup outcome, not a new record status or a way to release an existing API hold. The manifest now uses version 3, adding manual decisions and discarded-record history; versions 1 and 2 are migrated when opened. The validator still accepts `needs_review` with no candidates so earlier manual-review receipts remain readable and held. Fresh completed empty searches now return `not_found`; `existing` continues to require candidate evidence. Receipt validation does not repeat the API search or prove a human approved a person.

`_record_bytes()` is a schema and content-integrity check. It does not repeat the extractor’s email, ZIP, state, or telephone rules. It also does not encrypt or authenticate the data against a person who can deliberately edit both records and history.

New record JSON is initially written in `review` before the manifest references it. If the process stops between those steps, the next opening sees an unknown file and stops. It does not guess whether that file is ready to send.

`.queue.lock` protects cooperating processes opening the **same queue root**. It does not coordinate different roots, different computers, or someone editing files by hand. A second run fails promptly instead of waiting indefinitely. A stale lock requires an operator to establish that the previous run has stopped.

All retained contact files are validated repeatedly, including old sent records; discarded entries do not require a contact file. This strengthens local integrity checks but increases file reads as history grows. The code suggests repeated whole-queue work across a large batch; this review did not benchmark a large archive.

Deleting or corrupting a required sent or active JSON blocks opening the entire queue, including unrelated pending records. Only a saved discarded disposition permits its contact file to be missing. Back up the manifest together with the remaining tracked JSON. Moving old sent files into an unrecognized archive folder is not currently supported.

The history stores current state plus recorded manual decisions; it is not an immutable or complete chronological event log. It retains statuses, timestamps, current reasons, selected receipts, and historical record IDs. Restoring an older valid-looking history can roll back knowledge of later attempts; record hashes do not prove that restored sending history is up to date. A restore procedure needs remote reconciliation as well as intact files.

### Migration and folder handling

Opening valid version-1 or version-2 history upgrades it to version 3; flat version-1 records are moved into the appropriate subfolders. Older code cannot interpret the new discarded-record state, so do not reopen an upgraded queue with an older checkout. An interrupted move can leave a mixture of old and new paths; reopening reconciles that mixture using the saved statuses.

The new default refuses to silently abandon meaningful data in the old `senders_pdfs` location. A custom queue path continues to be supported. `--queue-dir` must identify the main root containing the history, not `pending`, `sent`, or `review`.

Empty `.gitkeep` files preserve the folder layout in a clone and are allowed. Finder’s `.DS_Store` and recognized private temporary-write files are also handled explicitly. This is not a general-purpose folder for arbitrary documents, notes, or manually added JSON.

## 10. `send_person.py` in detail

The sender reads contact JSON. It does not import the PDF finder or extractor to refresh the data. A newly downloaded correction will not affect eligibility until an extraction run notices it.

| Function | Current line | Responsibility and boundary |
|---|---:|---|
| `require_environment_variable()` | 37 | Retained annotated learning helper; the current network workflow uses `ActionBuilderConfig.from_environment()` instead |
| `load_approved_person()` | 47 | Reads a UTF-8 JSON object; stronger managed-queue checks happen separately |
| `require_string()` | 59 | Requires one nonblank string; does not validate its meaning |
| `build_actionbuilder_payload()` | 74 | Maps the flat local record to a nested request without file/network access |
| `submit_to_actionbuilder()` | 191 | Uses shared validated configuration, performs one POST, and requires a returned person with one valid native Action Builder UUID |
| `show_success()` | 249 | Prints a success message, the native `action_builder:` person ID, and a returned browser URL if present; does not establish success itself |
| `show_lookup_result()` | 265 | Separates saved/fresh decisions and numbered candidates; translates matching/differing field names into readable labels without printing remote contact values |
| `queue_for_input()` | 300 | Distinguishes managed queue paths from ordinary standalone JSON |
| `send_queue()` | 320 | Locks and validates the queue, prepares the batch, then previews, checks only, or checks and sends |
| `check_queue()` | 431 | Calls the queue workflow with `check_only=True`; no POST path is taken |
| `main()` | 436 | Parses arguments and selects managed or standalone workflow; standalone submission also performs a fresh lookup |

The GET stage is executed code, not only a comment. The low-level POST helper is not the operator entry point: normal CLI submissions pass through the lookup gate. `submit_to_actionbuilder()` itself does not perform a lookup, and `RecordQueue.begin_send()` does not independently require or age-check a lookup receipt. The ordering guarantee is implemented by `send_queue()`, not enforced by every lower-level function. A future launcher or service must call the coordinated workflow rather than calling the POST helper directly.

The matching code exists in `action_builder_lookup.py`; the new `review_person.py` handles explicit review decisions; a desktop launcher remains future work. Comments and names help explain the code, but executable paths and tests determine what actually happens.

### Payload mapping

This table describes the local builder, not a claim that the live campaign accepts every field in its current configuration.

| Local value | Outgoing field | Transformation or added assumption |
|---|---|---|
| `given_name` | `person.given_name` | Required string, stripped |
| `family_name` | `person.family_name` | Required string, stripped |
| `additional_name` | `person.additional_name` | Included only if it is a nonblank string |
| `email` | `person.email_addresses[0].address` | Stripped and lowercased; address type is `home` |
| `phone` | `person.phone_numbers[0].number` | Required string, checked with `isdigit()`; number type is `Mobile` |
| `address_line_1` | `person.postal_addresses[0].address_lines[0]` | One string in a list, including any joined second line |
| `locality` | `person.postal_addresses[0].locality` | Required string, stripped |
| `region` | `person.postal_addresses[0].region` | Required string, stripped and uppercased |
| `postal_code` | `person.postal_addresses[0].postal_code` | Required string, leading zero preserved |
| Fixed value | `person.action_builder:entity_type` | `Person` |
| Fixed values | Postal country and address type | `US` and `physical` |

The builder returns `{"person": person}`. The outer object is significant: it uses the signup-helper request shape.

Extraction requires all nine fields; the builder independently requires only seven. It treats middle name and email as optional. That difference matters to standalone input and direct function calls.

The source does not establish whether a phone is mobile, an email is personal, or an address is the preferred physical location. Those are present defaults to approve as business decisions. The sender does not set explicit contact-subscription or consent fields.

### What happens in queue mode

1. Open and lock the queue. This can migrate or reconcile local files.
2. Print family status counts and select pending records (or one eligible managed record).
3. Build **all** selected payloads before the first request. A payload failure leaves the batch unclaimed.
4. In ordinary preview mode, print payloads and return without networking or loading credentials.
5. For check-only or submit mode, load and validate the shared destination and create a paced lookup client.
6. Before each prepared record, recheck that it is still pending. An earlier result in the batch may have held a connected version.
7. Perform fresh GET searches and save a completed result with `record_lookup()`. A lookup failure stops the batch before this record is claimed; earlier completed work remains recorded.
8. For `existing` or `needs_review`, hold the record and connected versions in `review`, then continue to other eligible records. A candidate is enough to block creation even when some names or addresses differ.
9. For `not_found`, both email and phone searches completed successfully without candidates. Check-only mode saves this result and leaves the record pending without a send claim or POST.
10. In submit mode, a fresh `not_found` result permits pacing, a durable send claim, a POST, and recording completion. Old receipts cannot replace the fresh check, and being in `pending` alone is not permission to send.
11. A POST failure, unusable successful response, or failed completion write marks the attempt uncertain and stops the batch. The shared response check now rejects the malformed success bodies described in F6.

The lookup client spaces GET requests; the sender also leaves a 0.3-second gap before a POST and before a GET following a POST. This pacing is per run, not organization-wide coordination.

`begin_send()` precedes the **POST**, not the first network operation: the GET searches now happen before the claim. A GET failure leaves the current record pending without consuming a send attempt. A POST failure may mean the server wrote the person, so it follows the uncertain-attempt path instead.

The claim is outside the submission `try` block. If claiming itself fails, no POST is made. If its status was durably saved before a later move failed, reopening treats it as an interrupted attempt. The `except BaseException` block also preserves uncertainty for a keyboard interruption during an attempt. If history saving fails, the prior durable sending claim is the fallback protection.

There is no automatic POST retry loop. Submit batches can create unmatched people while holding other records for review. Errors stop the remaining work, and earlier remote creations are not rolled back.

### Network boundary and configuration

After a fresh no-match decision, the coordinated submit workflow calls the creation helper using this operation:

```text
POST https://{subdomain}.actionbuilder.org/api/rest/v1/campaigns/{campaign_id}/people
```

It includes the `OSDI-API-Token` header and asks for `application/hal+json`. Passing `json=payload` lets Requests serialize the object and supply its JSON content type. The call uses `timeout=(5, 30)` and `allow_redirects=False`. Redirects are explicitly rejected; HTTP errors raise; JSON must decode into a dictionary. F6 describes the new returned-person/identifier validation and its limits. F7 explains the new destination-format validation shared with GET checks.

A timeout pair is a connection limit and a read limit, not a guaranteed 35-second total run duration. Requests documents the distinction, JSON request handling, and HTTP error behavior. [Requests Quickstart](https://requests.readthedocs.io/en/latest/user/quickstart/)

`load_dotenv()` is a third-party helper that loads plain-text settings into the process environment. The installed function searches for `.env` using its calling context and parent directories, and defaults to `override=False`. An already-set environment variable can therefore take precedence over an edited `.env`. It does not encrypt settings or verify a token. [python-dotenv documentation](https://saurabh-kumar.com/python-dotenv/)

The three settings are:

| Setting | Meaning |
|---|---|
| `ACTION_BUILDER_API_KEY` | Credential placed in the API-token header |
| `ACTION_BUILDER_SUBDOMAIN` | The intended organization’s hostname label, not a whole URL |
| `ACTION_BUILDER_CAMPAIGN_ID` | The API campaign identifier supplied for the intended destination |

Preview does not load or validate these settings. It confirms the local payload, not access rights, campaign configuration, network connectivity, or where a later process will send.

## 10A. `action_builder_lookup.py`: deciding whether to create a person

This module is the new stage between submitting the pending list and claiming a POST. Its job is to ask Action Builder about candidates and produce an understandable decision. It does not decide which conflicting source is correct, update an existing remote record, or delete local files.

### Search and decision rules

For each contact, `check()` searches email and normalized US phone **separately**. It sends neither `family_name`, `given_name`, nor `name` filters: the user reported that these were rejected by the API on September 21. Returned candidates are combined using their native `action_builder:<UUID>` identifier, then their names and addresses are compared locally. Removing remote name queries does not remove those comparisons.

Both searches must complete successfully. A mismatch between a returned person and the requested filter, malformed fields/IDs, changed candidate comparisons during the search, or unreliable pagination stops the check. It does not silently reinterpret those conditions as an empty result.

| Result | Meaning | Local behavior | Remote behavior |
|---|---|---|---|
| `existing` | Exactly one candidate, a Person entity, with all nine contact comparisons matching | Save evidence and hold in `review` | No POST or update |
| `needs_review` with candidates | Details differ, entity type is uncertain/different, or more than one candidate remains | Save evidence and hold in `review` | No POST or update |
| `not_found` | Both email/phone searches completed successfully with no candidates | Save an empty candidate list; remain pending in check-only mode | Submit mode claims and POSTs after its own fresh check |
| Historical `needs_review` with no candidates | Earlier policy held empty searches for manual identity review | Remains readable and held; no automatic release or requeue | No POST or update |
| `LookupError` | Cannot complete or trust the check | Stop the batch; no send claim for this record | No POST for this record |

This table describes the normal pending/check/submit workflow. A held record can instead enter the explicit per-person review flow in section 10B, where fresh evidence plus the required human identity confirmations can authorize a creation. A lookup error never grants that permission.

“Existing” is the label for this matching rule, not proof of a unique real-world identity. Even a perfect field match is held for operator review. With zero email/phone candidates, a person could still exist under changed contact details. The requested policy accepts this limitation and permits creation after complete empty searches. `not_found` means not found by those two values in the configured campaign; an empty candidate list is not an API error when both searches succeeded.

Text comparisons ignore case and repeated whitespace. Middle initials ignore a trailing period. Phone comparisons handle ordinary US formatting and the leading country code. Address wording is not expanded: `Street` and `St` can produce a difference. One complete remote address is compared at a time; fields from different addresses are not combined to manufacture a match. These are explicit comparisons, not fuzzy name/address matching.

The address with the most equal fields supplies the four reported address comparisons; equal scores keep the first encountered address. Country is validated as a string if returned but is not part of those comparisons. Thus `existing` means the nine comparison fields and Person entity type agree, not that every remote field agrees. It is still held for review. Address selection no longer decides whether a candidate is kept: email/phone candidates remain in the result regardless of name/address differences. F11 explains the removed surname fallback and the remaining address-summary behavior.

### Function reference

| Component | Current line | What to notice while reading |
|---|---:|---|
| `LookupError` | 34 | Represents an incomplete check; deliberately different from `not_found` |
| `ActionBuilderConfig` / `__post_init__()` | 39 / 46 | Validates token presence/whitespace, subdomain and campaign formatting, and example placeholders; hides the token from `repr()` |
| `ActionBuilderConfig.from_environment()` | 78 | Loads environment settings only when explicitly called; ordinary preview does not call it |
| `people_url`, `headers`, `destination` | 93 / 100 / 104 | Build the one campaign endpoint and request headers; the history-facing destination omits the API key |
| `LookupResult` / `as_history()` | 110 / 117 | Package a decision and create a dated receipt with IDs and field names rather than remote personal values |
| `_text()` / `_phone()` | 127 / 132 | Compare text/US phone formatting without mutating pending JSON |
| `_identifier()` | 142 | Requires exactly one native `action_builder:` UUID; other identifiers do not become the candidate key |
| `_values()` / `_validate_person()` | 161 / 171 | Check collection and returned-field types before the client trusts comparisons |
| `_comparison()` | 193 | Compares the nine contact fields and selects one remote address; see F11 |
| `ActionBuilderLookup` / `__init__()` | 237 / 243 | Store configuration and per-client pacing; constants bound searches to 100 pages and space requests by 0.3 seconds |
| `ActionBuilderLookup._get_page()` | 247 | Make a paced GET with encoded parameters, `(5, 30)` timeouts, no redirects, and a response-shape check; errors omit personal query values |
| `ActionBuilderLookup._search()` | 287 | Escape apostrophes in equality filters and verify every numbered page before returning candidates |
| `ActionBuilderLookup.check()` | 356 | Search only email/phone, compare returned names/addresses, and return `existing`, `needs_review`, or `not_found` after both searches complete empty |
| `main()` | 445 | Accept `--queue-dir` and call `send_person.check_queue()` for the managed GET-only workflow |

The imports inside `main()` let the search client be imported by the sender without automatically running the queue command. Executing the lookup file directly enters its guarded CLI; importing it only provides definitions.

### Pagination, evidence, and limits

`_search()` validates the numeric page, total-page count, page size, collection structure, and returned identities. It rejects inconsistent or changing pagination and repeated IDs across pages. Searches are bounded at 100 pages; exceeding that bound is an error requiring attention, not a no-match result. It builds each subsequent request on the validated campaign endpoint rather than following an arbitrary server-provided next URL with credentials.

A successful receipt lives in `records[record_id]["lookup"]` in `.queue-state.json`. It contains `outcome`, `reason`, `checked_at`, `destination`, and `candidates`. Candidate summaries contain `identifiers`, `matching_fields`, and `differing_fields`. Neither API keys nor returned contact values are copied into this receipt, although IDs, source filenames, and the separate contact JSON still need appropriate handling.

A failed search does not replace a prior completed receipt with a false no-match result. That older receipt may remain visible, but the submit workflow always performs a fresh check. Completed empty searches save `not_found` with `candidates: []`. `_validate_lookup()` still accepts earlier empty-candidate `needs_review` receipts without releasing their holds. It still rejects an `existing` outcome without candidate evidence and contradictory `not_found` receipts with candidates.

The persistent API hold intentionally survives later PDF imports, explicit PDF resolution, and connected historical filename aliases, including when its candidate list is empty. The interactive `review_person.py` command now handles these records one at a time. A human can investigate saved IDs and differences when present, or manually search the intended campaign for a historical empty-candidate hold. Upgrading does not automatically requeue those earlier holds. Do not edit history or move files to force a send. The command records explicit manual decisions and never silently releases an entire connected group.

This feature can catch a stale pending version when matching data already exists remotely. It cannot detect a newer unprocessed PDF in Downloads, fix the extractor's late-correction cases, guarantee organization-wide uniqueness, or make a GET followed by POST atomic across computers. Continue rescan/preview discipline, resolve F2–F5, and validate the interactive review workflow before unattended rollout. Allowing unmatched contacts to send does not close these separate findings.

## 10B. `review_person.py`: working through held people

Run `.venv/bin/python review_person.py` from the project folder, or add `--queue-dir "/path/to/queue-root"` for another managed queue. The [README](README.md) gives the everyday command sequence. This command has no automatic `--submit` batch mode: each creation is a separate decision.

### What the operator sees and chooses

`run_review()` opens the queue and holds its lock throughout the interaction. It takes a sorted snapshot of unique held contacts, including uncertain attempts. Before displaying each next item, it checks whether that item is still reviewable, because an earlier decision may have replaced or retired it. A second cooperating process cannot extract or send through this same queue while the review is open.

For each person, the command displays spaced sections for all nine local contact values, the hold reason, source filenames, and saved Action Builder evidence. Names, email/phone, and the address are grouped separately, and field labels align the values. It also warns about related successful or uncertain attempts. Source names can include connected historical filenames, not only the PDF that produced the currently shown values.

`show_review()` calls the shared `show_lookup_result(..., saved=True)` renderer for the saved receipt, then displays its timestamp and destination. Fresh searches use the renderer's default fresh heading. Numbered candidate blocks show the native remote person ID, readable matching-field labels, and **Different or missing fields**: a failed comparison can mean different data or absent remote data. With no differences, the block says that all compared fields match. Candidate values and names are not stored in the receipt or fetched for this display; inspect the existing people in Action Builder using their IDs. A saved result describes the local details at the time of that check and does not release the review hold.

The batch lookup output likewise identifies the local person by name and labels the hash filename **Local queue file**, keeping it separate from the **Action Builder person ID**. Successful submission output selects the native `action_builder:` identifier as **Created person ID**, even if a custom identifier precedes it in the response. These presentation changes leave lookup decisions, queue transitions, and sending confirmations unchanged.

The initial question asks whether the operator has checked the person in Action Builder and against the paperwork. Answering no still permits local editing, keeping, or quitting. Sending and discarding require an affirmative check. The action menu supports:

| Choice | Result |
|---|---|
| **Keep**, or Enter | Leave the contact held and advance; no credentials or API calls |
| **Change** | Edit the local contact, validate it, save a replacement if changed, and redisplay the entire corrected person |
| **Send** | Start fresh GET checks and the confirmation flow below; selecting this action alone does not POST |
| **Discard** | Ask for explicit confirmation, then remove this contact JSON from review while retaining its disposition in history |
| **Quit** | End the pass without changing unprocessed contacts |

Empty input never approves sending, overriding a warning, or discarding. EOF and keyboard interruption stop with exit code 130. A normal pass, keep, declined confirmation, or quit returns 0 even when work remains held; handled errors return 1. An exit code of 0 is therefore not evidence that every review has been resolved.

### Editing and preserving history

`edit_record()` lists the nine allowed fields with their current values. Choose a field number, inspect the old value, and enter its replacement. Enter keeps the old value. Invalid replacements display an explanation and reprompt that field. Enter or `done` at the field menu finishes editing; the complete contact must validate before it is saved.

The editor reuses the extractor's email, US phone, state, and ZIP normalizers. Names need letters and cannot contain digits; middle initial is one letter with an optional period; city must contain a letter; all nine fields remain required. The editor does not add SSN, beneficiary, employment, or other source-document fields. Edits affect the local contact only: they do not rewrite the PDF or update an existing Action Builder person.

A changed contact has a new content hash. `save_review_edit()` writes the corrected JSON, links it to the original history, marks the old hash `discarded`, and removes the old contact file after saving the disposition. The replacement remains in `review`. All connected versions remain held. The old values are not retained as a full contact backup in the decision list.

The editor refuses an edit whose resulting hash already exists anywhere in queue history, including pending, sent, or discarded records. It does not overwrite, merge, or restore that record. Consequently, changing back to a retired exact contact is also refused; there is no restore command. An unchanged edit retains the current item without creating another decision.

After saving, the command redisplays the person and asks whether to send. Actual changes reset the earlier human-check answer, so the revised person must be checked before approval. Declining leaves the corrections saved for another review pass. Interrupting before the edit is completed does not save the unfinished set of corrections.

### Sending one manually approved person

`review_send()` first requires the human-check answer and rejects a connected record with an existing successful receipt. This related-success block cannot be overridden through the CLI. It then validates the local values, builds the usual payload, loads the connection settings, and performs a fresh email-and-phone lookup in that configured campaign. Inspecting or editing alone never loads `.env` or makes a request.

`record_review_lookup()` persists the new evidence and a `checked` decision while keeping the review hold. Even a fresh `not_found` does not turn this reviewed record back into automatic pending work. The command displays the fresh candidate IDs and differing fields before asking for approval:

1. If this record or a connected historical version has an uncertain attempt, confirm that the earlier attempt was checked in the displayed campaign and the person was **not already created**.
2. If the fresh lookup returns candidates, confirm that those candidate IDs were checked and they are **different people**, so creating a new person is appropriate. An exact local-field match still requires this explicit identity decision; it is never silently ignored.
3. Either kind of override requires a nonblank explanation of no more than 500 characters. When both warnings apply, both confirmations are required. A complete fresh no-match result instead uses a fixed approval explanation without another typed reason.
4. Confirm creation once more with the person's name, Action Builder subdomain, and campaign shown. Every confirmation defaults to no.

Only then does `begin_review_send()` require the same-session lookup for that destination, save an `approved_send` decision, and persist `sending` before the POST starts. Connected alternatives remain held. A successful returned person with one usable native Action Builder UUID lets `finish_send()` move this record to `sent`. The usual sender and this command share the same response validation.

A failed/incomplete GET makes no claim or POST. A POST timeout, interruption, malformed successful response, or failed completion write is treated as uncertain; the command stops the pass instead of trying the next person or retrying automatically. If persisting `uncertain` also fails, the previously saved claim still prevents a normal automatic retry when the queue reopens.

The CLI records decisions, times, reasons, and destinations, but does not authenticate an operator or prove that the manual check actually happened. Candidate overrides remain human identity decisions. Queue methods enforce persistence and claim rules; the interactive confirmation requirements belong to this coordinated CLI, not to the raw POST helper.

### Discarding and unresolved source problems

After the checked answer and a separate discard confirmation, `discard_review()` saves the decision before unlinking the contact JSON. Its hash, source associations, prior attempt/lookup evidence, and minimal manual decision history remain. Re-importing the same contact, using another filename, or PDF `--resolve` cannot recreate a discarded hash. Discarding is not a remote deletion and never deletes the Downloads PDF. There is no undo/restore command.

Some extraction failures create only a family hold, with no contact JSON available. The interactive list cannot display or edit such a person; inspect/correct that source PDF and rerun extraction. Likewise, this command does not edit the remote person, merge duplicate people, or attach an uncertain local attempt to a known remote ID. If the person already exists, keep or discard the local contact and handle any remote correction separately.

### Function reference

| Function | Responsibility |
|---|---|
| `confirm()` | Accepts explicit yes; Enter/no declines; invalid replies reprompt |
| `normalize_review_field()` | Validates and normalizes a single allowed contact value |
| `normalize_review_record()` | Requires and checks the complete nine-field record |
| `show_person()` / `show_review()` | Display local values, source/reason, saved candidate summaries, and related-attempt warnings |
| `edit_record()` | Collect corrections, show old values, validate, save through the queue, and return the replacement handle |
| `review_send()` | Coordinate human checking, fresh GET evidence, override/final confirmations, durable claim, POST, and outcome handling |
| `run_review()` | Lock the queue and walk the unique review snapshot with keep/change/send/discard/quit choices |
| `main()` | Parse `--queue-dir`, report controlled errors, and assign exit codes |

## 11. What the public API documentation confirms

The URL and enclosing `person` object match Action Builder’s **Person Signup Helper**. That helper supports creating or updating a person using supplied identifiers, with Action Builder identifiers taking precedence. Custom identifier uniqueness is not enforced. The current payload supplies no identifiers, and the documentation does not promise that this request will automatically match by name, email, or phone. [Person Signup Helper](https://www.actionbuilder.org/docs/v1/person_signup_helper.html)

The documented person fields include the names and contact structures used by this builder. Phone numbers include a country code; postal address lines are arrays. Availability of fields can depend on the entity configuration. That organization-specific configuration was not verified here. [People resource fields](https://www.actionbuilder.org/docs/v1/people.html)

The API overview documents JSON requests, the token header, a four-calls-per-second limit, and GET filtering including email and phone. The sender’s per-run delay does not coordinate traffic from other computers. The current lookup sends only `email_address` and `phone_number` equality filters as encoded GET parameters. The earlier assumption that a surname filter was usable did not hold for the reported live endpoint. [Action Builder API overview](https://www.actionbuilder.org/docs/v1/index.html)

The user’s September 21 live observations were: unfiltered GET, email filter, and phone filter returned HTTP 200; `family_name`, `given_name`, and `name` filters were rejected. Those observations explain the narrower query set but do not prove comprehensive search coverage or a successful creation. The implementation tests use mocked responses to verify requests, parsing, no-match decisions, and holds. Sender orchestration tests replace submission; their passing result does not validate live POST success interpretation.

## 12. Running modes and their differences

Run commands from the project folder. The examples below use this Mac project’s existing virtual environment; another installation may use a different Python command.

| Command | Effect | Network effect |
|---|---|---|
| `.venv/bin/python extract_person.py` | Scan configured Downloads and update the managed queue | No API request |
| `.venv/bin/python extract_person.py --downloads-dir "/path/to/test-downloads" --queue-dir "/path/to/test-queue"` | Use isolated folders for a controlled import | No API request |
| `.venv/bin/python extract_person.py "/path/to/person.pdf"` | Queue an explicit PDF while checking configured siblings | No API request |
| `.venv/bin/python extract_person.py "/path/to/person (1).pdf" --resolve` | Explicitly choose an unsent version under the current rules | No API request |
| `.venv/bin/python extract_person.py "/path/to/person.pdf" --output "/path/to/standalone.json"` | Bypass the queue and create standalone JSON | No API request |
| `.venv/bin/python send_person.py` | Preview the managed pending records | No API request |
| `.venv/bin/python send_person.py --queue-dir "/path/to/test-queue"` | Preview another managed root | No API request |
| `.venv/bin/python action_builder_lookup.py` | Check email/phone; hold candidates in review and leave completed no-match results pending | Real GETs only when run; no remote writes |
| `.venv/bin/python action_builder_lookup.py --queue-dir "/path/to/test-queue"` | Check another managed queue root | Real GETs to the configured campaign, even if the local folder is named test |
| `.venv/bin/python send_person.py --submit` | Freshly check pending people; hold candidates and send completed no-match results | Real GETs and eligible POSTs; errors stop the batch |
| `.venv/bin/python review_person.py` | Show held contacts one by one; explicitly keep, correct, discard, or approve one creation | Local choices are offline; choosing send makes real GETs, and final approval permits a POST |
| `.venv/bin/python review_person.py --queue-dir "/path/to/test-queue"` | Review another managed queue | Uses the configured campaign when sending; a local test folder does not select a remote test campaign |
| `.venv/bin/python send_person.py "/path/to/standalone.json" --submit` | Fresh lookup permits POST only for completed no-match results; no managed history | Real GETs and an eligible POST, without a durable queue claim/receipt |

Preview is not entirely read-only with respect to local files. Opening the queue can create folders/history, migrate old layouts, finish interrupted moves, or change an interrupted `sending` status to `uncertain`. “Preview” guarantees the application takes its no-POST path; it does not mean “the filesystem cannot change.”

The sender returns 0 after ordinary preview or a run with no newly held records, including an empty pending queue. That can coexist with pre-existing review groups. A checking/submitting run that holds records returns 1, as do handled errors; keyboard interruption returns 130. The check-only command follows the same outcomes. Interactive review returns 0 after ordinary keep/quit/completion even if held records remain, 1 on handled errors, and 130 on EOF/interruption. A future launcher must distinguish no pending work from all work successfully resolved.

## 13. What the tests establish

**Recorded full-suite run before the terminal-formatting update: 207 tests passed after the interactive-review and response-validation changes on September 21, 2026, in 2.071 seconds on Python 3.14.7.** The rerun used `.venv/bin/python -B -m unittest discover -s tests`; add `-v` as below to see individual test names. The earlier email/phone implementation had 160 tests; the interactive-review revision added 23 review-queue, 20 interactive CLI, and 4 submission-receipt tests. The checks used invented data, temporary queues, and mocked HTTP; no real `.env` credentials, production queue, or live Action Builder requests were used.

**Terminal-readability validation:** the same 207-test suite passed after the display changes in 1.670 seconds. Terminal output was also inspected using invented data, including a successful response whose custom identifier precedes the native Action Builder ID. These checks used no live queue or network requests.

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

The tests cover matching and differences, candidate IDs, incomplete/error responses, pagination, destination validation, no-match clearance and submission, fresh checks before POST, offline preview, historical hold preservation, and batch failure behavior. Interactive-review tests add editing/normalization, explicit confirmations, candidate and uncertain overrides, related-success blocking, discard/re-import behavior, schema migration, decision validation, and failures around durable deletion and sending. They establish the tested local rules, not the behavior of your actual campaign.

The original pre-lookup review ran:

```sh
.venv/bin/python -B -m unittest discover -s tests -q
```

**Historical baseline: 105 tests passed on Python 3.14.7 before the lookup feature.** The `-B` option avoids creating new Python bytecode caches. It does not change the program’s business rules.

| Test file | Tests | Main evidence |
|---|---:|---|
| `test_extract_person.py` | 29 | Template selection, required fields, transformations, layout changes, normalization, ambiguous answers, standalone output preservation |
| `test_pdf_downloads_finder.py` | 17 | Filename matching, numbered copies, fingerprints, incomplete/recent/future files, symlinks, changed files, source preservation |
| `test_auto_import.py` | 19 | Queueing, repeat scans, same/different contact copies, invalid siblings, deferral, resolution, selected-file race handling |
| `test_queue_layout.py` | 15 | Folder transitions, manual misplaced files, interrupted moves, version-1 migration, placeholders, malformed/unknown/duplicate entries, old-default guard |
| `test_send_queue.py` | 25 | Local duplicate protection, holds, attempts, preview, claim ordering, batch preflight, pacing, failure stopping, managed-path protections |
| `test_action_builder_lookup.py` | 38 | Email/phone-only requests, successful empty-result clearance, local name/address comparisons, exact/partial/conflicting matches, IDs/field types, complete pagination, query escaping, and sanitized errors |
| `test_lookup_queue.py` | 17 | Offline preview, fresh checks, GET-before-claim ordering, historical empty and populated holds across aliases/reopens/resolution, no-match submission, failed checks, standalone gating, and real-client sender integration with mocked HTTP |
| `test_review_queue.py` | 23 | Unique held lists, edit/hash links, collisions, discarded history and re-import prevention, durable deletion/recovery, fresh manual claims, connected holds, migration, and decision validation |
| `test_review_person.py` | 20 | Offline review, one-by-one display, editing and corrected payloads, safe defaults, EOF/interruption, candidate/uncertain confirmations, claim ordering, and ambiguous attempts |
| `test_submission_receipt.py` | 4 | Valid nested person IDs, rejected missing/error/malformed/ambiguous receipts, and normal sender uncertainty without retry after a bad successful response |
| **Total** | **207** | **Local regression checks; not live integration, complete coverage, or a production-readiness certificate** |

The synthetic PDF tests are valuable: they isolate exactly where text is placed, deliberately change labels/answers, and establish that missing answers do not shift neighboring data into the wrong field. They do not establish the acceptance rate for a collection of real Jotform exports.

The original sender tests mocked `submit_to_actionbuilder()` and prohibited real HTTP. They covered orchestration while leaving the actual POST boundary under-tested. Lookup implementation adds focused mocked GET and sender-integration coverage; the unified table now includes those additions. The added receipt-boundary regressions address the previously demonstrated F6 cases.

The original additional review probes reproduced F2–F7 in temporary directories or mocked requests; F6 and F7 are now addressed as noted above. During the September 18 refresh, an independent review repeated F2–F6 and reproduced the same failures. F11 was also demonstrated with invented contacts and mocked surname-search results; that candidate-exclusion branch is no longer used after the September 21 change. These are review probes, not additional passing regression tests in the repository. Test count alone is not a coverage percentage, and no line/branch coverage percentage was measured.

The interrupted-move tests inject failures at selected points. They are not physical power-loss tests, killed-subprocess recovery tests, or cross-computer concurrency tests.

Useful next regressions, tied to findings:

| Scenario to add | Required outcome after a fix |
|---|---|
| Shared identical record held through two aliases | A deliberate resolution can complete without circular holds |
| Shared record diverges into two versions | The alias policy prevents unintended submission of both versions |
| Previously queued family appears only in final discovery | Its older pending record is held until the correction is processed |
| A malformed correction raises an unexpected parser exception | Its predecessor is not left ready to send |
| HTTP 200 with `{}`, an error object, or no returned person (now covered) | No `sent` completion; outcome remains uncertain for reconciliation |
| Valid success receipt (mocked cases now covered) | Exactly one claimed record completes with the expected usable identifier; still compare a controlled live response |
| Redirect, 401/403, 429, server error, invalid JSON, timeout | Defined error/uncertain behavior; no accidental automatic second POST |
| Malformed subdomain or campaign component (now covered) | Rejected before credentials are attached or a record is claimed |
| Email/phone candidate with multiple remote addresses | Candidate stays in review regardless of which complete address supplies comparison details; cover address order and ties |
| Destination changes for an existing queue | Explicit mismatch handling rather than silently reusing its ledger |
| Standalone input, if retained | An explicitly accepted validation and repeat-submission policy |

## 14. Dependencies, setup, and data handling

The reviewed virtual environment runs Python 3.14.7 on this Mac. All eight installed distribution versions below match `requirements.txt`. The highest declared minimum Python version among these installed distributions is 3.10. That dependency floor is not the same as testing every supported Python version or operating system.

| Distribution | Required and installed version | Installed `Requires-Python` | Role |
|---|---|---|---|
| `pypdf` | `6.18.1` | `>=3.9` | Reads PDF structure and positioned text |
| `requests` | `2.34.2` | `>=3.10` | Sends GET and POST requests |
| `python-dotenv` | `1.2.3` | `>=3.10` | Supplies the imported `dotenv` settings loader |
| `dotenv` | `0.9.9` | Not declared | Additional distribution in the current requirements; the imported loader is provided by python-dotenv |
| `urllib3` | `2.8.0` | `>=3.10` | HTTP connection support used by Requests |
| `certifi` | `2026.7.22` | `>=3.7` | Certificate bundle used by the HTTP stack |
| `idna` | `3.19` | `>=3.9` | Internationalized hostname support |
| `charset-normalizer` | `3.5.1` | `>=3.7` | Encoding support |

These values came from the local requirements file and installed package metadata. No dependency upgrade or package installation was performed for this review. The extra `dotenv` distribution can be considered for later requirements cleanup, separately from validating the current pinned environment.

Version pins make the intended environment explicit. They do not prove a fresh installation works on every target computer. Test a clean installation using the actual deployment OS, Python, browser download behavior, and user account before copying the workflow broadly. No Windows or packaged-desktop application validation was performed in this review.

`Path.home() / 'Downloads'` is a conventional location, not a query of the browser’s chosen download directory. Redirected or customized Downloads locations require `--downloads-dir`. The eventual launcher needs an explicit installation/configuration strategy for this.

The original PDF contains more sensitive information than the nine-field contact JSON. This workflow does not create additional PDF copies, but it also does not delete, archive, or set a retention period for the originals. Contact JSON, source filenames, receipts, and `.env` remain local readable files. Terminal previews intentionally display the contact payload.

The queue uses private file creation and POSIX directory permissions where implemented. That is not encryption, and it does not itself configure Windows access controls, backups, or every existing folder’s permissions. Treat storage location and access as actual deployment decisions.

`.gitignore` excludes the default generated-data locations and history from ordinary new Git tracking while retaining empty status folders. A custom `--queue-dir` or `--output` inside another repository folder is not automatically covered; that location needs its own ignore rule. Ignore rules do not encrypt files or retroactively remove files already committed to a repository. This review did not perform a repository-history audit.

## 15. A practical weekend review plan

**First pass: trace one invented person.** Read `extract_person.main()`, then `import_downloads()`, `_import_group()`, and `extract_approved_person()`. Write down what exists at each stage: path, `Download`, group, nine-field dictionary, JSON file, and manifest entry. Be able to explain why the source PDF is never the object sent to the API.

**Second pass: study corrections.** Read `filename_family()`, the two hashes, `known_sources()`, `hold()`, `defer()`, and `enqueue()`. Walk through identical copies, a changed telephone number, a missing required field, an incomplete download, an explicit choice, and a correction after a send attempt. Pay particular attention to aliases: two filenames can share one contact record.

**Third pass: trace a check and submission without real requests.** Read `send_queue()`, `ActionBuilderLookup.check()`, and the corresponding tests. Follow payload construction, whole-batch validation, checked configuration, email/phone GETs, and each saved decision. Compare a candidate hold with a complete empty `not_found` result: check-only stays pending; submit claims, posts, and records completion. Trace failed GETs and uncertain POSTs separately. Explain what remains on disk at each stopping point.

**Manual-review pass:** read `review_person.run_review()`, `edit_record()`, `review_send()`, and the queue methods in section 10B. Trace one kept record, one corrected hash, one confirmed discard, a candidate override, and an uncertain earlier send. Confirm what each prompt authorizes and why a fresh review lookup by itself leaves the hold intact.

**Fourth pass: inspect the API boundary.** Read `ActionBuilderLookup._search()`, `_get_page()`, and `submit_to_actionbuilder()` against the linked primary documentation. Confirm the wrapper, destination, required configuration, token header, country-code phone format, expected returned person/identifier, and error cases. Then inspect which of these are actually tested rather than assumed.

**Fifth pass: decide the business rules.** Record answers to these questions before implementation changes:

- What evidence is sufficient to conclude two submissions are the same person?
- What should happen when one person already exists remotely with different details?
- Which fields may be missing, and who resolves those cases?
- Are `Mobile`, `home`, `physical`, and `US` always appropriate defaults?
- Should `A` and `A.`, capitalization, or address abbreviations be treated as equivalent?
- Can two applicants legitimately produce the same abbreviated filename?
- Can two computers process the same download or work in the same campaign concurrently?
- Does each queue belong to one fixed destination?
- Who checks uncertain attempts, and what evidence closes them?
- What should users see when some records succeed and others need review?

**Sixth pass: validate representative exports.** Create controlled test submissions through the actual form/export path using invented contact data. Include long and multiword names, accents used by your applicants, a two-line address, leading-zero ZIPs, each supported phone spelling, missing optional-by-business fields, corrected forms, and browser-generated numbered copies. Compare every extracted field to the visible PDF. Use an isolated Downloads directory and isolated queue.

**Seventh pass: establish rollout evidence.** Fix the confirmed defects and add their regressions. Decide the remote identity/recovery behavior. Verify a fresh installation on each intended OS. Only after those checks, plan a controlled integration exercise in a designated test campaign with known invented records and a documented cleanup/reconciliation process. No such live exercise was performed as part of this review.

## 16. Python concepts that explain this implementation

| Concept | Where you see it | Plain-language meaning |
|---|---|---|
| Module import | `from record_queue import ...` | Reuses definitions from another file; it does not run that file’s guarded CLI job |
| Module guard | `if __name__ == '__main__':` | Starts the command only when the file is executed directly |
| `Path` | Throughout | Represents a filesystem path and offers operations such as reading, resolving, and renaming |
| Dataclass | `Download`, `Box`, `QueueItem` | A small named bundle of related values |
| `frozen=True` | Most descriptor dataclasses | Prevents reassigning descriptor attributes; it does not recursively freeze every contained object |
| Dictionary | Contact record, manifest | Maps named keys to values |
| Set | Allowed fields, hashes | Represents unique values and supports comparisons such as unexpected keys |
| Type hints | `dict[str, str]`, `Path | None` | Document intended values; they do not automatically validate all runtime inputs |
| Context manager | `with RecordQueue(...)`, `with file.open(...)` | Acquires a resource and arranges cleanup when control exits, including on exceptions |
| Exception | `ExtractionError`, `QueueError` | Signals that normal processing cannot safely continue along that path |
| Hash/fingerprint | SHA-256 | A deterministic content identifier, not an encrypted copy or a remote person ID |
| Canonical serialization | `_record_bytes()` | Writes equivalent dictionary structures in a consistent order/format before hashing |
| Atomic replacement | `os.replace()` | Changes one directory entry as a single filesystem operation; it is not a transaction spanning the API and several files |
| Flush/fsync | Queue and standalone writers | Requests that buffered data and relevant directory changes reach storage before continuing |
| Callback | PDF visitor | A function pypdf calls as it encounters text while parsing |
| Mock | Sender tests | A controlled substitute for a dependency, allowing behavior to be tested without a real network request |
| Exit code | `SystemExit(main())` | A number another program or launcher can use to decide whether and how to continue |

## 17. Review boundaries and source snapshot

The September 18 documentation review read the five runtime modules present then and their test coverage, reran the then-current suite, and repeated open failure probes with invented data. That review changed documentation, not behavior. The September 21 implementation changes lookup behavior in response to user-reported filter results and the requested sending sequence: only email/phone are queried; candidates are held, while both successful empty searches return `not_found` and allow submission after a fresh check. The later interactive-review change adds a sixth runtime module, explicit decisions, schema-3 discarded history, and stricter shared POST response validation. Local regression checks use mocked HTTP and temporary data; the reported live GET observations are separate evidence. No end-to-end live creation, line/branch coverage percentage, large-archive benchmark, or second-operating-system validation is claimed.

The earlier line-comment pass added paired Python/Program explanations across the runtime and test files. The September 21 comment-only cleanup replaced that repetitive narration with concise comments at meaningful steps. At that historical checkpoint, the parsed Python structure (the abstract syntax tree, or AST) of the twelve Python files then present matched the saved pre-cleanup version, ignoring source locations. That established only that the comment cleanup preserved behavior and docstring values. The later manual-review and receipt changes intentionally change executable code; they are validated by the new tests, not by that earlier AST comparison. The source snapshot below identifies the current reviewed revision.

Function locations in this guide refer to the current source below. SHA-256 fingerprints identify the exact bytes reviewed; they are not a signature, warranty, or assurance that the code is defect-free.

```text
pdf_downloads_finder.py
e0b3179ce5880cefce9c3c84f5279f7f95537368e28b292a12dc7129c94128cf

extract_person.py
00a40f7b38f7600efdb35b4e3f2cf111a37e440a4de5024e09ba1f8ea3837f31

record_queue.py
c904c6758666d1e1aa02718f4a2b514c5a75470c36f93536dba62a3ae9c31161

action_builder_lookup.py
47e60f094162e71a860511d5d2d4a50e7c4f3a8dedc6a6e15b78a88b83ff0ae8

send_person.py
ba67fdbecac65d7353e152b2481f8348ab596b5ca0c616d70419dc40933eb169

review_person.py
e8dad4bb3a44d7db93899356ef0c69b54881af051ed59087192ff566e141eb08
```

After code changes, rerun the relevant regressions and update affected explanations, function locations, and fingerprints. A changed comment does not implement new behavior. A changed parser also does not automatically rebuild records whose source hashes are already remembered; that needs a deliberate migration/reprocessing policy.

The distinction between evidence and intention matters throughout the document:

- **Implemented and tested:** the named local test scenarios passed on the fingerprinted implementation.
- **Confirmed open:** F2–F5 remain open from the earlier local probes. The shared response check now rejects the demonstrated F6 malformed success cases.
- **Addressed or superseded:** F7's unsafe destination formatting is rejected by shared validation. F11's surname-candidate exclusion was removed with unsupported name queries; the best-address summary remains.
- **Current operating policy:** completed empty email/phone searches permit normal submission after a fresh check; candidates and errors block automatic creation. Interactive review can create one explicitly approved person after fresh checks and any required candidate/uncertain overrides; related recorded successes block another creation.
- **Needs a policy or live validation:** campaign scope, matching certainty, queue destination binding, operator reconciliation, actual export variants, and target-computer installation.

## 18. Optional offline reproduction of the two alias findings

Run this from the project folder if you want to observe F2 and F3 yourself. It uses invented contacts and temporary queues that are removed afterward. It imports only the local queue module: **no sender, credentials, real queue, or HTTP requests are involved**. The calls named `begin_send` and `finish_send` below change temporary local history; those queue methods do not perform network operations.

```sh
.venv/bin/python -B - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from record_queue import RecordQueue

person = {
    'given_name': 'Morgan', 'family_name': 'Example', 'additional_name': 'A',
    'email': 'morgan@example.test', 'phone': '12025550123',
    'address_line_1': '123 Example Street', 'locality': 'Example City',
    'region': 'MA', 'postal_code': '01234',
}
corrected = {**person, 'phone': '12025550199'}

def add(queue, family, data, source, resolve=False):
    return queue.enqueue(family, data, {source}, [family + '.pdf'], resolve=resolve)

def seed(queue):
    add(queue, 'family-a', person, 'source-a')
    add(queue, 'family-b', person, 'source-b')

with TemporaryDirectory(prefix='ab-review-') as temporary:
    with RecordQueue(Path(temporary) / 'held-aliases') as queue:
        seed(queue)
        queue.hold('family-a', {'bad-source'}, ['a-copy.pdf'], 'Needs review.')
        add(queue, 'family-b', person, 'new-source-b')
        for family in ('family-a', 'family-b', 'family-a', 'family-b'):
            print('Resolution:', family, add(queue, family, person, family, True))
        print('Pending after resolutions:', len(queue.pending_records()))

    with RecordQueue(Path(temporary) / 'divergent-aliases') as queue:
        seed(queue)
        add(queue, 'family-a', corrected, 'corrected-a')
        add(queue, 'family-a', corrected, 'corrected-a', True)
        add(queue, 'family-b', person, 'source-b', True)
        items = queue.pending_records()
        print('Divergent records pending:', len(items))
        for item in items:
            queue.begin_send(item)
            queue.finish_send(item, {'person': {'identifiers': ['invented-receipt']}})
        print('Local statuses after both transitions:', queue.status_counts())
PY
```

For the reviewed implementation, the first case prints four `review` outcomes and zero pending records. The second prints two pending records, then `{'sent': 2}`. These outputs demonstrate the findings; they are not the desired acceptance criteria for a future fix. After changes, replace these demonstrations with regressions asserting the intended alias and resolution policy.

## 19. Acceptance matrix and decisions before deployment

Use this as a review worksheet. A checkmark should mean the stated result was observed and recorded, not merely that the code looks plausible. The evidence column distinguishes scenarios already covered locally from open defects and work still needing controlled integration or manual validation. Repeat the appropriate cases after any related fix.

**Keep test data isolated.** Use invented contacts, synthetic or deliberately generated test PDFs, a separate Downloads directory, and a separate queue root. Local folder names such as `test-queue` do not change the remote destination: lookup, submit, and interactive review still use the configured Action Builder campaign. The repository's unit tests mock network calls. A manually run lookup or `--submit` command makes real GET requests; choosing Send during interactive review also makes real GETs. A `--submit` command can POST after complete empty searches, and final interactive approval can POST a reviewed person. Use the intended test campaign for integration work; naming a local folder `test` does not change that destination.

| Check | Concrete trigger | Required result | Current evidence/status |
|---|---|---|---|
| A1. One supported document | Import one completed matching PDF | Exactly nine correct fields; one pending JSON; source PDF unchanged | Synthetic tests pass; compare representative actual exports manually |
| A2. Repeated scan | Run extraction twice on unchanged paperwork | One pending contact, no additional contact or API call | Covered locally |
| A3. Browser copies | Add `(1)` and nested numbered copies with identical answers | One contact record; all source fingerprints remembered | Covered locally |
| A4. Conflicting correction | Add a same-family PDF with changed phone/address | Previous unsent record held; no automatic newest-file selection | Covered for tested same-family cases |
| A5. Deliberate PDF choice | Inspect copies, then use explicit `--resolve` on the chosen unsent PDF | Chosen version may return to pending; API/attempt holds remain | Covered locally; alias deadlock F3 remains |
| A6. Incomplete correction | Add `.crdownload`, a too-recent copy, or a source changing during extraction | Related family deferred and old pending eligibility revoked | Covered for tested races; F4 is an uncovered-by-suite exception |
| A7. Late new family | Existing queued family absent at scan start; its correction arrives during another extraction | Old queued contact must not remain eligible unnoticed | **Fails: F4** |
| A8. Malformed correction | Related PDF has nonnumeric `/UserUnit` | Error reported and older pending version held | **Fails: F5** |
| A9. Shared filename histories | Two filename families first share a contact, then diverge | One deliberate identity/version decision; no unintended two-version send or circular hold | **Fails: F2/F3** |
| A10. Offline preview | Run sender without `--submit` and without valid credentials | Payload shown; zero GET/POST calls | Covered locally; local queue migration/recovery is still allowed |
| A11. Exact remote contact | Mock one Person matching the nine fields in email/phone searches | `existing`, saved candidate ID/destination, review hold, zero POSTs | Verify with mocked regressions; full live response behavior still needs validation |
| A12. Conflicting identities | Email finds person A; phone finds person B | `needs_review`; no automated merge, update, or create | Covered locally |
| A13. Unsupported name filters | Check a person using the reported working API capabilities | Query only email and phone; never send `family_name`, `given_name`, or `name` filters | User reports rejected name filters; assert the exact query set with mocks |
| A14. Multiple remote addresses | Email/phone returns a person whose addresses match different components | Keep the candidate for review; compare one complete address without merging homes | Historical F11 surname-discard branch removed; retain address-comparison regressions |
| A15. Complete empty email/phone searches | Both supported searches return complete valid empty results | `not_found`; check-only stays pending without a claim; submit repeats checks, then claims and POSTs | Validate with mocked client and queue tests; changed contact details can still hide an existing person |
| A16. Search error or incomplete page | HTTP error, timeout, missing pagination, changing counts, repeated IDs, or malformed person | Error, no no-match decision, no send claim or POST for that record | Covered locally |
| A17. Saved no-match receipt | Pending history contains an older `not_found`; run lookup/submit again | Old receipt cannot bypass fresh checks; a new match holds, an error blocks, fresh no-match permits POST in submit mode | Validate compatibility and each fresh-check branch with mocks |
| A18. Hold across aliases | Candidate or historical empty-candidate API hold followed by re-import, alias, correction, or PDF `--resolve` | Every connected eligible version remains held; upgrading does not requeue old empty-result holds | Automatic bypass remains blocked; explicit per-record review decisions are tested separately |
| A19. POST response is not a person | Return fresh empty lookups and mock HTTP 200 with `{}`, error object, or `person: null` | Preserve uncertainty; do not mark sent or retry automatically | Receipt validation now rejects these bodies; verify the queue remains uncertain |
| A20. Accepted POST machinery | Return fresh empty lookups and mock a successful person response | Claim before POST; record sent and exclude from later pending batches | Mocked path and returned-ID checks; validate a controlled live response before rollout |
| A21. Uncertain outcome | Timeout/interruption after a claimed POST | Block automatic retry; require remote reconciliation | Automatic retries blocked; interactive review requires explicit remote checking before another attempt |
| A22. Queue integrity | Edit by hand, duplicate, delete, or symlink a required tracked JSON | Refuse to send; explain need for restoration/review | Covered locally; legitimate discarded entries intentionally have no contact JSON |
| A23. Another run | Open the same queue concurrently | Second cooperating process cannot acquire the queue lock | Local behavior tested; not a multi-computer identity guarantee |
| A24. Destination change | Change valid subdomain/campaign while reusing queue | Explicit destination policy prevents accidental ledger reuse | **Open design gap: F8** |
| A25. Installation on another computer | Fresh clone, supported Python, fresh virtual environment, pinned install | Offline tests and representative extraction work under that actual account/OS | Not established by this Mac's passing suite |
| A26. Finished desktop workflow | Double-click a shortcut while a correction, match, or error needs review | Clear outcome; never silently continue from failed extraction into submission | Launcher is not implemented; specify behavior before building it |
| A27. Interactive local review | Keep, edit, or discard a held record | No environment credentials or HTTP; complete local person displayed; edits normalized; destructive action explicitly confirmed | Covered with temporary queues and mocked input; discarded history suppresses identical re-import |
| A28. Candidate override | Fresh lookup still returns possible matches | Require checking IDs, explicit different-person confirmation, reason, and named-campaign final approval before claim/POST | Mocked CLI tests; operator identity judgment is not independently verified |
| A29. Earlier uncertain send | Review a contact connected to an uncertain attempt, including after editing/discarding an older version | Require remote-absence confirmation and reason; related recorded success blocks creation | Covered locally; no remote ID reconciliation or update implemented |
| A30. Discard interrupted | Save fails before deletion, or process stops after the disposition is saved | Never delete from unsaved state; reopening completes only saved, hash-checked removal | Queue regression tests cover injected failure points |
| A31. Edit collision or restore | Correct a contact to values already present in pending/sent/discarded history | Refuse overwrite, merge, or restore; preserve original files/history | Covered locally; no restore command exists |

### Decisions the operator must make

**A PDF conflict is a source-data decision.** Inspect the original and corrected paperwork, determine which answers are correct, and use the supported explicit PDF resolution only for an eligible unsent family. The larger `(1)` number, later modification time, or larger file size does not establish that the information is correct. A conflict after a send attempt needs remote reconciliation rather than a new-person submission.

**An API match is a person-identity decision.** Use the saved candidate IDs and differing field names to inspect the relevant Action Builder records. Decide whether the person is already represented, whether remote data needs a separate authorized correction, or whether this is an unrelated person sharing contact/name details. Automatic submission holds every such case. The review command shows fresh candidates and can create a person only after explicit confirmation that those candidates are different people. If the person already exists, keep or discard the local record. Remote updates and linking an uncertain attempt to an existing remote ID remain separate work.

**No email/phone candidate is also an identity decision.** The current policy returns `not_found` only when both supported searches complete successfully without candidates. That permits submission after a fresh check, but a person can still exist with both contact details changed. An earlier `needs_review` receipt with `candidates: []` remains held; the new policy does not automatically release or requeue historical holds. Those records now appear in `review_person.py`; no automatic bulk release occurs.

**An uncertain POST is an outcome decision.** A timeout is not proof that nothing happened. Check the intended campaign and available receipt/evidence before deciding whether a person was created. The review command records the manual decision, time, reason, and sending destination. It can retry only after explicit remote checking; it does not automatically reconcile an existing remote ID or authenticate the human operator.

**An incomplete lookup is a connectivity/contract decision.** Correct the connection, permissions, response-format issue, or pagination assumption and rerun the check. A previous successful `not_found` receipt must not be treated as permission to bypass a failed fresh lookup. The current CLI already stops the affected submission before claiming a POST.

### Evidence to retain for an initial pilot

Record the reviewed commit, actual Python/dependency versions, target OS/account, chosen Downloads location and marker, queue location, intended subdomain/campaign, and the acceptance checks performed. Keep invented-data comparison examples that show the visible PDF value, extracted JSON value, and intended API field. For network validation, record outcomes and remote IDs without copying API tokens or unnecessary personal data into Git.

Start the pilot with extraction, preview, and GET checks on one designated computer. Review candidates and any historical empty-result holds. Before live rollout, confirm the accepted email/phone identity limits, live response behavior and campaign scope, and address the open findings. The CLI can now create unmatched people with `--submit`; the interactive review workflow must also be validated with representative records. A single sender reduces one check-then-create race; it does not enforce server uniqueness or coordinate people entering records manually.


## 20. Git commit information for the current changes

This is a **review checkpoint**, not a production-release declaration. At the September 21 update, the current branch is `dev` and HEAD is `b3f5cda` (`Add review queues and pre-send Action Builder checks`). Queue layout, the initial lookup, and the license already exist in that commit. The working tree now includes email/phone lookup behavior, interactive review, schema-3 decisions/discarded history, response validation, and concise explanatory comments. No files were staged, committed, or pushed during this update.

### What the proposed commit contains

| Files | Change to review |
|---|---|
| `action_builder_lookup.py` | Remove name-filter requests and their candidate-exclusion branch. Query email/phone, retain local name/address comparisons, return `not_found` after both searches complete empty, and report HTTP errors without printing private query values or server bodies. |
| `record_queue.py` | Migrate to schema 3; add manual review lists, details, decisions, hashed corrections, discarded tombstones, and same-session review claims. Preserve connected holds and uncertain/successful evidence; save dispositions before removal and invalidate failed in-memory commits. |
| `review_person.py` | Add the one-person-at-a-time CLI: display, keep, edit, discard, or explicitly approve creation after fresh checks, warning-specific confirmations, and final named-campaign approval. |
| `send_person.py` | Explain preview/check/submit behavior and require a valid returned person/native ID before recording success. Normal pending submissions retain fresh no-match checks and claim ordering. |
| `tests/test_action_builder_lookup.py`, `tests/test_lookup_queue.py` | Exercise supported filters, no-match clearance and sending, sanitized failures, historical hold persistence across reopen/aliases/resolution, and the real lookup client connected to the sender with mocked HTTP. |
| `tests/test_review_queue.py`, `tests/test_review_person.py`, `tests/test_submission_receipt.py` | Cover durable local review decisions, edit/discard history, migration, CLI confirmations and overrides, uncertain attempts, and successful HTTP responses lacking a usable person receipt. |
| Runtime and test comments | Replace repetitive Python/Program narration with concise explanations of purpose and non-obvious decisions. The earlier comment-only cleanup preserved Python structure and docstrings; the subsequent features intentionally change executable behavior. |
| `README.md`, `CODE_REVIEW.md` | Explain setup, normal extraction/preview/lookup/submit, the interactive review branch, discarded-record limits, preserved holds, remaining findings, and the recorded test result. |

`requirements.txt`, `.env.example`, `.gitignore`, the queue placeholders, and `license.md` are unchanged in this working-tree snapshot. A pre-existing deletion of `COMMIT_MESSAGE.md` was left untouched; decide separately whether to include that deletion. Review the actual diff before staging, since your subsequent edits can change this list.

### Suggested title and description

The proposed title is:

```text
Add interactive review and approved Action Builder submissions
```

Suggested description: held contacts can now be reviewed one by one, corrected locally, kept, discarded with retained history, or explicitly submitted after fresh email/phone checks and the required identity confirmations. Normal pending submission permits completed no-match results. Schema 3 preserves decisions and prevents silent restoration of discarded hashes, while shared response validation rejects ambiguous success bodies. Include only validation results from the final revision you actually commit; this reviewed checkpoint passed 207 mocked/offline tests with no live submissions.

Keep the changed lookup decision, receipt validation, tests, and explanations together. Comment cleanup can be described in the same review checkpoint or separated carefully by hunk; do not split interdependent behavior changes into commits that cannot run together.

### Stage only the intended files

Run these commands from the project root. They are preparation instructions for you; they were **not executed** during this review.

First inspect the current state and rerun the checks:

```sh
git status --short
git diff --stat
git diff --check
```

On this Mac installation, run:

```sh
./.venv/bin/python -B -m unittest discover -s tests -v
```

For Windows, use the equivalent interpreter command from the [README](README.md). The recorded passing run was on macOS; it does not establish Windows behavior.

Stage the named changes:

```sh
git add -- README.md CODE_REVIEW.md
git add -- extract_person.py pdf_downloads_finder.py record_queue.py send_person.py action_builder_lookup.py
git add -- review_person.py
git add -- tests/test_auto_import.py tests/test_extract_person.py tests/test_pdf_downloads_finder.py
git add -- tests/test_send_queue.py tests/test_queue_layout.py
git add -- tests/test_action_builder_lookup.py tests/test_lookup_queue.py
git add -- tests/test_review_queue.py tests/test_review_person.py tests/test_submission_receipt.py
```

These commands cover the source/test files and intentionally leave the pre-existing `COMMIT_MESSAGE.md` deletion unstaged. Unchanged files add nothing to the staged diff. The commands do not stage runtime data.

Review exactly what will be committed, including the newly added files:

```sh
git diff --cached --name-status
git diff --cached --stat
git diff --cached --check
git diff --cached
```

Do not stage the local `.queue-state.json`, locks, real contact JSON, `.env`, Downloads PDFs, a virtual environment, or captured previews. The explicit staging list avoids relying on a broad `git add .`; inspect the staged diff before committing. No new historical secret scan is claimed by this update.

### Commit locally, then push

After reviewing the staged files:

```sh
git commit -m "Add interactive review and approved Action Builder submissions"
git show --stat --oneline HEAD
git status --short
```

Use `git commit` without `-m` if you want to enter a longer body in your editor. Do not claim the tests passed for a later code revision until you rerun the relevant checks.

Before pushing, confirm that `origin` points to the intended repository and that its visibility/access settings match the private-use policy in section 21. If `dev` and `origin` are still the intended branch and remote:

```sh
git push -u origin dev
```

If you use another review branch, replace `dev` with that branch. A rejected push means you should inspect the remote changes and reconcile the histories; it is not a reason to force-push. No remote access or publication was needed to prepare this commit information.

Committing a review checkpoint preserves the work. It does not resolve F2–F5, validate live POST behavior, or make the program ready for unattended installation. Interactive review and receipt validation are implemented and locally tested. The current policy allows unmatched submissions while retaining candidate holds and blocking failed checks; it cannot guarantee duplicate prevention when contact details change.

## 21. Private-use license, ownership, and practical limits

The owner's selected policy is **private use, reserved rights, and permission for outside use or redistribution**. The requested copyright name is **William Jerrells iii**. [license.md](license.md) now expresses that policy instead of the earlier tentative MIT note.

### What the terms do

- Identify the copyright holder for original project material to the extent they own or control it.
- Require express written permission for recipients other than the copyright holder.
- Allow authorized installation, configuration, operation, and necessary installation/backup copies within the purposes and computers covered by that permission.
- Reserve other uses, modification, publication, redistribution, sublicensing, and sale unless separately permitted in writing, subject to applicable law and independently granted hosting rights.
- Retain copyright/attribution notices and preserve separate third-party license terms.
- Include warranty disclaimers, no support commitment, and limitations of liability to the extent the law permits.

This is a custom proprietary/private-use notice, not MIT, GPL, or a claim that the repository is open source. Describing the project as “MIT licensed” elsewhere would conflict with the chosen policy.

For installations you authorize, keep a brief written record of who may use which version, for what internal purpose, and on which computers. For example, a permission record could identify the recipient organization/users, the repository or release, the allowed activity, and that `license.md` applies. That makes the boundary of your authorization clearer without granting public redistribution rights.

### What the terms do not establish

A license file cannot guarantee protection against lawsuits, eliminate all liability, prove ownership, or make inaccurate software reliable. Whether particular restrictions or disclaimers are enforceable depends on applicable law and circumstances, including whether relevant terms were communicated and accepted. Third-party claims and responsibilities concerning personal data or API access are not automatically resolved by a software disclaimer.

These terms are a starting point for your chosen policy. Have a qualified lawyer review them before relying on them for distribution or liability protection, and confirm that you have authority to license any employer-owned, commissioned, or contributed material. Prior permissions and third-party rights need separate consideration. This review did not determine legal ownership or perform jurisdiction-specific legal analysis. GitHub likewise recommends professional advice for legal licensing questions. [GitHub licensing guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

Keep the repository private if you intend to control access to its source. A restrictive license is not an access-control setting. GitHub's terms give users certain viewing/forking rights for public repositories; making a repository private later does not remove existing copies. The current remote's visibility was not checked or changed. [GitHub Terms of Service, user-generated content](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#d-user-generated-content), [repository licensing and visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

### Dependencies keep their own licenses

The installed package metadata and packaged license texts were inspected. They match the eight version pins in `requirements.txt`:

| Dependency | Pinned and installed version | License identified locally |
|---|---|---|
| `certifi` | `2026.7.22` | MPL-2.0 |
| `charset-normalizer` | `3.5.1` | MIT |
| `dotenv` | `0.9.9` | MIT; its metadata says UNKNOWN, but its included LICENSE contains MIT terms |
| `idna` | `3.19` | BSD-3-Clause |
| `pypdf` | `6.18.1` | BSD-3-Clause |
| `python-dotenv` | `1.2.3` | BSD-3-Clause |
| `requests` | `2.34.2` | Apache-2.0, with a packaged NOTICE file |
| `urllib3` | `2.8.0` | MIT |

This inventory reports the inspected artifacts; it is not a complete legal compatibility opinion or a substitute for the actual license texts. Your private-use terms apply to your original material, not to these libraries. Do not replace their licenses with `license.md` or claim exclusive ownership of them.

The current repository references dependencies through `requirements.txt`; it does not commit the virtual environment. If you later distribute an executable, installer, dependency archive, or copied environment, review the licenses of the exact artifacts included and preserve their required notices. In particular, do not omit Requests' NOTICE or overlook certifi's MPL terms when deciding how to redistribute or modify bundled components. A future packaging step needs its own license/notice review.

External Action Builder access, source forms, and downloaded personal information also remain subject to their own permissions and terms. The source-code license does not authorize access to them, grant rights in their content, or make them suitable for inclusion in Git.
