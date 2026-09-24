"""Look up a county when the operator enables the optional Census lookup.

This sends a residential street address, city, state, and ZIP to the U.S.
Census Bureau. It never sends names, contact details, or Action Builder
credentials. Census positions are interpolated along address ranges; the
matched address is returned for review, not as proof of rooftop accuracy.

API: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import requests


ENDPOINT = "https://geocoding.geo.census.gov/geocoder/geographies/address"
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
    "CO": "08", "CT": "09", "DE": "10", "DC": "11", "FL": "12",
    "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18",
    "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23",
    "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
    "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
    "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55",
    "WY": "56", "AS": "60", "GU": "66", "MP": "69", "PR": "72",
    "VI": "78",
}


class CountyLookupError(ValueError):
    """The county is unknown; never treat this as an outside-jurisdiction result."""


def _text(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CountyLookupError("The county lookup needs a complete, valid address.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CountyLookupError("The county lookup needs a complete, valid address.")
    return value.strip()


def _params(person: Any) -> tuple[dict[str, str], str]:
    if not isinstance(person, dict):
        raise CountyLookupError("The county lookup needs a complete, valid address.")
    street = _text(person.get("address_line_1"))
    city = _text(person.get("locality"))
    region = _text(person.get("region")).upper()
    postal_code = _text(person.get("postal_code"))
    if region not in STATE_FIPS or not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", postal_code):
        raise CountyLookupError("County lookup requires a supported U.S. state and ZIP code.")
    if "country" in person and _text(person["country"]).upper() not in {
        "US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA",
    }:
        raise CountyLookupError("County lookup requires a U.S. residential address.")
    # Construct the allowlist explicitly: never pass the intake record to requests.
    return {
        "street": street,
        "city": city,
        "state": region,
        "zip": postal_code,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Counties",
        "format": "json",
    }, region


def _county_result(document: Any, region: str) -> dict[str, str]:
    invalid = "Census returned an invalid county result. Review the address manually."
    if not isinstance(document, dict) or not isinstance(document.get("result"), dict):
        raise CountyLookupError(invalid)
    matches = document["result"].get("addressMatches")
    if not isinstance(matches, list):
        raise CountyLookupError(invalid)
    if len(matches) != 1:
        raise CountyLookupError("Census did not find one unambiguous address. Review the county manually.")
    match = matches[0]
    if not isinstance(match, dict) or not isinstance(match.get("geographies"), dict):
        raise CountyLookupError(invalid)
    counties = match["geographies"].get("Counties")
    if not isinstance(counties, list) or len(counties) != 1 or not isinstance(counties[0], dict):
        raise CountyLookupError("Census did not return one unambiguous county. Review the county manually.")
    county = counties[0]
    state_code = county.get("STATE")
    county_code = county.get("COUNTY")
    geoid = county.get("GEOID")
    if (
        not isinstance(state_code, str)
        or not isinstance(county_code, str)
        or not re.fullmatch(r"[0-9]{2}", state_code)
        or not re.fullmatch(r"[0-9]{3}", county_code)
        or county_code == "000"
        or not isinstance(geoid, str)
        or geoid != state_code + county_code
    ):
        raise CountyLookupError(invalid)
    components = match.get("addressComponents")
    if not isinstance(components, dict) or not isinstance(components.get("state"), str):
        raise CountyLookupError(invalid)
    if state_code != STATE_FIPS[region] or components["state"].strip().upper() != region:
        raise CountyLookupError("Census matched a different state. Review the address manually.")
    try:
        county_name = _text(county.get("NAME"), 200)
        matched_address = _text(match.get("matchedAddress"), 1000)
    except CountyLookupError:
        raise CountyLookupError(invalid) from None
    return {
        "county": county_name,
        "region": region,
        "matched_address": matched_address,
        "source": "census",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def county_from_address(person: dict[str, Any]) -> dict[str, str]:
    """Send only address fields to Census and return a county for operator review.

    Call only after the operator enables Census lookup in Settings (on an
    import or explicit retry). Importing this module never makes a request.
    It does not load .env or decide union jurisdiction.
    All failures have static messages without addresses or request URLs.
    """
    params, region = _params(person)
    try:
        with requests.Session() as session:
            # Avoid inherited .netrc authentication and environment proxies.
            session.trust_env = False
            response = session.get(
                ENDPOINT,
                params=params,
                headers={"Accept": "application/json"},
                timeout=(5, 30),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise CountyLookupError("Census county lookup is unavailable. Review the county manually.")
            document = response.json()
    except requests.RequestException:
        raise CountyLookupError("Census county lookup could not connect. Review the county manually.") from None
    except ValueError as exc:
        if isinstance(exc, CountyLookupError):
            raise
        raise CountyLookupError("Census returned an unreadable county result. Review the county manually.") from None
    return _county_result(document, region)
