"""SMART App Launch client steps: discovery, authorize URL, token exchange.

What it does: reads the server's `.well-known/smart-configuration`, builds the authorize URL
(with PKCE and state) and swaps the returned code for a token.
Where it sits in the pipeline: the portal calls these in order: discovery -> authorize ->
callback -> token exchange -> FHIR reads.
Standard and spec: SMART App Launch https://hl7.org/fhir/smart-app-launch/app-launch.html
(discovery: .../conformance.html). OAuth 2.0 authorization code flow RFC 6749.
Inputs / outputs: FHIR base URL + client settings -> endpoints, URLs, token response dict.
How to extend: add refresh-token handling or confidential clients (a client secret) here.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from interoperability_pipeline.common.errors import SmartAuthError
from interoperability_pipeline.smart.pkce import CODE_CHALLENGE_METHOD

# --- Constants ----------------------------------------------------------------------------------
WELL_KNOWN_PATH = "/.well-known/smart-configuration"  # where SMART servers publish their endpoints
RESPONSE_TYPE_CODE = "code"  # authorization code flow
GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
HTTP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class SmartEndpoints:
    """The two URLs a login needs, taken from discovery."""

    authorization_endpoint: str  # step 1: where the browser goes to log in and consent
    token_endpoint: str  # step 2: where the app swaps the code for a token
    code_challenge_methods: list[str]


def discover(fhir_base_url: str) -> SmartEndpoints:
    """Read the server's SMART configuration.

    Raises SmartAuthError if the server has none: such a server is open or not SMART-capable.

    Example:
        discover("https://launch.smarthealthit.org/v/r4/fhir").token_endpoint
        -> 'https://launch.smarthealthit.org/v/r4/auth/token'
    """
    try:
        response = requests.get(
            fhir_base_url.rstrip("/") + WELL_KNOWN_PATH, timeout=HTTP_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        config = response.json()
        return SmartEndpoints(
            authorization_endpoint=config["authorization_endpoint"],
            token_endpoint=config["token_endpoint"],
            code_challenge_methods=config.get("code_challenge_methods_supported", []),
        )
    except (requests.RequestException, KeyError, ValueError) as error:
        raise SmartAuthError(f"SMART discovery failed for {fhir_base_url}: {error}") from error


def build_authorize_url(
    endpoints: SmartEndpoints,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    fhir_base_url: str,
) -> str:
    """Build the URL the browser is sent to for login and consent.

    Raises SmartAuthError if the server does not support S256 PKCE: we refuse to log in
    without it rather than fall back to something weaker.

    Example: '<authorization_endpoint>?response_type=code&client_id=...&state=...&aud=...'
    """
    if CODE_CHALLENGE_METHOD not in endpoints.code_challenge_methods:
        raise SmartAuthError("server does not advertise PKCE S256; refusing to log in without it")
    query = {
        "response_type": RESPONSE_TYPE_CODE,
        "client_id": client_id,  # who is asking
        "redirect_uri": redirect_uri,  # where to send the browser back
        "scope": scope,  # what we ask permission for
        "state": state,  # echoed back; proves the response belongs to our request
        "aud": fhir_base_url,  # which FHIR server the token is for (SMART requires it)
        "code_challenge": code_challenge,  # the PKCE fingerprint
        "code_challenge_method": CODE_CHALLENGE_METHOD,
    }
    return f"{endpoints.authorization_endpoint}?{urlencode(query)}"


def exchange_code(
    endpoints: SmartEndpoints,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Swap the authorization code for tokens, proving ownership with the PKCE verifier.

    Returns the token response: access_token, expires_in, scope, patient, id_token, ...
    Raises SmartAuthError if the server refuses (for example a wrong verifier).
    """
    try:
        response = requests.post(
            endpoints.token_endpoint,
            data={
                "grant_type": GRANT_TYPE_AUTHORIZATION_CODE,
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,  # the secret; the server hashes it and compares
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise SmartAuthError(f"token request failed: {error}") from error
    if not response.ok:
        raise SmartAuthError(
            f"token endpoint refused: {response.status_code} {response.text[:300]}"
        )
    return response.json()  # type: ignore[no-any-return]
