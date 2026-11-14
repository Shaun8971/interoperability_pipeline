# Architecture

FHIR R4 is the hub. Every other standard is an adapter (spoke) at the edge.

```
Hospital simulator --HL7 v2/MLLP--> listener --> FHIR R4 server (HAPI)
```

(Diagram and "how to add X" guides grow with each module.)
