# Jane's Journey: a plain-language guide to this project

This guide explains everything we built, in simple words, with Jane's data as the example. You do
not need to know how to code to read it. If a word is new, look it up in **Section 3**.

Everything here is **synthetic** (made up). Jane Doe is not a real person, and no real patient data
is used anywhere.

---

## 1. The story in one page

Jane Doe goes into Mercy General Hospital. A lot of different computer systems now need to know
about her, and they all speak different "languages":

1. **The hospital's system** tells everyone "Jane was admitted", "Jane's potassium result is 4.1",
   "Jane was discharged". It speaks **HL7 v2**, a text format from the 1980s that is still used
   everywhere.
2. **Our system** listens, checks the messages, and stores Jane's information in a modern
   standard format called **FHIR**.
3. **Her insurance company** needs a bill. The hospital builds one in **X12 837** (the "claim"
   language). The insurer answers with an **X12 835** saying what it paid and what Jane owes.
4. **Jane herself** wants to look at her record on a website, without everyone else being able to
   see it. That needs a secure login standard called **SMART on FHIR**.

The project builds a small version of all four, one after another, so you can see how data really
moves through healthcare.

---

## 2. The big picture

```
Hospital simulator --HL7 v2 over MLLP--> listener --> FHIR server (the "hub")
                                                          |
   Payer simulator <---- X12 837 claim -------------------+
   Payer simulator ----- X12 835 payment explanation ----> saved back as an "EOB"
                                                          |
   Patient portal <-- SMART login + reading FHIR data ----+
```

**Why FHIR is in the middle ("hub and spoke").** Imagine every system talking directly to every
other: with 4 systems that is 12 connections, and every new system multiplies the work. Instead,
every system only talks to one place, FHIR. Each connection to FHIR is a "spoke" (an *adapter*
that translates between that system's language and FHIR). Add a new hospital or a new standard and
you only build one new spoke. Nothing after the hub needs to know which language the data
arrived in.

---

## 3. Words you need

Read this once; come back when a word is unfamiliar.

### HL7 v2 words
- **Message**: one complete piece of communication, like one letter. Example: "Jane was admitted".
- **Segment**: one line of a message. `PID` is the patient line, `PV1` the visit line, `OBX` one lab
  result line.
- **Field**: one piece of a line, separated by `|`. "PID-5" means the 5th field of the PID line
  (the patient's name).
- **Component**: a piece of a field, separated by `^`. The name `DOE^JANE^Q` has the components
  family name, given name, middle initial.
- **MSH-10 (control ID)**: a unique number on every message, like a tracking number. We use it to
  follow one message through the whole system.
- **TCP**: the way two computers send bytes to each other over a network. Think of it as a phone
  line. It carries a continuous stream of bytes and does not know where one message ends and the
  next begins.
- **MLLP**: the etiquette for using that phone line for HL7. Each message is wrapped in a start
  marker and an end marker so the receiver knows where a message begins and ends. (Why it is
  needed: messages can arrive stuck together, or split in half.)
- **ACK**: the receipt the receiver sends back. **AA** = "got it and stored it". **AE** = "your
  message has a problem; fix it and resend". **AR** = "I could not process it right now (my
  fault, not yours); try again later".

### FHIR words
- **FHIR** (said "fire"): a modern standard for health data, delivered over the web.
- **Resource**: one kind of record in FHIR. **Patient** (who), **Encounter** (one visit),
  **Observation** (one measurement, like potassium), **Condition** (a diagnosis), **Procedure**
  (a service performed), **Coverage** (insurance), **ExplanationOfBenefit** (what the insurer paid).
- **`id`**: a label the FHIR server gives a record (Jane's Patient has id `1000` on our server).
  Only meaningful *inside that one server*.
- **`identifier`**: a label that comes from the real world (Jane's hospital number, her MRN
  `MRN12345`). It always comes as a pair: **system + value** (whose number it is, and the number).
  `MRN12345` at Mercy is a different thing from `MRN12345` at another hospital, and the
  *system* is what tells them apart.
- **CapabilityStatement**: the server's published list of what it can do (found at `/metadata`).
  A menu, not the kitchen.
- **Conditional update / idempotent**: "create this record only if it doesn't exist yet, otherwise
  replace the existing one". If you send the same message twice you still get exactly one Jane.
  That property is called *idempotent*.
- **HAPI**: the free FHIR server we run on our own computer (in Docker).

### X12 words (insurance and money)
- **X12**: the standard language for business messages in US healthcare, especially billing.
- **837P**: the **claim** ("P" = professional services). Hospital to insurer: "please pay us for
  these services".
- **835**: the **remittance**. Insurer to hospital: "here is what we paid, what we adjusted, and why".
- **Envelopes (ISA, GS, ST)**: three nested wrappers around the claim. **ST/SE** = one document;
  **GS/GE** = a group of documents of one kind; **ISA/IEA** = the whole delivery. Each has a
  control number and a count so the receiver can tell if anything went missing, and at which level.
- **ICD-10 / CPT**: code lists. ICD-10 = diagnoses (Jane has `I10`, high blood pressure). CPT =
  services (`99213` office visit, `72148` MRI of the lower spine).
- **Contract rate (allowed amount)**: the most the insurer will recognise for a service, agreed
  in the contract with the hospital.
- **Coinsurance**: the patient's percentage share of the allowed amount (ours: 20%).
- **Prior authorization**: the insurer's advance approval for an expensive service (here, the MRI).
- **CO and PR**: the two adjustment groups. **CO** (contractual obligation) = the *hospital's*
  loss; the patient cannot be billed. **PR** (patient responsibility) = the *patient* owes it.
- **Reason codes**: why an amount was adjusted. `45` = over the contract rate, `2` = coinsurance,
  `197` = no prior authorization.
- **EOB (ExplanationOfBenefit)**: the FHIR record of the payment, written so a patient or an app
  can read it.

### SMART and login words
- **SMART on FHIR**: the standard way to add secure login and permissions to FHIR.
- **Authentication**: proving *who you are*. **Authorization**: deciding *what you may see*.
- **Authorize endpoint**: a web page on the server where a *person* logs in and clicks Allow.
- **Authorization code**: a one-time ticket the server hands back after you approve. It travels
  through the browser's address bar. On its own it is worthless.
- **Token endpoint**: an address the *app* calls directly to swap the ticket for the real thing.
- **Access token**: the "wristband". It says who you are, which patient, what you may read, and
  when it expires.
- **Scope**: one permission. `patient/Patient.read` = "may read this patient's Patient record".
- **PKCE** (said "pixie"): a trick that makes a stolen ticket useless (explained in Section 7).
- **`state`**: a random tag on the login that must come back unchanged, so a forged link is
  rejected.
- **JWT**: a small piece of text that carries claims and a signature. You can read one without any
  special tool (but never paste one into a website).
- **Backend services**: how a *program* (no person to click) logs in: by signing a note with a
  private key.

### Words from the last module
- **Stale event**: an old message arriving late, after a newer one.
- **Error queue**: a holding area for messages we could not accept. The raw message is kept so it
  can be fixed and replayed.
- **Source config**: a small file describing one hospital's habits.

---

## 4. Part 1: the hospital talks to us (HL7 v2)

### 4.1 What Jane's admit message looks like

```
MSH|^~\&|HOSPSIM|MERCY_GENERAL|PIPELINE|FHIRHUB|20261114093000||ADT^A01^ADT_A01|MSG00001|P|2.5.1
EVN|A01|20261114093000
PID|1||MRN12345^^^MERCY^MR||DOE^JANE^Q||19800315|F|||123 MAIN ST^^ITHACA^NY^14850
PV1|1|I|MED^101^A||||1234^SMITH^ALAN||||||||||||V0001|||||||||||||||||||||||||20261114093000
```

How to read it, line by line:

| Line | Meaning |
|---|---|
| `MSH` | The envelope. **MSH-4** `MERCY_GENERAL` is who sent it. **MSH-9** `ADT^A01` means "admit". **MSH-10** `MSG00001` is the tracking number. |
| `EVN` | When the event happened (09:30 on 14 Nov 2026). |
| `PID` | Who. **PID-3** is her hospital number `MRN12345^^^MERCY^MR` (number, then the authority MERCY, then type MR = medical record number). **PID-5** her name. **PID-7** birth date. **PID-8** sex. |
| `PV1` | The visit. `I` = inpatient. **PV1-19** `V0001` is the visit number. |

One quirk: the very first `|` in `MSH` counts as MSH-1, so MSH fields are numbered one off from
the others. Our code handles it.

### 4.2 How the bytes travel (TCP and MLLP)

The hospital's system opens a TCP connection to us ("a phone line") and sends the message wrapped
like this:

```
[start marker]  MSH|...the whole message...  [end marker]
```

Our listener keeps reading until it sees the end marker. Only then does it have one whole message.
**Why we need markers:** TCP is a stream with no gaps. Two messages can arrive glued together, or
one message can arrive in two pieces. The markers tell us exactly where each message begins and
ends.

### 4.3 What our listener does with each message

In order, for every message:

1. **Cut it out** of the stream (using the markers) and **split it** into lines, fields and
   components. (We wrote this ourselves with plain Python so you can see how it works.)
2. **Find out which hospital sent it** (MSH-4) and load that hospital's config file.
3. **Check it**: is the patient number there? Is the message type one we know? If not, reply AE
   with the exact reason.
4. **Check it is not an old event** arriving late (Section 7).
5. **Translate it to FHIR** and store it (Patient, then Encounter, then Observation).
6. **Reply with an ACK**, only *after* storing. (If we replied "AA" first and then failed to
   store it, the data would be lost and nobody would know.)
7. **Write down what happened** in the event log, with the tracking number.

### 4.4 The translation (HL7 to FHIR) for Jane

| From the message | To the FHIR record |
|---|---|
| PID-3 `MRN12345^^^MERCY^MR` | Patient `identifier` (system + value `MRN12345`) |
| PID-5 `DOE^JANE^Q` | Patient `name` (family DOE, given JANE and Q) |
| PID-7 `19800315` | Patient `birthDate` `1980-03-15` |
| PV1-19 `V0001` | Encounter `identifier` |
| EVN/MSH-9 `A01` | Encounter `status` `in-progress` (A03 = `finished`, A13 = `in-progress` again) |
| PV1-44 admit time | Encounter `period.start` |
| OBX-3 `2823-3^Potassium...^LN` | Observation `code` (LOINC 2823-3) |
| OBX-5 and OBX-6 `4.1` `mmol/L` | Observation `valueQuantity` |

### 4.5 Two traps handled on purpose

- **Time zones.** HL7 times carry no time zone (`20261114093000`), but FHIR needs one. If we
  guessed "UTC", Jane's admission would shift by five hours and nothing would look wrong.
  So each hospital's config says what zone its clocks use, and we add the right offset
  (`2026-11-14T09:30:00-05:00`).
- **Never guess.** If a lab code is one we do not recognise (`K+` instead of the standard
  LOINC code), we do **not** guess that it means potassium. We reject the message with a clear
  reason. Guessing wrong is how real patients get hurt.

### 4.6 `id` versus `identifier` (the Checkpoint 1.3 question, answered simply)

Jane's Patient has `id: 1000` and an identifier `MRN12345`. The **id** is our server's own label:
good for records on our server pointing at each other (the Encounter says "subject: Patient/1000").
The **identifier** is the real-world label plus *whose* label it is: good for finding the same
person from outside, and for the "create only if it doesn't exist" check. Never match people by
name (two people can be called Jane Doe).

### 4.7 The ACK, and what happens when no ACK comes

- After **AA**: the sender is done.
- After **AE**: the sender must *fix the message first*. Sending the identical message again just
  gets the same AE.
- After **AR**: the sender can resend later.
- **No ACK at all** is the nasty case. The sender cannot tell "it never arrived" from "it arrived
  and was stored but the receipt got lost". Resending risks a duplicate; not resending risks a
  loss. The answer is to **resend with the same tracking number** (limited tries, then raise an
  alarm) and to make the receiver **idempotent**, so a repeat changes nothing. That is why the
  conditional update matters.

---

## 5. Part 2: the money (X12 claim and payment)

### 5.1 The loop

```
FHIR: Jane's Encounter + Condition (I10) + Procedures (99213, 72148)
   --> build the 837 claim
   --> the payer simulator reads it and applies its rules
   --> it writes an 835 payment explanation
   --> we read the 835 and check the money adds up
   --> we store it in FHIR as an ExplanationOfBenefit
```

(Real hospitals get diagnoses and procedures from their billing system, not from admit messages,
so a small script `simulators/seed_claim_data.py` writes them into FHIR for us.)

### 5.2 What an X12 file looks like

```
CLM*V0001*1200***11:B:1*Y*A*Y*Y~
HI*ABK:I10~
SV1*HC:99213*200*UN*1***1~
SV1*HC:72148*1000*UN*1***1~
```

`~` ends a line (segment), `*` separates pieces (elements), `:` separates parts of a piece. Unlike
HL7, an X12 file **declares its own separators** in its first line (ISA), which is always exactly
106 characters long because every piece in that one line is padded to a fixed width. After that
line, everything is variable length, which is why separators are still needed.

Reading the sample: `CLM` is the claim (reference V0001, total $1,200). `HI*ABK:I10` is the
diagnosis. Each `SV1` is a service with its CPT code and charge ($200 and $1,000).

### 5.3 The three envelopes

```
ISA ...                  <- the whole delivery opens
  GS ...                 <- a group of the same kind of document opens
    ST*837 ...           <- one claim opens
      ... the claim ...
    SE ...               <- the claim closes (counts its own lines)
  GE ...                 <- the group closes (counts its documents)
IEA ...                  <- the delivery closes (counts its groups)
```

Each layer has a control number that must match at the start and the end, and a count. So the
receiver can detect loss at each level, and can accept or reject at each level (for example reject
one claim without rejecting the whole delivery).

### 5.4 The payer's three rules, with Jane's numbers

The insurer's contract: office visit (99213) allowed **$150**, MRI (72148) allowed **$800**.
Jane's coinsurance is **20% of the allowed amount**. The MRI needs prior authorization.

**The key idea that is easy to get wrong:** the difference between what the hospital *billed* and
what the contract *allows* is a **write-off**. Nobody collects it, and Jane does not owe it. The
coinsurance is calculated on the *allowed* amount, not on the billed amount.

**Scenario A: the claim has a prior authorization**

| Service | Billed | Allowed | Insurer pays | Write-off (CO-45) | Jane owes (PR-2) |
|---|---|---|---|---|---|
| Office visit | 200 | 150 | **120** | 50 | **30** |
| MRI | 1000 | 800 | **640** | 200 | **160** |
| **Total** | 1200 | | **760** | 250 | **190** |

Check each line: billed = paid + write-off + patient share. Visit: 200 = 120 + 50 + 30. MRI:
1000 = 640 + 200 + 160.

**Scenario B: no prior authorization**

- Office visit: the same as above (insurer 120, write-off 50, Jane 30).
- MRI: **denied**. The insurer pays 0, and the whole $1,000 is reason `197` ("no prior
  authorization") under group **CO**. CO means it is the *hospital's* loss, so Jane owes **$0**
  for the MRI. (In real life this depends on the contract; in this project the payer uses CO.)
- Totals: insurer pays $120, Jane owes **$30**, the hospital absorbs $1,050.

### 5.5 What the 835 looks like

```
BPR*I*120*C*CHK************20261114~     total payment: $120
CLP*V0001*1*1200*120*30*CI*PCNV0001*11*1~ claim: billed 1200, paid 120, patient owes 30
SVC*HC:99213*200*120~                     visit: billed 200, paid 120
CAS*CO*45*50~                               adjustment: contractual, reason 45, $50
CAS*PR*2*30~                                adjustment: patient responsibility, reason 2, $30
SVC*HC:72148*1000*0~                      MRI: billed 1000, paid 0
CAS*CO*197*1000~                            adjustment: contractual, reason 197, $1000
```

### 5.6 The reconciler (a bookkeeping check)

When an 835 arrives, we do **not** trust it blindly. The **reconciler** only checks the arithmetic
*inside the file*, like checking that the items on a receipt add up to the printed total:

- every service line: billed = paid + all adjustments,
- the claim: billed total = paid total + all adjustments, and the patient total = sum of the PR
  adjustments,
- the payment: the check amount = the sum of what the claims say was paid.

If anything fails, the whole file is refused and nothing is stored. In our test, we changed one
adjustment from 50 to 70 and the reconciler caught it. Note that the reconciler does **not** know
the contract rates. It would not notice an insurer that wrongly applied a different rate if the
numbers still added up. (That is a separate "expected payment" check that real hospitals also do;
we did not build it.)

### 5.7 837 versus 835 versus EOB

| Document | From to | What it says |
|---|---|---|
| **837** (claim) | hospital -> insurer | "Please pay us for these services." |
| **835** (remittance) | insurer -> hospital | "Here is what we paid, adjusted and why." |
| **EOB** (FHIR record) | read by the patient or an app | "Here is what your visit cost, what the plan paid, and what you owe." |

---

## 6. Part 3: logging in safely (SMART on FHIR)

### 6.1 Why

Our local FHIR server is **open**: anyone who can reach it can read every patient. Real systems
cannot be like that. SMART adds two things:

- **Authentication**: who are you?
- **Authorization**: what are you allowed to see? (which patient, which kinds of data, for how long)

### 6.2 The login story (a package pick-up counter)

1. **The app writes a secret on a slip of paper** and keeps it in its pocket (the *code
   verifier*). It also makes a scrambled version of the slip that cannot be un-scrambled (the
   *fingerprint*, or code challenge), plus a random tag (*state*).
2. **The app sends you to the server's login page**, carrying only the fingerprint and the tag
   (never the secret) and a list of the permissions it wants (the *scopes*).
3. **You log in and click Allow.**
4. **The server makes up a one-time ticket (the code)** and sends your browser back to the app
   with the ticket attached to the address. *This is where the code comes from: the server creates
   it.* It appears in the address bar for a split second.
5. **The app checks the tag** is the one it sent. If not, it refuses.
6. **The app goes directly to the server's back counter (the token endpoint)** with the ticket
   *and* the secret. The server scrambles the secret, compares it with the fingerprint from step 2,
   and if they match, hands over the **access token** (the wristband).
7. **The app uses the wristband** to read the record. The wristband says which patient it is for,
   which permissions were granted, and when it expires (one hour here).

**Why two separate steps (authorize, then token)?** The browser is an exposed place: addresses end
up in history, logs and other programs. So only a worthless one-time ticket passes through it. The
valuable token is fetched directly, app to server, and only by someone who also knows the secret.

### 6.3 What `state` and PKCE each stop

- **`state`** stops a **forged link**. If a stranger emails you a link containing *their* ticket,
  our app would otherwise swap it and log you in as *their* patient. The forged link has no matching
  tag, so we reject it (we tested this: `400 State mismatch`).
- **PKCE** stops a **stolen ticket**. Someone who copies the ticket out of the address bar cannot
  get a token without the secret. In our test the server answered `401 ... Invalid PKCE Verifier`
  for the thief, and gave the real app its token.

### 6.4 Why the patient id comes from the token, never from a search

The server decided at login whose wristband this is. If the app searched for "Jane" instead, it
could find a different Jane, or someone could steer the search to another record. Showing one
patient another patient's health data is a serious privacy breach.

### 6.5 Fewer permissions

If the app only asks for (or is only granted) `patient/Patient.read`, the portal shows the Patient
and politely says "Encounter and EOB not shown: you did not grant read access." It does not crash
and does not ask the server for data it was not allowed.

(A real-world note: the free SMART sandbox does *not* itself refuse reads outside the granted
scopes, so our portal checks the scopes itself. A production system would also refuse on the
server.)

### 6.6 Machines: backend services

A nightly job has no person to click Allow. So it proves who it is with a **signature**:

1. It has a **private key** (secret, never sent anywhere). The server has the matching **public
   key**.
2. It writes a short note ("I am client X; this note expires in 4 minutes") and **signs** it with
   the private key. That signed note is a JWT.
3. The server checks the signature with the public key. Only the holder of the private key could
   have made it. The server returns a **system token** (no patient in it: a program is not one
   person).

We tested it: the real key got a token; a stranger's key was refused (`401 invalid signature`).
The extra libraries (PyJWT and cryptography) are used only for this signature, because doing the
math by hand is risky.

### 6.7 SMART versus the open server

| | Open local server | With SMART |
|---|---|---|
| Who can read? | Anyone who can reach it | Only someone with a valid token |
| Limited to one patient? | No | Yes, by the token |
| Limited to certain data? | No | Yes, by scopes |
| Expires? | No | Yes |

---

## 7. Part 4: breaking it on purpose

### 7.1 The idea

The most dangerous failures are the **quiet** ones. There are three kinds:

- **LOUD**: a clear error reaches the sender or the log, and nothing wrong is stored. Good.
- **SILENT**: the input is dropped and nobody is told.
- **WRONG DATA**: it is stored but it is incorrect, and everything looks fine.

### 7.2 What happened with the six bad inputs

| Input | Result |
|---|---|
| MRN in PID-2 instead of PID-3 (for Hospital A) | LOUD: `AE: PID-3 patient identifier is empty` |
| Local lab code `K+` instead of a standard code | LOUD: `AE: unrecognised coding system; refusing to guess` |
| The same admit sent twice, back to back | Harmless: still one Jane, nothing changed |
| An A13 "cancel discharge" | LOUD at first (we did not know A13); now supported on purpose |
| An 835 that does not balance | LOUD: refused, nothing stored |
| A login where one permission was not granted | Handled visibly: portal shows what is missing |

### 7.3 The one that was dangerous: the late old message

Jane's visit was already `finished` (with a discharge time). Then an **old copy of the admit
message** arrived late (a network retry). The pipeline said "AA, accepted", logged no warning, and
Jane's visit turned back into `in-progress` with the discharge time gone. That is **WRONG DATA**:
it looked fine, and nobody would have known.

**The fix:** every Encounter now remembers the time of the event that last changed it. An older
event is **not applied**. We still reply AA (we do have this information, and the sender should
not keep retrying), with a note saying it was not applied, and a copy goes to the error queue so
a person can see it.

### 7.4 Answers to the checkpoint questions

**Which failures were silent, and why are they the most dangerous?** The late old admit was the
silent one: the system said "all good", so nobody investigated, and the wrong data (an open visit
for a patient who had gone home) would be used later for decisions and billing. A loud failure is
annoying but is *found and fixed*. A silent one *spreads*.

**What belongs in config and what belongs in code, and why?**
- **Config** (a file per hospital): facts that *differ from hospital to hospital*. Which PID field
  holds the patient number, the time zone, the system names, the translations of that hospital's
  local lab codes, the hospital's name in MSH-4.
- **Code**: rules that are the *same for everyone*. How to parse, validate and acknowledge; the
  rule "never guess"; storing safely; refusing old events.
- **Why:** a new hospital then costs one file, not a new program, and a mistake in a config file is
  small and easy to review. Hospital B (St. Luke's) works this way: its MRN is in PID-2, its clocks
  are in Central time, and its potassium code `K` is translated to the standard code, all via
  `config/sources/hospital_b.toml`, with no code change.

**Which detector catches which kind of failure?**
- **Unknown data** (a code, hospital, message type or field we have no rule for): the **error
  queue**. It keeps the raw message for review and replay.
- **Missing data** (something that never arrived, so there is nothing to fail loudly): the
  **volume checks**: a hospital that goes quiet, or visits admitted but never discharged.
- **Wrong-but-believable data** (it looks valid, but it contradicts what we already hold): checks
  that *compare* with stored facts, like the stale-event check, and the balance check on the 835.

---

## 8. Mistakes made along the way (and what they taught)

These are honest notes, because they are the best way to remember the ideas.

1. **A CapabilityStatement only *describes* what a server can do.** It does not add capabilities.
2. **"Record" is not an HL7 word.** The unit is a *message* made of *segments*. The parser never sees the
   markers: MLLP markers are removed first.
3. **An AE needs a fix before resending.** Sending it unchanged just repeats the error. And "no
   ACK" is dangerous because the sender cannot tell "lost" from "stored but receipt lost".
4. **The collision problem is about identifier *values*.** The `system` part of the identifier
   keeps two hospitals' `MRN12345` apart. The `id` is local to one server.
5. **The 837 is the claim; the EOB is not.** The EOB is for the patient. The 835 is for the hospital.
6. **A write-off is not what the patient owes.** And coinsurance applies to the *allowed* amount.
   A CO denial is the hospital's loss, not Jane's.
7. **The reconciler checks arithmetic, not contracts.**
8. **The token endpoint is not "the secret".** The secret is the code verifier; the token endpoint
   is the address where the ticket is swapped.
9. **A bug caught by a test:** a scope ending in `.write` was treated as readable (because "write"
   contains the letter `r`). Fixed.
10. **A stale-HAPI-cache gotcha:** the FHIR server caches identical searches for about a minute, so
    a count can look out of date right after a write.
11. **The sandbox is lenient:** it accepts reads with no token, and does not check scopes. So the
    portal does its own checks.
12. **Where to explain better:** the first questions about `state` and PKCE were too abstract. A
    step-by-step real run made them clear. Showing beats telling.

---

## 9. How to run everything

All commands are run from the project folder. First time only:

```bash
source .venv/bin/activate      # leaves you with (.venv) in the prompt; "deactivate" to leave
docker compose up -d           # starts the FHIR server (Docker Desktop must be open)
```

| What | Command |
|---|---|
| The whole journey, with a trace | `python run_journey.py` |
| The volume report | `python -m interoperability_pipeline.common.report` |
| The tests | `pytest` (74 tests) |
| The listener | `python -u -m interoperability_pipeline.hl7v2.listener` |
| Send one message | `python simulators/hospital_sender.py samples/hl7/adt_a01.hl7` |
| Build the claim (no prior auth) | `python -m interoperability_pipeline.x12.builder_837p` |
| Build it with a prior auth | `python -m interoperability_pipeline.x12.builder_837p --prior-auth PA-1 --output claim.x12` |
| Payer decides | `python simulators/payer.py claim.x12 --output remit.x12` |
| Store the payment | `python -m interoperability_pipeline.x12.remittance_to_fhir remit.x12` |
| Portal with no login | `python -m interoperability_pipeline.portal.server --profile local` then open http://localhost:8000 |
| Portal with SMART login | `python -m interoperability_pipeline.portal.server --profile sandbox` |
| Machine login | `python -m interoperability_pipeline.smart.backend_services` |
| Stop the FHIR server | `docker compose down` |

If `python` says "command not found", the virtual environment is not active: run the `source`
line, or write `.venv/bin/python` instead of `python`.

Look at the server in a browser: http://localhost:8080/fhir/Patient?_pretty=true (add
`&_format=json`), and http://localhost:8080/fhir/metadata for the capability statement.

---

## 10. Where things are

```
config/settings.toml          URLs, ports, portal profiles
config/sources/*.toml         one file per hospital (Hospital A, Hospital B)
config/claims.toml            insurance, fees, contract rates, payer rules (all synthetic)
samples/hl7/  samples/x12/    the synthetic messages (good and deliberately bad)
simulators/                   hospital sender, payer, claim-data seeder
run_journey.py                the one-command demo
src/interoperability_pipeline/
    hl7v2/    MLLP, parser, validation, ACK, mappers, listener
    fhir/     the client that talks to the FHIR server
    x12/      tokenizer, 837 builder, payer rules, 835 builder/parser, EOB mapper
    smart/    PKCE, login steps, token helpers, backend services
    portal/   the tiny patient web app
    mapping/  loads the hospital config files
    common/   settings, errors, event log + error queue, report
tests/                        74 tests
docs/                         ARCHITECTURE.md (design + "how to add X"), LEARNING_LOG.md
GUIDE.md                      this file
```

Each package has its own short README.md.

---

## 11. How to extend it later

The full "how to add X" guides are in `docs/ARCHITECTURE.md`. The short versions:

- **A new hospital:** copy `config/sources/hospital_a.toml`, edit the values. No code.
- **A new message event (for example A08):** add it to the supported list and, if it changes the
  visit, to the status table.
- **A new resource from a message:** one new pure function in `hl7v2/mappers.py` and one call in
  the listener.
- **A new X12 transaction (eligibility 270/271, claim status 276/277):** add a builder/parser
  next to the existing ones and reuse the tokenizer.
- **A different FHIR server, or the Epic sandbox:** change the base URL (and the portal profile).
- **An agent that reads the same data:** it gets a system token with backend services and reads
  through the same FHIR client. It would see exactly what the portal sees.

---

## 12. What is not built, and what to be careful about

- Everything is **synthetic** and runs on your own computer. Never put real patient data in.
- Never paste a token or key into a website. Our code decodes tokens locally.
- The local FHIR server is **open on purpose** for learning. A real one would sit behind SMART.
- The SMART sandbox is lenient about scopes, as noted above.
- **Not built:** an "expected payment" check against the contract; matching one person across
  hospitals (a master patient index); replaying messages from the error queue; refreshing tokens.
- The prior-authorization number and the contract rates are simulated by the payer rules in
  `config/claims.toml`.

---

## 13. Free places to read more

- FHIR R4: https://hl7.org/fhir/R4/
- Patient: https://hl7.org/fhir/R4/patient.html , Encounter: https://hl7.org/fhir/R4/encounter.html ,
  Observation: https://hl7.org/fhir/R4/observation.html ,
  ExplanationOfBenefit: https://hl7.org/fhir/R4/explanationofbenefit.html
- Conditional update: https://hl7.org/fhir/R4/http.html#cond-update
- HL7 v2 field definitions: https://hl7-definition.caristix.com
- HL7 v2 to FHIR mapping: https://build.fhir.org/ig/HL7/v2-to-fhir/
- X12 837 and 835 (segment by segment): https://www.stedi.com/edi/x12-005010/837 and
  https://www.stedi.com/edi/x12-005010/835
- Adjustment codes: https://x12.org/codes
- SMART App Launch: https://hl7.org/fhir/smart-app-launch/ ,
  backend services: https://hl7.org/fhir/smart-app-launch/backend-services.html
- PKCE (the official explanation): https://datatracker.ietf.org/doc/html/rfc7636
- SMART sandbox: https://launch.smarthealthit.org and https://docs.smarthealthit.org
- HAPI FHIR: https://hapifhir.io
- Synthea (realistic synthetic patients, for future use): https://synthetichealth.github.io/synthea/
- The Epic sandbox (a possible next step): https://fhir.epic.com
