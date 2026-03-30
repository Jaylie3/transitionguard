"""
Unit tests for TransitionGuard MCP Server
Validates LACE+ calculations, Charlson index, drug interactions, and HTTP endpoints
"""

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mcp_server import (
    LACEPlusCalculator, LACEPlusInput,
    CharlsonIndex,
    DrugInteractionChecker
)
from app import app


# ── HTTP client shared across endpoint tests ─────────────────────────────────
client = TestClient(app)


class TestLACEPlusCalculator(unittest.TestCase):
    """Test LACE+ readmission risk calculator"""
    
    def test_very_low_risk(self):
        """Test case with very low readmission risk"""
        input_data = LACEPlusInput(
            age=35,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result = LACEPlusCalculator.calculate(input_data)
        
        self.assertEqual(result.total_score, 0)
        self.assertEqual(result.risk_quintile, "Very Low")
        self.assertLess(result.thirty_day_readmission_probability, 0.05)
    
    def test_high_risk(self):
        """Test case with high readmission risk"""
        input_data = LACEPlusInput(
            age=79,
            length_of_stay_days=8,
            charlson_score=3,
            ed_visits_6mo=3,
            er_visits_past=1
        )
        result = LACEPlusCalculator.calculate(input_data)
        
        self.assertGreaterEqual(result.total_score, 12)
        self.assertIn(result.risk_quintile, ["High", "Very High"])
        self.assertGreater(result.thirty_day_readmission_probability, 0.20)
    
    def test_age_scoring(self):
        """Test age-based scoring"""
        # Age 65-74 should score 4
        input_low_age = LACEPlusInput(
            age=40,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result_low = LACEPlusCalculator.calculate(input_low_age)
        
        input_high_age = LACEPlusInput(
            age=75,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result_high = LACEPlusCalculator.calculate(input_high_age)
        
        # High age should have higher score
        self.assertLess(result_low.total_score, result_high.total_score)
    
    def test_length_of_stay_scoring(self):
        """Test length of stay impact on LACE+ score"""
        # Short stay (1 day) = 2 points
        input_short = LACEPlusInput(
            age=50,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result_short = LACEPlusCalculator.calculate(input_short)
        
        # Long stay (14+ days) = 7 points
        input_long = LACEPlusInput(
            age=50,
            length_of_stay_days=14,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result_long = LACEPlusCalculator.calculate(input_long)
        
        # Long stay should have significantly higher score
        self.assertEqual(result_short.component_breakdown["Length_of_stay"], 2)
        self.assertEqual(result_long.component_breakdown["Length_of_stay"], 7)
    
    def test_ed_visits_scoring(self):
        """Test ED visit impact on LACE+ score"""
        input_no_ed = LACEPlusInput(
            age=50,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=0,
            er_visits_past=0
        )
        result_no_ed = LACEPlusCalculator.calculate(input_no_ed)
        
        input_many_ed = LACEPlusInput(
            age=50,
            length_of_stay_days=1,
            charlson_score=0,
            ed_visits_6mo=4,
            er_visits_past=0
        )
        result_many_ed = LACEPlusCalculator.calculate(input_many_ed)
        
        # 4+ ED visits significantly increases score
        self.assertLess(result_no_ed.total_score, result_many_ed.total_score)
        self.assertEqual(result_many_ed.component_breakdown["ED_visits_6mo"], 7)


class TestCharlsonIndex(unittest.TestCase):
    """Test Charlson comorbidity index calculator"""
    
    def test_no_conditions(self):
        """Test patient with no comorbidities"""
        result = CharlsonIndex.calculate([], age=50)
        
        self.assertEqual(result["charlson_score"], 0)
        self.assertLess(result["one_year_mortality_estimate"], 0.02)
    
    def test_single_condition(self):
        """Test with single comorbidity"""
        result = CharlsonIndex.calculate(["I10"], age=50)  # Hypertension
        
        self.assertGreaterEqual(result["charlson_score"], 1)
        self.assertGreater(result["one_year_mortality_estimate"], 0.01)
    
    def test_multiple_conditions(self):
        """Test with multiple comorbidities"""
        conditions = ["I10", "E11", "J44", "N18"]  # HTN, DM2, COPD, CKD
        result = CharlsonIndex.calculate(conditions, age=50)
        
        self.assertGreater(result["charlson_score"], 3)
        self.assertGreater(result["one_year_mortality_estimate"], 0.05)
    
    def test_age_adjustment(self):
        """Test age-based score adjustment"""
        # Age < 50: no age adjustment
        result_young = CharlsonIndex.calculate(["I10"], age=40)
        
        # Age 50-59: 1 point per decade
        result_old = CharlsonIndex.calculate(["I10"], age=75)
        
        # Older age should have higher score
        self.assertLess(result_young["charlson_score"], result_old["charlson_score"])
    
    def test_cancer_high_weight(self):
        """Test that cancer has high weight (3 points)"""
        result = CharlsonIndex.calculate(["C80"], age=50)  # Malignant neoplasm
        
        self.assertGreaterEqual(result["charlson_score"], 3)


class TestDrugInteractionChecker(unittest.TestCase):
    """Test drug-drug interaction checker"""
    
    def test_no_interactions(self):
        """Test medication list with no interactions"""
        medications = [
            {"medication_name": "Lisinopril 10mg", "rxnorm_code": "314076"},
            {"medication_name": "Metformin 1000mg", "rxnorm_code": "6809"},
        ]
        result = DrugInteractionChecker.check_interactions(medications)
        
        self.assertEqual(result["total_interactions"], 0)
        self.assertFalse(result["requires_pharmacist_review"])
    
    def test_severe_interaction_detected(self):
        """Test detection of severe drug interaction"""
        medications = [
            {"medication_name": "Atenolol 50mg", "rxnorm_code": "1191"},
            {"medication_name": "Verapamil 120mg", "rxnorm_code": "2502"},
        ]
        result = DrugInteractionChecker.check_interactions(medications)
        
        self.assertGreater(result["total_interactions"], 0)
        self.assertTrue(result["requires_pharmacist_review"])
        self.assertEqual(result["severity_summary"]["severe"], 1)
    
    def test_multiple_interactions(self):
        """Test detection of multiple interactions in same list"""
        medications = [
            {"medication_name": "Warfarin 5mg", "rxnorm_code": "5202"},
            {"medication_name": "Aspirin 81mg", "rxnorm_code": "21"},
            {"medication_name": "Verapamil 120mg", "rxnorm_code": "2502"},
            {"medication_name": "Atenolol 50mg", "rxnorm_code": "1191"},
        ]
        result = DrugInteractionChecker.check_interactions(medications)
        
        # Should find Warfarin-Aspirin and Atenolol-Verapamil interactions
        self.assertGreaterEqual(result["total_interactions"], 2)
    
    def test_interaction_recommendation(self):
        """Test that interaction includes clinical recommendation"""
        medications = [
            {"medication_name": "Atenolol 50mg", "rxnorm_code": "1191"},
            {"medication_name": "Verapamil 120mg", "rxnorm_code": "2502"},
        ]
        result = DrugInteractionChecker.check_interactions(medications)
        
        if result["interactions_found"]:
            interaction = result["interactions_found"][0]
            self.assertIn("recommendation", interaction)
            self.assertGreater(len(interaction["recommendation"]), 10)


class TestIntegration(unittest.TestCase):
    """Integration tests for typical clinical scenarios"""
    
    def test_typical_discharge(self):
        """Test typical discharge scenario"""
        # Patient data
        age = 72
        los = 5
        conditions = ["I50", "N18", "E11", "F32"]
        ed_visits = 2
        
        # Calculate Charlson
        charlson = CharlsonIndex.calculate(conditions, age)
        
        # Calculate LACE+
        lace_input = LACEPlusInput(
            age=age,
            length_of_stay_days=los,
            charlson_score=charlson["charlson_score"],
            ed_visits_6mo=ed_visits,
            er_visits_past=1
        )
        lace = LACEPlusCalculator.calculate(lace_input)
        
        # Check interactions
        medications = [
            {"medication_name": "ACE inhibitor", "rxnorm_code": "25277"},
            {"medication_name": "Potassium supplement", "rxnorm_code": "200032"},
            {"medication_name": "Metformin", "rxnorm_code": "6809"},
        ]
        interactions = DrugInteractionChecker.check_interactions(medications)
        
        # Assertions
        self.assertGreater(charlson["charlson_score"], 0)
        self.assertGreater(lace.total_score, 5)
        self.assertEqual(lace.risk_quintile, "Medium")
        self.assertGreater(interactions["total_interactions"], 0)


# ============================================================================
# HTTP ENDPOINT TESTS (FastAPI / MCP JSON-RPC)
# ============================================================================

class TestHealthEndpoint(unittest.TestCase):
    """Tests for the /health liveness probe."""

    def test_health_returns_ok(self):
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("tools_available", data)

    def test_health_lists_all_tools(self):
        response = client.get("/health")
        tools = response.json()["tools_available"]
        self.assertIn("lace_plus_calculator", tools)
        self.assertIn("charlson_index_calculator", tools)
        self.assertIn("drug_interaction_checker", tools)


class TestMCPInitialize(unittest.TestCase):
    """Tests for MCP initialize handshake."""

    def test_initialize_returns_capabilities(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        response = client.post("/mcp", json=body)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["jsonrpc"], "2.0")
        result = data["result"]
        self.assertIn("protocolVersion", result)
        self.assertIn("capabilities", result)
        self.assertIn("serverInfo", result)
        self.assertEqual(result["serverInfo"]["name"], "TransitionGuard")

    def test_initialize_id_echoed(self):
        body = {"jsonrpc": "2.0", "id": 42, "method": "initialize", "params": {}}
        response = client.post("/mcp", json=body)
        self.assertEqual(response.json()["id"], 42)


class TestMCPToolsList(unittest.TestCase):
    """Tests for MCP tools/list."""

    def test_tools_list_returns_three_tools(self):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        response = client.post("/mcp", json=body)
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual(len(tools), 3)

    def test_tools_have_input_schema(self):
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        tools = client.post("/mcp", json=body).json()["result"]["tools"]
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)


class TestMCPToolsCall(unittest.TestCase):
    """Tests for MCP tools/call — each clinical tool."""

    def _call(self, tool_name: str, arguments: dict, headers: dict = None) -> dict:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        return client.post("/mcp", json=body, headers=headers or {}).json()

    # ── LACE+ ─────────────────────────────────────────────────────────────────

    def test_lace_plus_low_risk(self):
        result = self._call(
            "lace_plus_calculator",
            {"age": 35, "length_of_stay_days": 1, "charlson_score": 0,
             "ed_visits_6mo": 0, "er_visits_past": 0},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(content["risk_quintile"], "Very Low")
        self.assertLess(content["thirty_day_readmission_probability"], 0.05)

    def test_lace_plus_high_risk(self):
        result = self._call(
            "lace_plus_calculator",
            {"age": 79, "length_of_stay_days": 14, "charlson_score": 5,
             "ed_visits_6mo": 4, "er_visits_past": 1},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(content["risk_quintile"], "Very High")

    def test_lace_plus_sharp_context_propagated(self):
        sharp_headers = {
            "x-sharp-patient-id": "patient-123",
            "x-sharp-encounter-id": "enc-456",
        }
        result = self._call(
            "lace_plus_calculator",
            {"age": 60, "length_of_stay_days": 4, "charlson_score": 2,
             "ed_visits_6mo": 1, "er_visits_past": 0},
            headers=sharp_headers,
        )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(content["sharp_patient_id"], "patient-123")
        self.assertEqual(content["sharp_encounter_id"], "enc-456")
        sharp_ctx = result["result"]["sharp_context"]
        self.assertEqual(sharp_ctx["patient_id"], "patient-123")

    # ── Charlson ──────────────────────────────────────────────────────────────

    def test_charlson_no_conditions(self):
        result = self._call("charlson_index_calculator", {"conditions": [], "age": 45})
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(content["charlson_score"], 0)

    def test_charlson_multiple_conditions(self):
        result = self._call(
            "charlson_index_calculator",
            {"conditions": ["I10", "E11", "J44", "N18"], "age": 68},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertGreater(content["charlson_score"], 3)

    # ── Drug Interaction ──────────────────────────────────────────────────────

    def test_drug_interaction_fallback_severe(self):
        """Falls back to local DB when RxNav is mocked unavailable."""
        with patch("app.lookup_rxnav_interactions", new_callable=AsyncMock) as mock_rxnav:
            mock_rxnav.return_value = None  # simulate API unavailable
            result = self._call(
                "drug_interaction_checker",
                {"medications": [
                    {"medication_name": "Atenolol 50mg", "rxnorm_code": "1191"},
                    {"medication_name": "Verapamil 120mg", "rxnorm_code": "2502"},
                ]},
            )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertGreater(content["total_interactions"], 0)
        self.assertTrue(content["requires_pharmacist_review"])

    def test_drug_interaction_rxnav_result_used_when_available(self):
        """Uses RxNav result when API returns data."""
        mock_rxnav_data = {
            "interactions_found": [
                {
                    "medication_1": "Atenolol",
                    "medication_2": "Verapamil",
                    "severity": "severe",
                    "recommendation": "Avoid combination.",
                    "source": "NLM RxNav",
                }
            ],
            "total_interactions": 1,
            "severity_summary": {"severe": 1, "moderate": 0, "mild": 0},
            "requires_pharmacist_review": True,
            "data_source": "NLM RxNav Interaction API",
            "justification": "Screened 2 medications via NLM RxNav API.\n",
        }
        with patch("app.lookup_rxnav_interactions", new_callable=AsyncMock) as mock_rxnav:
            mock_rxnav.return_value = mock_rxnav_data
            result = self._call(
                "drug_interaction_checker",
                {"medications": [
                    {"medication_name": "Atenolol 50mg", "rxnorm_code": "1191"},
                    {"medication_name": "Verapamil 120mg", "rxnorm_code": "2502"},
                ]},
            )
        content = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(content["data_source"], "NLM RxNav Interaction API")
        self.assertEqual(content["total_interactions"], 1)

    # ── Error handling ─────────────────────────────────────────────────────────

    def test_unknown_tool_returns_error(self):
        result = self._call("nonexistent_tool", {})
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32601)

    def test_missing_required_params_returns_error(self):
        result = self._call("lace_plus_calculator", {"age": 70})  # missing required fields
        self.assertIn("error", result)

    def test_unknown_method_returns_error(self):
        body = {"jsonrpc": "2.0", "id": 9, "method": "foo/bar", "params": {}}
        result = client.post("/mcp", json=body).json()
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32601)

    def test_invalid_json_returns_error(self):
        response = client.post(
            "/mcp", data="not json", headers={"content-type": "application/json"}
        )
        data = response.json()
        self.assertIn("error", data)


class TestFHIRDischargeTrigger(unittest.TestCase):
    """Tests for the FHIR discharge trigger webhook."""

    def _sample_encounter(self, status="finished") -> dict:
        return {
            "resourceType": "Encounter",
            "id": "enc-001",
            "status": status,
            "subject": {"reference": "Patient/patient-001"},
            "period": {"start": "2026-03-18T08:00:00Z", "end": "2026-03-23T14:30:00Z"},
            "length": {"value": 5, "unit": "days"},
            "hospitalization": {
                "dischargeDisposition": {
                    "coding": [{"display": "Discharge to home"}]
                }
            },
            "serviceProvider": {"display": "General Hospital"},
        }

    def test_finished_encounter_triggers_workflow(self):
        response = client.post(
            "/fhir/discharge-trigger", json=self._sample_encounter("finished")
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["received"])
        self.assertEqual(data["action"], "workflow_initiated")
        self.assertEqual(data["discharge_event"]["patient_id"], "patient-001")
        self.assertEqual(data["discharge_event"]["length_of_stay_days"], 5)

    def test_non_finished_encounter_skipped(self):
        response = client.post(
            "/fhir/discharge-trigger", json=self._sample_encounter("in-progress")
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "skipped")

    def test_wrong_resource_type_rejected(self):
        body = {"resourceType": "Patient", "id": "p-001"}
        response = client.post("/fhir/discharge-trigger", json=body)
        self.assertEqual(response.status_code, 422)

    def test_sharp_context_echoed_in_response(self):
        headers = {
            "x-sharp-patient-id": "patient-sharp-999",
            "x-sharp-encounter-id": "enc-sharp-001",
        }
        response = client.post(
            "/fhir/discharge-trigger",
            json=self._sample_encounter("finished"),
            headers=headers,
        )
        data = response.json()
        self.assertEqual(data["sharp_context"]["patient_id"], "patient-sharp-999")
        self.assertEqual(data["sharp_context"]["encounter_id"], "enc-sharp-001")

    def test_sharp_patient_id_fallback(self):
        """Patient ID from SHARP header fills in when Encounter.subject is missing."""
        encounter = self._sample_encounter("finished")
        del encounter["subject"]
        headers = {"x-sharp-patient-id": "patient-fallback"}
        response = client.post(
            "/fhir/discharge-trigger", json=encounter, headers=headers
        )
        data = response.json()
        self.assertEqual(data["discharge_event"]["patient_id"], "patient-fallback")

    def test_next_steps_included(self):
        response = client.post(
            "/fhir/discharge-trigger", json=self._sample_encounter("finished")
        )
        data = response.json()
        self.assertIn("next_steps", data)
        self.assertGreater(len(data["next_steps"]), 0)


if __name__ == "__main__":
    unittest.main()
