"""
Unit tests for TransitionGuard MCP Server
Validates LACE+ calculations, Charlson index, and drug interactions
"""

import unittest
from mcp_server import (
    LACEPlusCalculator, LACEPlusInput,
    CharlsonIndex,
    DrugInteractionChecker
)


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


if __name__ == "__main__":
    unittest.main()
