"""Tests for PKCE, scopes, token decoding, the authorize URL and the portal's safety checks."""

import base64
import json
import threading
from http.server import HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from interoperability_pipeline.common.errors import SmartAuthError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.portal.server import make_handler, summarize
from interoperability_pipeline.smart.client import SmartEndpoints, build_authorize_url
from interoperability_pipeline.smart.pkce import make_code_challenge, make_code_verifier, make_state
from interoperability_pipeline.smart.tokens import decode_jwt_payload, scope_allows

ENDPOINTS = SmartEndpoints("https://auth.example/authorize", "https://auth.example/token", ["S256"])


def test_pkce_challenge_matches_the_rfc_7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 appendix B
    assert make_code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_and_state_are_random_and_long_enough() -> None:
    assert make_code_verifier() != make_code_verifier()
    assert 43 <= len(make_code_verifier()) <= 128
    assert make_state() != make_state()


def test_scope_checks() -> None:
    granted = "launch/patient openid patient/Patient.read patient/Encounter.rs"
    assert scope_allows(granted, "Patient")
    assert scope_allows(granted, "Encounter")  # v2 letters include r
    assert not scope_allows(granted, "ExplanationOfBenefit")
    assert scope_allows("patient/*.*", "ExplanationOfBenefit")
    assert scope_allows("patient/*.read", "Patient")
    assert not scope_allows("user/Patient.read", "Patient")  # wrong context (user, not patient)
    assert not scope_allows("patient/Patient.write", "Patient")  # write is not read


def test_jwt_payload_is_decoded_without_a_library() -> None:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "abc", "exp": 1}).encode()).decode().rstrip("=")
    )
    assert decode_jwt_payload(f"e30.{payload}.sig") == {"sub": "abc", "exp": 1}
    with pytest.raises(SmartAuthError):
        decode_jwt_payload("not-a-jwt")


def test_authorize_url_carries_pkce_state_and_audience() -> None:
    url = build_authorize_url(
        ENDPOINTS,
        client_id="c",
        redirect_uri="http://localhost:8000/callback",
        scope="a b",
        state="S",
        code_challenge="CH",
        fhir_base_url="https://fhir.example/fhir",
    )
    query = {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
    assert query["response_type"] == "code" and query["state"] == "S"
    assert query["code_challenge"] == "CH" and query["code_challenge_method"] == "S256"
    assert query["aud"] == "https://fhir.example/fhir"


def test_login_is_refused_when_the_server_has_no_pkce() -> None:
    weak = SmartEndpoints("https://a/authorize", "https://a/token", [])
    with pytest.raises(SmartAuthError):
        build_authorize_url(
            weak,
            client_id="c",
            redirect_uri="r",
            scope="s",
            state="S",
            code_challenge="CH",
            fhir_base_url="https://f",
        )


def test_summaries() -> None:
    patient = {"name": [{"given": ["Jane"], "family": "Doe"}], "birthDate": "1980-03-15"}
    assert summarize("Patient", patient) == "Jane Doe, born 1980-03-15"


@pytest.fixture
def portal_url():  # type: ignore[no-untyped-def]
    settings = load_settings()
    profile = settings["portal"]["profiles"]["sandbox"]
    server = HTTPServer(("127.0.0.1", 0), make_handler("sandbox", profile, settings))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_callback_without_a_login_in_progress_is_rejected(portal_url: str) -> None:
    response = requests.get(f"{portal_url}/callback?code=stolen&state=guess", timeout=5)
    assert response.status_code == 400 and "State mismatch" in response.text


def test_callback_reporting_an_error_is_shown_not_crashed(portal_url: str) -> None:
    response = requests.get(f"{portal_url}/callback?error=access_denied", timeout=5)
    assert response.status_code == 400 and "Login refused" in response.text


def test_record_requires_login(portal_url: str) -> None:
    response = requests.get(f"{portal_url}/record", allow_redirects=False, timeout=5)
    assert response.status_code == 302 and response.headers["Location"] == "/"


# --- Backend services (signing) ------------------------------------------------------------------
def test_client_assertion_is_signed_and_has_the_required_claims() -> None:
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from interoperability_pipeline.smart.backend_services import (
        ASSERTION_LIFETIME_SECONDS,
        build_client_assertion,
    )

    key = rsa.generate_private_key(65537, 2048)
    token = build_client_assertion(key, "client-1", "https://auth.example/token", now=1_000_000)
    claims = pyjwt.decode(
        token,
        key.public_key(),
        algorithms=["RS256"],
        audience="https://auth.example/token",
        options={"verify_exp": False},
    )
    assert claims["iss"] == claims["sub"] == "client-1"
    assert claims["exp"] == 1_000_000 + ASSERTION_LIFETIME_SECONDS
    assert len(claims["jti"]) >= 16  # unique id so a replayed copy can be rejected
    other_key = rsa.generate_private_key(65537, 2048)
    with pytest.raises(pyjwt.InvalidSignatureError):  # a different public key cannot verify it
        pyjwt.decode(
            token,
            other_key.public_key(),
            algorithms=["RS256"],
            audience="https://auth.example/token",
            options={"verify_exp": False},
        )


def test_public_jwks_contains_no_private_material() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    from interoperability_pipeline.smart.backend_services import public_jwks

    key = public_jwks(rsa.generate_private_key(65537, 2048))["keys"][0]
    assert set(key) >= {"kty", "n", "e", "kid"} and not {"d", "p", "q"} & set(key)
