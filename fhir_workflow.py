"""
FHIR Workflow Integration for TransitionGuard
Handles discharge event triggers and coordinates with EHR systems
"""

from dataclasses import dataclass
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import json


# ============================================================================
# FHIR ENCOUNTER TRIGGER & EVENT SUBSCRIPTION
# ============================================================================

FHIR_SUBSCRIPTION_CONFIG = {
    "resourceType": "Subscription",
    "id": "transition-guard-discharge-trigger",
    "status": "active",
    "reason": "Trigger TransitionGuard on patient discharge",
    "criteria": "Encounter?status=finished",
    "channel": {
        "type": "rest-hook",
        "endpoint": "https://transitionguard.promptopinion.cloud/api/v1/fhir/discharge-trigger",
        "header": ["Authorization: Bearer {api_key}"]
    },
    "end": "2027-12-31T23:59:59Z"
}


@dataclass
class DischargeEvent:
    """Represents a patient discharge event from FHIR Encounter resource"""
    encounter_id: str
    patient_id: str
    discharge_datetime: str
    hospital_name: str
    discharge_disposition: str  # e.g., "home", "SNF", "rehab"
    length_of_stay_days: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "encounter_id": self.encounter_id,
            "patient_id": self.patient_id,
            "discharge_datetime": self.discharge_datetime,
            "hospital_name": self.hospital_name,
            "discharge_disposition": self.discharge_disposition,
            "length_of_stay_days": self.length_of_stay_days,
            "trigger_timestamp": datetime.now().isoformat()
        }


# ============================================================================
# FHIR QUERY TEMPLATES FOR PATIENT CONTEXT
# ============================================================================

FHIR_QUERY_TEMPLATES = {
    # Core demographics
    "patient": {
        "url": "GET /Patient/{patient_id}",
        "required_fields": ["identifier", "name", "birthDate", "address", "telecom", "maritalStatus"],
        "description": "Get patient demographics for care packet personalization"
    },
    
    # Discharge encounter details
    "encounter": {
        "url": "GET /Encounter/{encounter_id}",
        "required_fields": ["status", "period", "hospitalization", "reasonCode", "class", "type"],
        "description": "Get admission reason, length of stay, disposition"
    },
    
    # Active conditions (comorbidities, diagnoses)
    "conditions": {
        "url": "GET /Condition?patient={patient_id}&clinical-status=active&_count=100",
        "required_fields": ["code", "clinicalStatus", "verificationStatus", "onsetDateTime"],
        "description": "Get all active diagnoses for LACE+ calculation and gap analysis"
    },
    
    # Current medications (for reconciliation)
    "medications": {
        "url": "GET /MedicationRequest?patient={patient_id}&status=active&_count=100",
        "required_fields": ["medicationCodeableConcept", "dosageInstruction", "authorizeddOn", "dosageInstruction.timing"],
        "description": "Get discharge medication list for interaction checking"
    },
    
    # Recent hospital encounters (for ED/ER visit count)
    "recent_encounters": {
        "url": "GET /Encounter?patient={patient_id}&status=finished&date=ge{6_months_ago}&_count=50",
        "required_fields": ["period", "type", "class", "hospitalization.admitSource"],
        "description": "Get recent encounters to count ED/ER visits for LACE+ E component"
    },
    
    # Lab results (Charlson, drug monitoring)
    "observations": {
        "url": "GET /Observation?patient={patient_id}&date=ge{90_days_ago}&_count=100",
        "required_fields": ["code", "value", "effectiveDateTime", "referenceRange"],
        "description": "Get recent lab values for comorbidity assessment and monitoring needs"
    },
    
    # Allergies and adverse reactions
    "allergies": {
        "url": "GET /AllergyIntolerance?patient={patient_id}",
        "required_fields": ["code", "reaction.manifestation", "criticality"],
        "description": "Get known allergies to avoid contraindicated medications"
    },
    
    # Prior care plans (for continuity)
    "care_plans": {
        "url": "GET /CarePlan?patient={patient_id}&status=active",
        "required_fields": ["activity", "goal", "period"],
        "description": "Get existing care plans to identify known gaps or ongoing care"
    },
    
    # Prior readmissions (for context)
    "readmission_history": {
        "url": "GET /Encounter?patient={patient_id}&status=finished&date=ge{12_months_ago}&reasonCode=readmission&_count=10",
        "required_fields": ["period", "reasonCode", "diagnosis"],
        "description": "Get readmission history to identify patterns"
    }
}


# ============================================================================
# FHIR RESOURCE BUILDERS FOR OUTPUT
# ============================================================================

class FHIRRiskAssessmentBuilder:
    """Build FHIR RiskAssessment resource for LACE+ score"""
    
    @staticmethod
    def create(patient_id: str, encounter_id: str, lace_result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "resourceType": "RiskAssessment",
            "status": "final",
            "source": {
                "reference": f"Device/transitionguard-mcp"
            },
            "subject": {
                "reference": f"Patient/{patient_id}"
            },
            "context": {
                "reference": f"Encounter/{encounter_id}"
            },
            "occurrenceDateTime": datetime.now().isoformat(),
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "80316-1",  # 30-day readmission risk
                        "display": "Hospital readmission risk 30 day - predicted probability"
                    }
                ],
                "text": "LACE+ Readmission Risk Score"
            },
            "method": {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "code": "129394005",
                        "display": "Clinical case analysis"
                    }
                ],
                "text": "LACE+ Algorithm (van Walraven et al., 2010)"
            },
            "prediction": [
                {
                    "outcome": {
                        "text": f"{lace_result['risk_quintile']} Risk - 30-day Readmission"
                    },
                    "probabilityDecimal": lace_result['thirty_day_readmission_probability'],
                    "whenPeriod": {
                        "start": datetime.now().isoformat(),
                        "end": (datetime.now() + timedelta(days=30)).isoformat()
                    },
                    "rationale": lace_result['justification']
                }
            ],
            "basis": [
                {
                    "reference": f"Condition/{cond_id}",
                    "display": "Active condition contributing to risk"
                }
            ]
        }


class FHIRDocumentReferenceBuilder:
    """Build FHIR DocumentReference for Transition Care Packet"""
    
    @staticmethod
    def create(
        patient_id: str,
        encounter_id: str,
        packet_content: Dict[str, Any],
        packet_pdf_url: str
    ) -> Dict[str, Any]:
        return {
            "resourceType": "DocumentReference",
            "status": "current",
            "type": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "28031-0",
                        "display": "Comprehensive discharge summary"
                    }
                ],
                "text": "Transition Care Packet"
            },
            "subject": {
                "reference": f"Patient/{patient_id}"
            },
            "context": {
                "encounter": [
                    {
                        "reference": f"Encounter/{encounter_id}"
                    }
                ],
                "period": {
                    "start": datetime.now().isoformat()
                }
            },
            "date": datetime.now().isoformat(),
            "author": [
                {
                    "reference": "Organization/transitionguard-platform"
                }
            ],
            "title": "Transition Care Packet - Post-Discharge Care Coordination",
            "description": "Automated comprehensive discharge package including LACE+ risk assessment, care gaps, and patient education",
            "content": [
                {
                    "attachment": {
                        "contentType": "application/pdf",
                        "url": packet_pdf_url,
                        "title": "Transition Care Packet PDF"
                    },
                    "format": {
                        "system": "urn:ietf:rfc:3986",
                        "code": "urn:ihe:rad:pdf"
                    }
                }
            ],
            "relatesTo": [
                {
                    "code": "appends",
                    "target": {
                        "reference": f"RiskAssessment/{encounter_id}-lace-plus"
                    }
                }
            ]
        }


class FHIRCommunicationRequestBuilder:
    """Build FHIR CommunicationRequest for patient education delivery"""
    
    @staticmethod
    def create(
        patient_id: str,
        encounter_id: str,
        education_content: str,
        priority: str = "routine"
    ) -> Dict[str, Any]:
        return {
            "resourceType": "CommunicationRequest",
            "status": "active",
            "priority": priority,  # "routine" or "urgent"
            "subject": {
                "reference": f"Patient/{patient_id}"
            },
            "about": [
                {
                    "reference": f"Encounter/{encounter_id}"
                }
            ],
            "authoredOn": datetime.now().isoformat(),
            "sender": {
                "reference": "Organization/transitionguard-platform"
            },
            "medium": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationMode",
                            "code": "WRITTEN",
                            "display": "written"
                        }
                    ]
                },
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationMode",
                            "code": "ELECTRONIC",
                            "display": "electronic"
                        }
                    ]
                }
            ],
            "payload": [
                {
                    "contentString": education_content
                }
            ],
            "reasonCode": [
                {
                    "coding": [
                        {
                            "system": "http://snomed.info/sct",
                            "code": "11816003",
                            "display": "Diffuse hyperplasia of endocrine pancreas (post-discharge education)"
                        }
                    ],
                    "text": "Post-discharge patient education"
                }
            ]
        }


class FHIRTaskBuilder:
    """Build FHIR Task resources for action items from care gaps"""
    
    @staticmethod
    def create(
        patient_id: str,
        encounter_id: str,
        gap_action: Dict[str, Any],
        due_date: str
    ) -> Dict[str, Any]:
        return {
            "resourceType": "Task",
            "status": "requested",
            "intent": "plan",
            "code": {
                "text": gap_action.get("action", "Care coordination action")
            },
            "description": gap_action.get("gap_description", ""),
            "focus": {
                "reference": f"Encounter/{encounter_id}"
            },
            "for": {
                "reference": f"Patient/{patient_id}"
            },
            "authoredOn": datetime.now().isoformat(),
            "restriction": {
                "period": {
                    "end": due_date
                }
            },
            "priority": "high" if gap_action.get("urgency") == "urgent" else "normal",
            "owner": {
                "reference": f"Practitioner/{gap_action.get('assigned_to', 'case-manager')}"
            }
        }


# ============================================================================
# WORKFLOW ORCHESTRATION ENGINE
# ============================================================================

class TransitionGuardWorkflowEngine:
    """Orchestrates the end-to-end discharge workflow"""
    
    def __init__(self, fhir_client, mcp_client, agentic_platform):
        self.fhir = fhir_client
        self.mcp = mcp_client
        self.agent_platform = agentic_platform
    
    def handle_discharge_event(self, discharge_event: DischargeEvent) -> Dict[str, Any]:
        """
        Main workflow: triggered when patient is discharged.
        
        Returns: Transition Care Packet with all components
        """
        
        print(f"[TransitionGuard] Discharge event received for {discharge_event.patient_id}")
        start_time = datetime.now()
        
        # Step 1: Query FHIR for patient context
        print("[Step 1] Querying FHIR for patient context...")
        patient_data = self._query_fhir_context(discharge_event)
        
        # Step 2: Calculate clinical scores
        print("[Step 2] Calculating LACE+, Charlson, drug interactions...")
        scores = self._calculate_clinical_scores(patient_data)
        
        # Step 3: Invoke CareGap sub-agent
        print("[Step 3] Delegating to CareGap agent...")
        care_gaps = self._invoke_caregap_agent(discharge_event, patient_data, scores)
        
        # Step 4: Invoke PatientEd sub-agent
        print("[Step 4] Delegating to PatientEd agent...")
        patient_education = self._invoke_patientedu_agent(discharge_event, patient_data, scores, care_gaps)
        
        # Step 5: Assemble Transition Care Packet
        print("[Step 5] Assembling Transition Care Packet...")
        packet = self._assemble_packet(discharge_event, patient_data, scores, care_gaps, patient_education)
        
        # Step 6: Output to EHR and notify care team
        print("[Step 6] Outputting to EHR and notifying care team...")
        self._send_outputs(discharge_event, packet)
        
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"[TransitionGuard] Workflow completed in {elapsed:.1f} seconds")
        
        return packet
    
    def _query_fhir_context(self, event: DischargeEvent) -> Dict[str, Any]:
        """Query FHIR for complete patient context"""
        context = {
            "encounter": self.fhir.get(f"Encounter/{event.encounter_id}"),
            "patient": self.fhir.get(f"Patient/{event.patient_id}"),
            "conditions": self.fhir.search("Condition", 
                f"patient={event.patient_id}&clinical-status=active"),
            "medications": self.fhir.search("MedicationRequest",
                f"patient={event.patient_id}&status=active"),
            "recent_encounters": self.fhir.search("Encounter",
                f"patient={event.patient_id}&status=finished&date=ge{(datetime.now() - timedelta(days=180)).date()}"),
            "observations": self.fhir.search("Observation",
                f"patient={event.patient_id}&date=ge{(datetime.now() - timedelta(days=90)).date()}"),
            "allergies": self.fhir.search("AllergyIntolerance",
                f"patient={event.patient_id}"),
        }
        return context
    
    def _calculate_clinical_scores(self, patient_data: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke MCP tools for LACE+, Charlson, drug interactions"""
        
        # Extract needed data
        age = self._calculate_age(patient_data["patient"]["birthDate"])
        conditions = [c["code"]["coding"][0]["code"] for c in patient_data["conditions"]]
        medications = [
            {
                "medication_name": m.get("medicationCodeableConcept", {}).get("text", "Unknown"),
                "rxnorm_code": m.get("medicationCodeableConcept", {}).get("coding", [{}])[0].get("code", "")
            }
            for m in patient_data["medications"]
        ]
        
        # Charlson
        charlson = self.mcp.invoke_tool("charlson_index_calculator", {
            "conditions": conditions,
            "age": age
        })
        
        # LACE+
        lace = self.mcp.invoke_tool("lace_plus_calculator", {
            "age": age,
            "length_of_stay_days": patient_data["encounter"]["length_of_stay_days"],
            "charlson_score": charlson["charlson_score"],
            "ed_visits_6mo": len([e for e in patient_data["recent_encounters"] if e.get("class") == "EMER"]),
            "er_visits_past": 1 if any(e for e in patient_data["recent_encounters"] if e.get("class") == "EMER") else 0
        })
        
        # Drug interactions
        interactions = self.mcp.invoke_tool("drug_interaction_checker", {
            "medications": medications
        })
        
        return {
            "charlson": charlson,
            "lace": lace,
            "interactions": interactions
        }
    
    def _invoke_caregap_agent(self, event: DischargeEvent, patient_data: Dict, scores: Dict) -> Dict:
        """Delegate to CareGap sub-agent"""
        return self.agent_platform.invoke_agent("CareGap", {
            "patient_id": event.patient_id,
            "conditions": patient_data["conditions"],
            "discharge_date": event.discharge_datetime,
            "severity": scores["lace"]["risk_quintile"]
        }, timeout=20)
    
    def _invoke_patientedu_agent(self, event: DischargeEvent, patient_data: Dict, scores: Dict, gaps: Dict) -> str:
        """Delegate to PatientEd sub-agent"""
        return self.agent_platform.invoke_agent("PatientEd", {
            "patient_name": patient_data["patient"]["name"][0]["text"],
            "conditions": patient_data["conditions"],
            "medications": patient_data["medications"],
            "risk_score": scores["lace"],
            "care_gaps": gaps.get("priority_actions", [])
        }, timeout=15)
    
    def _assemble_packet(self, event: DischargeEvent, patient_data: Dict, scores: Dict, gaps: Dict, education: str) -> Dict:
        """Assemble final Transition Care Packet"""
        
        packet = {
            "encounter_id": event.encounter_id,
            "patient_id": event.patient_id,
            "created_at": datetime.now().isoformat(),
            "lace_plus_assessment": scores["lace"],
            "charlson_assessment": scores["charlson"],
            "drug_interactions": scores["interactions"],
            "care_gaps": gaps,
            "patient_education": education,
            "fhir_resources": {
                "risk_assessment": FHIRRiskAssessmentBuilder.create(
                    event.patient_id,
                    event.encounter_id,
                    scores["lace"]
                ),
                "communication_request": FHIRCommunicationRequestBuilder.create(
                    event.patient_id,
                    event.encounter_id,
                    education,
                    priority="urgent" if scores["lace"]["risk_quintile"] == "Very High" else "routine"
                ),
                "tasks": [
                    FHIRTaskBuilder.create(
                        event.patient_id,
                        event.encounter_id,
                        action,
                        self._due_date_from_urgency(action.get("urgency", "routine"))
                    )
                    for action in gaps.get("care_gaps", [])
                ]
            }
        }
        
        return packet
    
    def _send_outputs(self, event: DischargeEvent, packet: Dict):
        """Send outputs to EHR and notify care team"""
        
        # Store to EHR
        self.fhir.create("DocumentReference", FHIRDocumentReferenceBuilder.create(
            event.patient_id,
            event.encounter_id,
            packet,
            "https://transitionguard.promptopinion.cloud/packets/{}.pdf".format(event.encounter_id)
        ))
        
        # Send FHIR resources
        for resource in packet["fhir_resources"]["tasks"]:
            self.fhir.create("Task", resource)
        
        # Notify care team
        self.agent_platform.notify_care_team({
            "patient_id": event.patient_id,
            "encounter_id": event.encounter_id,
            "lace_score": packet["lace_plus_assessment"]["total_score"],
            "risk_level": packet["lace_plus_assessment"]["risk_quintile"],
            "urgent_gaps_count": len([g for g in packet["care_gaps"].get("care_gaps", []) if g.get("urgency") == "urgent"])
        })
    
    @staticmethod
    def _calculate_age(birth_date: str) -> int:
        """Calculate age from birth date string (YYYY-MM-DD)"""
        birth = datetime.fromisoformat(birth_date)
        return (datetime.now() - birth).days // 365
    
    @staticmethod
    def _due_date_from_urgency(urgency: str) -> str:
        """Determine due date based on urgency level"""
        if urgency == "urgent":
            due_date = datetime.now() + timedelta(days=3)
        elif urgency == "high":
            due_date = datetime.now() + timedelta(days=7)
        else:
            due_date = datetime.now() + timedelta(days=14)
        return due_date.isoformat()
