# mapping package (per-hospital configuration)

`sources.py` loads one TOML file per sending hospital from `config/sources/` and picks the right
one for each message by its sending facility (MSH-4).

What belongs in a source file (it differs per hospital): which PID field holds the patient id,
the time zone, identifier system URIs, and reviewed translations of local lab codes.
What stays in code (the same for everyone): how to parse, validate, ACK, write idempotently, and
the rule that nothing is guessed.

To onboard a hospital: copy `config/sources/hospital_a.toml`, edit it, done. No code change.
