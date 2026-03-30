# TransitionGuard Deployment & Testing Guide

## Overview

This guide provides step-by-step instructions for deploying TransitionGuard to the Prompt Opinion healthcare platform, testing against a FHIR sandbox, and validating clinical outputs.

**Estimated Time:** 6 hours (development + testing)  
**Target Environment:** Prompt Opinion Sandbox (pre-production)  
**SLA:** 60-second Transition Care Packet generation per discharge

---

## Part 1: MCP Server Setup & Deployment

### 1.1 Prerequisites

```
- Python 3.9+
- Node.js 16+ (optional, for REST wrapper)
- Docker (recommended for containerization)
- git CLI
- Prompt Opinion API key (from platform admin)
```

### 1.2 Clone / Setup Repository

```bash
# Clone the TransitionGuard repo
git clone https://github.com/Jaylie3/transitionguard.git
cd transitionguard

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 1.3 requirements.txt (Minimal)

```txt
fhirclient>=4.2.0
requests>=2.28.0
pydantic>=1.10.0
fastapi>=0.95.0
uvicorn>=0.21.0
python-dateutil>=2.8.0
```

### 1.4 Launch MCP Server Locally

```bash
# Run MCP server as REST wrapper (exposes tools via HTTP)
python mcp_server.py

# Expected output:
# TransitionGuard MCP Server listening on http://localhost:8000
# Tools registered:
#   - LACE_Plus_Calculator
#   - Charlson_Index_Calculator
#   - Drug_Interaction_Checker
```

### 1.5 Test MCP Tools Locally

```bash
# Test 1: LACE+ Calculator
curl -X POST http://localhost:8000/tools/lace_plus_calculator \
  -H "Content-Type: application/json" \
  -d '{
    "age": 72,
    "length_of_stay_days": 5,
    "charlson_score": 2,
    "ed_visits_6mo": 2,
    "er_visits_past": 1
  }'

# Expected response:
{
  "total_score": 14,
  "risk_quintile": "High",
  "thirty_day_readmission_probability": 0.271,
  "component_breakdown": {
    "Length_of_stay": 4,
    "Age": 4,
    "Charlson": 2,
    "ED_visits_6mo": 4,
    "ER_history": 0
  },
  "justification": "LACE+ Score: 14/32 (High Risk)..."
}
```

---

## Part 2: Docker Containerization

### 2.1 Create Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY mcp_server.py .
COPY . .

# Expose MCP server port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Start server
CMD ["python", "-u", "mcp_server.py"]
```

### 2.2 Build & Push to Container Registry

```bash
# Build image
docker build -t transitionguard-mcp:1.0 .

# Tag for registry
docker tag transitionguard-mcp:1.0 \
  your-registry.azurecr.io/transitionguard-mcp:1.0

# Push to Azure Container Registry (or DockerHub)
docker push your-registry.azurecr.io/transitionguard-mcp:1.0

# Verify
docker run -p 8000:8000 your-registry.azurecr.io/transitionguard-mcp:1.0
```

---

## Part 3: Agent Configuration in Prompt Opinion

### 3.1 Register MCP Server in Prompt Opinion

1. **Log into Prompt Opinion Admin Dashboard**
2. **Navigate:** Settings → Integrations → MCP Servers
3. **Click:** "Register New MCP Server"
4. **Fill in:**
   - **Name:** TransitionGuard MCP
   - **Endpoint:** `https://your-registry.azurecr.io/transitionguard-mcp:1.0` (or HTTP URL if local)
   - **API Key:** `${TRANSITIONGUARD_API_KEY}` (set in platform secrets)
   - **Tools to expose:** `lace_plus_calculator`, `charlson_index_calculator`, `drug_interaction_checker`

### 3.2 Deploy Agent Configuration

1. **Upload agent_configuration.yaml to Prompt Opinion**
2. **CLI Method:**
   ```bash
   promptopinion agents deploy \
     --config agent_configuration.yaml \
     --environment sandbox
   ```

3. **Expected Output:**
   ```
   ✓ Orchestrator agent "TransitionGuard" deployed
     Status: active
     MCP tools: 3 registered
   
   ✓ Sub-agent "CareGap" deployed
     Status: active
     Timeout: 20 seconds
   
   ✓ Sub-agent "PatientEd" deployed
     Status: active
     Timeout: 15 seconds
   ```

### 3.3 Set Environment Variables

```bash
export TRANSITIONGUARD_API_KEY="sk_test_xxxx"
export FHIR_SANDBOX_URL="https://hapi.fhir.org/baseR4"
export PROMPT_OPINION_ENDPOINT="https://sandbox.promptopinion.cloud"
export LOG_LEVEL="DEBUG"
```

---

## Part 4: FHIR Sandbox Testing

### 4.1 Setup FHIR Test Data

```bash
# Use Synthea to generate realistic test patient data
docker run --rm -it \
  -v $(pwd)/synthea-output:/output \
  mitre/synthea:latest -p 50 -m "Hospital" --exporter.fhir.export true

# This generates 50 realistic patient records with:
#   - Demographics (age, gender, address)
#   - Active conditions (HTN, COPD, DM2, CKD)
#   - Medications
#   - Recent encounters
#   - Lab values
```

### 4.2 Load Test Data into FHIR Sandbox

```bash
# Upload the generated FHIR bundle
curl -X POST https://hapi.fhir.org/baseR4 \
  -H "Content-Type: application/fhir+json" \
  -d @synthea-output/fhir/hospitalInformation0.json

# Verify data loaded
curl https://hapi.fhir.org/baseR4/Patient?_count=5
```

### 4.3 Create Test Discharge Scenarios

```python
# test_discharge_scenarios.py

test_scenarios = [
    {
        "name": "Low-risk discharge",
        "patient_age": 35,
        "length_of_stay": 2,
        "conditions": ["Z00"],  # Encounter for general exam
        "ed_visits_6mo": 0,
        "expected_lace": "≤4 (Very Low)",
        "expected_readmission_prob": "<5%"
    },
    {
        "name": "High-complexity discharge",
        "patient_age": 79,
        "length_of_stay": 8,
        "conditions": ["I50", "N18", "E11", "J44", "F32"],  # HF, CKD, DM2, COPD, Depression
        "ed_visits_6mo": 4,
        "expected_lace": ">13 (High/Very High)",
        "expected_readmission_prob": ">25%"
    },
    {
        "name": "Drug interaction risk",
        "patient_age": 72,
        "medications": [
            "Atenolol 50mg",
            "Verapamil 120mg",  # Contraindicated combo
            "Warfarin 5mg"
        ],
        "expected_interactions": 2,
        "expected_severity": ["severe", "severe"]
    },
    {
        "name": "Post-surgical readmission risk",
        "patient_age": 65,
        "procedure": "Hip replacement",
        "length_of_stay": 3,
        "conditions": ["T84.0", "M80"],  # Surgical complication, osteoporosis
        "ed_visits_6mo": 1,
        "expected_gaps": ["PT/OT assessment", "Pain management plan", "Infection prevention"]
    }
]
```

---

## Part 5: Integration Testing

### 5.1 Test Discharge Event Trigger

```bash
# Create a discharge event (Encounter status change to "finished")
curl -X POST https://hapi.fhir.org/baseR4/Encounter/123/_history \
  -H "Content-Type: application/fhir+json" \
  -d '{
    "resourceType": "Encounter",
    "id": "123",
    "status": "finished",
    "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"},
    "period": {
      "start": "2026-03-20T10:00:00Z",
      "end": "2026-03-23T14:30:00Z"
    },
    "subject": {"reference": "Patient/456"}
  }'

# Verify TransitionGuard received the trigger and started processing
# Check logs:
tail -f transitionguard.log | grep "Discharge event received"
```

### 5.2 Monitor Agent Execution

```bash
# Check agent status in Prompt Opinion
promptopinion agents status TransitionGuard

# Expected output:
# Agent: TransitionGuard
# Status: active
# Last execution: 2026-03-23T14:35:22Z
# Duration: 45.3 seconds
# Result: SUCCESS
```

### 5.3 Retrieve and Validate Output

```bash
# Get the generated Transition Care Packet from FHIR
curl https://hapi.fhir.org/baseR4/DocumentReference?patient=456

# Expected response includes:
{
  "resourceType": "Bundle",
  "entry": [
    {
      "resource": {
        "resourceType": "DocumentReference",
        "type": {
          "text": "Transition Care Packet"
        },
        "subject": {"reference": "Patient/456"},
        "content": [
          {
            "attachment": {
              "url": "https://transitionguard.promptopinion.cloud/packets/123.pdf"
            }
          }
        ]
      }
    },
    {
      "resource": {
        "resourceType": "RiskAssessment",
        "prediction": [
          {
            "outcome": {"text": "High Risk - 30-day Readmission"},
            "probabilityDecimal": 0.271
          }
        ]
      }
    }
  ]
}
```

---

## Part 6: Clinical Validation

### 6.1 Validate LACE+ Calculations

```python
# test_lace_validation.py

from mcp_server import LACEPlusCalculator, LACEPlusInput

# Test case from original LACE+ Toronto cohort
test_cases = [
    {
        "name": "Example from van Walraven et al., 2010",
        "input": LACEPlusInput(
            age=72,
            length_of_stay_days=5,
            charlson_score=2,
            ed_visits_6mo=2,
            er_visits_past=1
        ),
        "expected_score": 14,
        "expected_quintile": "High",
        "expected_probability_range": (0.25, 0.30)
    }
]

# Run validation
for test in test_cases:
    result = LACEPlusCalculator.calculate(test["input"])
    
    assert result.total_score == test["expected_score"], \
        f"Score mismatch: {result.total_score} vs {test['expected_score']}"
    
    assert result.risk_quintile == test["expected_quintile"], \
        f"Quintile mismatch: {result.risk_quintile} vs {test['expected_quintile']}"
    
    assert test["expected_probability_range"][0] <= result.thirty_day_readmission_probability <= test["expected_probability_range"][1], \
        f"Probability out of range: {result.thirty_day_readmission_probability}"
    
    print(f"✓ {test['name']} passed")
```

### 6.2 Compare Against Manual Care Plans

1. **Select 10 recent discharges from pilot hospital**
2. **Run TransitionGuard on each patient**
3. **Compare auto-generated packet against manually-created case manager plans:**
   - Care gaps identified (recall, precision)
   - Readmission risk accuracy
   - Patient education clarity (survey)

```bash
# Generate comparison report
python compare_manual_vs_automated.py \
  --manual-plans-dir ./pilot_hospital_care_plans \
  --auto-packets-dir ./transitionguard-output \
  --output pilot_validation_report.pdf
```

### 6.3 Readmission Prediction Validation

```python
# Validate that LACE+ score correlates with actual readmissions
# Query hospital for 12-month readmission outcomes

hospital_readmission_data = [
    {
        "patient_id": "123",
        "transitionguard_lace_score": 14,
        "transitionguard_risk_quintile": "High",
        "transitionguard_predicted_probability": 0.271,
        "actual_30day_readmission": True,  # Patient was readmitted
        "actual_30day_readmission_diagnosis": "COPD exacerbation"
    },
    # ... 99 more records
]

# Calculate:
# - Sensitivity: What % of actual readmissions did LACE+ flag as high-risk?
# - Specificity: What % of non-readmissions did LACE+ correctly predict?
# - AUC-ROC: Overall discriminative ability

from sklearn.metrics import roc_auc_score, confusion_matrix

y_true = [r["actual_30day_readmission"] for r in hospital_readmission_data]
y_pred_prob = [r["transitionguard_predicted_probability"] for r in hospital_readmission_data]

auc = roc_auc_score(y_true, y_pred_prob)
print(f"LACE+ Discriminative Ability (AUC-ROC): {auc:.3f}")
# Expected: ≥0.70 (acceptable), ≥0.80 (excellent)
```

---

## Part 7: Performance Testing

### 7.1 Load Testing

```bash
# Use Apache JMeter or similar to simulate concurrent discharges
# Goal: Verify 60-second SLA under load

# Simulate 10 concurrent discharge events
ab -n 100 -c 10 \
  -p discharge_event.json \
  -T application/json \
  https://transitionguard.promptopinion.cloud/api/v1/discharge-trigger

# Expected results:
# - Requests per second: >5
# - Mean response time: <60 seconds
# - 95th percentile: <70 seconds
# - Error rate: <1%
```

### 7.2 Monitor System Resources

```bash
# Track CPU, memory, and latency during testing
docker stats transitionguard-mcp --no-stream

# Expected for single instance:
# CPU: <50%
# Memory: <512MB
# Network I/O: <10MB/s
```

---

## Part 8: Deployment to Production

### 8.1 Pre-Production Checklist

- [ ] All unit tests passing (`pytest`)
- [ ] Integration tests pass with sandbox FHIR server
- [ ] Clinical validation report completed
- [ ] Load testing SLA verified
- [ ] HIPAA audit logging configured
- [ ] API security reviewed (authentication, rate limiting)
- [ ] Documentation reviewed by clinical team
- [ ] Incident response plan documented

### 8.2 Gradual Rollout Strategy

```
Week 1: Pilot with 1 hospital (1,000 discharges/month)
Week 2: Expand to 5 hospitals (5,000 discharges/month)
Week 3: Expand to 25 hospitals (25,000 discharges/month)
Week 4+: Full production (6,000 hospitals potential)
```

### 8.3 Production Deployment

```bash
# Deploy to production environment
promptopinion agents deploy \
  --config agent_configuration.yaml \
  --environment production \
  --replicas 3 \
  --autoscale-min 3 \
  --autoscale-max 10

# Enable monitoring
promptopinion monitoring enable \
  --agent TransitionGuard \
  --metrics latency,error_rate,readmission_correlation \
  --alert-threshold-latency 70 \
  --alert-threshold-error-rate 5
```

---

## Part 9: Monitoring & Optimization

### 9.1 Key Metrics to Track

```
1. Execution latency (target: <60 seconds, P95: <70 seconds)
2. Error rate (target: <1%)
3. LACE+ score correlation with actual readmissions (target: AUC-ROC ≥0.75)
4. Care gap detection accuracy (compare to manual review)
5. Patient education clarity (survey score)
6. Care team adoption rate (% of providers viewing packets)
7. Cost per packet (target: <$10)
```

### 9.2 Daily Monitoring Dashboard

```
- Packets generated: [COUNT]
- Avg latency: [TIME]
- High-risk patients (LACE+ Very High): [COUNT]
- Urgent care gaps identified: [COUNT]
- System uptime: [%]
```

### 9.3 Optimization (If Needed)

```
- If latency > 70s: Parallelize FHIR queries
- If error rate > 1%: Implement fallback templates
- If low adoption: Conduct UX research with case managers
- If LACE+ AUC < 0.70: Add patient-specific calibration factors
```

---

## Part 10: Troubleshooting Guide

### Issue: Discharge events not triggering TransitionGuard

**Root causes:**
1. Subscription not registered in FHIR server
2. FHIR server not calling webhook endpoint
3. API key invalid or expired

**Solutions:**
```bash
# 1. Check subscription
curl https://hapi.fhir.org/baseR4/Subscription/transition-guard-discharge-trigger

# 2. Verify endpoint reachability
curl -v https://transitionguard.promptopinion.cloud/api/v1/fhir/discharge-trigger

# 3. Test with manual trigger
curl -X POST https://transitionguard.promptopinion.cloud/api/v1/discharge-trigger \
  -H "Authorization: Bearer ${TRANSITIONGUARD_API_KEY}" \
  -H "Content-Type: application/json" \
  -d @discharge_event.json
```

### Issue: LACE+ scores seem incorrect

**Validation steps:**
```python
# Manually recalculate with known patient data
from mcp_server import LACEPlusCalculator, LACEPlusInput

input_data = LACEPlusInput(
    age=72,
    length_of_stay_days=5,
    charlson_score=2,
    ed_visits_6mo=2,
    er_visits_past=1
)

result = LACEPlusCalculator.calculate(input_data)
print(result.justification)  # Check component breakdown
```

### Issue: Sub-agent timeouts

**Solutions:**
1. Increase timeout thresholds in agent_configuration.yaml
2. Reduce query complexity (limit FHIR result sets)
3. Scale up sub-agent resources
4. Implement fallback templates (use defaults if agent times out)

---

## Testing Checklist

```
[ ] Unit tests for MCP tools (test_mcp_server.py)
    [ ] LACE+ calculations match reference values
    [ ] Charlson index matches known cohort data
    [ ] Drug interaction database has known pairs

[ ] Integration tests with sandbox FHIR
    [ ] Agent receives discharge events
    [ ] Patient data queries succeed
    [ ] MCP tools invoked correctly
    [ ] Sub-agents communicate properly

[ ] Clinical validation
    [ ] LACE+ scores validated against van Walraven cohort
    [ ] Care gaps match manual review
    [ ] Patient education at 6th-grade reading level

[ ] Performance testing
    [ ] 60-second SLA met under normal load
    [ ] 70-second SLA met at peak load (100 concurrent discharges)
    [ ] System resources <50% CPU, <512MB memory

[ ] Security & compliance
    [ ] HIPAA audit logging enabled
    [ ] API authentication verified
    [ ] Rate limiting configured
    [ ] Data retention policy enforced

[ ] User acceptance testing
    [ ] Case managers can access packets
    [ ] Patient portal displays education clearly
    [ ] Care team receives notifications
```

---

## Next Steps

1. **Week 1:** Complete Part 1-5 (MCP server + basic testing)
2. **Week 2:** Complete Part 6-7 (clinical + performance validation)
3. **Week 3:** Complete Part 8 (production deployment preparation)
4. **Week 4+:** Monitor & optimize

---

## Support & Contact

**For issues, questions, or feedback:**
- Prompt Opinion Developer Docs: https://docs.promptopinion.cloud
- Slack channel: #transitionguard-dev
- Email: support@promptopinion.cloud

---

*Last updated: March 30, 2026*  
*Version: 1.0*
