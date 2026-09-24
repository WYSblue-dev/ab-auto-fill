"""Local county reference for an operator's inside-construction review.

This does not geocode addresses or establish exact jurisdictional boundaries.
The county is entered or looked up for review; the residence-local tag is unchanged.
"""
from __future__ import annotations

import re


SCOPE = "Inside construction"
SOURCE_URL = "https://www.ibewlocal1105.org/about/"
MAP_URL = (
    "https://ibew.org/wp-content/uploads/2024/10/"
    "OH_Inside_Final-2018_v2-corrected-erroneous-things.pdf"
)
_FULL_COUNTIES = frozenset({"coshocton", "guernsey", "licking", "muskingum", "perry"})
_PARTIAL_COUNTIES = frozenset({"knox", "tuscarawas"})
_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI "
    "MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
    "VA WA WV WI WY AS GU MP PR VI".split()
)
_UNKNOWN_COUNTIES = frozenset({
    "unknown", "none", "null", "na", "n a", "not known", "not provided",
    "not available", "unavailable", "select", "select county", "county",
})


def _county_name(value: str) -> str:
    if not isinstance(value, str):
        return ""
    name = " ".join(value.split())
    name = re.sub(r"\s+county$", "", name, flags=re.IGNORECASE).strip()
    if (
        not 2 <= len(name) <= 80
        or not name[0].isalpha()
        or not name[-1].isalpha()
        or any(not (char.isalpha() or char in " .'-’") for char in name)
        or name.casefold() in _UNKNOWN_COUNTIES
    ):
        return ""
    return name


def jurisdiction_reference(county: str, region: str, local: str) -> dict[str, str]:
    """Return a county-level suggestion and, when outside, a copyable note.

    ``inside`` covers the five wholly included Ohio counties. ``outside`` is
    only a rough county reference, not an address or boundary verification.
    Partial counties and missing inputs return ``review``. Locals without a
    reference profile return ``unavailable``; they never inherit Local 1105.
    """
    result = {
        "status": "unavailable",
        "summary": "No county reference is configured for this local.",
        "note": "",
        "source_url": "https://ibew.org/inside-jurisdictional-maps/",
        "map_url": "https://ibew.org/inside-jurisdictional-maps/",
        "scope": SCOPE,
    }
    if not isinstance(local, str) or local.strip() != "1105":
        return result

    result.update(source_url=SOURCE_URL, map_url=MAP_URL)
    county_name = _county_name(county)
    state = region.strip().upper() if isinstance(region, str) else ""
    if state == "OHIO":
        state = "OH"
    if not county_name or state not in _STATE_CODES:
        result.update(
            status="review",
            summary="Enter a residence county and valid state abbreviation for county review.",
        )
        return result

    county_key = county_name.casefold()
    if state == "OH" and county_key in _FULL_COUNTIES:
        result.update(
            status="inside",
            summary=f"Inside by county reference: {county_name.title()} County, OH, is listed for Local 1105.",
        )
    elif state == "OH" and county_key in _PARTIAL_COUNTIES:
        result.update(
            status="review",
            summary=(
                f"Review the address on the map: only southern portions of "
                f"{county_name.title()} County, OH, are listed for Local 1105."
            ),
        )
    else:
        result.update(
            status="outside",
            summary=f"Outside by county reference: {county_name} County, {state}, is not listed for Local 1105.",
            note=(
                f"Residence reported in {county_name} County, {state}, appears outside "
                "Local 1105's inside-construction jurisdiction by county reference. "
                "This is a rough reference; the exact address boundary has not been verified."
            ),
        )
    return result
