# portal package (the patient-facing end)

`server.py` is a tiny web app on Python's built-in `http.server`. Two profiles in
`config/settings.toml`: `sandbox` (SMART login against the SMART Health IT sandbox) and `local`
(no login, straight to our own HAPI server). Run: `python -m interoperability_pipeline.portal.server --profile local`.

It shows Patient, Encounter and ExplanationOfBenefit. If a scope was not granted it says so
instead of failing. The patient id always comes from the token, never from a search.
