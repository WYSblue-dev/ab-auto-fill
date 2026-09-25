"""Bounded retries for read-only Action Builder requests.

This helper never sends a POST, parses a response body, or retries a semantic
verification failure. Callers retain endpoint validation, request pacing, and
privacy-safe error messages.
"""

from __future__ import annotations

import time

import requests


_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


def get_with_retries(url, *, headers, params=None, timeout=(5, 30), allow_redirects=False):
    """Return the response, or raise the last request error, after at most 3 GETs.

    The two retry waits are one and two seconds. Successful and permanent-error
    responses return immediately, unchanged. TLS verification failures are not
    retried even though requests classifies them as connection errors.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=timeout,
                allow_redirects=allow_redirects,
            )
        except requests.exceptions.SSLError:
            raise
        except (requests.Timeout, requests.ConnectionError):
            if attempt == _MAX_ATTEMPTS - 1:
                raise
        else:
            if response.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                return response
            response.close()
        time.sleep(2 ** attempt)
