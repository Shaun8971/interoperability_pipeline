"""Thin FHIR R4 client: conditional update (idempotent write) and count.

What it does: sends resources to the FHIR server in a way that is safe to repeat.
Where it sits in the pipeline: the hub's front door; every spoke (HL7 v2 now, X12
and SMART later) writes and reads FHIR through this module.
Standard and spec: FHIR R4 RESTful API, https://hl7.org/fhir/R4/http.html
  - Conditional update: https://hl7.org/fhir/R4/http.html#cond-update
  - Search:             https://hl7.org/fhir/R4/search.html
Inputs / outputs: resource type + search parameters + resource dict -> FhirWriteResult.
How to extend: add ``read``, ``search`` or ``transaction`` methods here; keep each one
a thin wrapper over one HTTP call so it stays easy to test.
"""

from dataclasses import dataclass
from typing import Any

import requests

from interoperability_pipeline.common.errors import FhirError

# --- Constants --------------------------------------------------------------------
FHIR_JSON_MEDIA_TYPE = "application/fhir+json"  # the FHIR JSON content type
HTTP_CREATED = 201  # server created a new resource
SERVER_ERROR_FLOOR = 500  # 5xx = the server failed; the same request may work later
# Ask the server to return the stored resource so we learn its server-assigned id.
PREFER_REPRESENTATION = {"Prefer": "return=representation"}


@dataclass(frozen=True)
class FhirWriteResult:
    """What happened to one resource: its type, the server-assigned id, created vs updated."""

    resource_type: str
    resource_id: str
    created: bool


class FhirClient:
    """Talks to one FHIR server."""

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def conditional_update(
        self, resource_type: str, search_params: dict[str, str], resource: dict[str, Any]
    ) -> FhirWriteResult:
        """Create the resource if nothing matches ``search_params``, else replace the match.

        Why: one atomic server-side step, so resending a message (or two arriving at
        once) can never create a duplicate. A plain lookup-then-create has a race.

        Example (first send, then a resend of the same message):
            conditional_update("Patient", {"identifier": "<system>|MRN12345"}, patient)
            -> FhirWriteResult("Patient", "1", created=True)
            -> FhirWriteResult("Patient", "1", created=False)
        """
        try:
            response = requests.put(
                f"{self._base_url}/{resource_type}",
                params=search_params,
                json=resource,
                headers={"Content-Type": FHIR_JSON_MEDIA_TYPE, **PREFER_REPRESENTATION},
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as error:
            raise FhirError(f"FHIR server unreachable: {error}", retryable=True) from error
        if not response.ok:
            raise FhirError(
                f"FHIR server answered {response.status_code}: {response.text[:300]}",
                retryable=response.status_code >= SERVER_ERROR_FLOOR,
            )
        stored = response.json()
        return FhirWriteResult(
            resource_type=stored["resourceType"],
            resource_id=stored["id"],
            created=response.status_code == HTTP_CREATED,
        )

    def count(self, resource_type: str, search_params: dict[str, str]) -> int:
        """Return how many resources match a search (uses ``_summary=count``).

        Example:
            client.count("Patient", {"identifier": "...|MRN12345"}) -> 1
        """
        try:
            response = requests.get(
                f"{self._base_url}/{resource_type}",
                params={**search_params, "_summary": "count"},
                headers={"Accept": FHIR_JSON_MEDIA_TYPE},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise FhirError(f"FHIR search failed: {error}", retryable=True) from error
        return int(response.json()["total"])

    def find_id(self, resource_type: str, search_params: dict[str, str]) -> str | None:
        """Return the id of the first resource matching a search, or None if there is none.

        Example:
            client.find_id("Encounter", {"identifier": "<system>|V0001"}) -> "1001"
        """
        try:
            response = requests.get(
                f"{self._base_url}/{resource_type}",
                params={**search_params, "_elements": "id", "_count": "1"},
                headers={"Accept": FHIR_JSON_MEDIA_TYPE},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise FhirError(f"FHIR search failed: {error}", retryable=True) from error
        entries = response.json().get("entry", [])
        return entries[0]["resource"]["id"] if entries else None
