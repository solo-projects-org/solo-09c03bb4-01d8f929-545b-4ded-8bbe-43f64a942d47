"""Manuscript result comparison.

Only the three effect-size fields (``estimate``, ``ci_low``, ``ci_high``)
produced by *this* run's ``result.json`` are compared against the
manuscript JSON. A field counts as consistent only when both sides are
finite real numbers and the absolute error is within tolerance.
"""
from __future__ import annotations

import math
from typing import Any

FIELDS = ("estimate", "ci_low", "ci_high")
TOLERANCE = 1e-6


def _is_finite_number(value: Any) -> bool:
    # bool is a subclass of int but is not a measurement.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _json_safe(value: Any) -> Any:
    """Keep reports strictly JSON-serializable: NaN/inf become strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # "nan", "inf", "-inf"
    return value


def compare_results(expected: dict, actual: dict) -> dict:
    """Compare the three effect-size fields.

    Returns ``{"checks": [...], "all_finite": bool, "consistent": bool}``.
    ``all_finite`` is False when any *actual* value is missing or not a
    finite number (the run then fails with kind ``invalid_result``).
    Extra keys in either mapping are ignored.
    """
    checks = []
    all_finite = True
    consistent = True
    for field in FIELDS:
        exp = expected.get(field)
        act = actual.get(field)
        act_ok = _is_finite_number(act)
        exp_ok = _is_finite_number(exp)
        if not act_ok:
            all_finite = False

        abs_error = None
        ok = False
        if act_ok and exp_ok:
            abs_error = abs(float(act) - float(exp))
            ok = abs_error <= TOLERANCE
            note = None if ok else "absolute error exceeds tolerance"
        elif not act_ok:
            note = "actual value missing or not a finite number"
        else:
            note = "expected value missing or not a finite number"

        if not ok:
            consistent = False
        checks.append(
            {
                "field": field,
                "expected": _json_safe(exp),
                "actual": _json_safe(act),
                "abs_error": abs_error,
                "tolerance": TOLERANCE,
                "ok": ok,
                "note": note,
            }
        )
    return {"checks": checks, "all_finite": all_finite, "consistent": consistent}
