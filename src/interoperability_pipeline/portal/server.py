"""Tiny patient portal: SMART standalone login, then Patient / Encounter / EOB reads.

What it does: a small web app (standard library ``http.server``). With profile "sandbox" it
runs the SMART login (discovery, authorize with PKCE + state, token exchange) and reads the
logged-in patient's data with the token. With profile "local" there is no login: it reads Jane
straight from our own HAPI server.
Where it sits in the pipeline: the patient-facing end (Module 3). Reads the FHIR hub; EOBs
written by Module 2 show up here.
Standard and spec: SMART App Launch https://hl7.org/fhir/smart-app-launch/app-launch.html ;
FHIR REST https://hl7.org/fhir/R4/http.html
Inputs / outputs: browser requests -> HTML pages. Config: [portal] in config/settings.toml.
How to extend: add a page by adding a ``_page_*`` method and a route in ``do_GET``; add a
resource type to ``RESOURCE_READS``.
Run:  python -m interoperability_pipeline.portal.server [--profile sandbox|local]
Then open http://localhost:8000

Safety choices (why): the one-time `state` is checked and discarded; the token never goes into
logs or pages; query strings are not logged (the callback URL carries the one-time code);
the server listens on this machine only; every dynamic value is HTML-escaped.
"""

import argparse
import hmac
import html
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from interoperability_pipeline.common.errors import SmartAuthError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.mappers import (
    identifier_search_value,
)
from interoperability_pipeline.mapping.sources import load_source
from interoperability_pipeline.smart.client import (
    SmartEndpoints,
    build_authorize_url,
    discover,
    exchange_code,
)
from interoperability_pipeline.smart.pkce import make_code_challenge, make_code_verifier, make_state
from interoperability_pipeline.smart.tokens import decode_jwt_payload, scope_allows

# --- Constants -------------------------------------------------------------------------------------
AUTH_SMART, AUTH_NONE = "smart", "none"  # profile "auth" values
SESSION_COOKIE = "portal_session"
HTTP_TIMEOUT_SECONDS = 15
HOSPITAL_AUTHORITY, JANE_MRN = "MERCY", "MRN12345"  # used only by the no-login (local) profile
# What the record page shows: (resource type, how to search for it given the patient id).
# "Patient" is a direct read; the others are searches by patient.
RESOURCE_READS = ["Patient", "Encounter", "ExplanationOfBenefit"]
ROUTE_HOME, ROUTE_LOGIN, ROUTE_CALLBACK = "/", "/login", "/callback"
ROUTE_RECORD, ROUTE_TOKEN, ROUTE_LOGOUT = "/record", "/token", "/logout"


@dataclass
class Session:
    """What the server remembers about one browser between requests (in memory only)."""

    state: str = ""  # one-time anti-forgery value for the login in progress
    code_verifier: str = ""  # PKCE secret for the login in progress
    token_response: dict[str, Any] = field(default_factory=dict)  # set after a successful login


def summarize(resource_type: str, resource: dict[str, Any]) -> str:
    """One readable line per FHIR resource.

    Example:
        >>> summarize("Patient", {"name": [{"given": ["Jane"], "family": "Doe"}], "birthDate": "1980-03-15"})
        'Jane Doe, born 1980-03-15'
    """
    if resource_type == "Patient":
        name = (resource.get("name") or [{}])[0]
        full = " ".join([*name.get("given", []), name.get("family", "")]).strip() or "(no name)"
        return f"{full}, born {resource.get('birthDate', 'unknown')}"
    if resource_type == "Encounter":
        start = resource.get("period", {}).get("start", "unknown start")
        return f"visit {resource.get('id')}: {resource.get('status', '?')}, started {start}"
    if resource_type == "ExplanationOfBenefit":
        totals = {
            t["category"]["coding"][0]["code"]: t["amount"]["value"]
            for t in resource.get("total", [])
        }
        return f"claim {resource.get('id')}: billed {totals.get('submitted', '?')}, paid {totals.get('benefit', '?')}"
    return f"{resource_type}/{resource.get('id')}"


def page(title: str, body: str) -> bytes:
    """Wrap an HTML fragment in a minimal page. ``body`` must already be escaped HTML."""
    document = (
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:46rem;margin:2rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1>{body}"
    )
    return document.encode()


def make_handler(profile_name: str, profile: dict[str, Any], settings: dict[str, Any]) -> type:
    """Create the request-handler class bound to one profile (config is injected, not global)."""
    sessions: dict[str, Session] = {}
    fhir_base = profile["fhir_base_url"].rstrip("/")
    uses_smart = profile["auth"] == AUTH_SMART
    endpoints_cache: list[SmartEndpoints] = []

    class Handler(BaseHTTPRequestHandler):
        # ----- plumbing -------------------------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Log the method and path only: the callback query string holds the one-time code.
            print(
                f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}"
            )

        def _session(self) -> tuple[str, Session]:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            session_id = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
            if session_id not in sessions:
                session_id = secrets.token_urlsafe(24)
                sessions[session_id] = Session()
            return session_id, sessions[session_id]

        def _send(
            self,
            status: int,
            body: bytes,
            session_id: str | None = None,
            location: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if location:
                self.send_header("Location", location)
            if session_id:
                self.send_header(
                    "Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Lax; Path=/"
                )
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str, session_id: str | None = None) -> None:
            self._send(302, b"", session_id, location)

        def _endpoints(self) -> SmartEndpoints:
            if not endpoints_cache:  # step 1: discovery, once per run
                endpoints_cache.append(discover(fhir_base))
            return endpoints_cache[0]

        # ----- routes ---------------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
            parsed = urlparse(self.path)
            session_id, session = self._session()
            routes = {
                ROUTE_HOME: self._home,
                ROUTE_LOGIN: self._login,
                ROUTE_CALLBACK: self._callback,
                ROUTE_RECORD: self._record,
                ROUTE_TOKEN: self._token,
                ROUTE_LOGOUT: self._logout,
            }
            handler = routes.get(parsed.path)
            if handler is None:
                self._send(404, page("Not found", "<p>No such page.</p>"), session_id)
                return
            handler(session_id, session, parse_qs(parsed.query))

        def _home(self, session_id: str, session: Session, query: dict) -> None:
            mode = "SMART login" if uses_smart else "no login (open server)"
            action = (
                f"<p><a href='{ROUTE_LOGIN}'>Log in with SMART</a></p>"
                if uses_smart
                else f"<p><a href='{ROUTE_RECORD}'>View Jane's record</a></p>"
            )
            signed_in = (
                f"<p>Signed in. <a href='{ROUTE_RECORD}'>Record</a> | <a href='{ROUTE_TOKEN}'>Token</a>"
                f" | <a href='{ROUTE_LOGOUT}'>Log out</a></p>"
                if session.token_response
                else ""
            )
            self._send(
                200,
                page(
                    "Patient portal",
                    (
                        f"<p>Profile: <b>{html.escape(profile_name)}</b> ({mode})<br>"
                        f"FHIR server: <code>{html.escape(fhir_base)}</code></p>{action}{signed_in}"
                    ),
                ),
                session_id,
            )

        def _login(self, session_id: str, session: Session, query: dict) -> None:
            if not uses_smart:
                self._redirect(ROUTE_RECORD, session_id)
                return
            try:
                endpoints = self._endpoints()
                session.code_verifier = make_code_verifier()  # the PKCE secret: stays here
                session.state = make_state()  # anti-forgery value: checked on return
                url = build_authorize_url(
                    endpoints,
                    client_id=profile["client_id"],
                    redirect_uri=profile["redirect_uri"],
                    scope=profile["scope"],
                    state=session.state,
                    code_challenge=make_code_challenge(
                        session.code_verifier
                    ),  # only the fingerprint leaves
                    fhir_base_url=fhir_base,
                )
            except SmartAuthError as error:
                self._send(
                    502, page("Login failed", f"<p>{html.escape(str(error))}</p>"), session_id
                )
                return
            self._redirect(url, session_id)  # the browser now visits the login + consent screen

        def _callback(self, session_id: str, session: Session, query: dict) -> None:
            if "error" in query:  # the user (or server) refused: nothing to exchange
                reason = html.escape(query.get("error_description", query["error"])[0])
                session.state = session.code_verifier = ""
                self._send(
                    400,
                    page("Login refused", f"<p>{reason}</p><p><a href='/'>Home</a></p>"),
                    session_id,
                )
                return
            returned_state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            # Constant-time compare; an empty expected state (no login in progress) never matches.
            if (
                not session.state
                or not hmac.compare_digest(session.state, returned_state)
                or not code
            ):
                session.state = session.code_verifier = ""
                self._send(
                    400,
                    page(
                        "Rejected",
                        "<p>State mismatch: this response does not belong to a login "
                        "we started. Possible forged request.</p>",
                    ),
                    session_id,
                )
                return
            verifier, session.state, session.code_verifier = (
                session.code_verifier,
                "",
                "",
            )  # one-time use
            try:
                session.token_response = exchange_code(
                    self._endpoints(),
                    code=code,
                    client_id=profile["client_id"],
                    redirect_uri=profile["redirect_uri"],
                    code_verifier=verifier,
                )
            except SmartAuthError as error:
                self._send(
                    502,
                    page("Token exchange failed", f"<p>{html.escape(str(error))}</p>"),
                    session_id,
                )
                return
            self._redirect(ROUTE_RECORD, session_id)

        def _record(self, session_id: str, session: Session, query: dict) -> None:
            headers = {"Accept": "application/fhir+json"}
            granted = ""
            if uses_smart:
                if not session.token_response:
                    self._redirect(ROUTE_HOME, session_id)
                    return
                headers["Authorization"] = f"Bearer {session.token_response['access_token']}"
                granted = session.token_response.get("scope", "")
                # The patient id comes from the token response (what the patient chose at login),
                # never from a search: a search could return someone else's record.
                patient_id = session.token_response.get("patient", "")
                if not patient_id:
                    self._send(
                        200,
                        page("Record", "<p>The token carries no patient context.</p>"),
                        session_id,
                    )
                    return
            else:
                client = FhirClient(fhir_base, HTTP_TIMEOUT_SECONDS)
                config = load_source("hospital_a")
                patient_id = (
                    client.find_id(
                        "Patient",
                        {
                            "identifier": identifier_search_value(
                                config.patient_identifier_systems[HOSPITAL_AUTHORITY], JANE_MRN
                            )
                        },
                    )
                    or ""
                )
                if not patient_id:
                    self._send(
                        200,
                        page("Record", "<p>Jane is not in the FHIR server yet.</p>"),
                        session_id,
                    )
                    return
            sections = [f"<p>Patient id: <code>{html.escape(patient_id)}</code></p>"]
            for resource_type in RESOURCE_READS:
                sections.append(f"<h2>{resource_type}</h2>")
                if uses_smart and not scope_allows(granted, resource_type):
                    sections.append(
                        f"<p><i>Not shown: you did not grant read access to {resource_type} "
                        f"(needs <code>patient/{resource_type}.read</code>). You can log in again "
                        "and allow it.</i></p>"
                    )
                    continue
                url = (
                    f"{fhir_base}/Patient/{patient_id}"
                    if resource_type == "Patient"
                    else (f"{fhir_base}/{resource_type}?patient={patient_id}&_count=10")
                )
                try:
                    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
                except requests.RequestException as error:
                    sections.append(f"<p>Could not reach the server: {html.escape(str(error))}</p>")
                    continue
                if response.status_code in (401, 403):
                    sections.append(
                        f"<p><i>The server refused this read ({response.status_code}).</i></p>"
                    )
                    continue
                if not response.ok:
                    sections.append(f"<p>Server error {response.status_code}.</p>")
                    continue
                body = response.json()
                resources = (
                    [e["resource"] for e in body.get("entry", [])]
                    if resource_type != "Patient"
                    else [body]
                )
                if not resources:
                    sections.append("<p><i>None on file.</i></p>")
                for resource in resources:
                    sections.append(f"<p>{html.escape(summarize(resource_type, resource))}</p>")
            nav = (
                f"<p><a href='{ROUTE_TOKEN}'>Token details</a> | <a href='{ROUTE_LOGOUT}'>Log out</a></p>"
                if uses_smart
                else ""
            )
            self._send(200, page("Record", "".join(sections) + nav), session_id)

        def _token(self, session_id: str, session: Session, query: dict) -> None:
            if not session.token_response:
                self._redirect(ROUTE_HOME, session_id)
                return
            token = session.token_response
            rows = [
                ("patient (from the token response)", token.get("patient")),
                ("scope granted", token.get("scope")),
                ("token_type", token.get("token_type")),
                ("expires_in (seconds)", token.get("expires_in")),
            ]
            for name in ("access_token", "id_token"):
                if name in token:
                    try:
                        claims = decode_jwt_payload(token[name])
                    except (SmartAuthError, ValueError):
                        continue
                    if "exp" in claims:
                        claims["exp (readable)"] = time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(claims["exp"])
                        )
                    rows.append(
                        (f"{name} claims (decoded locally; the token itself is not shown)", claims)
                    )
            table = "".join(
                f"<tr><td><b>{html.escape(str(k))}</b></td><td><pre>{html.escape(str(v))}</pre></td></tr>"
                for k, v in rows
            )
            self._send(
                200,
                page("Token details", f"<table>{table}</table><p><a href='/'>Home</a></p>"),
                session_id,
            )

        def _logout(self, session_id: str, session: Session, query: dict) -> None:
            sessions.pop(session_id, None)
            self._redirect(ROUTE_HOME)

    return Handler


def main() -> None:
    settings = load_settings()
    portal = settings["portal"]
    parser = argparse.ArgumentParser(description="Run the patient portal.")
    parser.add_argument("--profile", default=portal["profile"], choices=sorted(portal["profiles"]))
    arguments = parser.parse_args()
    profile = portal["profiles"][arguments.profile]
    server = HTTPServer(
        (portal["host"], portal["port"]), make_handler(arguments.profile, profile, settings)
    )
    print(f"portal running: http://localhost:{portal['port']}  (profile: {arguments.profile})")
    server.serve_forever()


if __name__ == "__main__":
    main()
