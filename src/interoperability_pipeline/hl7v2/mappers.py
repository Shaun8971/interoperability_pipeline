"""Map parsed HL7 v2 segments to FHIR R4 resources (pure functions, no network).

What it does: builds FHIR resource dictionaries from PID (Patient), PV1 (Encounter)
and OBX (Observation).
Where it sits in the pipeline: parser --> validation --> **mappers** --> FHIR client.
Standard and spec:
  - FHIR Patient: https://hl7.org/fhir/R4/patient.html
  - FHIR Encounter: https://hl7.org/fhir/R4/encounter.html
  - FHIR Observation: https://hl7.org/fhir/R4/observation.html
  - LOINC (lab codes, free): https://loinc.org
  - UCUM (units, free): https://ucum.org
  - HL7 v2-to-FHIR mapping guide (free): https://build.fhir.org/ig/HL7/v2-to-fhir/
  - PID segment: https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/PID
Inputs / outputs: list of segments + config -> FHIR resource dict.
How to extend: add one ``<resource>_from_hl7`` function per resource; keep it pure.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from interoperability_pipeline.common.errors import MappingError
from interoperability_pipeline.hl7v2.parser import find_segment, get_field, split_components

# --- Segment names ------------------------------------------------------------------
SEGMENT_PID = "PID"  # patient identification
SEGMENT_PV1 = "PV1"  # patient visit
SEGMENT_OBR = "OBR"  # observation request (the lab order)

# --- Code systems and code maps (each with its meaning) -------------------------------
IDENTIFIER_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"  # HL7 v2 table 0203
HL7_DATE_LENGTH = 8  # YYYYMMDD
HL7_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"  # YYYYMMDDHHMMSS
HL7_TIMESTAMP_LENGTH = 14
# MSH-9.2 trigger event -> FHIR Encounter.status
TRIGGER_TO_ENCOUNTER_STATUS = {
    "A01": "in-progress",  # admit: the visit has started
    "A03": "finished",  # discharge: the visit is over
    "A13": "in-progress",  # cancel discharge: the patient is still here
}
# Extension recording WHEN the event that last changed an Encounter happened (EVN-2). Used to
# refuse older events that arrive late (see the stale-message check in the listener).
LAST_EVENT_EXTENSION_URL = "http://interop-pipeline.example.org/StructureDefinition/last-event-time"
# PV1-2 patient class (HL7 table 0004) -> v3 ActCode used by Encounter.class
ENCOUNTER_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
# Observation constants
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
OBSERVATION_CATEGORY_LABORATORY = "laboratory"  # every ORU^R01 result we handle is a lab result
INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
UCUM_SYSTEM = "http://unitsofmeasure.org"  # FHIR's standard unit system
VALUE_TYPE_NUMERIC = "NM"  # OBX-2 value type: numeric
DEFAULT_CODING_SYSTEMS = {"LN": "http://loinc.org"}  # used when a source file lists none
# OBX-11 result status (HL7 table 0085) -> FHIR Observation.status
RESULT_STATUS_MAP = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled"}
PATIENT_CLASS_MAP = {"I": "IMP", "O": "AMB", "E": "EMER"}  # inpatient, outpatient, emergency
# PID-8 administrative sex (HL7 table 0001) -> FHIR AdministrativeGender
GENDER_MAP = {"F": "female", "M": "male", "O": "other", "U": "unknown"}
FHIR_GENDER_FALLBACK = "unknown"  # used when PID-8 is empty or an unrecognised code
# Position of each piece inside PID-3 (type CX) and PID-11 (type XAD), 0-based.
CX_ID_NUMBER, CX_ASSIGNING_AUTHORITY, CX_TYPE_CODE = 0, 3, 4
XAD_STREET, XAD_CITY, XAD_STATE, XAD_POSTAL_CODE = 0, 2, 3, 4


@dataclass(frozen=True)
class SourceConfig:
    """Everything that differs per sending hospital (one file in config/sources/)."""

    patient_identifier_systems: dict[str, str]  # PID-3.4 authority -> identifier system URI
    visit_identifier_systems: dict[str, str]  # authority -> visit-number system URI
    observation_identifier_systems: dict[str, str]  # authority -> lab-result system URI
    timezone: str  # IANA zone name for HL7 timestamps, e.g. "America/New_York"
    patient_id_field: int = 3  # which PID field holds the patient identifier (standard: 3)
    # OBX-3.3 coding system name -> FHIR code system URI; codes in these are used as-is
    coding_systems: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CODING_SYSTEMS))
    # "<system>|<local code>" -> {system, code, display}: reviewed translations of local codes
    code_translations: dict[str, dict[str, str]] = field(default_factory=dict)
    name: str = "unnamed"  # file name, for logs
    sending_facility: str = ""  # MSH-4 value that selects this config


def identifier_search_value(system: str, value: str) -> str:
    """Format a FHIR identifier search token ``system|value``.

    Example:
        >>> identifier_search_value("http://mercy-general.example.org/mrn", "MRN12345")
        'http://mercy-general.example.org/mrn|MRN12345'
    """
    return f"{system}|{value}"


def patient_identifier(segments: list[list[str]], config: SourceConfig) -> dict[str, str]:
    """Return {"system": ..., "value": ...} for the patient (field set by config, normally PID-3).

    Raises MappingError if the assigning authority is not in config: guessing a system
    could merge two different people, so we refuse instead.

    Example:
        PID-3 'MRN12345^^^MERCY^MR' -> {"system": ".../mrn", "value": "MRN12345"}
    """
    pid = _require_pid(segments)
    # first repetition only of the configured identifier field (PID-3, or PID-2 for some hospitals)
    parts = split_components(get_field(pid, config.patient_id_field).split("~")[0])
    parts += [""] * (CX_TYPE_CODE + 1 - len(parts))  # pad so indexing is always safe
    authority = _patient_authority(segments, config)
    if authority not in config.patient_identifier_systems:
        raise MappingError(f"unknown assigning authority {authority!r} in PID-3")
    system = config.patient_identifier_systems[authority]
    return {"system": system, "value": parts[CX_ID_NUMBER]}  # PID-3.1


def patient_from_hl7(segments: list[list[str]], config: SourceConfig) -> dict[str, Any]:
    """Build a FHIR Patient from the PID segment.

    Example (Jane's A01): returns
        {"resourceType": "Patient",
         "identifier": [{"use": "usual", "type": {...MR...}, "system": ".../mrn",
                         "value": "MRN12345"}],
         "name": [{"family": "DOE", "given": ["JANE", "Q"]}],
         "birthDate": "1980-03-15", "gender": "female", "address": [...]}
    """
    pid = _require_pid(segments)
    identifier = patient_identifier(segments, config)
    identifier_text = get_field(pid, config.patient_id_field)  # PID-3 (or the configured field)
    type_code = (split_components(identifier_text) + [""] * 5)[CX_TYPE_CODE]  # CX.5: e.g. MR

    name_parts = split_components(get_field(pid, 5))  # PID-5: family^given^middle
    given_names = [part for part in name_parts[1:3] if part]
    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "identifier": [  # PID-3 -> Patient.identifier
            {
                "use": "usual",
                "type": {"coding": [{"system": IDENTIFIER_TYPE_SYSTEM, "code": type_code}]},
                **identifier,
            }
        ],
        "name": [{"family": name_parts[0], "given": given_names}],  # PID-5 -> Patient.name
        "birthDate": _hl7_date_to_fhir(get_field(pid, 7)),  # PID-7 -> Patient.birthDate
        "gender": GENDER_MAP.get(
            get_field(pid, 8), FHIR_GENDER_FALLBACK
        ),  # PID-8 -> Patient.gender
    }
    address_parts = (
        split_components(get_field(pid, 11)) + [""] * 5
    )  # PID-11: street^^city^state^zip
    if any(address_parts):
        patient["address"] = [  # PID-11 -> Patient.address
            {
                "line": [address_parts[XAD_STREET]],
                "city": address_parts[XAD_CITY],
                "state": address_parts[XAD_STATE],
                "postalCode": address_parts[XAD_POSTAL_CODE],
            }
        ]
    return patient


def encounter_identifier(segments: list[list[str]], config: SourceConfig) -> dict[str, str]:
    """Return {"system": ..., "value": ...} for the visit in PV1-19.

    Example:
        PV1-19 'V0001' -> {"system": ".../visit", "value": "V0001"}
    """
    pv1 = _require_segment(segments, SEGMENT_PV1)
    visit_number = get_field(pv1, 19).strip()  # PV1-19: visit number
    if not visit_number:
        raise MappingError("PV1-19 visit number is empty; cannot identify the Encounter")
    authority = _patient_authority(segments, config)  # the visit belongs to the same hospital
    if authority not in config.visit_identifier_systems:
        raise MappingError(f"no visit identifier system configured for {authority!r}")
    return {"system": config.visit_identifier_systems[authority], "value": visit_number}


def encounter_from_hl7(
    segments: list[list[str]], config: SourceConfig, patient_id: str
) -> dict[str, Any]:
    """Build a FHIR Encounter from PV1 (and the trigger event in MSH-9.2).

    ``patient_id`` is the server-assigned Patient id (from the earlier write), so
    Encounter.subject can point at Jane.

    Example (Jane's A03): status "finished", period start 2026-11-14T09:30:00-05:00,
    end 2026-11-16T11:00:00-05:00, subject Patient/1000.
    """
    pv1 = _require_segment(segments, SEGMENT_PV1)
    msh = _require_segment(segments, "MSH")
    trigger_event = (split_components(get_field(msh, 9)) + ["", ""])[1]  # MSH-9.2
    if trigger_event not in TRIGGER_TO_ENCOUNTER_STATUS:
        raise MappingError(f"no Encounter status defined for trigger event {trigger_event!r}")

    patient_class = get_field(pv1, 2)  # PV1-2: patient class
    if patient_class not in PATIENT_CLASS_MAP:
        raise MappingError(f"unsupported patient class {patient_class!r} in PV1-2")

    period: dict[str, str] = {}
    admit_time = get_field(pv1, 44)  # PV1-44: admit date/time
    discharge_time = get_field(pv1, 45)  # PV1-45: discharge date/time
    if admit_time:
        period["start"] = _hl7_timestamp_to_fhir(admit_time, config.timezone)
    if discharge_time:
        period["end"] = _hl7_timestamp_to_fhir(discharge_time, config.timezone)

    encounter: dict[str, Any] = {
        "resourceType": "Encounter",
        "identifier": [encounter_identifier(segments, config)],  # PV1-19 -> Encounter.identifier
        "status": TRIGGER_TO_ENCOUNTER_STATUS[trigger_event],  # MSH-9.2 -> Encounter.status
        "class": {  # PV1-2 -> Encounter.class
            "system": ENCOUNTER_CLASS_SYSTEM,
            "code": PATIENT_CLASS_MAP[patient_class],
        },
        "subject": {"reference": f"Patient/{patient_id}"},  # the Patient we just wrote
    }
    if period:
        encounter["period"] = period  # PV1-44/45 -> Encounter.period
    encounter["extension"] = [  # EVN-2 (or MSH-7) -> when this change happened, for ordering
        {"url": LAST_EVENT_EXTENSION_URL, "valueDateTime": message_event_time(segments, config)}
    ]
    return encounter


def message_event_time(segments: list[list[str]], config: SourceConfig) -> str:
    """When the event happened: EVN-2 if present, else the message time MSH-7, as a FHIR dateTime.

    Example: EVN-2 '20261116110000' with zone America/New_York -> '2026-11-16T11:00:00-05:00'
    """
    evn = find_segment(segments, "EVN")
    msh = _require_segment(segments, "MSH")
    raw = (get_field(evn, 2) if evn else "") or get_field(msh, 7)  # EVN-2 recorded, MSH-7 sent
    return _hl7_timestamp_to_fhir(raw, config.timezone)


def observation_from_hl7(
    segments: list[list[str]],
    obx: list[str],
    config: SourceConfig,
    patient_id: str,
    encounter_id: str | None,
) -> dict[str, Any]:
    """Build a FHIR Observation from one OBX segment (plus OBR for its order number).

    Not mapped yet (deliberately, to stay small): OBX-7 reference range, OBX-4 sub-ID.

    Example (Jane's potassium): code LOINC 2823-3, valueQuantity 4.1 mmol/L, status final,
    effectiveDateTime 2026-11-14T10:00:00-05:00, subject Patient/1000, encounter Encounter/1001.
    """
    value_type = get_field(obx, 2)  # OBX-2: value type
    if value_type != VALUE_TYPE_NUMERIC:
        raise MappingError(f"unsupported OBX-2 value type {value_type!r}; only NM is mapped")

    code_parts = split_components(get_field(obx, 3)) + [""] * 3  # OBX-3: id^text^coding system
    code_id, code_text, coding_system_name = code_parts[:3]
    translation = config.code_translations.get(f"{coding_system_name}|{code_id}")
    if translation:  # a local code a human has already mapped to a standard one (see config)
        coding = {
            "system": translation["system"],
            "code": translation["code"],
            "display": translation.get("display", code_text),
        }
    elif coding_system_name in config.coding_systems:  # already a standard code system
        coding = {
            "system": config.coding_systems[coding_system_name],
            "code": code_id,
            "display": code_text,
        }
    else:  # never guess what an unknown local code means
        raise MappingError(
            f"OBX-3 code {code_id!r} uses unrecognised coding system {coding_system_name!r}; "
            "refusing to guess what it means"
        )

    try:
        quantity_value = float(get_field(obx, 5))  # OBX-5: observation value
    except ValueError as error:
        raise MappingError(f"OBX-5 value {get_field(obx, 5)!r} is not a number") from error

    result_status = get_field(obx, 11)  # OBX-11: observation result status
    if result_status not in RESULT_STATUS_MAP:
        raise MappingError(f"unsupported OBX-11 result status {result_status!r}")

    unit = get_field(obx, 6)  # OBX-6: units
    observation: dict[str, Any] = {
        "resourceType": "Observation",
        "identifier": [_observation_identifier(segments, obx, config)],
        "status": RESULT_STATUS_MAP[result_status],  # OBX-11 -> Observation.status
        "category": [
            {
                "coding": [
                    {
                        "system": OBSERVATION_CATEGORY_SYSTEM,
                        "code": OBSERVATION_CATEGORY_LABORATORY,
                    }
                ]
            }
        ],
        "code": {  # OBX-3 -> Observation.code
            "coding": [coding],
            "text": code_text,
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "valueQuantity": {  # OBX-5 + OBX-6 -> Observation.valueQuantity
            "value": quantity_value,
            "unit": unit,
            "system": UCUM_SYSTEM,
            "code": unit,
        },
    }
    if encounter_id is not None:
        observation["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    observed_at = get_field(obx, 14)  # OBX-14: date/time of the observation
    if observed_at:
        observation["effectiveDateTime"] = _hl7_timestamp_to_fhir(observed_at, config.timezone)
    interpretation = get_field(obx, 8)  # OBX-8: abnormal flags (N, H, L...)
    if interpretation:
        observation["interpretation"] = [
            {"coding": [{"system": INTERPRETATION_SYSTEM, "code": interpretation}]}
        ]
    return observation


def _observation_identifier(
    segments: list[list[str]], obx: list[str], config: SourceConfig
) -> dict[str, str]:
    """Identifier = <OBR-3 order number>-<OBX-1 set ID>, e.g. 'LAB0001-1'.

    Why: a result has no ID of its own. This pair is stable across resends, so a
    resent message updates the same Observation instead of adding a second potassium.
    """
    obr = _require_segment(segments, SEGMENT_OBR)
    order_number = split_components(get_field(obr, 3))[0].strip()  # OBR-3.1: filler order number
    set_id = get_field(obx, 1).strip()  # OBX-1: set ID within the message
    if not order_number or not set_id:
        raise MappingError("OBR-3 order number or OBX-1 set ID is empty; cannot identify result")
    authority = _patient_authority(segments, config)
    if authority not in config.observation_identifier_systems:
        raise MappingError(f"no result identifier system configured for {authority!r}")
    return {
        "system": config.observation_identifier_systems[authority],
        "value": f"{order_number}-{set_id}",
    }


def _patient_authority(segments: list[list[str]], config: SourceConfig) -> str:
    """CX.4 of the configured identifier field: the assigning authority, e.g. 'MERCY'."""
    pid = _require_pid(segments)
    parts = split_components(get_field(pid, config.patient_id_field).split("~")[0]) + [""] * 5
    return parts[CX_ASSIGNING_AUTHORITY]


def _require_segment(segments: list[list[str]], name: str) -> list[str]:
    segment = find_segment(segments, name)
    if segment is None:
        raise MappingError(f"message has no {name} segment")
    return segment


def _require_pid(segments: list[list[str]]) -> list[str]:
    pid = find_segment(segments, SEGMENT_PID)
    if pid is None:
        raise MappingError("message has no PID segment")
    return pid


def _hl7_date_to_fhir(hl7_date: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD (FHIR date). Raises MappingError otherwise.

    Example:
        >>> _hl7_date_to_fhir("19800315")
        '1980-03-15'
    """
    if len(hl7_date) != HL7_DATE_LENGTH or not hl7_date.isdigit():
        raise MappingError(f"unsupported date {hl7_date!r}; expected YYYYMMDD")
    return f"{hl7_date[:4]}-{hl7_date[4:6]}-{hl7_date[6:]}"


def _hl7_timestamp_to_fhir(hl7_timestamp: str, timezone_name: str) -> str:
    """Convert YYYYMMDDHHMMSS (no zone) to a FHIR dateTime with the source's zone offset.

    Why: FHIR requires a zone on any dateTime that has a time. Silently assuming UTC
    would shift Jane's admission by hours, a classic "wrong but believable" bug.

    Example:
        >>> _hl7_timestamp_to_fhir("20261114093000", "America/New_York")
        '2026-11-14T09:30:00-05:00'
    """
    if len(hl7_timestamp) != HL7_TIMESTAMP_LENGTH or not hl7_timestamp.isdigit():
        raise MappingError(f"unsupported timestamp {hl7_timestamp!r}; expected YYYYMMDDHHMMSS")
    parsed = datetime.strptime(hl7_timestamp, HL7_TIMESTAMP_FORMAT)
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).isoformat()
