import logging
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from app.schemas import (
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    DirectiveInterpretation,
    HourlyPlanEntry
)
from app.guardrails import validate_and_sanitize_directives
from app.optimizer import solve_energy_schedule
from app.llm import interpret_operator_notes
from app.replay_validator import replay_validate, ValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gridwise.api")

app = FastAPI(
    title="BUP CSE Fest 2026 - GridWise LLM Energy Optimizer",
    version="1.0.0"
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Malformed JSON or structurally invalid request."}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Internal server error: {exc}", exc_info=False)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Controlled internal error processing request."}
    )

@app.get("/")
async def root():
    """Root landing endpoint for browser visitors."""
    return {
        "service": "GridWise LLM Smart Campus Energy Optimizer",
        "status": "running",
        "health": "/health",
        "docs": "/docs"
    }

@app.get("/health")
async def health_check():
    """Readiness endpoint for the competition judging harness."""
    return {"status": "ok"}

@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
async def optimize_energy(payload: OptimizeEnergyRequest):
    """
    Main LLM interpretation + 24-hour energy optimization endpoint.
    """
    try:
        battery_dict = payload.battery.model_dump()
        hours_dict = [h.model_dump() for h in payload.hours]
        num_notes = len(payload.operator_notes)

        # 1. LLM Interpretation
        raw_directives = interpret_operator_notes(payload.operator_notes, battery_dict)

        # 2. Deterministic Guardrails
        validated_directives = validate_and_sanitize_directives(
            raw_directives,
            num_notes=num_notes,
            battery_capacity=payload.battery.capacity_kwh
        )

        # 3. Mathematical Optimization (LP)
        hourly_plan, total_grid, total_cost, peak_grid = solve_energy_schedule(
            hours_data=hours_dict,
            battery_data=battery_dict,
            directives=validated_directives
        )

        # 4. Deterministic Replay Validator — re-checks ALL physical constraints
        #    independently before any response is returned to the caller.
        try:
            replay_validate(
                hourly_plan=hourly_plan,
                hours_data=hours_dict,
                battery_data=battery_dict,
                directives=validated_directives,
                total_grid_kwh=total_grid,
                total_cost_bdt=total_cost,
                peak_grid_kwh=peak_grid
            )
        except ValidationError as ve:
            logger.error(f"Replay validation failed for scenario {payload.scenario_id}: {ve}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Physical constraint validation failed: {ve}"
            )

        # 5. Generate plan summary
        active_directives = [
            d["directive_type"] for d in validated_directives if d.get("applies")
        ]
        if active_directives:
            summary_text = (
                f"Optimal 24-hour schedule generated obeying directives: {', '.join(active_directives)}. "
                f"Minimized grid electricity cost to {total_cost:.2f} BDT with {total_grid:.2f} kWh grid energy "
                f"and peak grid draw of {peak_grid:.2f} kWh, satisfying end-of-day battery neutrality."
            )
        else:
            summary_text = (
                f"Optimal 24-hour baseline schedule generated with no active constraints. "
                f"Minimized grid electricity cost to {total_cost:.2f} BDT with {total_grid:.2f} kWh grid energy "
                f"and peak grid draw of {peak_grid:.2f} kWh."
            )

        return OptimizeEnergyResponse(
            scenario_id=payload.scenario_id,
            directive_interpretation=[
                DirectiveInterpretation(**d) for d in validated_directives
            ],
            hourly_plan=[
                HourlyPlanEntry(**entry) for entry in hourly_plan
            ],
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid,
            plan_summary=summary_text
        )
    except Exception as e:
        logger.error(f"Error processing scenario {payload.scenario_id}: {e}", exc_info=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error computing optimal energy schedule."
        )
