Add review queues and pre-send Action Builder checks

Organize extracted contact JSON into composed_info/pending, sent, and review
with durable local history, legacy queue migration, and interrupted-move recovery.
Keep generated contact data and machine-specific history out of Git while
tracking empty folder placeholders.

Add action_builder_lookup.py for GET-only checks of pending contacts and reuse
the same client immediately before managed and standalone submissions. Search
email and phone independently, inspect possible name/address matches, validate
pagination and destination settings, and retain matching evidence in queue
history. Hold existing or ambiguous matches without posting or updating them;
stop before a send claim when lookup fails. Keep API holds across corrections,
explicit PDF resolution, and connected filename aliases.

Expand README setup instructions and CODE_REVIEW.md with the current workflow,
function references, test evidence, known defects, acceptance scenarios, and
rollout limits. Replace the license planning note with private-use terms under
William Jerrells iii, including warranty and liability disclaimers and separate
treatment of third-party licenses.

Validation: 153 tests pass on Python 3.14.7 with invented records, temporary
queues, and mocked API calls. No live campaign or production queue was used.

This is a review checkpoint, not a production-ready release. The code review
documents remaining alias, stale-input, response-validation, recovery, and
matching gaps, including a reproduced multi-address lookup false negative.
Desktop launching and coordination across sending computers remain future work.
