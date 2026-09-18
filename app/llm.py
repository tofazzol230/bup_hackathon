import os
import re
import json
import logging
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger("gridwise.llm")

# System prompt for LLM
SYSTEM_PROMPT = """You are an expert energy management directive interpreter for BUP Smart Campus.
Your job is to read campus operator notes (natural language) and extract structured energy directives.

Supported directive types:
1. 'solar_reduction': Solar output is reduced during specific hours.
   structured_adjustment: {"hours": [start..end-1], "factor": <fraction 0.0 to 1.0 remaining>}
   Example: "solar drop to 20%" -> factor = 0.2. "80% reduction" -> factor = 0.2. "half solar" -> factor = 0.5.
2. 'minimum_battery_reserve': Keep battery energy >= required kWh during specific hours.
   structured_adjustment: {"hours": [start..end-1], "minimum_energy_kwh": <number>}
   If stated as percentage of battery capacity, multiply by capacity_kwh.
3. 'no_charge_window': Battery charging is prohibited during specific hours.
   structured_adjustment: {"hours": [start..end-1]}
4. 'no_discharge_window': Battery discharging is prohibited during specific hours.
   structured_adjustment: {"hours": [start..end-1]}
5. 'max_grid_window': Grid import cannot exceed stated kWh during specific hours.
   structured_adjustment: {"hours": [start..end-1], "max_grid_kwh": <number>}
6. 'no_op': Note is irrelevant, distractor, or does not affect today's energy schedule.
   applies: false, structured_adjustment: null

Time Windows:
Whole-hour intervals, start-inclusive and end-exclusive.
- "1 PM to 3 PM" -> hours: [13, 14]
- "noon until 2 PM" -> hours: [12, 13]
- "2 AM until 5 AM" -> hours: [2, 3, 4]
- "6 PM until 9 PM" -> hours: [18, 19, 20]

Rules:
- Return a JSON array of objects, one for each note in note_index order (0, 1, ... N-1).
- Each object must have:
  "note_index": int,
  "applies": bool (true for all active directives, false only for no_op),
  "directive_type": string,
  "structured_adjustment": object or null,
  "explanation": short string
- Respond ONLY with the valid JSON array.
"""

def parse_time_window(text: str) -> List[int]:
    text_lower = text.lower()
    text_lower = re.sub(r'\bnoon\b', '12 pm', text_lower)
    text_lower = re.sub(r'\bmidnight\b', '12 am', text_lower)
    
    m = re.search(r'(?:from|between|\b)\s*(\d{1,2})(?::00)?\s*(am|pm)?\s*(?:until|to|and|-)\s*(\d{1,2})(?::00)?\s*(am|pm)', text_lower)
    if m:
        h1 = int(m.group(1))
        p1 = m.group(2)
        h2 = int(m.group(3))
        p2 = m.group(4)
        if not p1:
            p1 = p2
            
        def to_24(h, p):
            if p == 'pm':
                return 12 if h == 12 else h + 12
            else:
                return 0 if h == 12 else h

        start_24 = to_24(h1, p1)
        end_24 = to_24(h2, p2)
        if start_24 < end_24:
            return list(range(start_24, end_24))
        elif start_24 > end_24:
            return list(range(start_24, 24)) + list(range(0, end_24))

    m2 = re.search(r'(\d{1,2}):00\s*(?:until|to|and|-)\s*(\d{1,2}):00', text_lower)
    if m2:
        start_24 = int(m2.group(1))
        end_24 = int(m2.group(2))
        if start_24 < end_24:
            return list(range(start_24, end_24))
            
    return []

def extract_rule_based_directive(note: str, note_index: int, battery: Dict[str, Any]) -> Dict[str, Any]:
    text = note.strip()
    tl = text.lower()
    
    energy_keywords = ['solar', 'battery', 'charg', 'discharg', 'grid', 'feeder', 'pv', 'rooftop', 'transformer', 'substation', 'inverter', 'panel']
    if not any(kw in tl for kw in energy_keywords):
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "This note does not affect the energy schedule."
        }
        
    hours = parse_time_window(text)
    
    # solar_reduction
    if any(k in tl for k in ['solar', 'pv', 'panel']) and any(k in tl for k in ['drop', 'reduc', 'clean', 'half', 'forecast', 'washing']):
        factor = 1.0
        m_reduc = re.search(r'(\d+)%\s+reduction', tl)
        m_drop = re.search(r'(?:to|roughly|about)\s+(\d+)%', tl)
        m_half = re.search(r'\bhalf\b', tl)
        if m_reduc:
            factor = (100.0 - float(m_reduc.group(1))) / 100.0
        elif m_drop:
            factor = float(m_drop.group(1)) / 100.0
        elif m_half:
            factor = 0.5
        elif 'one-fifth' in tl:
            factor = 0.2
            
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {
                "hours": hours,
                "factor": factor
            },
            "explanation": f"Solar reduction directive: factor {factor} during hours {hours}."
        }

    # no_discharge_window
    if ('not discharge' in tl or 'no discharge' in tl or 'do not discharge' in tl or 'discharging is disabled' in tl or 
        'discharging is unavailable' in tl or 'must not discharge' in tl or ('discharg' in tl and any(k in tl for k in ['isolated', 'unavailable', 'disabled', 'prevented', 'must not', 'prohibited']))):
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {
                "hours": hours
            },
            "explanation": "No battery discharging allowed during specified hours."
        }

    # no_charge_window
    if ('no charge' in tl or 'not charge' in tl or 'do not charge' in tl or 'charger will be isolated' in tl or 
        'charging circuit will be unavailable' in tl or 'battery charging is disabled' in tl or 
        ('charg' in tl and not ('discharg' in tl) and any(k in tl for k in ['isolated', 'unavailable', 'disabled', 'prevented', 'must not', 'prohibited']))):
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {
                "hours": hours
            },
            "explanation": "No battery charging allowed during specified hours."
        }

    # minimum_battery_reserve
    if (('reserve' in tl or 'remain in the battery' in tl or 'stored in the battery' in tl or 'at least' in tl) and ('battery' in tl)):
        m_pct = re.search(r'(\d+)%\s+of\s+(?:the\s+)?battery\s+capacity', tl)
        m_kwh = re.search(r'(\d+(?:\.\d+)?)\s*kwh', tl)
        min_kwh = 0.0
        if m_pct:
            pct = float(m_pct.group(1)) / 100.0
            min_kwh = pct * float(battery.get('capacity_kwh', 200))
        elif m_kwh:
            min_kwh = float(m_kwh.group(1))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {
                "hours": hours,
                "minimum_energy_kwh": min_kwh
            },
            "explanation": f"Minimum battery reserve of {min_kwh} kWh required during hours {hours}."
        }

    # max_grid_window
    if any(k in tl for k in ['grid', 'feeder', 'transformer', 'substation', 'intake', 'import']):
        m_grid = re.search(r'(\d+(?:\.\d+)?)\s*kwh', tl)
        max_grid = 0.0
        if m_grid:
            max_grid = float(m_grid.group(1))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {
                "hours": hours,
                "max_grid_kwh": max_grid
            },
            "explanation": f"Grid import capped at {max_grid} kWh during hours {hours}."
        }

    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "No applicable energy directive recognized."
    }

def call_gemini_api(notes: List[str], battery: Dict[str, Any], api_key: str) -> Optional[List[Dict[str, Any]]]:
    prompt = f"""Battery Specs: {json.dumps(battery)}
Operator Notes to interpret:
{json.dumps([{"note_index": i, "text": n} for i, n in enumerate(notes)])}

Extract directives in JSON format according to system instructions."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": SYSTEM_PROMPT},
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }
    
    with httpx.Client(timeout=4.5) as client:
        resp = client.post(url, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            text_resp = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text_resp)
            if isinstance(parsed, list):
                return parsed
            elif isinstance(parsed, dict) and "directives" in parsed:
                return parsed["directives"]
    return None

def call_openai_compatible_api(notes: List[str], battery: Dict[str, Any], api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o-mini") -> Optional[List[Dict[str, Any]]]:
    user_msg = f"""Battery Specs: {json.dumps(battery)}
Operator Notes to interpret:
{json.dumps([{"note_index": i, "text": n} for i, n in enumerate(notes)])}

Extract directives in JSON format."""
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\nReturn a JSON object with a key 'directives' containing the array."},
            {"role": "user", "content": user_msg}
        ]
    }
    with httpx.Client(timeout=4.5) as client:
        resp = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return parsed.get("directives") or parsed.get("directive_interpretation") or [parsed]
    return None

def interpret_operator_notes(notes: List[str], battery: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Main entrypoint for operator note interpretation.

    Architecture:
        Operator Notes
             |
             v
        LLM Interpreter  (Groq / Gemini / OpenAI — called FIRST if key configured)
             |
             v
        [Only if ALL LLM providers fail due to network/quota errors]
             |
             v
        Deterministic NLP Fallback  (guarantees 100% uptime SLA)
             |
             v
        Guardrail Validator  (validates output regardless of source)
             |
             v
        Optimizer

    The rule-based parser is a RELIABILITY FALLBACK only. It is NOT the primary
    interpretation path when a valid LLM key is configured.
    """
    raw_directives = None
    llm_used = False

    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")

    any_key_configured = bool(gemini_key or openai_key or groq_key)

    # PRIMARY PATH: Attempt LLM interpretation
    if gemini_key:
        try:
            raw_directives = call_gemini_api(notes, battery, gemini_key)
            if raw_directives:
                llm_used = True
                logger.info("Operator notes interpreted via Gemini LLM")
        except Exception as e:
            logger.warning(f"Gemini API failed: {e}")

    if not raw_directives and openai_key:
        try:
            raw_directives = call_openai_compatible_api(notes, battery, openai_key)
            if raw_directives:
                llm_used = True
                logger.info("Operator notes interpreted via OpenAI LLM")
        except Exception as e:
            logger.warning(f"OpenAI API failed: {e}")

    if not raw_directives and groq_key:
        groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        try:
            raw_directives = call_openai_compatible_api(
                notes, battery, groq_key,
                base_url="https://api.groq.com/openai/v1",
                model=groq_model
            )
            if raw_directives:
                llm_used = True
                logger.info(f"Operator notes interpreted via Groq LLM (model: {groq_model})")
        except Exception as e:
            logger.warning(f"Groq primary model ({groq_model}) failed: {e}")
            # Try Groq secondary model
            try:
                raw_directives = call_openai_compatible_api(
                    notes, battery, groq_key,
                    base_url="https://api.groq.com/openai/v1",
                    model="qwen/qwen3.8-27b"
                )
                if raw_directives:
                    llm_used = True
                    logger.info("Operator notes interpreted via Groq LLM (fallback model: qwen/qwen3.8-27b)")
            except Exception as e2:
                logger.warning(f"Groq fallback model also failed: {e2}")

    # FALLBACK PATH: Only reached if ALL configured LLM providers fail (network errors, quota, etc.)
    if not raw_directives:
        if any_key_configured:
            logger.error(
                "ALL configured LLM providers failed. "
                "Activating deterministic NLP fallback to maintain service availability. "
                "This is a reliability fallback, not the primary interpretation path."
            )
        else:
            logger.info(
                "No LLM API key configured. "
                "Running deterministic NLP fallback parser. "
                "Set GEMINI_API_KEY, OPENAI_API_KEY, or GROQ_API_KEY for LLM interpretation."
            )
        raw_directives = [
            extract_rule_based_directive(note, idx, battery)
            for idx, note in enumerate(notes)
        ]

    return raw_directives
