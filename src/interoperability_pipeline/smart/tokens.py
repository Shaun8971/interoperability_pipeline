"""Inspect JWTs locally (no third-party website) and check granted SMART scopes.

What it does: ``decode_jwt_payload`` reads the claims inside a JWT, and ``scope_allows``
answers "does this granted-scope string permit reading this resource type?".
Where it sits in the pipeline: after the token exchange; the portal uses it to explain tokens
and to avoid asking for data the patient did not allow.
Standard and spec: JWT RFC 7519 https://datatracker.ietf.org/doc/html/rfc7519 ; SMART scopes
https://hl7.org/fhir/smart-app-launch/scopes-and-launch-context.html
Inputs / outputs: token string -> claims dict; (scope string, resource type) -> bool.
How to extend: write operations ("write") and v2 scopes with search filters are not handled.

Security note: decoding does NOT verify the signature. It is for looking, never for trusting;
the FHIR server is the one that verifies tokens. Never paste a token into a website to decode it.
"""

import base64
import json
from typing import Any

from interoperability_pipeline.common.errors import SmartAuthError

PATIENT_CONTEXT = "patient"  # scope prefix for "this one patient's data"
WILDCARD = "*"  # in a scope: any resource type, or any permission
READ_PERMISSIONS_V1 = {"read", WILDCARD}  # SMART v1: patient/Patient.read
V2_LETTERS = set("cruds")  # SMART v2 permissions are made only of these letters (create, read, ...)
V2_READ_LETTER = "r"  # SMART v2: patient/Patient.rs includes r (read)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Return the claims (payload) of a JWT without verifying it.

    Example:
        >>> decode_jwt_payload("eyJhbGciOiJub25lIn0.eyJzdWIiOiJhYmMifQ.")
        {'sub': 'abc'}
    """
    parts = token.split(".")
    if len(parts) != 3:  # header.payload.signature
        raise SmartAuthError("token is not a JWT (expected three dot-separated parts)")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)  # restore the base64 padding that JWTs drop
    return json.loads(base64.urlsafe_b64decode(payload))


def scope_allows(granted_scopes: str, resource_type: str, context: str = PATIENT_CONTEXT) -> bool:
    """Does the granted scope string allow *reading* this resource type for the patient?

    Handles ``patient/Patient.read``, ``patient/*.read``, ``patient/*.*`` and the v2 form
    ``patient/Patient.rs``.

    Example:
        >>> scope_allows("launch/patient patient/Patient.read", "Patient")
        True
        >>> scope_allows("launch/patient patient/Patient.read", "ExplanationOfBenefit")
        False
    """
    for scope in granted_scopes.split():
        prefix, _, rest = scope.partition("/")
        if prefix != context or "." not in rest:
            continue
        resource, _, permission = rest.partition(".")
        if resource not in (resource_type, WILDCARD):
            continue
        is_v2_letters = bool(permission) and set(permission) <= V2_LETTERS  # "rs" yes, "write" no
        if permission in READ_PERMISSIONS_V1 or (is_v2_letters and V2_READ_LETTER in permission):
            return True
    return False
