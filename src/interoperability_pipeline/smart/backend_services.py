"""SMART Backend Services: a program (no person, no browser) gets a system token.

What it does: makes an RSA key pair, signs a short-lived JWT ("client assertion") with the
private key, and sends it to the token endpoint. The server checks the signature with the
matching public key and returns a system access token.
Where it sits in the pipeline: the machine-to-machine counterpart of the portal's login
(Module 3); a future agent or nightly job would use this to read FHIR.
Standard and spec: SMART Backend Services
https://hl7.org/fhir/smart-app-launch/backend-services.html ; JWT RFC 7519 ; RS256 = RSA
signature with SHA-256 (RFC 7518 section 3.3).
Inputs / outputs: private key file + client id + token URL -> token response dict.
How to extend: register the public key (JWKS) with a real server instead of the sandbox's
URL trick; add token caching/refresh before expiry.
Run:  python -m interoperability_pipeline.smart.backend_services

Why a signed JWT instead of a password: a password is a secret that must be sent to the server
every time. Here the secret (private key) never leaves this machine; only a signature made with
it does, and a signature is useless to anyone who intercepts it after it expires.
"""

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import jwt  # PyJWT
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from interoperability_pipeline.common.errors import SmartAuthError
from interoperability_pipeline.common.settings import PROJECT_ROOT, load_settings
from interoperability_pipeline.smart.client import SmartEndpoints, discover
from interoperability_pipeline.smart.tokens import decode_jwt_payload

# --- Constants ------------------------------------------------------------
# Environment variable naming where the private key lives (see .env.example).
KEY_PATH_ENV_VAR = "BACKEND_PRIVATE_KEY_PATH"
DEFAULT_KEY_PATH = PROJECT_ROOT / "keys" / "backend_private.pem"
RSA_KEY_SIZE_BITS = 2048  # the common minimum for RS256
RSA_PUBLIC_EXPONENT = 65537  # standard choice
SIGNING_ALGORITHM = "RS256"
KEY_ID = "interoperability-pipeline-key-1"  # "kid": tells the server which public key to use
CLIENT_ID = "interoperability-pipeline-backend"
CLIENT_ASSERTION_TYPE = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"  # fixed by the spec
)
GRANT_TYPE_CLIENT_CREDENTIALS = "client_credentials"
ASSERTION_LIFETIME_SECONDS = 240  # the spec allows at most 5 minutes; shorter is safer
SYSTEM_SCOPE = "system/*.read"  # read any resource type, as a system (not on behalf of a person)
HTTP_TIMEOUT_SECONDS = 15
# Launch-config positions in the sandbox's encoded URL (smart-launcher-v2 src/isomorphic/codec.ts)
LAUNCH_TYPE_BACKEND_SERVICE = 4
CLIENT_TYPE_BACKEND_SERVICE = 3
PKCE_AUTO = 1


def load_or_create_private_key(path: Path) -> rsa.RSAPrivateKey:
    """Load the private key, or create it on first run (file mode 600: owner only).

    Example: load_or_create_private_key(Path("keys/backend_private.pem")) -> RSAPrivateKey
    """
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]
    key = rsa.generate_private_key(RSA_PUBLIC_EXPONENT, RSA_KEY_SIZE_BITS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)  # nobody else on this machine may read the private key
    return key


def public_jwks(private_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """The PUBLIC half of the key as a JWKS (JSON Web Key Set): safe to share with the server.

    Example: {"keys": [{"kty": "RSA", "alg": "RS256", "kid": "...", "n": "...", "e": "AQAB"}]}
    """
    numbers = private_key.public_key().public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return {
        "keys": [
            {
                "kty": "RSA",
                "alg": SIGNING_ALGORITHM,
                "use": "sig",
                "kid": KEY_ID,
                "n": b64(numbers.n),
                "e": b64(numbers.e),
                "key_ops": ["verify"],
            }
        ]
    }


def sandbox_base_url(jwks: dict[str, Any], scope: str = SYSTEM_SCOPE) -> str:
    """Build the sandbox's FHIR base URL that registers this client's public key.

    The sandbox has no sign-up form: it reads the client id and public key out of this URL.
    """
    config = [
        LAUNCH_TYPE_BACKEND_SERVICE,
        "",
        "",
        "",
        0,
        0,
        0,
        scope,
        "",
        CLIENT_ID,
        "",
        "",
        "",
        json.dumps(jwks, separators=(",", ":")),
        CLIENT_TYPE_BACKEND_SERVICE,
        PKCE_AUTO,
        "",
    ]
    encoded = base64.urlsafe_b64encode(json.dumps(config, separators=(",", ":")).encode())
    return f"https://launch.smarthealthit.org/v/r4/sim/{encoded.decode().rstrip('=')}/fhir"


def build_client_assertion(
    private_key: rsa.RSAPrivateKey, client_id: str, token_url: str, now: float | None = None
) -> str:
    """Create and sign the JWT that proves who we are.

    Claims (all required by the spec):
      iss, sub = our client id;  aud = the token endpoint URL;  exp = expiry (short);
      jti = a unique id, so the server can reject a replayed copy.

    Example:
        build_client_assertion(key, "my-client", "https://.../token") -> 'eyJhbGciOiJSUzI1NiIs...'
    """
    issued_at = int(now if now is not None else time.time())
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": token_url,
        "exp": issued_at + ASSERTION_LIFETIME_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, private_key, algorithm=SIGNING_ALGORITHM, headers={"kid": KEY_ID})


def request_system_token(
    endpoints: SmartEndpoints, private_key: rsa.RSAPrivateKey, client_id: str, scope: str
) -> dict[str, Any]:
    """Ask the token endpoint for a system token by presenting the signed assertion.

    Raises SmartAuthError if the server refuses (unknown key, expired assertion, bad scope).
    """
    assertion = build_client_assertion(private_key, client_id, endpoints.token_endpoint)
    try:
        response = requests.post(
            endpoints.token_endpoint,
            data={
                "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                "scope": scope,
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": assertion,
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


def main() -> None:
    key_path = Path(os.environ.get(KEY_PATH_ENV_VAR, DEFAULT_KEY_PATH))
    private_key = load_or_create_private_key(key_path)
    base_url = sandbox_base_url(public_jwks(private_key))
    endpoints = discover(base_url)
    print(f"private key: {key_path} (never sent anywhere)")
    print("signing a JWT and asking for a system token ...")
    token = request_system_token(endpoints, private_key, CLIENT_ID, SYSTEM_SCOPE)
    claims = decode_jwt_payload(token["access_token"])
    print("token fields    :", sorted(token))
    print("scope granted   :", token.get("scope"))
    print("expires_in      :", token.get("expires_in"), "seconds")
    print(
        "patient in token:",
        token.get("patient", "(none: a system token is not tied to one patient)"),
    )
    print("expires at      :", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(claims["exp"])))
    response = requests.get(
        f"{base_url}/Patient?_count=3&_elements=name",
        headers={
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/fhir+json",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    names = [
        " ".join(
            [*e["resource"]["name"][0].get("given", []), e["resource"]["name"][0].get("family", "")]
        )
        for e in response.json().get("entry", [])
    ]
    print(f"read with the system token: HTTP {response.status_code}, first patients: {names}")
    # Show that a forged assertion is refused: signed with a different key the server does not know.
    stranger_key = rsa.generate_private_key(RSA_PUBLIC_EXPONENT, RSA_KEY_SIZE_BITS)
    try:
        request_system_token(endpoints, stranger_key, CLIENT_ID, SYSTEM_SCOPE)
        print("!! a stranger's key was accepted (this would be bad)")
    except SmartAuthError as error:
        print("stranger's key (not registered):", str(error)[:140])


if __name__ == "__main__":
    load_settings()  # fail early if config is missing
    main()
