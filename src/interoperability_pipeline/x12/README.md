# x12 package (the X12 spoke)

Reads and writes X12 healthcare transactions. Module 2 uses the 837P claim (out) and the
835 remittance (in).

| File | Job |
|---|---|
| `tokenizer.py` | Read delimiters from the fixed-width ISA, split into segments/elements |
| `claim_data.py` | Gather Jane's Patient, Encounter, Condition, Procedure from FHIR into a plain object |
| `builder_837p.py` | Pure functions: claim object -> 837P text (one function per loop) |
| `adjudication.py` | The payer simulator's rules: read an 837, pay the contract rate, apply coinsurance, deny without prior auth |
| `builder_835.py` | Write the 835 remittance |
| `parser_835.py` | Read an 835 and check every amount balances |
| `eob_mapper.py` | 835 -> FHIR Coverage + ExplanationOfBenefit |
| `remittance_to_fhir.py` | Parse, reconcile, and only then store |

X12 compared with HL7 v2: both are delimited text, but X12 declares its delimiters in the
ISA header, is strictly structured (loops, envelopes with counts), and carries money.

Connects to the hub through `../fhir/client.py`.
To add another transaction (270/271, 276/277): add a builder or parser next to these, reuse
the tokenizer, and keep the FHIR reads/writes separate from the X12 text.
