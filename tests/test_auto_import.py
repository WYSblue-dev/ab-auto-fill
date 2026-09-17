"""Exercise Downloads -> validated PDF -> durable queue using invented people.

Every download, queue, and CLI output stays inside a temporary directory.
No test calls Action Builder or reads the real Downloads folder.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import extract_person
from record_queue import QueueError, RecordQueue
import test_extract_person as fixture


EXPECTED = fixture.EXPECTED


class AutomaticImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.downloads = self.folder / "Downloads"
        self.downloads.mkdir()
        self.queue_dir = self.folder / "senders_pdfs"

    def write_download(self, name, *, replace=None, omit=(), extra_page=False, age=60):
        # Reuse the real synthetic template rather than mocking successful parsing.
        builder = fixture.ChecklistExtractionTests()
        builder.pdf_path = self.downloads / name
        pages = [fixture.checklist(omit=omit, replace=replace)]
        if extra_page:
            pages.append([(72, 720, "Unrelated paperwork makes these PDF bytes different.")])
        path = builder.write_pdf(pages)
        self.age_file(path, age)
        return path

    @staticmethod
    def age_file(path, age):
        timestamp = time.time() - age
        os.utime(path, (timestamp, timestamp))

    def import_downloads(self, **options):
        return extract_person.import_downloads(self.downloads, self.queue_dir, **options)

    def queued_records(self):
        with RecordQueue(self.queue_dir) as queue:
            return [json.loads(item.path.read_text(encoding="utf-8"))
                    for item in queue.pending_records()]

    def run_cli(self, *arguments):
        script = Path(extract_person.__file__).resolve()
        return subprocess.run(
            [sys.executable, "-B", str(script), *map(str, arguments),
             "--downloads-dir", str(self.downloads),
             "--queue-dir", str(self.queue_dir), "--min-age-seconds", "0"],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_one_valid_pdf_enters_queue_and_second_scan_is_unchanged(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        first = self.import_downloads()
        self.assertEqual((first.queued, first.review, first.deferred), (1, 0, 0))
        self.assertEqual(self.queued_records(), [EXPECTED])
        second = self.import_downloads()
        self.assertEqual((second.queued, second.unchanged), (0, 1))
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_identical_browser_copies_make_one_record(self):
        original = self.write_download("Morgan-Example-ORGANIZED.pdf")
        copy = self.downloads / "Morgan-Example-ORGANIZED (1).pdf"
        copy.write_bytes(original.read_bytes())
        self.age_file(copy, 60)
        result = self.import_downloads()
        self.assertEqual(result.queued, 1)
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_different_pdf_bytes_with_same_contact_values_make_one_record(self):
        original = self.write_download("Morgan-Example-ORGANIZED.pdf")
        copy = self.write_download("Morgan-Example-ORGANIZED (1).pdf", extra_page=True)
        self.assertNotEqual(original.read_bytes(), copy.read_bytes())
        result = self.import_downloads()
        self.assertEqual((result.queued, result.review), (1, 0))
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_changed_contact_values_hold_whole_filename_family(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.write_download("Morgan-Example-ORGANIZED (1).pdf",
                            replace={"phone": "(202) 555-0199"})
        result = self.import_downloads()
        self.assertEqual((result.queued, result.review), (0, 1))
        self.assertEqual(self.queued_records(), [])

    def test_changed_copy_revokes_an_earlier_pending_record(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.assertEqual(self.import_downloads().queued, 1)
        self.write_download("Morgan-Example-ORGANIZED (1).pdf",
                            replace={"phone": "(202) 555-0199"})
        self.assertEqual(self.import_downloads().review, 1)
        self.assertEqual(self.queued_records(), [])

    def test_invalid_sibling_blocks_valid_original(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.write_download("Morgan-Example-ORGANIZED (1).pdf", omit={"given_name"})
        result = self.import_downloads()
        self.assertEqual((result.queued, result.review), (0, 1))
        self.assertEqual(self.queued_records(), [])

    def test_invalid_sibling_revokes_prior_pending_record(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.assertEqual(self.import_downloads().queued, 1)
        self.write_download("Morgan-Example-ORGANIZED (1).pdf", omit={"email"})
        self.assertEqual(self.import_downloads().review, 1)
        self.assertEqual(self.queued_records(), [])

    def test_incomplete_sibling_defers_and_revokes_prior_pending_record(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.assertEqual(self.import_downloads().queued, 1)
        partial = self.downloads / "Morgan-Example-ORGANIZED (1).pdf.crdownload"
        partial.write_bytes(b"Browser is still downloading.")
        result = self.import_downloads()
        self.assertEqual((result.queued, result.deferred), (0, 1))
        self.assertEqual(self.queued_records(), [])

    def test_finished_identical_download_restores_deferred_record(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.assertEqual(self.import_downloads().queued, 1)
        partial = self.downloads / "Morgan-Example-ORGANIZED (1).pdf.crdownload"
        partial.write_bytes(b"Download in progress.")
        self.assertEqual(self.import_downloads().deferred, 1)
        partial.unlink()
        self.write_download("Morgan-Example-ORGANIZED (1).pdf", extra_page=True)
        result = self.import_downloads()
        self.assertEqual((result.queued, result.review, result.deferred), (1, 0, 0))
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_recent_copy_defers_family_without_touching_other_people(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        self.write_download("Morgan-Example-ORGANIZED (1).pdf", age=0)
        self.write_download("Taylor-Example-ORGANIZED.pdf",
                            replace={"given_name": "Taylor", "email": "taylor@example.test"})
        result = self.import_downloads()
        self.assertEqual((result.queued, result.deferred), (1, 1))
        self.assertEqual(self.queued_records()[0]["given_name"], "Taylor")

    def test_bad_family_does_not_stop_unrelated_valid_submission(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf", omit={"email"})
        self.write_download("Taylor-Example-ORGANIZED.pdf",
                            replace={"given_name": "Taylor", "email": "taylor@example.test"})
        result = self.import_downloads()
        self.assertEqual((result.queued, result.review), (1, 1))
        self.assertEqual(self.queued_records()[0]["given_name"], "Taylor")

    def test_marker_filter_and_override_are_respected(self):
        self.write_download("Morgan-Example-CUSTOM.pdf")
        self.assertEqual(self.import_downloads().queued, 0)
        self.assertEqual(self.import_downloads(marker="CUSTOM").queued, 1)

    def test_import_preserves_download_bytes_and_timestamps(self):
        path = self.write_download("Morgan-Example-ORGANIZED.pdf")
        original = (path.read_bytes(), path.stat().st_mtime_ns)
        self.import_downloads()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)

    def test_no_argument_cli_scans_configured_downloads(self):
        self.write_download("Morgan-Example-ORGANIZED.pdf")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_explicit_pdf_without_output_queues_record(self):
        path = self.write_download("Morgan-Example-ORGANIZED.pdf")
        result = self.run_cli(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.queued_records(), [EXPECTED])

    def test_resolve_selects_one_version_and_remembers_all_current_hashes(self):
        original = self.write_download("Morgan-Example-ORGANIZED.pdf")
        corrected = self.write_download("Morgan-Example-ORGANIZED (1).pdf",
                                        replace={"phone": "(202) 555-0199"})
        self.assertEqual(self.import_downloads().review, 1)
        result = self.run_cli(corrected, "--resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.queued_records(), [{**EXPECTED, "phone": "12025550199"}])
        # Old copies still in Downloads must not reopen a resolved conflict.
        self.assertEqual(self.import_downloads().unchanged, 1)
        with RecordQueue(self.queue_dir) as queue:
            hashes = {hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (original, corrected)}
            self.assertTrue(queue.known_sources("morgan-example-organized", hashes))

    def test_selected_import_defers_new_correction_arriving_during_parsing(self):
        selected = self.write_download("Morgan-Example-ORGANIZED.pdf")
        original_extract = extract_person.extract_approved_person

        def correction_arrives(path):
            record = original_extract(path)
            self.write_download("Morgan-Example-ORGANIZED (1).pdf",
                                replace={"phone": "(202) 555-0199"})
            return record

        with patch.object(extract_person, "extract_approved_person", side_effect=correction_arrives):
            result = extract_person.import_selected_pdf(
                selected, self.queue_dir, self.downloads, min_age_seconds=0, resolve=True
            )
        self.assertEqual((result.queued, result.deferred), (0, 1))
        self.assertEqual(self.queued_records(), [])

    def test_selected_recent_or_missing_correction_revokes_earlier_pending(self):
        for state in ("recent", "missing"):
            with self.subTest(state=state):
                self.queue_dir = self.folder / f"queue-{state}"
                stem = f"Morgan-{state}-ORGANIZED"
                original = self.write_download(f"{stem}.pdf")
                extract_person.import_selected_pdf(original, self.queue_dir, self.downloads)
                selected = self.write_download(f"{stem} (1).pdf", age=0)
                if state == "missing":
                    selected.unlink()
                result = extract_person.import_selected_pdf(
                    selected, self.queue_dir, self.downloads, resolve=True
                )
                self.assertEqual(result.deferred, 1)
                self.assertEqual(self.queued_records(), [])

    def test_known_external_pdf_is_rechecked_under_queue_lock(self):
        downloaded = self.write_download("Morgan-Example-ORGANIZED.pdf")
        selected = self.folder / downloaded.name
        downloaded.rename(selected)
        # A selected PDF need not live inside the configured Downloads folder.
        first = extract_person.import_selected_pdf(selected, self.queue_dir, self.downloads)
        self.assertEqual(first.queued, 1)
        original_discover = extract_person.discover_pdfs
        scans = 0

        def correction_arrives_after_snapshot(*args, **kwargs):
            nonlocal scans
            # The sender must be unable to acquire this lock during discovery.
            with self.assertRaises(QueueError):
                with RecordQueue(self.queue_dir):
                    pass
            found = original_discover(*args, **kwargs)
            scans += 1
            if scans == 1:
                self.write_download("Morgan-Example-ORGANIZED (1).pdf",
                                    replace={"phone": "(202) 555-0199"})
            return found

        with patch.object(extract_person, "discover_pdfs", side_effect=correction_arrives_after_snapshot):
            result = extract_person.import_selected_pdf(selected, self.queue_dir, self.downloads)
        self.assertEqual(scans, 2)
        self.assertEqual((result.unchanged, result.deferred), (0, 1))
        self.assertEqual(self.queued_records(), [])


if __name__ == "__main__":
    unittest.main()
