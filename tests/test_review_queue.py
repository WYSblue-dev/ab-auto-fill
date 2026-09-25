"""Check manual review history using invented contacts in temporary queues."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from record_queue import QueueError, RecordQueue, STATE_NAME
from test_send_queue import CONFIG, CLEAR, RECORD, SUCCESS
from action_builder_lookup import LookupResult


PERSON_ID = SUCCESS["person"]["identifiers"][0]
OTHER_PERSON_ID = "action_builder:22222222-2222-4222-8222-222222222222"


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "queue"

    def held(self, queue, *, family="first", record=None):
        queue.enqueue(family, record or RECORD, {f"source-{family}"}, [f"{family}.pdf"])
        queue.hold(family, {f"source-{family}"}, [f"{family}.pdf"], "Check the source details.")
        return next(item for item in queue.review_records() if item.family == family)

    def state(self):
        return json.loads((self.directory / STATE_NAME).read_text(encoding="utf-8"))

    def approve(self, queue, item):
        queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
        queue.begin_review_send(item, config_destination=CONFIG.destination,
                                reason="Checked Action Builder manually; this is a new person.")

    def test_review_list_is_unique_and_excludes_pending_sent_and_discarded(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.enqueue("alias", RECORD, {"alias-source"}, ["alias.pdf"])
            queue.enqueue("pending", {**RECORD, "given_name": "Pending"}, {"p"}, ["p.pdf"])
            queue.enqueue("sent", {**RECORD, "given_name": "Sent"}, {"s"}, ["s.pdf"])
            sent = next(entry for entry in queue.pending_records() if entry.family == "sent")
            queue.begin_send(sent)
            queue.finish_send(sent, SUCCESS)
            self.assertEqual([entry.id for entry in queue.review_records()], [item.id])
            details = queue.review_details(item)
            self.assertEqual(details["families"], ["alias", "first"])
            self.assertEqual(details["source_names"], ["alias.pdf", "first.pdf"])
            queue.discard_review(item, "Already handled manually.")
            self.assertEqual(queue.review_records(), [])

    def test_discard_deletes_contact_but_retains_tombstone_across_reopen(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.discard_review(item, "Unneeded duplicate paperwork.")
            self.assertFalse(item.path.exists())
            self.assertEqual(queue.status_for("first"), "discarded")
            queue.discard_review(item, "Repeated confirmation.")
        state = self.state()
        entry = state["records"][item.id]
        self.assertEqual(state["version"], 4)
        self.assertEqual(entry["status"], "discarded")
        self.assertEqual(len(entry["decisions"]), 1)
        self.assertEqual(entry["decisions"][0]["action"], "discarded")
        self.assertNotIn(RECORD["email"], json.dumps(entry))
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.review_records(), [])

    def test_discarded_hash_cannot_be_reimported_or_resolved_under_new_alias(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.discard_review(item, "Already handled.")
        with RecordQueue(self.directory) as queue:
            for family in ("first", "renamed"):
                for resolve in (False, True):
                    self.assertEqual(queue.enqueue(family, RECORD, {"again"}, ["again.pdf"], resolve=resolve), "unchanged")
            self.assertFalse(item.path.exists())
            self.assertEqual(queue.review_records(), [])
            self.assertEqual(queue.pending_records(), [])
            self.assertTrue(queue.known_sources("renamed", {"again"}))

    def test_discard_revokes_connected_pending_versions_but_not_unrelated_records(self):
        alternate = {**RECORD, "phone": "12025550188"}
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.enqueue("alias", RECORD, {"a"}, ["a.pdf"])
            queue.enqueue("alias", alternate, {"b"}, ["b.pdf"], resolve=True)
            queue.enqueue("unrelated", {**RECORD, "given_name": "Other"}, {"c"}, ["c.pdf"])
            queue.discard_review(item, "Discard stale copy.")
            self.assertEqual([entry.family for entry in queue.pending_records()], ["unrelated"])
            self.assertEqual(queue.enqueue("alias", alternate, {"b"}, ["b.pdf"], resolve=True), "review")

    def test_discard_is_committed_before_unlink_and_reopen_finishes_cleanup(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)

            def interrupted(identifier, source):
                self.assertEqual(self.state()["records"][identifier]["status"], "discarded")
                self.assertTrue(source.is_file())
                raise OSError("Invented interruption before deleting the contact file.")

            with patch.object(queue, "_remove_discarded_file", side_effect=interrupted):
                with self.assertRaises(OSError):
                    queue.discard_review(item, "Do not send.")
        self.assertTrue(item.path.is_file())
        with RecordQueue(self.directory) as queue:
            self.assertFalse(item.path.exists())
            self.assertEqual(queue.review_records(), [])

    def test_discard_never_unlinks_when_history_write_fails(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            with patch.object(queue, "_write_json", side_effect=OSError("Invented disk error.")):
                with self.assertRaises(OSError):
                    queue.discard_review(item, "Do not send.")
            self.assertTrue(item.path.is_file())
            self.assertEqual(self.state()["records"][item.id]["status"], "review")
            with self.assertRaisesRegex(QueueError, "Close and reopen"):
                queue.review_records()
            self.assertTrue(item.path.is_file())
        with RecordQueue(self.directory) as queue:
            self.assertEqual([entry.id for entry in queue.review_records()], [item.id])

    def test_interrupted_discard_does_not_delete_modified_file(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            with patch.object(queue, "_remove_discarded_file", side_effect=OSError("Stopped.")):
                with self.assertRaises(OSError):
                    queue.discard_review(item, "Do not send.")
        item.path.write_text(json.dumps({**RECORD, "given_name": "Changed"}))
        with self.assertRaises(QueueError):
            with RecordQueue(self.directory):
                pass
        self.assertTrue(item.path.exists())

    def test_edit_writes_new_hash_and_removes_old_file_with_links_preserved(self):
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.directory) as queue:
            old = self.held(queue)
            queue.enqueue("alias", RECORD, {"a"}, ["a.pdf"])
            edited = queue.save_review_edit(old, changed)
            expected = hashlib.sha256(json.dumps(changed, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(edited.id, expected)
            self.assertNotEqual(edited.id, old.id)
            self.assertFalse(old.path.exists())
            self.assertEqual(json.loads(edited.path.read_text()), changed)
            self.assertEqual(queue.review_details(edited)["families"], ["alias", "first"])
            self.assertIsNone(queue.review_details(edited)["lookup"])
            self.assertEqual(queue.pending_records(), [])
        state = self.state()
        self.assertEqual(state["records"][old.id]["replacement_id"], edited.id)
        self.assertEqual(state["records"][old.id]["decisions"][-1]["action"], "edited")
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.enqueue("first", RECORD, {"again"}, ["again.pdf"], resolve=True), "unchanged")
            self.assertEqual(self.state()["families"]["first"]["current_id"], edited.id)
            self.assertEqual(queue.enqueue("new-alias", changed, {"b"}, ["b.pdf"], resolve=True), "review")
            self.assertEqual(queue.pending_records(), [])

    def test_unchanged_edit_keeps_same_item_without_new_decision(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.assertEqual(queue.save_review_edit(item, RECORD), item)
            self.assertNotIn("decisions", self.state()["records"][item.id])

    def test_edit_reopen_finishes_old_file_removal_after_durable_replacement(self):
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.directory) as queue:
            old = self.held(queue)
            with patch.object(queue, "_remove_discarded_file", side_effect=OSError("Stopped before old-file removal.")):
                with self.assertRaises(OSError):
                    queue.save_review_edit(old, changed)
            state = self.state()
            replacement_id = state["records"][old.id]["replacement_id"]
            self.assertTrue(old.path.is_file())
            self.assertEqual(state["records"][replacement_id]["status"], "review")
        with RecordQueue(self.directory) as queue:
            self.assertFalse(old.path.exists())
            edited = queue.review_records()[0]
            self.assertEqual(edited.id, replacement_id)
            self.assertEqual(json.loads(edited.path.read_text()), changed)
            self.assertEqual(queue.pending_records(), [])

    def test_edit_collision_with_pending_sent_or_discarded_hash_is_rejected(self):
        changed = {**RECORD, "given_name": "Other"}
        for status in ("pending", "sent", "discarded"):
            with self.subTest(status=status):
                self.directory = Path(self.temporary.name) / status
                with RecordQueue(self.directory) as queue:
                    original = self.held(queue)
                    queue.enqueue("other", changed, {"b"}, ["b.pdf"])
                    other = queue.pending_records()[0]
                    if status == "sent":
                        queue.begin_send(other)
                        queue.finish_send(other, SUCCESS)
                    elif status == "discarded":
                        queue.hold("other", {"b"}, ["b.pdf"], "Do not send.")
                        queue.discard_review(other, "Do not send.")
                    before = self.state()
                    with self.assertRaisesRegex(QueueError, "already exist in queue history"):
                        queue.save_review_edit(original, changed)
                    self.assertEqual(self.state(), before)
                    self.assertTrue(original.path.is_file())

    def test_edit_and_discard_reject_pending_records(self):
        with RecordQueue(self.directory) as queue:
            queue.enqueue("first", RECORD, {"a"}, ["a.pdf"])
            item = queue.pending_records()[0]
            with self.assertRaises(QueueError):
                queue.save_review_edit(item, {**RECORD, "given_name": "Changed"})
            with self.assertRaises(QueueError):
                queue.discard_review(item, "No.")

    def test_fresh_review_lookup_keeps_hold_across_reimport_resolution(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
            self.assertEqual(queue.review_details(item)["lookup"]["outcome"], "not_found")
            self.assertEqual(queue.enqueue("first", RECORD, {"b"}, ["b.pdf"], resolve=True), "review")
            self.assertEqual(queue.pending_records(), [])
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.enqueue("alias", RECORD, {"c"}, ["c.pdf"], resolve=True), "review")

    def test_manual_claim_requires_fresh_same_session_same_destination_lookup(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            with self.assertRaisesRegex(QueueError, "fresh review lookup"):
                queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")
            queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
            with self.assertRaisesRegex(QueueError, "fresh review lookup"):
                queue.begin_review_send(item, config_destination={**CONFIG.destination, "campaign_id": "other"}, reason="Checked.")
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            with self.assertRaisesRegex(QueueError, "fresh review lookup"):
                queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")

    def test_manual_claim_has_durable_audit_and_uses_existing_success_transition(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            entry = self.state()["records"][item.id]
            self.assertEqual(entry["status"], "sending")
            self.assertEqual(entry["decisions"][-1]["action"], "approved_send")
            self.assertEqual(entry["decisions"][-1]["destination"], CONFIG.destination)
            self.assertNotIn(CONFIG.api_key, json.dumps(entry))
            self.assertNotIn(RECORD["email"], json.dumps(entry))
            queue.finish_send(item, SUCCESS)
            self.assertEqual(queue.status_for("first"), "sent")
            self.assertEqual(queue.review_records(), [])
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.enqueue("alias", RECORD, {"b"}, ["b.pdf"], resolve=True), "already_sent")

    def test_manual_claim_revokes_alternatives_and_blocks_second_related_send(self):
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.enqueue("first", changed, {"b"}, ["b.pdf"], resolve=True)
            self.approve(queue, item)
            self.assertEqual(queue.pending_records(), [])
            queue.finish_send(item, SUCCESS)
            other = queue.review_records()[0]
            self.assertTrue(queue.review_details(other)["related_sent"])
            queue.record_review_lookup(other, CLEAR.as_history(CONFIG))
            with self.assertRaisesRegex(QueueError, "already sent"):
                queue.begin_review_send(other, config_destination=CONFIG.destination, reason="Checked.")

    def test_manual_claim_rejects_another_related_send_still_in_progress(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.enqueue("first", {**RECORD, "phone": "12025550199"}, {"b"}, ["b.pdf"])
            self.approve(queue, item)
            other = queue.review_records()[0]
            queue.record_review_lookup(other, CLEAR.as_history(CONFIG))
            with self.assertRaisesRegex(QueueError, "still in progress"):
                queue.begin_review_send(other, config_destination=CONFIG.destination, reason="Checked.")

    def test_uncertain_send_requires_new_checked_manual_claim_after_reopen(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertTrue(queue.review_details(item)["related_uncertain"])
            with self.assertRaises(QueueError):
                queue.begin_send(item)
            with self.assertRaises(QueueError):
                queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")
            self.approve(queue, item)
            queue.mark_uncertain(item, "Invented timeout.")
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertEqual(sum(decision["action"] == "approved_send"
                                 for decision in self.state()["records"][item.id]["decisions"]), 2)

    def test_uncertain_lookup_rejects_changed_destination_without_replacing_saved_evidence(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.mark_uncertain(item, "Invented timeout.")
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            before = self.state()
            for key, value in (("campaign_id", "other-campaign"), ("subdomain", "other-organization")):
                changed = CLEAR.as_history(CONFIG)
                changed["destination"][key] = value
                with self.subTest(key=key), self.assertRaisesRegex(QueueError, "saved submission lookup"):
                    queue.record_review_lookup(item, changed)
                self.assertEqual(self.state(), before)
                self.assertEqual(queue.review_details(item)["lookup"], before["records"][item.id]["lookup"])
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertEqual(queue.review_details(item)["lookup"], before["records"][item.id]["lookup"])

    def test_uncertain_lookup_accepts_updated_evidence_in_same_destination(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.mark_uncertain(item, "Invented timeout.")
            refreshed = CLEAR.as_history(CONFIG)
            refreshed["reason"] = "Fresh contact check completed after the unconfirmed attempt."
            queue.record_review_lookup(item, refreshed)
            self.assertEqual(queue.review_details(item)["lookup"], refreshed)
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.review_details(queue.review_records()[0])["lookup"], refreshed)

    def test_unattempted_review_lookup_can_change_destination(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
            changed = CLEAR.as_history(CONFIG)
            changed["destination"]["campaign_id"] = "other-campaign"
            queue.record_review_lookup(item, changed)
            self.assertEqual(queue.review_details(item)["lookup"], changed)
            self.assertEqual(queue.review_details(item)["status"], "review")

    def test_uncertainty_survives_edit_of_prior_attempt(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.mark_uncertain(item, "Invented timeout.")
            edited = queue.save_review_edit(item, {**RECORD, "phone": "12025550199"})
            self.assertTrue(queue.review_details(edited)["related_uncertain"])
            self.assertFalse(queue.review_details(edited)["related_sent"])
            self.approve(queue, edited)
            queue.finish_send(edited, SUCCESS)

    def test_uncertainty_survives_discard_of_related_prior_attempt(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.mark_uncertain(item, "Invented timeout.")
            queue.enqueue("first", {**RECORD, "phone": "12025550199"}, {"b"}, ["b.pdf"])
            queue.discard_review(item, "Discard obsolete contact details; remote result must still be checked.")
            alternate = queue.review_records()[0]
            self.assertTrue(queue.review_details(alternate)["related_uncertain"])
            self.assertEqual(queue.pending_records(), [])

    def test_success_receipt_still_blocks_manual_send_after_uncertain_bookkeeping(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.finish_send(item, SUCCESS)
            queue.mark_uncertain(item, "Invented bookkeeping error after success.")
            self.assertTrue(queue.review_details(item)["related_sent"])
            queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
            with self.assertRaisesRegex(QueueError, "already sent"):
                queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")

    def test_version_two_history_migrates_without_changing_record_status(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
        state = self.state()
        state["version"] = 2
        (self.directory / STATE_NAME).write_text(json.dumps(state))
        with RecordQueue(self.directory) as queue:
            self.assertEqual([entry.id for entry in queue.review_records()], [item.id])
        self.assertEqual(self.state()["version"], 4)

    def claimed_with_receipt(self, queue):
        queue.enqueue("first", RECORD, {"first-source"}, ["first.pdf"])
        item = queue.pending_records()[0]
        queue.record_lookup(item, CLEAR.as_history(CONFIG))
        queue.begin_send(item)
        queue.record_person_receipt(item, SUCCESS, destination=CONFIG.destination)
        return item

    def test_person_receipt_is_durable_detached_and_not_completed_or_private_response_data(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            receipt = {"person": {"identifiers": [PERSON_ID, "custom:private-response-value"],
                                  "browser_url": "https://example.test/private-response-value",
                                  "email": "private-response-value@example.test"},
                       "unexpected": "private-response-value"}
            destination = CONFIG.destination
            queue.record_person_receipt(item, receipt, destination=destination)
            destination["campaign_id"] = "mutated"
            receipt["person"]["identifiers"][0] = OTHER_PERSON_ID
            saved = queue.pending_person_receipt(item)
            self.assertEqual(saved, {"identifiers": [PERSON_ID], "destination": CONFIG.destination})
            saved["identifiers"][0] = OTHER_PERSON_ID
            saved["destination"]["campaign_id"] = "mutated"
            details = queue.review_details(item)
            self.assertEqual(details["status"], "sending")
            self.assertTrue(details["related_created"])
            self.assertFalse(details["related_sent"])
            self.assertEqual(details["pending_receipt"]["identifiers"], [PERSON_ID])
            stored = self.state()["records"][item.id]
            self.assertNotIn("result", stored)
            self.assertNotIn("private-response-value", json.dumps(stored))
            self.assertNotIn(CONFIG.api_key, json.dumps(stored))
        # A process stopping immediately after the receipt save cannot re-send it.
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertEqual(queue.pending_person_receipt(item),
                             {"identifiers": [PERSON_ID], "destination": CONFIG.destination})
            self.assertEqual(queue.pending_records(), [])

    def test_receipt_save_requires_claim_matching_saved_destination_and_valid_single_native_id(self):
        with RecordQueue(self.directory) as queue:
            queue.enqueue("first", RECORD, {"source"}, ["first.pdf"])
            item = queue.pending_records()[0]
            with self.assertRaisesRegex(QueueError, "claimed"):
                queue.record_person_receipt(item, SUCCESS, destination=CONFIG.destination)
            queue.begin_send(item)
            before = self.state()
            with self.assertRaisesRegex(QueueError, "saved submission lookup"):
                queue.record_person_receipt(item, SUCCESS, destination=CONFIG.destination)
            self.assertEqual(self.state(), before)
        self.directory = Path(self.temporary.name) / "validated"
        with RecordQueue(self.directory) as queue:
            item = self.claimed_with_receipt(queue)
            before = self.state()
            for identifiers in ([], ["custom:no-native-id"], ["action_builder:invalid"],
                                [PERSON_ID, OTHER_PERSON_ID], [None], [OTHER_PERSON_ID]):
                with self.subTest(identifiers=identifiers), self.assertRaises(QueueError):
                    queue.record_person_receipt(item, {"person": {"identifiers": identifiers}},
                                                destination=CONFIG.destination)
                self.assertEqual(self.state(), before)
            with self.assertRaisesRegex(QueueError, "API error"):
                queue.record_person_receipt(item, {**SUCCESS, "errors": []}, destination=CONFIG.destination)
            for key in ("subdomain", "campaign_id"):
                with self.subTest(key=key), self.assertRaisesRegex(QueueError, "saved submission lookup"):
                    queue.record_person_receipt(item, SUCCESS, destination={**CONFIG.destination, key: "other"})
                self.assertEqual(self.state(), before)

    def test_known_person_verification_requires_same_id_destination_and_uncertain_status(self):
        with RecordQueue(self.directory) as queue:
            item = self.claimed_with_receipt(queue)
            with self.assertRaisesRegex(QueueError, "uncertain"):
                queue.finish_person_verification(item, destination=CONFIG.destination,
                                                 identifiers=[PERSON_ID], reason="Verified member details.")
            queue.mark_uncertain(item, "Member checks did not finish.")
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            before = self.state()
            for destination, identifiers in (({**CONFIG.destination, "campaign_id": "other"}, [PERSON_ID]),
                                             (CONFIG.destination, [OTHER_PERSON_ID]),
                                             (CONFIG.destination, [PERSON_ID, PERSON_ID])):
                with self.subTest(destination=destination, identifiers=identifiers), self.assertRaisesRegex(QueueError, "saved person receipt"):
                    queue.finish_person_verification(item, destination=destination,
                                                     identifiers=identifiers, reason="Verified member details.")
                self.assertEqual(self.state(), before)
            # The pre-send lookup was not_found; completing a known ID needs no new lookup.
            self.assertEqual(queue.review_details(item)["lookup"]["outcome"], "not_found")
            queue.finish_person_verification(item, destination=CONFIG.destination,
                                             identifiers=[PERSON_ID], reason="Verified both tags and assessment.")
            self.assertIsNone(queue.pending_person_receipt(item))
            self.assertEqual(queue.review_details(item)["status"], "sent")
            self.assertEqual(self.state()["records"][item.id]["result"], {"identifiers": [PERSON_ID]})
            self.assertEqual(self.state()["records"][item.id]["decisions"][-1]["action"], "checked")
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.review_records(), [])
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.status_for("first"), "sent")

    def test_finish_send_rejects_other_id_without_losing_pending_receipt(self):
        with RecordQueue(self.directory) as queue:
            item = self.claimed_with_receipt(queue)
            before = self.state()
            with self.assertRaisesRegex(QueueError, "saved person receipt"):
                queue.finish_send(item, {"person": {"identifiers": [OTHER_PERSON_ID]}})
            self.assertEqual(self.state(), before)
            queue.finish_send(item, SUCCESS)
            self.assertIsNone(queue.pending_person_receipt(item))
            self.assertEqual(queue.review_details(item)["status"], "sent")

    def test_receipt_persisted_before_interrupted_bookkeeping_survives_reopen(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)

            original_reconcile = queue._reconcile_records

            def interrupt_after_save(*args, **kwargs):
                stored = self.state()["records"][item.id]
                if "pending_receipt" not in stored:
                    return original_reconcile(*args, **kwargs)
                self.assertEqual(stored["status"], "sending")
                self.assertEqual(stored["pending_receipt"]["identifiers"], [PERSON_ID])
                self.assertNotIn("result", stored)
                raise OSError("Invented interruption after receipt was saved.")

            with patch.object(queue, "_reconcile_records", side_effect=interrupt_after_save):
                with self.assertRaises(OSError):
                    queue.record_person_receipt(item, SUCCESS, destination=CONFIG.destination)
            with self.assertRaisesRegex(QueueError, "Close and reopen"):
                queue.finish_send(item, SUCCESS)
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertEqual(queue.pending_person_receipt(item)["identifiers"], [PERSON_ID])
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertFalse(queue.review_details(item)["related_sent"])
            self.assertEqual(queue.pending_records(), [])

    def test_failed_receipt_write_never_invents_durable_creation_evidence(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            with patch.object(queue, "_write_json", side_effect=OSError("Invented disk failure.")):
                with self.assertRaises(OSError):
                    queue.record_person_receipt(item, SUCCESS, destination=CONFIG.destination)
            self.assertNotIn("pending_receipt", self.state()["records"][item.id])
            with self.assertRaisesRegex(QueueError, "Close and reopen"):
                queue.pending_person_receipt(item)
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertIsNone(queue.pending_person_receipt(item))
            self.assertEqual(queue.review_details(item)["status"], "uncertain")
            self.assertEqual(queue.pending_records(), [])

    def test_completed_known_person_verification_survives_interrupted_move(self):
        with RecordQueue(self.directory) as queue:
            item = self.claimed_with_receipt(queue)
            queue.mark_uncertain(item, "Member verification stopped.")
            with patch.object(queue, "_move_record", side_effect=OSError("Invented move interruption.")):
                with self.assertRaises(OSError):
                    queue.finish_person_verification(item, destination=CONFIG.destination,
                                                     identifiers=[PERSON_ID], reason="Verified both tags and assessment.")
            stored = self.state()["records"][item.id]
            self.assertEqual(stored["status"], "sent")
            self.assertNotIn("pending_receipt", stored)
            self.assertEqual(stored["result"]["identifiers"], [PERSON_ID])
        with RecordQueue(self.directory) as queue:
            self.assertEqual(queue.status_for("first"), "sent")
            self.assertEqual(queue.review_records(), [])
            self.assertTrue((self.directory / "sent" / item.path.name).is_file())

    def test_pending_receipt_blocks_another_manual_create_after_discard_or_reimport(self):
        for action in ("same", "discard"):
            with self.subTest(action=action):
                self.directory = Path(self.temporary.name) / action
                with RecordQueue(self.directory) as queue:
                    item = self.claimed_with_receipt(queue)
                    queue.mark_uncertain(item, "Member verification stopped.")
                    original_id = item.id
                    changed = {**RECORD, "phone": "12025550199"}
                    if action == "discard":
                        queue.enqueue("first", changed, {"changed"}, ["changed.pdf"])
                        queue.discard_review(item, "Remove obsolete paperwork, retain the receipt.")
                        item = queue.review_records()[0]
                    self.assertTrue(queue.review_details(item)["related_created"])
                    self.assertFalse(queue.review_details(item)["related_sent"])
                    queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
                    with self.assertRaisesRegex(QueueError, "confirmed person receipt"):
                        queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")
                with RecordQueue(self.directory) as queue:
                    item = queue.review_records()[0]
                    self.assertTrue(queue.review_details(item)["related_created"])
                    self.assertEqual(self.state()["records"][original_id]["pending_receipt"]["identifiers"], [PERSON_ID])
                    self.assertEqual(queue.enqueue("first", RECORD if action == "same" else changed,
                                                   {"again"}, ["again.pdf"], resolve=True), "review")
                    self.assertEqual(queue.pending_records(), [])
                    queue.record_review_lookup(item, CLEAR.as_history(CONFIG))
                    with self.assertRaisesRegex(QueueError, "confirmed person receipt"):
                        queue.begin_review_send(item, config_destination=CONFIG.destination, reason="Checked.")

    def test_pending_receipt_blocks_edit_of_original_and_related_records_without_mutation(self):
        with RecordQueue(self.directory) as queue:
            original = self.claimed_with_receipt(queue)
            queue.mark_uncertain(original, "Member verification stopped.")
            changed = {**RECORD, "phone": "12025550199"}
            queue.enqueue("first", changed, {"changed"}, ["changed.pdf"])
            for item in queue.review_records():
                before = self.state()
                with self.subTest(item=item.id), self.assertRaisesRegex(QueueError, "Verify the created person"):
                    queue.save_review_edit(item, {**RECORD, "given_name": "Edited"})
                self.assertEqual(self.state(), before)
                self.assertTrue(item.path.is_file())
            self.assertEqual(queue.pending_person_receipt(original)["identifiers"], [PERSON_ID])

    def test_historical_receiptless_reconciliation_still_works_but_known_receipt_cannot_change_id(self):
        exact = LookupResult("existing", "One exact matching person.", [{
            "identifiers": [PERSON_ID], "matching_fields": ["email", "phone"], "differing_fields": []}])
        for with_receipt in (False, True):
            with self.subTest(with_receipt=with_receipt):
                self.directory = Path(self.temporary.name) / str(with_receipt)
                with RecordQueue(self.directory) as queue:
                    if with_receipt:
                        item = self.claimed_with_receipt(queue)
                    else:
                        item = self.held(queue)
                        self.approve(queue, item)
                    queue.mark_uncertain(item, "Interrupted verification.")
                    if with_receipt:
                        wrong = json.loads(json.dumps(exact.as_history(CONFIG)))
                        wrong["candidates"][0]["identifiers"] = [OTHER_PERSON_ID]
                        before = self.state()
                        with self.assertRaisesRegex(QueueError, "saved person receipt"):
                            queue.reconcile_sent(item, lookup=wrong, reason="Verified exact contact.")
                        self.assertEqual(self.state(), before)
                    else:
                        with self.assertRaisesRegex(QueueError, "saved person receipt"):
                            queue.finish_person_verification(item, destination=CONFIG.destination,
                                                             identifiers=[PERSON_ID], reason="Checked.")
                    queue.reconcile_sent(item, lookup=exact.as_history(CONFIG), reason="Verified exact contact.")
                    self.assertIsNone(queue.pending_person_receipt(item))
                    self.assertEqual(queue.review_details(item)["status"], "sent")

    def test_version_three_history_migrates_without_inventing_person_receipts(self):
        with RecordQueue(self.directory) as queue:
            item = self.held(queue)
            self.approve(queue, item)
            queue.mark_uncertain(item, "Old interrupted attempt.")
        state = self.state()
        state["version"] = 3
        (self.directory / STATE_NAME).write_text(json.dumps(state))
        with RecordQueue(self.directory) as queue:
            item = queue.review_records()[0]
            self.assertIsNone(queue.pending_person_receipt(item))
            self.assertFalse(queue.review_details(item)["related_created"])
        self.assertEqual(self.state()["version"], 4)
        self.assertEqual(self.state()["records"], state["records"])

    def test_malformed_pending_receipt_is_rejected_before_recovery_or_moves(self):
        mutations = [
            lambda state, entry: state.update(version=3),
            lambda state, entry: entry["pending_receipt"].update(token="invented"),
            lambda state, entry: entry["pending_receipt"].update(identifiers=["action_builder:invalid"]),
            lambda state, entry: entry["pending_receipt"].update(identifiers=[PERSON_ID, OTHER_PERSON_ID]),
            lambda state, entry: entry["pending_receipt"]["destination"].update(campaign_id="other"),
            lambda state, entry: entry.update(status="sent"),
            lambda state, entry: entry.update(result={"identifiers": [PERSON_ID]}),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                self.directory = Path(self.temporary.name) / f"bad-receipt-{number}"
                with RecordQueue(self.directory) as queue:
                    item = self.claimed_with_receipt(queue)
                state = self.state()
                mutate(state, state["records"][item.id])
                (self.directory / STATE_NAME).write_text(json.dumps(state))
                with self.assertRaises(QueueError):
                    with RecordQueue(self.directory):
                        pass
                self.assertEqual(self.state(), state)
                self.assertTrue((self.directory / "review" / item.path.name).is_file())

    def test_corrupt_review_decisions_are_rejected_before_deleting_anything(self):
        mutations = [
            lambda entry: entry.update(decisions="not a list"),
            lambda entry: entry.update(status="discarded"),
            lambda entry: entry.update(replacement_id="missing"),
            lambda entry: entry.update(decisions=[{"action": [], "at": "now", "reason": "reason", "previous_status": "review"}]),
            lambda entry: entry.update(decisions=[{"action": "discarded", "at": "now", "reason": "reason", "previous_status": []}]),
            lambda entry: entry.update(decisions=[{"action": "approved_send", "at": "now", "reason": "reason", "previous_status": "review", "destination": {"token": "no"}}]),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                self.directory = Path(self.temporary.name) / f"corrupt-{number}"
                with RecordQueue(self.directory) as queue:
                    item = self.held(queue)
                state = self.state()
                mutate(state["records"][item.id])
                (self.directory / STATE_NAME).write_text(json.dumps(state))
                with self.assertRaises(QueueError):
                    with RecordQueue(self.directory):
                        pass
                self.assertTrue(item.path.is_file())


if __name__ == "__main__":
    unittest.main()
