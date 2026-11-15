"""PKCE (Proof Key for Code Exchange) and OAuth `state` helpers.

What it does: makes the random secret (code verifier), its fingerprint (code challenge) and
the anti-forgery `state` value used in a SMART login.
Where it sits in the pipeline: the first step of the portal's login (Module 3).
Standard and spec: RFC 7636 https://datatracker.ietf.org/doc/html/rfc7636 ; SMART App Launch
https://hl7.org/fhir/smart-app-launch/app-launch.html#step-1-app-asks-for-authorization
Inputs / outputs: nothing in -> random strings out; verifier -> challenge.
How to extend: only the S256 method is implemented, because "plain" gives no protection.

The idea: send the fingerprint (challenge) with the login request, and keep the secret
(verifier). Send the secret only when swapping the code for a token. The server hashes it
and compares. Someone who steals the code but not the secret cannot finish the exchange.
"""

import base64
import hashlib
import secrets

CODE_CHALLENGE_METHOD = (
    "S256"  # "SHA-256 of the verifier"; the only method SMART servers should accept
)
VERIFIER_BYTES = 64  # 64 random bytes -> 86 characters, inside RFC 7636's 43-128 limit
STATE_BYTES = 32  # 32 random bytes of anti-forgery state


def make_code_verifier() -> str:
    """Make the random secret kept by the app until the token exchange.

    Example: make_code_verifier() -> 'k3vYg...' (86 URL-safe characters, different every time)
    """
    return secrets.token_urlsafe(VERIFIER_BYTES)


def make_code_challenge(code_verifier: str) -> str:
    """Fingerprint of the verifier: base64url(SHA-256(verifier)) with no padding.

    Example (the worked example in RFC 7636 appendix B):
        >>> make_code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_state() -> str:
    """Random value sent with the login request and checked on the way back.

    Why: it ties the browser's return trip to *our* login attempt, so a forged callback
    (someone else's code) is recognised and rejected.
    """
    return secrets.token_urlsafe(STATE_BYTES)
