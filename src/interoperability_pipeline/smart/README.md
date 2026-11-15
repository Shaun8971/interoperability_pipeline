# smart package (SMART on FHIR access)

| File | Job |
|---|---|
| `pkce.py` | The PKCE secret (verifier), its fingerprint (challenge) and the anti-forgery `state` |
| `client.py` | Discovery, build the authorize URL, swap the code for a token |
| `tokens.py` | Decode a JWT locally (never on a website) and check granted scopes |
| `backend_services.py` | A program (no person) signs a JWT with its private key and gets a system token |

Two login styles: a person logging in through a browser (authorize page -> code -> token), and a
program proving itself with a signature. Private keys live in `keys/` (git-ignored).
