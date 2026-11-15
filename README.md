# interoperability-pipeline

A learning project: one synthetic patient, Jane, moves through the four core US healthcare data
standards in one small system built from scratch (no real patient data, no paid services).

**Summary.** Jane's hospital sends HL7 v2 messages (admit, lab result, discharge) over MLLP to a
listener that validates them, acknowledges them, and stores them in a FHIR R4 server. From the
FHIR data the system builds an X12 837P claim; a payer simulator answers with an X12 835; the
835 is checked (every amount must balance) and stored back in FHIR as an ExplanationOfBenefit.
A small patient portal reads the FHIR data, with SMART on FHIR login against the SMART Health IT
sandbox (or no login against the local server), and a backend-services script shows how a program
gets a token. A second hospital is onboarded with a config file only, and bad input is refused
loudly and queued, never dropped silently.

```
Hospital simulator --HL7 v2 over MLLP--> listener --> FHIR R4 server (HAPI, Docker)
                                                          |
   Payer simulator <---- X12 837P claim -----------------+
   Payer simulator ----- X12 835 remittance ------------> stored as ExplanationOfBenefit
                                                          |
   Patient portal <-- SMART on FHIR login + FHIR reads ---+
```

New here? Read **GUIDE.md** first: it explains everything in plain language.

## Run the demo

```bash
source .venv/bin/activate           # or use .venv/bin/python
docker compose up -d                # FHIR server at http://localhost:8080/fhir
python run_journey.py               # the whole journey, with a trace by message control ID
python -m interoperability_pipeline.common.report          # volume report
python -m interoperability_pipeline.portal.server --profile local   # portal at http://localhost:8000
pytest                              # 74 tests
```

Other entry points: `python -m interoperability_pipeline.hl7v2.listener`,
`python simulators/hospital_sender.py samples/hl7/adt_a01.hl7`,
`python -m interoperability_pipeline.smart.backend_services`.

See `docs/ARCHITECTURE.md` for the design and "how to add X" guides, and `docs/LEARNING_LOG.md`
for what was learned at each checkpoint.
