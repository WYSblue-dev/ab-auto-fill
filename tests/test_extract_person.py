"""Regression tests for checklist extraction, using entirely invented answers.

Run from the project folder: .venv/bin/python -m unittest discover -s tests -v
These PDFs exist only inside a temporary directory. No API requests are made.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
)

from extract_person import (
    ExtractionError,
    extract_approved_person,
    normalize_state,
    normalize_us_phone,
    normalize_zip,
    save_approved_person,
)


# Coordinates are PDF points: (0, 0) is the bottom-left of a Letter page.
# Keep these independent of the parser's constants so bad constants fail tests.
LABELS = [
    (207.97, 739.12, "New"),
    (249.98, 739.12, "Member"),
    (324.00, 739.12, "Checklist"),
    (240.98, 715.31, "LPX"),
    (280.99, 715.31, "Data"),
    (325.01, 715.31, "Entry"),
    (72.00, 654.44, "Last"),
    (95.83, 654.44, "Name:________________________________________________"),
    (72.00, 625.34, "First"),
    (96.42, 625.34, "Name:________________________________________________"),
    (432.00, 625.34, "MI:____"),
    (72.00, 596.25, "Address:__________________________________________________"),
    (72.00, 567.16, "City:_____________________________________"),
    (324.00, 567.16, "State:_______"),
    (432.00, 567.16, "Zip:____________"),
    (72.00, 538.07, "Phone:____________________________________________________"),
    (72.00, 363.51, "Email:____________________________________________________"),
]

# Only these nine contact fields should leave the extractor.
ANSWERS = {
    "family_name": (132.00, 657.73, "O'Neil-Smith"),
    "given_name": (133.00, 628.23, "Morgan"),
    "additional_name": (450.00, 627.73, "J"),
    "address_line_1": (117.10, 598.31, "22 Fiction Lane"),
    "locality": (97.66, 568.02, "Example City"),
    "region": (357.60, 567.79, "MA"),
    "postal_code": (452.71, 568.61, "02108"),
    "phone": (111.00, 541.23, "(202) 555-0143"),
    "email": (105.58, 366.14, "Morgan@Example.test"),
}

EXPECTED = {
    "given_name": "Morgan",
    "family_name": "O'Neil-Smith",
    "additional_name": "J",
    "email": "morgan@example.test",
    "phone": "12025550143",
    "address_line_1": "22 Fiction Lane",
    "locality": "Example City",
    "region": "MA",
    "postal_code": "02108",
}


def checklist(*, omit=(), replace=None):
    """Make a fresh fixture; one test cannot accidentally modify another."""
    fragments = list(LABELS)
    for field, (x, y, value) in ANSWERS.items():
        if field not in omit:
            value = (replace or {}).get(field, value)
            fragments.append((x, y, value))
    return fragments


def pdf_string(value):
    """Escape parentheses and backslashes, which have special PDF meanings."""
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class ChecklistExtractionTests(unittest.TestCase):
    def setUp(self):
        # Cleanup runs even when a test assertion fails.
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pdf_path = Path(self.directory.name) / "invented-checklist.pdf"

    def write_pdf(
        self, pages, *, transformed=False, width=612, rotation=0,
        media_box=None, crop_box=None, user_unit=1,
    ):
        """Build tiny text PDFs directly; no extra PDF-writing dependency."""
        writer = PdfWriter()
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                    # StandardEncoding maps ASCII apostrophes to a curly glyph.
                    # Declare the encoding so the literal fixture text is exact.
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
        )
        for fragments in pages:
            page = writer.add_blank_page(width=width, height=792)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            commands = []
            for x, y, value in fragments:
                if transformed:
                    # The real export uses both a page transform and text transform.
                    # The resulting visible location must still equal (x, y).
                    transform = "0.75 0 0 -0.75 72 756 cm"
                    text_matrix = f"1 0 0 -1 {(x - 72) / .75} {(756 - y) / .75} Tm"
                else:
                    transform = ""
                    text_matrix = f"1 0 0 1 {x} {y} Tm"
                commands.append(
                    f"q {transform} BT /F1 11 Tf {text_matrix} "
                    f"({pdf_string(value)}) Tj ET Q"
                )
            stream = DecodedStreamObject()
            # WinAnsi also lets us verify that accented names stay intact.
            stream.set_data("\n".join(commands).encode("cp1252"))
            page[NameObject("/Contents")] = writer._add_object(stream)
            if rotation:
                page.rotate(rotation)
            if media_box is not None:
                page.mediabox = RectangleObject(media_box)
            if crop_box is not None:
                page.cropbox = RectangleObject(crop_box)
            page[NameObject("/UserUnit")] = NumberObject(user_unit)
        with self.pdf_path.open("wb") as output:
            writer.write(output)
        return self.pdf_path

    def test_complete_checklist_returns_only_approved_fields(self):
        fragments = checklist()
        # An unrelated number outside a contact box must never become an answer.
        fragments.append((103, 687, "000-00-0000"))
        result = extract_approved_person(self.write_pdf([fragments]))
        self.assertEqual(result, EXPECTED)

    def test_content_stream_order_does_not_control_field_mapping(self):
        fragments = checklist()
        # A fixed seed gives the same difficult ordering on every test run.
        random.Random(29).shuffle(fragments)
        self.assertEqual(extract_approved_person(self.write_pdf([fragments])), EXPECTED)

    def test_page_and_text_transforms_are_combined(self):
        path = self.write_pdf([checklist()], transformed=True)
        self.assertEqual(extract_approved_person(path), EXPECTED)

    def test_checklist_can_be_second_page(self):
        cover = [(72, 720, "Other paperwork"), (133, 628, "Wrong Person")]
        path = self.write_pdf([cover, checklist()])
        self.assertEqual(extract_approved_person(path), EXPECTED)

    def test_missing_first_name_is_not_replaced_by_middle_initial(self):
        path = self.write_pdf([checklist(omit={"given_name"})])
        with self.assertRaisesRegex(ExtractionError, "(?i)first|given"):
            extract_approved_person(path)

    def test_missing_email_is_not_filled_from_later_page(self):
        later_page = [(105.58, 366.14, "someone-else@example.test")]
        path = self.write_pdf([checklist(omit={"email"}), later_page])
        with self.assertRaisesRegex(ExtractionError, "(?i)email"):
            extract_approved_person(path)

    def test_every_contact_field_is_required_for_this_form(self):
        for field in ANSWERS:
            with self.subTest(field=field):
                path = self.write_pdf([checklist(omit={field})])
                with self.assertRaises(ExtractionError):
                    extract_approved_person(path)

    def test_wrong_template_is_rejected(self):
        # Keeping answers but removing the identifying heading is insufficient.
        fragments = [part for part in checklist() if part[1] < 700]
        with self.assertRaises(ExtractionError):
            extract_approved_person(self.write_pdf([fragments]))

    def test_multiple_checklists_require_review(self):
        with self.assertRaises(ExtractionError):
            extract_approved_person(self.write_pdf([checklist(), checklist()]))

    def test_shifted_label_requires_review(self):
        # The answers alone cannot prove that a changed form still maps correctly.
        fragments = [
            (x, y + 30, value) if value.startswith("Email:") else (x, y, value)
            for x, y, value in checklist()
        ]
        with self.assertRaises(ExtractionError):
            extract_approved_person(self.write_pdf([fragments]))

    def test_wrong_page_size_and_rotation_require_review(self):
        for options in ({"width": 595}, {"rotation": 90}):
            with self.subTest(options=options):
                path = self.write_pdf([checklist()], **options)
                with self.assertRaises(ExtractionError):
                    extract_approved_person(path)

    def test_wrapped_address_is_joined_in_reading_order(self):
        fragments = checklist(omit={"address_line_1"})
        # Store the second line first, as a PDF is permitted to do.
        fragments.extend([(117.1, 588, "Apt 5"), (117.1, 602, "22 Fiction Lane")])
        result = extract_approved_person(self.write_pdf([fragments]))
        self.assertEqual(result, {**EXPECTED, "address_line_1": "22 Fiction Lane Apt 5"})

    def test_state_near_address_boundary_cannot_fill_missing_address(self):
        fragments = checklist(omit={"address_line_1"})
        # The state sits near the shared edge but still belongs only to State.
        fragments = [
            (x, 582, value) if value == "MA" else (x, y, value)
            for x, y, value in fragments
        ]
        path = self.write_pdf([fragments])
        with self.assertRaisesRegex(ExtractionError, "(?i)address.*missing"):
            extract_approved_person(path)

    def test_shifted_page_boxes_and_changed_units_require_review(self):
        options = [
            {"media_box": (10, 10, 622, 802)},
            {"crop_box": (5, 5, 617, 797)},
            {"user_unit": 2},
        ]
        for option in options:
            with self.subTest(option=option):
                path = self.write_pdf([checklist()], **option)
                with self.assertRaises(ExtractionError):
                    extract_approved_person(path)

    def test_accented_names_are_preserved(self):
        entered = {
            "given_name": "Jos\u00e9",
            "family_name": "Pe\u00f1a-Garc\u00eda",
            "additional_name": "\u00c9.",
            "locality": "S\u00e3o Tom\u00e9",
        }
        path = self.write_pdf([checklist(replace=entered)])
        self.assertEqual(extract_approved_person(path), {**EXPECTED, **entered})

    def test_middle_initial_must_be_one_letter(self):
        for invalid_initial in ["JJ", "1", "N/A"]:
            with self.subTest(initial=invalid_initial):
                path = self.write_pdf(
                    [checklist(replace={"additional_name": invalid_initial})]
                )
                with self.assertRaisesRegex(ExtractionError, "Middle Initial"):
                    extract_approved_person(path)

    def test_conflicting_fragments_in_one_field_require_review(self):
        fragments = checklist() + [(200, 628.23, "Different")]
        with self.assertRaises(ExtractionError):
            extract_approved_person(self.write_pdf([fragments]))

    def test_state_full_name_and_leading_zero_zip_survive_extraction(self):
        path = self.write_pdf([checklist(replace={"region": "Massachusetts"})])
        result = extract_approved_person(path)
        self.assertEqual(result["region"], "MA")
        self.assertEqual(result["postal_code"], "02108")

    def test_invalid_contact_values_require_review(self):
        replacements = [
            {"region": "ZZ"},
            {"postal_code": "2108"},
            {"phone": "123"},
            {"email": "not-an-email"},
        ]
        for replacement in replacements:
            with self.subTest(field=next(iter(replacement))):
                path = self.write_pdf([checklist(replace=replacement)])
                with self.assertRaises(ExtractionError):
                    extract_approved_person(path)

    def test_saved_json_contains_the_validated_record(self):
        record = extract_approved_person(self.write_pdf([checklist()]))
        output = Path(self.directory.name) / "approved.json"
        save_approved_person(record, output)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), EXPECTED)

    def run_cli(self, pdf_path, output):
        # Launch the actual command in a temporary working folder.
        # Explicit paths ensure the repository's pending_person.json is untouched.
        script = Path(__file__).resolve().parents[1] / "extract_person.py"
        return subprocess.run(
            [sys.executable, "-B", str(script), str(pdf_path), "--output", str(output)],
            cwd=self.directory.name,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_success_writes_approved_json(self):
        path = self.write_pdf([checklist()])
        output = Path(self.directory.name) / "approved.json"
        result = self.run_cli(path, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), EXPECTED)
        self.assertIn("Nothing was sent", result.stdout)

    def test_cli_failed_extraction_preserves_earlier_output(self):
        output = Path(self.directory.name) / "approved.json"
        original = b'{"previous": "record remains unchanged"}\n'
        output.write_bytes(original)
        path = self.write_pdf([checklist(omit={"given_name"})])
        result = self.run_cli(path, output)
        # A future launcher can use the exit code to stop before submission.
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), original)
        self.assertIn("No new record was written", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("O'Neil-Smith", result.stderr)

    def test_cli_failed_extraction_does_not_create_output(self):
        output = Path(self.directory.name) / "approved.json"
        path = self.write_pdf([checklist(omit={"email"})])
        result = self.run_cli(path, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_cli_cannot_overwrite_input_even_with_json_filename(self):
        original_path = self.write_pdf([checklist()])
        original = original_path.read_bytes()
        for suffix in [".pdf", ".json"]:
            with self.subTest(suffix=suffix):
                # A file extension does not change the PDF's actual contents.
                path = Path(self.directory.name) / f"source{suffix}"
                path.write_bytes(original)
                result = self.run_cli(path, path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), original)

    def test_cli_requires_json_output_suffix(self):
        path = self.write_pdf([checklist()])
        output = Path(self.directory.name) / "notes.txt"
        output.write_text("Keep these notes.", encoding="utf-8")
        result = self.run_cli(path, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "Keep these notes.")


class ContactNormalizationTests(unittest.TestCase):
    def test_supported_state_names_and_abbreviations(self):
        for entered, expected in [(" ma ", "MA"), ("New York", "NY"), ("oregon", "OR")]:
            with self.subTest(entered=entered):
                self.assertEqual(normalize_state(entered), expected)

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_state("ZZ")

    def test_zip_codes_remain_strings(self):
        for value in ["02108", "02108-1234"]:
            self.assertEqual(normalize_zip(value), value)

    def test_phone_accepts_us_country_code(self):
        self.assertEqual(normalize_us_phone("+1 (202) 555-0143"), "12025550143")


if __name__ == "__main__":
    unittest.main()
