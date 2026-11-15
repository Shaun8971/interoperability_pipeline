# fhir package (the hub's front door)

`client.py` is the only place that talks HTTP to the FHIR server. Every other package (HL7 v2,
X12, the portal) goes through it.

| Method | What it does |
|---|---|
| `conditional_update` | Create the resource if nothing matches, else replace the match. One atomic step on the server, so resends never duplicate. |
| `search` / `find_id` / `count` | Read back what is stored. |

Why it matters: FHIR is the single internal data model. Nothing downstream needs to know whether
data arrived as HL7 v2 or X12. To use a different FHIR server, change `[fhir] base_url` in
`config/settings.toml`.
