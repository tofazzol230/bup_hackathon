from typing import List, Dict, Any, Optional
import math

VALID_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op"
}

def validate_and_sanitize_directives(
    raw_directives: List[Dict[str, Any]],
    num_notes: int,
    battery_capacity: float
) -> List[Dict[str, Any]]:
    """
    Validates and normalizes directive interpretations according to Section 08 guardrails.
    Guarantees:
      - Exactly num_notes entries in ascending note_index order (0..N-1).
      - Applies is strictly False for no_op, and True for other valid directives.
      - structured_adjustment is None for no_op.
      - Hours are sorted unique integers between 0 and 23.
      - Numeric values are non-negative and within valid physical bounds.
    """
    cleaned: List[Dict[str, Any]] = []
    
    # Map by note_index if available
    directive_map = {}
    for item in raw_directives:
        if isinstance(item, dict) and "note_index" in item:
            try:
                idx = int(item["note_index"])
                directive_map[idx] = item
            except (ValueError, TypeError):
                pass

    for i in range(num_notes):
        item = directive_map.get(i)
        if item is None and i < len(raw_directives) and isinstance(raw_directives[i], dict):
            item = raw_directives[i]

        if not isinstance(item, dict):
            # Fallback safe no_op
            cleaned.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "No actionable energy directive found."
            })
            continue

        directive_type = item.get("directive_type", "no_op")
        if directive_type not in VALID_DIRECTIVE_TYPES:
            directive_type = "no_op"

        explanation = str(item.get("explanation", "Processed operator directive."))

        if directive_type == "no_op":
            cleaned.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": explanation
            })
            continue

        raw_adj = item.get("structured_adjustment")
        if not isinstance(raw_adj, dict):
            cleaned.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": f"Malformed adjustment for {directive_type}; treated safely as no_op."
            })
            continue

        # Sanitize hours
        hours_raw = raw_adj.get("hours", [])
        if not isinstance(hours_raw, list):
            hours_raw = []
        valid_hours = []
        for h in hours_raw:
            try:
                h_int = int(h)
                if 0 <= h_int <= 23:
                    valid_hours.append(h_int)
            except (ValueError, TypeError):
                continue
        valid_hours = sorted(list(set(valid_hours)))

        if not valid_hours:
            cleaned.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": f"No valid hours specified for {directive_type}; treated safely as no_op."
            })
            continue

        adj: Dict[str, Any] = {"hours": valid_hours}

        if directive_type == "solar_reduction":
            factor = raw_adj.get("factor", 1.0)
            try:
                factor = float(factor)
                if math.isnan(factor) or math.isinf(factor):
                    factor = 1.0
            except (ValueError, TypeError):
                factor = 1.0
            factor = max(0.0, min(1.0, factor))
            adj["factor"] = round(factor, 4)

        elif directive_type == "minimum_battery_reserve":
            min_kwh = raw_adj.get("minimum_energy_kwh", 0.0)
            try:
                min_kwh = float(min_kwh)
                if math.isnan(min_kwh) or math.isinf(min_kwh):
                    min_kwh = 0.0
            except (ValueError, TypeError):
                min_kwh = 0.0
            min_kwh = max(0.0, min(battery_capacity, min_kwh))
            adj["minimum_energy_kwh"] = round(min_kwh, 2)

        elif directive_type == "max_grid_window":
            max_grid = raw_adj.get("max_grid_kwh", 0.0)
            try:
                max_grid = float(max_grid)
                if math.isnan(max_grid) or math.isinf(max_grid):
                    max_grid = 0.0
            except (ValueError, TypeError):
                max_grid = 0.0
            max_grid = max(0.0, max_grid)
            adj["max_grid_kwh"] = round(max_grid, 2)

        elif directive_type in ("no_charge_window", "no_discharge_window"):
            pass  # Only "hours" needed

        cleaned.append({
            "note_index": i,
            "applies": True,
            "directive_type": directive_type,
            "structured_adjustment": adj,
            "explanation": explanation
        })

    return cleaned
