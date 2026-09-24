"""The county reference is local, advisory, and independent of person submission."""
import unittest

from jurisdiction_reference import MAP_URL, SOURCE_URL, jurisdiction_reference


class JurisdictionReferenceTests(unittest.TestCase):
    def test_full_counties_accept_case_spacing_suffix_and_ohio(self):
        for county in ("Coshocton", "GUERNSEY COUNTY", "  licking  County ", "Muskingum", "Perry"):
            with self.subTest(county=county):
                result = jurisdiction_reference(county, " Ohio ", " 1105 ")
                self.assertEqual(result["status"], "inside")
                self.assertEqual(result["note"], "")
                self.assertEqual(result["scope"], "Inside construction")
                self.assertEqual(result["source_url"], SOURCE_URL)
                self.assertEqual(result["map_url"], MAP_URL)

    def test_partial_counties_always_require_address_review(self):
        for county in ("Knox", "tuscarawas COUNTY"):
            with self.subTest(county=county):
                result = jurisdiction_reference(county, "OH", "1105")
                self.assertEqual(result["status"], "review")
                self.assertEqual(result["note"], "")
                self.assertIn("southern portions", result["summary"])
                self.assertIn("address", result["summary"])

    def test_unconfigured_local_does_not_use_1105_profile_or_sources(self):
        for local in ("", "999", "01105", "Local 1105", None):
            with self.subTest(local=local):
                result = jurisdiction_reference("Licking", "OH", local)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["note"], "")
                self.assertEqual(result["source_url"], "https://ibew.org/inside-jurisdictional-maps/")
                self.assertEqual(result["map_url"], "https://ibew.org/inside-jurisdictional-maps/")
                self.assertNotIn("1105", result["summary"])

    def test_state_disambiguates_matching_county_name(self):
        for county in ("Licking", "Knox"):
            with self.subTest(county=county):
                result = jurisdiction_reference(county, " mo ", "1105")
                self.assertEqual(result["status"], "outside")
                self.assertIn(f"{county} County, MO", result["note"])
                self.assertIn("Local 1105", result["note"])

    def test_unlisted_ohio_county_has_copyable_advisory_note(self):
        result = jurisdiction_reference("Franklin County", "OH", "1105")
        self.assertEqual(result["status"], "outside")
        self.assertTrue(result["summary"].startswith("Outside by county reference"))
        self.assertIn("Franklin County, OH", result["note"])
        self.assertNotIn("County County", result["note"])
        self.assertIn("Local 1105", result["note"])
        self.assertIn("inside-construction", result["note"])
        self.assertIn("rough reference", result["note"])
        self.assertIn("has not been verified", result["note"])

    def test_missing_invalid_or_placeholder_county_never_means_outside(self):
        for county in ("", "  ", None, "County", "Unknown", "N/A", "none", "not provided", "123", "Franklin, Ohio", "<script>"):
            with self.subTest(county=county):
                result = jurisdiction_reference(county, "OH", "1105")
                self.assertEqual(result["status"], "review")
                self.assertEqual(result["note"], "")

    def test_missing_or_invalid_state_never_means_outside(self):
        for region in ("", None, "XX", "123", "O", "OH, WV", "AE"):
            with self.subTest(region=region):
                result = jurisdiction_reference("Licking", region, "1105")
                self.assertEqual(result["status"], "review")
                self.assertEqual(result["note"], "")

    def test_sensible_county_punctuation_and_unicode_are_allowed(self):
        for county, state in (("St. Louis", "MO"), ("O'Brien", "IA"), ("Doña Ana", "NM")):
            with self.subTest(county=county):
                result = jurisdiction_reference(county, state, "1105")
                self.assertEqual(result["status"], "outside")
                self.assertIn(county, result["note"])


if __name__ == "__main__":
    unittest.main()
