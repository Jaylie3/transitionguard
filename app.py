"""
TransitionGuard FastAPI MCP HTTP Server
Exposes clinical tools via MCP JSON-RPC 2.0 protocol with SHARP context propagation.
"""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mcp_server import (
    CharlsonIndex,
    DrugInteractionChecker,
    LACEPlusCalculator,
    LACEPlusInput,
    TransitionGuardMCPServer,
)
from fhir_workflow import (
    DischargeEvent,
    FHIRRiskAssessmentBuilder,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("transitionguard")

app = FastAPI(
    title="TransitionGuard MCP Server",
    description=(
        "Intelligent discharge care coordination tools. "
        "Exposes LACE+, Charlson, and Drug Interaction tools via MCP protocol."
    ),
    version="1.0.0",
)

# ============================================================================
# SHARP CONTEXT
# ============================================================================

SHARP_PATIENT_HEADER = "x-sharp-patient-id"
SHARP_TOKEN_HEADER = "x-sharp-fhir-token"
SHARP_SERVER_HEADER = "x-sharp-fhir-server-url"
SHARP_ENCOUNTER_HEADER = "x-sharp-encounter-id"


@dataclass
class SHARPContext:
    """SHARP extension context propagated by Prompt Opinion from the EHR session."""

    patient_id: Optional[str] = None
    fhir_token: Optional[str] = None
    fhir_server_url: Optional[str] = None
    encounter_id: Optional[str] = None

    @classmethod
    def from_headers(cls, headers) -> "SHARPContext":
        return cls(
            patient_id=headers.get(SHARP_PATIENT_HEADER),
            fhir_token=headers.get(SHARP_TOKEN_HEADER),
            fhir_server_url=headers.get(SHARP_SERVER_HEADER),
            encounter_id=headers.get(SHARP_ENCOUNTER_HEADER),
        )

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "patient_id": self.patient_id,
            "fhir_server_url": self.fhir_server_url,
            "encounter_id": self.encounter_id,
            "fhir_token_present": bool(self.fhir_token),
        }


# ============================================================================
# MCP TOOL DEFINITIONS (MCP spec: inputSchema in camelCase)
# ============================================================================

MCP_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "lace_plus_calculator",
        "description": (
            "Calculates LACE+ readmission risk score based on patient factors. "
            "Returns 30-day readmission probability and risk quintile (Very Low / Low / "
            "Medium / High / Very High). Validated on 6,752 patients at Toronto Western Hospital."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "age": {"type": "integer", "description": "Patient age in years"},
                "length_of_stay_days": {
                    "type": "integer",
                    "description": "Length of hospitalization in days",
                },
                "charlson_score": {
                    "type": "integer",
                    "description": "Charlson comorbidity index score (use charlson_index_calculator first)",
                },
                "ed_visits_6mo": {
                    "type": "integer",
                    "description": "Number of ED visits in past 6 months",
                },
                "er_visits_past": {
                    "type": "integer",
                    "description": "Patient has prior ER visits (1=yes, 0=no)",
                },
            },
            "required": [
                "age",
                "length_of_stay_days",
                "charlson_score",
                "ed_visits_6mo",
                "er_visits_past",
            ],
        },
    },
    {
        "name": "charlson_index_calculator",
        "description": (
            "Calculates Charlson comorbidity score from ICD-10 condition codes. "
            "Returns score and 1-year mortality estimate. Used in 10,000+ publications."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conditions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of active ICD-10 condition codes (e.g. ['I10', 'E11', 'J44'])",
                },
                "age": {"type": "integer", "description": "Patient age in years"},
            },
            "required": ["conditions", "age"],
        },
    },
    {
        "name": "drug_interaction_checker",
        "description": (
            "Screens a medication list for clinically significant drug-drug interactions. "
            "Queries the NLM RxNav Interaction API (live) and falls back to a curated local "
            "database. Returns severity (severe/moderate/mild) and clinical recommendations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "medications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "medication_name": {"type": "string"},
                            "rxnorm_code": {"type": "string"},
                        },
                        "required": ["medication_name", "rxnorm_code"],
                    },
                    "description": "List of medications with RxNorm codes",
                }
            },
            "required": ["medications"],
        },
    },
]


# ============================================================================
# RXNAV LIVE DRUG INTERACTION LOOKUP
# ============================================================================

RXNAV_API_BASE = "https://rxnav.nlm.nih.gov/REST"
RXNAV_SEVERITY_MAP = {
    "high": "severe",
    "moderate": "moderate",
    "low": "mild",
    "N/A": "mild",
}


async def lookup_rxnav_interactions(
    medications: List[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """
    Query the NLM RxNav Interaction API for drug-drug interactions.
    Returns structured interaction data or None if the API is unavailable.

    API reference: https://rxnav.nlm.nih.gov/InteractionAPIs.html
    """
    rxcuis = [m.get("rxnorm_code", "") for m in medications if m.get("rxnorm_code")]
    if len(rxcuis) < 2:
        return None

    url = f"{RXNAV_API_BASE}/interaction/list.json"
    params = {"rxcuis": " ".join(rxcuis)}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("RxNav API unavailable, falling back to local DB: %s", exc)
        return None

    interactions_found = []
    severity_count = {"severe": 0, "moderate": 0, "mild": 0}

    groups = data.get("fullInteractionTypeGroup") or []
    for group in groups:
        for itype in group.get("fullInteractionType", []):
            for pair in itype.get("interactionPair", []):
                raw_severity = (pair.get("severity") or "N/A").lower()
                normalized = RXNAV_SEVERITY_MAP.get(raw_severity, "mild")
                description = pair.get("description", "")
                concepts = pair.get("interactionConcept", [])

                # Map RxCUI back to the original medication name
                def _name_for_rxcui(rxcui: str) -> str:
                    for med in medications:
                        if med.get("rxnorm_code") == rxcui:
                            return med["medication_name"]
                    return rxcui

                med_names = []
                for concept in concepts:
                    min_concepts = concept.get("minConceptItem", {})
                    rxcui = min_concepts.get("rxcui", "")
                    med_names.append(_name_for_rxcui(rxcui))

                if len(med_names) >= 2:
                    interactions_found.append(
                        {
                            "medication_1": med_names[0],
                            "medication_2": med_names[1],
                            "severity": normalized,
                            "recommendation": description,
                            "source": "NLM RxNav",
                        }
                    )
                    severity_count[normalized] += 1

    return {
        "interactions_found": interactions_found,
        "total_interactions": len(interactions_found),
        "severity_summary": severity_count,
        "requires_pharmacist_review": severity_count["severe"] > 0,
        "data_source": "NLM RxNav Interaction API",
        "justification": (
            f"Screened {len(rxcuis)} medications via NLM RxNav API.\n"
            f"Interactions found: {len(interactions_found)}\n"
            f"- Severe: {severity_count['severe']}\n"
            f"- Moderate: {severity_count['moderate']}\n"
            f"- Mild: {severity_count['mild']}\n"
        ),
    }


# ============================================================================
# TOOL DISPATCH
# ============================================================================

async def dispatch_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    sharp: SHARPContext,
) -> Dict[str, Any]:
    """Dispatch a tool call to the appropriate handler."""

    if tool_name == "lace_plus_calculator":
        input_data = LACEPlusInput(
            age=arguments["age"],
            length_of_stay_days=arguments["length_of_stay_days"],
            charlson_score=arguments["charlson_score"],
            ed_visits_6mo=arguments["ed_visits_6mo"],
            er_visits_past=arguments["er_visits_past"],
        )
        result = LACEPlusCalculator.calculate(input_data)
        payload = asdict(result)
        if sharp.patient_id:
            payload["sharp_patient_id"] = sharp.patient_id
        if sharp.encounter_id:
            payload["sharp_encounter_id"] = sharp.encounter_id
        return payload

    elif tool_name == "charlson_index_calculator":
        return CharlsonIndex.calculate(
            conditions=arguments["conditions"],
            age=arguments["age"],
        )

    elif tool_name == "drug_interaction_checker":
        medications = arguments["medications"]
        # Try live RxNav first; fall back to local curated DB
        rxnav_result = await lookup_rxnav_interactions(medications)
        if rxnav_result is not None:
            return rxnav_result
        return DrugInteractionChecker.check_interactions(medications)

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


# ============================================================================
# JSON-RPC HELPERS
# ============================================================================

def jsonrpc_ok(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def jsonrpc_error(id_: Any, code: int, message: str) -> JSONResponse:
    body = {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message},
    }
    return JSONResponse(content=body, status_code=200)


# ============================================================================
# MCP ENDPOINT
# ============================================================================

@app.post("/mcp")
async def mcp_handler(request: Request) -> JSONResponse:
    """
    MCP JSON-RPC 2.0 endpoint.

    Supported methods:
    - initialize       — Protocol handshake; returns server capabilities.
    - tools/list       — Returns the list of available tool definitions.
    - tools/call       — Invokes a named tool with provided arguments.

    SHARP context is read from request headers and injected into tool results
    so that patient/encounter identity flows through multi-agent chains.
    """
    try:
        body = await request.json()
    except Exception:
        return jsonrpc_error(None, -32700, "Parse error: body is not valid JSON")

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if not method:
        return jsonrpc_error(rpc_id, -32600, "Invalid Request: 'method' is required")

    sharp = SHARPContext.from_headers(request.headers)

    # ── initialize ───────────────────────────────────────────────────────────
    if method == "initialize":
        return JSONResponse(
            content=jsonrpc_ok(
                rpc_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "TransitionGuard",
                        "version": "1.0.0",
                        "description": (
                            "Discharge care coordination MCP server. "
                            "Provides LACE+, Charlson, and drug interaction tools."
                        ),
                    },
                },
            )
        )

    # ── tools/list ───────────────────────────────────────────────────────────
    elif method == "tools/list":
        return JSONResponse(content=jsonrpc_ok(rpc_id, {"tools": MCP_TOOL_DEFS}))

    # ── tools/call ───────────────────────────────────────────────────────────
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        if not tool_name:
            return jsonrpc_error(rpc_id, -32602, "Invalid params: 'name' is required")

        try:
            result = await dispatch_tool(tool_name, arguments, sharp)
        except (KeyError, TypeError) as exc:
            return jsonrpc_error(rpc_id, -32602, f"Invalid params: {exc}")
        except ValueError as exc:
            return jsonrpc_error(rpc_id, -32601, str(exc))

        logger.info(
            "tool=%s patient=%s encounter=%s",
            tool_name,
            sharp.patient_id or "unknown",
            sharp.encounter_id or "unknown",
        )

        return JSONResponse(
            content=jsonrpc_ok(
                rpc_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2)}
                    ],
                    "sharp_context": sharp.to_dict(),
                },
            )
        )

    # ── unknown method ────────────────────────────────────────────────────────
    else:
        return jsonrpc_error(rpc_id, -32601, f"Method not found: {method}")


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
def health_check() -> Dict[str, Any]:
    """Liveness probe endpoint used by the Dockerfile HEALTHCHECK."""
    tools = [t["name"] for t in MCP_TOOL_DEFS]
    return {
        "status": "ok",
        "service": "TransitionGuard MCP Server",
        "version": "1.0.0",
        "tools_available": tools,
        "sharp_headers": [
            SHARP_PATIENT_HEADER,
            SHARP_TOKEN_HEADER,
            SHARP_SERVER_HEADER,
            SHARP_ENCOUNTER_HEADER,
        ],
    }


# ============================================================================
# FHIR DISCHARGE TRIGGER WEBHOOK
# ============================================================================

def _patient_id_from_encounter(encounter: Dict[str, Any], sharp: SHARPContext) -> str:
    """
    Extract patient ID from an Encounter resource.
    Falls back to the SHARP context patient ID when the subject reference is absent.
    """
    patient_ref = (encounter.get("subject") or {}).get("reference", "")
    if patient_ref:
        return patient_ref.split("/")[-1] if "/" in patient_ref else patient_ref
    return sharp.patient_id or ""


@app.post("/fhir/discharge-trigger")
async def fhir_discharge_trigger(request: Request) -> Dict[str, Any]:
    """
    FHIR Subscription rest-hook endpoint.

    Receives an Encounter resource when a patient is discharged
    (Encounter.status = 'finished') and initiates the TransitionGuard workflow.
    SHARP context headers carry the EHR session credentials so downstream
    FHIR queries are automatically authorized.
    """
    sharp = SHARPContext.from_headers(request.headers)

    try:
        encounter = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be a valid FHIR Encounter JSON")

    resource_type = encounter.get("resourceType")
    status = encounter.get("status")

    if resource_type != "Encounter":
        raise HTTPException(
            status_code=422,
            detail=f"Expected resourceType 'Encounter', got '{resource_type}'",
        )

    if status != "finished":
        return {
            "received": True,
            "action": "skipped",
            "reason": f"Encounter status is '{status}'; only 'finished' triggers workflow",
        }

    # Extract core fields from the FHIR Encounter resource
    encounter_id = encounter.get("id", "unknown")
    patient_id = _patient_id_from_encounter(encounter, sharp)

    period = encounter.get("period") or {}
    discharge_datetime = period.get("end", "")

    length_obj = encounter.get("length") or {}
    try:
        length_of_stay_days = int(length_obj.get("value") or 0)
    except (TypeError, ValueError):
        length_of_stay_days = 0

    coding_list = (
        (encounter.get("hospitalization") or {})
        .get("dischargeDisposition", {})
        .get("coding") or [{}]
    )
    disposition = (coding_list[0] if coding_list else {}).get("display", "unknown")

    hospital = (encounter.get("serviceProvider") or {}).get("display", "unknown")

    event = DischargeEvent(
        encounter_id=encounter_id,
        patient_id=patient_id,
        discharge_datetime=discharge_datetime,
        hospital_name=hospital,
        discharge_disposition=disposition,
        length_of_stay_days=length_of_stay_days,
    )

    logger.info(
        "Discharge trigger received: encounter=%s patient=%s los=%sd disposition=%s sharp_patient=%s",
        encounter_id,
        patient_id,
        length_of_stay_days,
        disposition,
        sharp.patient_id or "not_in_headers",
    )

    return {
        "received": True,
        "action": "workflow_initiated",
        "discharge_event": event.to_dict(),
        "sharp_context": sharp.to_dict(),
        "next_steps": [
            "Query FHIR for patient conditions, medications, and encounter history",
            "Calculate Charlson comorbidity index",
            "Calculate LACE+ readmission risk score",
            "Check drug-drug interactions",
            "Identify care gaps (CareGap sub-agent)",
            "Generate patient education (PatientEd sub-agent)",
            "Assemble Transition Care Packet",
        ],
    }


# ============================================================================
# ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
