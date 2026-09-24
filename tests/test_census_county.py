"""County lookups use invented records and mocked sessions; no network or .env."""

from copy import deepcopy
from datetime import datetime
import importlib
import traceback
import unittest
from unittest.mock import MagicMock, patch

import requests

import census_county as lookup


def person():
    return {
        "first_name": "Invented", "last_name": "Person",
        "email": "invented@example.test", "phone": "12025550123",
        "address_line_1": "123 Fictional Street", "locality": "Example City",
        "region": "OH", "postal_code": "43001", "classification": "CE/CW",
        "api_key": "must-not-leave-the-computer",
    }


def document():
    return {"result": {"addressMatches": [{
        "matchedAddress": "123 FICTIONAL ST, EXAMPLE CITY, OH, 43001",
        "addressComponents": {"state": "OH"},
        "geographies": {"Counties": [{
            "NAME": "Licking County", "STATE": "39", "COUNTY": "089", "GEOID": "39089",
        }]},
    }]}}


class CountyLookupTests(unittest.TestCase):
    def setUp(self):
        factory = patch.object(lookup.requests, "Session")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.session = MagicMock()
        self.factory.return_value.__enter__.return_value = self.session
        self.response = self.session.get.return_value
        self.response.status_code = 200
        self.response.json.return_value = document()

    def test_single_match_returns_county_and_reviewable_address(self):
        result = lookup.county_from_address(person())
        self.assertEqual(result["county"], "Licking County")
        self.assertEqual(result["region"], "OH")
        self.assertEqual(result["source"], "census")
        self.assertEqual(result["matched_address"], "123 FICTIONAL ST, EXAMPLE CITY, OH, 43001")
        self.assertIsNotNone(datetime.fromisoformat(result["checked_at"]).tzinfo)

    def test_request_contains_only_address_fields_and_uses_fixed_destination(self):
        lookup.county_from_address(person())
        self.session.get.assert_called_once_with(
            lookup.ENDPOINT,
            params={
                "street": "123 Fictional Street", "city": "Example City", "state": "OH", "zip": "43001",
                "benchmark": "Public_AR_Current", "vintage": "Current_Current", "layers": "Counties", "format": "json",
            },
            headers={"Accept": "application/json"}, timeout=(5, 30), allow_redirects=False,
        )
        self.assertIs(self.session.trust_env, False)
        self.assertNotIn("must-not-leave", str(self.session.get.call_args))
        self.assertNotIn("invented@example.test", str(self.session.get.call_args))
        self.assertNotIn("CE/CW", str(self.session.get.call_args))
        self.assertNotIn("12025550123", str(self.session.get.call_args))

    def test_unmatched_and_multiple_matches_do_not_choose_county(self):
        for matches in ([], document()["result"]["addressMatches"] * 2):
            with self.subTest(matches=len(matches)):
                self.response.json.return_value = {"result": {"addressMatches": matches}}
                with self.assertRaisesRegex(lookup.CountyLookupError, "unambiguous address"):
                    lookup.county_from_address(person())

    def test_missing_multiple_and_invalid_county_candidates_require_review(self):
        candidates = document()["result"]["addressMatches"][0]["geographies"]["Counties"]
        for counties in (None, [], candidates * 2, [None], {}):
            with self.subTest(counties=counties):
                result = document()
                result["result"]["addressMatches"][0]["geographies"]["Counties"] = counties
                self.response.json.return_value = result
                with self.assertRaises(lookup.CountyLookupError):
                    lookup.county_from_address(person())

    def test_other_state_from_county_or_matched_address_is_rejected(self):
        for change_county in (True, False):
            with self.subTest(change_county=change_county):
                result = document()
                match = result["result"]["addressMatches"][0]
                if change_county:
                    match["geographies"]["Counties"][0].update(STATE="42", GEOID="42089")
                else:
                    match["addressComponents"]["state"] = "PA"
                self.response.json.return_value = result
                with self.assertRaisesRegex(lookup.CountyLookupError, "different state"):
                    lookup.county_from_address(person())

    def test_inconsistent_or_malformed_county_identifiers_are_rejected(self):
        for changes in (
            {"GEOID": "39119"}, {"STATE": 39}, {"COUNTY": 89},
            {"COUNTY": "89"}, {"COUNTY": "000", "GEOID": "39000"},
            {"GEOID": None}, {"NAME": None}, {"NAME": ""},
        ):
            with self.subTest(changes=changes):
                result = document()
                result["result"]["addressMatches"][0]["geographies"]["Counties"][0].update(changes)
                self.response.json.return_value = result
                with self.assertRaises(lookup.CountyLookupError):
                    lookup.county_from_address(person())

    def test_malformed_response_envelope_is_rejected(self):
        for result in (None, [], {}, {"result": []}, {"result": {"addressMatches": {}}},
                       {"result": {"addressMatches": [None]}}):
            with self.subTest(result=result):
                self.response.json.return_value = result
                with self.assertRaises(lookup.CountyLookupError):
                    lookup.county_from_address(person())

    def test_missing_match_address_or_state_is_rejected(self):
        for field, value in (("matchedAddress", None), ("matchedAddress", ""),
                             ("addressComponents", {}), ("addressComponents", None)):
            with self.subTest(field=field, value=value):
                result = document()
                result["result"]["addressMatches"][0][field] = value
                self.response.json.return_value = result
                with self.assertRaises(lookup.CountyLookupError):
                    lookup.county_from_address(person())

    def test_foreign_or_incomplete_input_never_leaves_computer(self):
        invalid_records = [None, {}, {**person(), "country": "CA"}, {**person(), "region": "ON"},
                           {**person(), "postal_code": "123"}, {**person(), "address_line_1": ""},
                           {**person(), "locality": None}, {**person(), "address_line_1": "bad\naddress"}]
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(lookup.CountyLookupError):
                lookup.county_from_address(record)
        self.factory.assert_not_called()

    def test_input_state_normalization_does_not_modify_person(self):
        record = {**person(), "region": " oh ", "postal_code": "43001-1234"}
        original = deepcopy(record)
        self.assertEqual(lookup.county_from_address(record)["region"], "OH")
        self.assertEqual(record, original)
        self.assertEqual(self.session.get.call_args.kwargs["params"]["zip"], "43001-1234")

    def test_connection_and_json_errors_do_not_expose_address_or_url(self):
        sensitive = "https://example.invalid/?street=123-SECRET-STREET"
        cases = ((self.session.get, requests.Timeout(sensitive)),
                 (self.response.json, ValueError(sensitive)))
        for method, error in cases:
            with self.subTest(error=type(error).__name__):
                method.side_effect = error
                try:
                    lookup.county_from_address(person())
                except lookup.CountyLookupError as exc:
                    rendered = "".join(traceback.format_exception(exc))
                    self.assertNotIn(sensitive, rendered)
                    self.assertNotIn("SECRET-STREET", rendered)
                    self.assertTrue(exc.__suppress_context__)
                else:
                    self.fail("Expected a privacy-safe lookup failure.")
                method.side_effect = None

    def test_redirect_and_http_errors_do_not_parse_remote_body(self):
        for status in (301, 302, 400, 429, 500):
            with self.subTest(status=status):
                self.response.status_code = status
                with self.assertRaises(lookup.CountyLookupError):
                    lookup.county_from_address(person())
        self.response.json.assert_not_called()

    def test_import_does_not_start_network_or_load_settings(self):
        importlib.reload(lookup)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
