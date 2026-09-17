"""Check Downloads discovery with invented filenames and temporary files.

These tests do not read the real Downloads folder or contact Action Builder.
The finder examines names and file bytes; the extractor validates PDF contents.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pdf_downloads_finder import (
    DEFAULT_DOWNLOADS,
    DEFAULT_MARKER,
    discover_pdfs,
)


class DownloadsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.downloads = Path(self.temporary.name)

    def write_download(self, filename, data=b"invented PDF contents", *, age=60):
        """Old timestamps represent downloads that have finished writing."""
        path = self.downloads / filename
        path.write_bytes(data)
        timestamp = time.time() - age
        os.utime(path, (timestamp, timestamp))
        return path

    def scan(self, **options):
        return discover_pdfs(self.downloads, **options)

    def names(self, result):
        return {
            item.path.name
            for group in result.groups
            for item in group.files
        }

    def test_defaults_use_the_current_users_downloads_folder(self):
        self.assertEqual(DEFAULT_DOWNLOADS, Path.home() / "Downloads")
        self.assertEqual(DEFAULT_MARKER, "ORGANIZED")

    def test_only_complete_matching_pdf_names_are_discovered(self):
        expected = {
            "Mo-Smit-Alex-Taylor-ORGANIZED.pdf",
            "Jo-Lane-ORGANIZED.PDF",
            "Sam-Test_organized.pdf",
            "Robin Test ORGANIZED.pdf",
        }
        ignored = {
            "ordinary-document.pdf",
            "Mo-Smit-UNORGANIZED.pdf",
            "Mo-Smit-ORGANIZED-notes.pdf",
            "Mo-Smit-ORGANIZED.pdf.txt",
            ".Mo-Smit-ORGANIZED.pdf",
            "Mo-Smit-ORGANIZED.pdf.crdownload",
        }
        for filename in expected | ignored:
            self.write_download(filename)
        result = self.scan()
        self.assertEqual(self.names(result), expected)
        self.assertFalse(result.errors)

    def test_custom_marker_can_match_an_uppercase_organizer_name(self):
        accepted = self.write_download("Mo-Smit-NATE-CORDER.pdf")
        self.write_download("Mo-Smit-ORGANIZED.pdf")
        self.write_download("Mo-Smit-NATE-CORDER-notes.pdf")
        result = self.scan(marker="NATE-CORDER")
        self.assertEqual(self.names(result), {accepted.name})

    def test_subdirectories_and_symlinks_are_not_imported(self):
        nested = self.downloads / "Nested-ORGANIZED.pdf"
        nested.mkdir()
        (nested / "Mo-Smit-ORGANIZED.pdf").write_bytes(b"nested file")
        source = self.write_download("ordinary-document.pdf")
        (self.downloads / "Linked-ORGANIZED.pdf").symlink_to(source)
        self.assertFalse(self.scan().groups)

    def test_original_and_numbered_downloads_share_one_family(self):
        names = {
            "Mo-Smit-Alex-Taylor-ORGANIZED.pdf",
            "Mo-Smit-Alex-Taylor-ORGANIZED (1).pdf",
            "Mo-Smit-Alex-Taylor-ORGANIZED (2).pdf",
            "Mo-Smit-Alex-Taylor-ORGANIZED (2) (1).pdf",
        }
        for filename in names:
            self.write_download(filename)
        result = self.scan()
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].family, "mo-smit-alex-taylor-organized")
        self.assertEqual(self.names(result), names)
        # Copies are kept for comparison; discovery must not silently pick one.
        self.assertEqual(len(result.groups[0].files), 4)

    def test_case_variations_use_the_same_family(self):
        self.write_download("Mo-Smit-ORGANIZED.pdf")
        self.write_download("mo-smit-organized (1).PDF")
        result = self.scan()
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].files), 2)

    def test_abbreviated_person_name_alone_does_not_merge_families(self):
        self.write_download("Mo-Smit-Alex-Taylor-ORGANIZED.pdf")
        self.write_download("Mo-Smit-Jordan-Reed-ORGANIZED.pdf")
        result = self.scan()
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(
            {group.family for group in result.groups},
            {"mo-smit-alex-taylor-organized", "mo-smit-jordan-reed-organized"},
        )

    def test_fingerprints_distinguish_redownloads_from_changed_paperwork(self):
        original = self.write_download("Mo-Smit-ORGANIZED.pdf", b"first version")
        duplicate = self.write_download("Mo-Smit-ORGANIZED (1).pdf", b"first version")
        corrected = self.write_download("Mo-Smit-ORGANIZED (2).pdf", b"corrected version")
        result = self.scan()
        by_name = {item.path.name: item for item in result.groups[0].files}
        expected_hash = hashlib.sha256(b"first version").hexdigest()
        self.assertEqual(by_name[original.name].sha256, expected_hash)
        self.assertEqual(by_name[duplicate.name].sha256, expected_hash)
        self.assertNotEqual(by_name[corrected.name].sha256, expected_hash)
        self.assertEqual(by_name[original.name].mtime_ns, original.stat().st_mtime_ns)
        self.assertEqual(by_name[duplicate.name].copy_number, 1)
        self.assertEqual(by_name[corrected.name].copy_number, 2)

    def test_files_are_ordered_by_time_then_browser_copy_number(self):
        paths = [
            self.write_download("Mo-Smit-ORGANIZED (2).pdf"),
            self.write_download("Mo-Smit-ORGANIZED.pdf"),
            self.write_download("Mo-Smit-ORGANIZED (1).pdf"),
        ]
        timestamp = time.time() - 120
        for path in paths:
            os.utime(path, (timestamp, timestamp))
        result = self.scan()
        self.assertEqual([item.copy_number for item in result.groups[0].files], [0, 1, 2])
        # A newer original still sorts after an older browser copy.
        newer = time.time() - 30
        os.utime(paths[1], (newer, newer))
        result = self.scan()
        self.assertEqual([item.copy_number for item in result.groups[0].files], [1, 2, 0])

    def test_recent_corrected_copy_defers_its_entire_family(self):
        self.write_download("Mo-Smit-ORGANIZED.pdf")
        self.write_download("Mo-Smit-ORGANIZED (1).pdf", age=0)
        unrelated = self.write_download("Jo-Lane-ORGANIZED.pdf")
        result = self.scan()
        self.assertEqual(self.names(result), {unrelated.name})
        self.assertIn("mo-smit-organized", result.deferred)
        # The old original must not be queued while a replacement is arriving.
        self.assertNotIn("mo-smit-organized", {group.family for group in result.groups})

    def test_empty_or_future_dated_copy_defers_the_entire_family(self):
        for suffix, data, age in [("empty", b"", 60), ("future", b"new content", -60)]:
            with self.subTest(case=suffix):
                base = f"Person-{suffix}-ORGANIZED"
                self.write_download(f"{base}.pdf")
                self.write_download(f"{base} (1).pdf", data, age=age)
                result = self.scan()
                self.assertIn(base.casefold(), result.deferred)
                self.assertNotIn(base.casefold(), {group.family for group in result.groups})

    def test_partial_download_companion_defers_original_family(self):
        for extension in ["crdownload", "part", "download"]:
            with self.subTest(extension=extension):
                base = f"Person-{extension}-ORGANIZED"
                self.write_download(f"{base}.pdf")
                self.write_download(f"{base} (1).pdf.{extension}")
                result = self.scan()
                self.assertIn(base.casefold(), result.deferred)
                self.assertNotIn(base.casefold(), {group.family for group in result.groups})

    def test_unrelated_partial_download_does_not_block_a_ready_person(self):
        ready = self.write_download("Mo-Smit-ORGANIZED.pdf")
        self.write_download("ordinary-document.pdf.crdownload", age=0)
        self.assertEqual(self.names(self.scan()), {ready.name})

    def test_configured_stability_interval_is_respected(self):
        path = self.write_download("Mo-Smit-ORGANIZED.pdf", age=30)
        self.assertEqual(self.names(self.scan(min_age_seconds=5)), {path.name})
        result = self.scan(min_age_seconds=120)
        self.assertFalse(result.groups)
        self.assertIn("mo-smit-organized", result.deferred)

    def test_discovery_leaves_original_downloads_untouched(self):
        self.write_download("Mo-Smit-ORGANIZED.pdf", b"first version")
        self.write_download("Mo-Smit-ORGANIZED (1).pdf", b"revised version")
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.downloads.iterdir()
        }
        self.scan()
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.downloads.iterdir()
        }
        self.assertEqual(after, before)

    def test_copy_changed_during_hashing_defers_its_entire_family(self):
        original = self.write_download("Mo-Smit-ORGANIZED.pdf", b"original")
        corrected = self.write_download("Mo-Smit-ORGANIZED (1).pdf", b"replacement")
        real_sha256 = hashlib.sha256
        changed = False

        def digest_that_simulates_an_active_browser():
            # Compute real hashes, but append bytes while the copy is being read.
            digest = real_sha256()
            wrapper = Mock(wraps=digest)

            def update(data):
                nonlocal changed
                if data == b"replacement" and not changed:
                    changed = True
                    with corrected.open("ab") as unfinished_download:
                        unfinished_download.write(b" still downloading")
                digest.update(data)

            wrapper.update.side_effect = update
            return wrapper

        with patch("pdf_downloads_finder.hashlib.sha256", digest_that_simulates_an_active_browser):
            result = self.scan()

        self.assertTrue(changed, "The fixture must actually change the file during hashing.")
        self.assertFalse(result.groups)
        self.assertEqual(set(result.deferred["mo-smit-organized"]), {original, corrected})
        self.assertFalse(result.errors)

    def test_missing_downloads_folder_is_reported(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            discover_pdfs(self.downloads / "does-not-exist")


if __name__ == "__main__":
    unittest.main()
