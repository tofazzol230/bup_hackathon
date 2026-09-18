"""
replay_validator.py — Deterministic post-optimizer physical constraint checker.

After the LP solver returns a plan, this module re-validates every physical
rule independently, before the response is returned to the caller.

Checks performed:
  1. Exactly 24 unique hours (0..23)
  2. Energy balance per hour
  3. Solar used <= effective solar
  4. Battery state transition consistency
  5. Battery minimum energy (including directive reserve)
  6. Battery capacity upper bound
  7. Charge rate limit (and no_charge_window)
  8. Discharge rate limit (and no_discharge_window)
  9. Max grid cap window
 10. End-of-day battery neutrality
 11. Reported total_grid_kwh matches sum of hourly grid
 12. Reported total_cost_bdt matches recalculated cost
 13. Reported peak_grid_kwh matches max of hourly grid
"""

from typing import List, Dict, Any

TOLERANCE = 0.02  # kWh / BDT tolerance for floating-point rounding


class ValidationError(ValueError):
    """Raised when the optimizer plan violates a physical constraint."""
    pass


def replay_validate(
    hourly_plan: List[Dict[str, Any]],
    hours_data: List[Dict[str, Any]],
    battery_data: Dict[str, Any],
    directives: List[Dict[str, Any]],
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float
) -> None:
    """
    Runs all physical constraint checks. Raises ValidationError if any fail.
    Call this after solve_energy_schedule() and before returning the response.
    """
    n = 24
    capacity = float(battery_data["capacity_kwh"])
    initial_energy = float(battery_data["initial_energy_kwh"])
    base_min_energy = float(battery_data["minimum_energy_kwh"])
    max_charge_rate = float(battery_data["max_charge_kwh_per_hour"])
    max_discharge_rate = float(battery_data["max_discharge_kwh_per_hour"])

    # --- Precompute directive constraints ---
    # Effective solar
    eff_solar = [float(h["solar_kwh"]) for h in hours_data]
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "solar_reduction":
            adj = d.get("structured_adjustment") or {}
            factor = float(adj.get("factor", 1.0))
            for hr in adj.get("hours", []):
                if 0 <= hr < n:
                    eff_solar[hr] *= factor

    # Minimum reserve per hour
    min_reserve = [base_min_energy] * n
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "minimum_battery_reserve":
            adj = d.get("structured_adjustment") or {}
            val = float(adj.get("minimum_energy_kwh", base_min_energy))
            for hr in adj.get("hours", []):
                if 0 <= hr < n:
                    min_reserve[hr] = max(min_reserve[hr], val)

    # No charge hours
    no_charge_hours = set()
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "no_charge_window":
            for hr in (d.get("structured_adjustment") or {}).get("hours", []):
                if 0 <= hr < n:
                    no_charge_hours.add(hr)

    # No discharge hours
    no_discharge_hours = set()
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "no_discharge_window":
            for hr in (d.get("structured_adjustment") or {}).get("hours", []):
                if 0 <= hr < n:
                    no_discharge_hours.add(hr)

    # Max grid per hour
    max_grid_cap = [float("inf")] * n
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "max_grid_window":
            adj = d.get("structured_adjustment") or {}
            cap = float(adj.get("max_grid_kwh", float("inf")))
            for hr in adj.get("hours", []):
                if 0 <= hr < n:
                    max_grid_cap[hr] = min(max_grid_cap[hr], cap)

    # --- CHECK 1: Exactly 24 unique hours 0..23 ---
    if len(hourly_plan) != 24:
        raise ValidationError(f"hourly_plan has {len(hourly_plan)} entries, expected 24")
    plan_hours = [e["hour"] for e in hourly_plan]
    if sorted(plan_hours) != list(range(24)):
        raise ValidationError(f"hourly_plan hours are not exactly 0..23: {sorted(plan_hours)}")

    # Sort by hour for sequential checks
    plan = sorted(hourly_plan, key=lambda e: e["hour"])

    recomputed_grid = 0.0
    recomputed_cost = 0.0
    recomputed_peak = 0.0
    prev_energy = initial_energy

    for entry in plan:
        h = entry["hour"]
        grid = float(entry["grid_kwh"])
        solar = float(entry["solar_used_kwh"])
        action = entry["battery_action"]
        bat_kwh = float(entry["battery_kwh"])
        energy_after = float(entry["battery_energy_after_kwh"])
        demand = float(hours_data[h]["demand_kwh"])
        tariff = float(hours_data[h]["tariff_bdt_per_kwh"])

        # Derive charge / discharge
        if action == "charge":
            charge = bat_kwh
            discharge = 0.0
        elif action == "discharge":
            charge = 0.0
            discharge = bat_kwh
        else:
            charge = 0.0
            discharge = 0.0

        # --- CHECK 2: Energy balance ---
        balance = grid + solar + discharge - charge
        if abs(balance - demand) > TOLERANCE:
            raise ValidationError(
                f"Hour {h}: energy balance violated. "
                f"grid({grid}) + solar({solar}) + discharge({discharge}) - charge({charge}) "
                f"= {balance:.4f}, expected demand {demand}"
            )

        # --- CHECK 3: Solar used <= effective solar ---
        if solar > eff_solar[h] + TOLERANCE:
            raise ValidationError(
                f"Hour {h}: solar_used({solar}) > effective_solar({eff_solar[h]:.4f})"
            )

        # --- CHECK 4: Battery state transition ---
        expected_energy = prev_energy + charge - discharge
        if abs(energy_after - expected_energy) > TOLERANCE:
            raise ValidationError(
                f"Hour {h}: battery transition violated. "
                f"prev({prev_energy}) + charge({charge}) - discharge({discharge}) "
                f"= {expected_energy:.4f}, reported {energy_after}"
            )

        # --- CHECK 5: Battery minimum reserve ---
        if energy_after < min_reserve[h] - TOLERANCE:
            raise ValidationError(
                f"Hour {h}: battery_energy_after({energy_after}) < min_reserve({min_reserve[h]})"
            )

        # --- CHECK 6: Battery capacity ---
        if energy_after > capacity + TOLERANCE:
            raise ValidationError(
                f"Hour {h}: battery_energy_after({energy_after}) > capacity({capacity})"
            )

        # --- CHECK 7: Charge rate and no_charge_window ---
        if charge > max_charge_rate + TOLERANCE:
            raise ValidationError(
                f"Hour {h}: charge({charge}) > max_charge_rate({max_charge_rate})"
            )
        if h in no_charge_hours and charge > TOLERANCE:
            raise ValidationError(
                f"Hour {h}: charging {charge} kWh violates no_charge_window"
            )

        # --- CHECK 8: Discharge rate and no_discharge_window ---
        if discharge > max_discharge_rate + TOLERANCE:
            raise ValidationError(
                f"Hour {h}: discharge({discharge}) > max_discharge_rate({max_discharge_rate})"
            )
        if h in no_discharge_hours and discharge > TOLERANCE:
            raise ValidationError(
                f"Hour {h}: discharging {discharge} kWh violates no_discharge_window"
            )

        # --- CHECK 9: Grid cap ---
        if grid > max_grid_cap[h] + TOLERANCE:
            raise ValidationError(
                f"Hour {h}: grid({grid}) > max_grid_cap({max_grid_cap[h]})"
            )

        recomputed_grid += grid
        recomputed_cost += grid * tariff
        if grid > recomputed_peak:
            recomputed_peak = grid
        prev_energy = energy_after

    # --- CHECK 10: End-of-day battery neutrality ---
    if abs(prev_energy - initial_energy) > TOLERANCE:
        raise ValidationError(
            f"Battery neutrality violated: final_SOC={prev_energy:.4f}, "
            f"initial_SOC={initial_energy}"
        )

    # --- CHECK 11: Reported total_grid_kwh matches ---
    if abs(total_grid_kwh - recomputed_grid) > TOLERANCE:
        raise ValidationError(
            f"total_grid_kwh({total_grid_kwh}) != recomputed({recomputed_grid:.4f})"
        )

    # --- CHECK 12: Reported total_cost_bdt matches ---
    if abs(total_cost_bdt - recomputed_cost) > TOLERANCE:
        raise ValidationError(
            f"total_cost_bdt({total_cost_bdt}) != recomputed({recomputed_cost:.4f})"
        )

    # --- CHECK 13: Reported peak_grid_kwh matches ---
    if abs(peak_grid_kwh - recomputed_peak) > TOLERANCE:
        raise ValidationError(
            f"peak_grid_kwh({peak_grid_kwh}) != recomputed({recomputed_peak:.4f})"
        )
