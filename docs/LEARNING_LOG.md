# Learning log

One entry per checkpoint: the concept, what we built, what I got wrong and the correction, and sources.

## Checkpoint 0: CapabilityStatement

- **Concept:** every FHIR server publishes a CapabilityStatement at `[base]/metadata`. It lists the FHIR version, the resource types supported, the interactions allowed on each (read, create, update, search...), the search parameters, and features like `conditionalCreate`.
- **Built:** HAPI FHIR running in Docker at `http://localhost:8080/fhir` (FHIR 4.0.1 = R4); Python project, ruff, pytest.
- **What I got wrong:** I thought the extra metadata and links add to the server's capabilities. Correction: the statement only *describes* capabilities, which come from the server's code. It is a published promise an app can check before relying on a feature. Our pipeline needs Patient `identifier` search and `conditionalCreate`.
- **Sources:** https://hl7.org/fhir/R4/capabilitystatement.html , https://hl7.org/fhir/R4/http.html#ccreate
