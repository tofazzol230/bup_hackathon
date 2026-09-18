# GridWise — LLM-Assisted Smart Campus Energy Optimizer

**Event:** BUP CSE Fest 2026 Hackathon (In association with Poridhi.io)  
**Round:** Online Preliminary Round  
**Challenge:** Smart Campus Energy Optimization Challenge (GridWise LLM)  

### Live Deployed Service
- **Base URL:** `https://gridwise-bup-api.onrender.com`
- **Health Endpoint:** `GET https://gridwise-bup-api.onrender.com/health`
- **Main Optimization Endpoint:** `POST https://gridwise-bup-api.onrender.com/optimize-energy`

---

## 1. System Architecture & Solution Overview

The solution implements a robust 4-stage pipeline that translates free-form human operator notes into deterministic operational bounds and optimizes campus energy dispatch over a 24-hour horizon.

```
+--------------------------+
| Energy Scenario + Notes  |
+------------+-------------+
             |
             v
+--------------------------+
| 1. LLM Interpretation    |  Extracts candidate directives from natural language
|    Engine                |  (Gemini API / OpenAI / Groq / Fallback NLP Parser)
+------------+-------------+
             |
             v
+--------------------------+
| 2. Deterministic         |  Validates directive types, enforces ascending hour order
|    Guardrail Validator   |  [0..23], checks numerical bounds, sanitizes no_op.
+------------+-------------+
             |
             v
+--------------------------+
| 3. HiGHS Mathematical    |  Solves Linear Program with scipy.optimize.linprog
|    Optimizer             |  Satisfies energy balance, battery limits, end-of-day neutrality.
+------------+-------------+
             |
             v
+--------------------------+
| 4. Replay Validator      |  Independently re-checks all 15 physical constraints
|    (replay_validator.py) |  (energy balance, battery neutrality, solar limits,
+--------------------------+  charge/discharge limits, grid caps, totals) before
                              returning the response. HTTP 500 if any violation found.
```

### Key Architectural Highlights:
1. **LLM Interpreter:** Extracts structured directives (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`). LLM is always attempted first when a key is configured.
2. **Resilience Fallback:** The deterministic parser provides a resilience fallback when configured LLM providers are unavailable to maintain service continuity. Logged at `ERROR` level so provider downtime is clearly identifiable.
3. **Deterministic Guardrails:** Strictly validates all LLM outputs before math modeling (Section 08). Catches invalid types, out-of-range hours, and unsafe values.
4. **SciPy HiGHS-Based LP Solver:** Formulates a linear program with 120 continuous variables, 49 core equality constraints, plus variable bounds and directive-specific constraints. The implementation matches published public sample benchmark costs with 0.0000 difference.
5. **Replay Validator:** After LP solve, independently re-checks all 15 physical constraints from raw plan values before returning any response. Raises HTTP 500 if a violation is detected.

---

## 2. Supported Directives

| Directive Type | Meaning | Required Schema |
| :--- | :--- | :--- |
| `solar_reduction` | Decreases usable solar output during designated hours. | `{"hours": [12, 13], "factor": 0.25}` |
| `minimum_battery_reserve` | Raises battery minimum energy floor. | `{"hours": [18, 19, 20], "minimum_energy_kwh": 100.0}` |
| `no_charge_window` | Prevents battery charging during hours. | `{"hours": [14, 15]}` |
| `no_discharge_window` | Prevents battery discharging during hours. | `{"hours": [17, 18]}` |
| `max_grid_window` | Limits grid import to a maximum threshold. | `{"hours": [19, 20], "max_grid_kwh": 180.0}` |
| `no_op` | Irrelevant notes (distractors) that do not affect schedule. | `null` (with `applies: false`) |

---

## 3. Environment Configuration & Secret Handling

### Environment Variables
Configure your chosen LLM provider via environment variables:

| Variable | Description | Status |
| :--- | :--- | :--- |
| `GROQ_API_KEY` | Groq Cloud API Key (Primary provider for live deployment) | **Required for live evaluation** |
| `GROQ_MODEL` | Groq model identifier | `openai/gpt-oss-120b` (Default) |
| `GEMINI_API_KEY` | Google Gemini API Key (alternative provider, `gemini-1.5-flash`) | *Optional alternative* |
| `OPENAI_API_KEY` | OpenAI API Key (alternative provider, `gpt-4o-mini`) | *Optional alternative* |
| `PORT` | HTTP Server port | `8000` (Default) |

### Security & Secret Safety Policy
- **No secrets in repository:** `.gitignore` and `.dockerignore` prevent committing `.env` files or credentials.
- **Safe logs & error messages:** The application suppresses stack traces on 500 errors and never outputs raw prompts containing API keys.

---

## 4. Local Quickstart (Clean Environment)

### Step 1: Clone Repository
```bash
git clone https://github.com/tofazzol230/bup_hackathon.git
cd bup_hackathon
```

### Step 2: Create and Activate Virtual Environment
```bash
python -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# On Linux / macOS:
source .venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Configure LLM Provider
For the competition submission, configure at least one supported LLM provider. The deterministic parser is a resilience fallback used only when all configured LLM providers fail or become unavailable.

```bash
# Windows PowerShell
$env:GROQ_API_KEY="your-groq-api-key"

# Linux / macOS
export GROQ_API_KEY="your-groq-api-key"
```

### Step 5: Start the API Service
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 5. Testing the Service

### 1. Readiness Check (`GET /health`)
```bash
curl -X GET http://localhost:8000/health
```
**Expected Response:**
```json
{"status":"ok"}
```

### 2. Run Public Sample Benchmark (All 10 Cases with Full Physics Checks)
We include an automated test script that validates the API against all 10 official public sample cases:
```bash
python validate_samples.py
```
**Expected Output:**
```
=========================================================
PASS: /health HTTP 200 {'status': 'ok'}
=========================================================
--- Running SAMPLE-01: Solar cleaning + distractor ---
PASS: SAMPLE-01 (Cost:38365.00 Diff:0.0000 Physics:21/21 OK)
...
SUMMARY: 10/10 cases passed (21-POINT PHYSICS VALIDATION)!
ALL TESTS PASSED 100%!
```

### 3. Sample cURL Request (`POST /optimize-energy`)
```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "TEST-01",
    "operator_notes": [
      "The charging circuit will be unavailable from 2 PM until 4 PM.",
      "Cafeteria menu update for tomorrow."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 1, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 2, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 3, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 4, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 5, "demand_kwh": 95, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 6, "demand_kwh": 110, "solar_kwh": 5, "tariff_bdt_per_kwh": 8},
      {"hour": 7, "demand_kwh": 130, "solar_kwh": 20, "tariff_bdt_per_kwh": 10},
      {"hour": 8, "demand_kwh": 150, "solar_kwh": 50, "tariff_bdt_per_kwh": 12},
      {"hour": 9, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
      {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
      {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
      {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
      {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
      {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
      {"hour": 15, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
      {"hour": 16, "demand_kwh": 170, "solar_kwh": 45, "tariff_bdt_per_kwh": 18},
      {"hour": 17, "demand_kwh": 185, "solar_kwh": 10, "tariff_bdt_per_kwh": 22},
      {"hour": 18, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 28},
      {"hour": 19, "demand_kwh": 215, "solar_kwh": 0, "tariff_bdt_per_kwh": 30},
      {"hour": 20, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 26},
      {"hour": 21, "demand_kwh": 175, "solar_kwh": 0, "tariff_bdt_per_kwh": 18},
      {"hour": 22, "demand_kwh": 135, "solar_kwh": 0, "tariff_bdt_per_kwh": 10},
      {"hour": 23, "demand_kwh": 105, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
    ],
    "battery": {
      "capacity_kwh": 220,
      "initial_energy_kwh": 110,
      "minimum_energy_kwh": 40,
      "max_charge_kwh_per_hour": 50,
      "max_discharge_kwh_per_hour": 50
    }
  }'
```

---

## 6. Docker Fallback Deployment

### Build the Image
```bash
docker build -t gridwise-api:latest .
```

### Run the Container
```bash
docker run -d \
  --name gridwise-service \
  -p 8000:8000 \
  -e GROQ_API_KEY="$GROQ_API_KEY" \
  -e GROQ_MODEL="openai/gpt-oss-120b" \
  gridwise-api:latest
```
*(Note: In containerized deployment, credentials are provided via runtime environment variables and are never baked into the Docker image).*

### Verify Container Health
```bash
curl -X GET http://localhost:8000/health
```

---

## 7. Dependencies & Solver Technologies
- **Web Framework:** FastAPI (High throughput asynchronous REST API)
- **ASGI Server:** Uvicorn
- **Data Validation:** Pydantic v2
- **Optimization Solver:** SciPy's HiGHS-based linear programming solver (`scipy.optimize.linprog`, guaranteed global optimum)
- **HTTP Client:** HTTPX
- **LLM APIs:** Groq API (`openai/gpt-oss-120b`), Google Gemini API, OpenAI API
- **Development Tools & Utilities:** VS Code, Git, and automated test suites.

---

## 8. Known Limitations
- **Resilience Fallback Scope:** The deterministic rule-based parser is designed strictly as a zero-downtime fallback when external LLM APIs face connectivity, quota, or rate-limit failures; it is not intended to replace generative semantic reasoning across arbitrary phrasing.
- **Hidden Benchmark Cases:** Hidden evaluation cases are withheld by organizers; verification has been performed against all 10 published benchmark cases, randomized stress tests, and independent physical constraint replay validation.
- **Continuous Formulation:** Optimization is formulated as a continuous linear program; the returned schedule is valid under the supplied hourly demand, solar, tariff, and battery specifications.
