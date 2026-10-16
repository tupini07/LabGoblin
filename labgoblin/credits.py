"""Cumulative credit accounting from owned receipts, never premium multipliers."""

from decimal import Decimal, ROUND_CEILING
import json

from labgoblin.protocol import BudgetExhausted, UncertainExecution, integer, number


NANO = 1_000_000_000
MIN_SESSION = 30 * NANO
COACH_RESERVE = MIN_SESSION
MAX_CONSULT_RESERVE = 300 * NANO


def consultation_reserve(allowance, *, enabled=False):
    if not enabled:
        return 0
    return MAX_CONSULT_RESERVE if allowance is None else min(MAX_CONSULT_RESERVE, max(MIN_SESSION, allowance // 10))


def parse_limit(value, name="max_ai_credits"):
    if value == "unlimited":
        return None
    number(value, name)
    if value < 30 or Decimal(str(value)) * NANO != (Decimal(str(value)) * NANO).to_integral_value():
        raise ValueError(f"{name} must be >=30 credits with at most nine decimal places, or 'unlimited'")
    return value


def nano_limit(value):
    return None if value is None else int(Decimal(str(value)) * NANO)


def argument(value):
    return format(Decimal(value) / NANO, "f")


def usage_nano(value):
    if not isinstance(value, dict) or "totalNanoAiu" not in value:
        raise ValueError("Usage lacks totalNanoAiu; premium-request cost is not AI credits")
    amount = number(value["totalNanoAiu"], "totalNanoAiu", zero=True)
    return int(Decimal(str(amount)).to_integral_value(rounding=ROUND_CEILING))


def summarize(rows, limit, *, adjustments=None):
    used, reserved, estimated, partial = 0, 0, 0, 0
    unknown, covered, active = [], [], []
    adjustments = adjustments or {}
    for row in rows:
        envelope = json.loads(row["envelope"])
        if not row["receipt"]:
            active.append(envelope["id"])
            if envelope["allowance_nano"] is not None:
                reserved += integer(envelope["allowance_nano"], "reserved credits")
            continue
        receipt = json.loads(row["receipt"])
        if not receipt["executed"]:
            continue
        if receipt["usage"] is not None:
            used += usage_nano(receipt["usage"])
            continue
        adjustment = adjustments.get(envelope["id"])
        lower = usage_nano(receipt["metadata"]["partial_usage"]) if receipt["metadata"].get("partial_usage") else 0
        if adjustment and not adjustment["estimated"]:
            amount = usage_nano(adjustment["usage"])
            if amount < lower:
                raise ValueError("Reconciled usage is below the retained partial lower bound")
            used += amount
        else:
            unknown.append(envelope["id"])
            partial += lower
            used += lower
            if adjustment:
                amount = usage_nano(adjustment["usage"])
                if amount < lower:
                    raise ValueError("Approved estimate is below the retained partial lower bound")
                estimated += amount - lower
                covered.append(envelope["id"])
    uncovered = [item for item in unknown if item not in covered]
    cap = nano_limit(limit)
    remaining = None if cap is None or uncovered else max(0, cap - used - estimated - reserved)
    return {"configured": limit if limit is not None else "unlimited", "soft_cap": True,
            "used_nano": used, "used": used / NANO, "partial_nano": partial,
            "estimated_nano": estimated, "estimated": estimated / NANO,
            "accounted_nano": used + estimated, "reserved_nano": reserved,
            "remaining_nano": remaining, "remaining": None if remaining is None else remaining / NANO,
            "unknown": unknown, "uncovered_unknown": uncovered, "active": active,
            "remaining_is_estimated": bool(covered)}


def available(summary, *, coach=False, consultant=False):
    if summary["uncovered_unknown"]:
        raise UncertainExecution("Final credit usage is unknown; record audited usage or an explicit operator estimate before running")
    if summary["active"]:
        raise UncertainExecution("A previous owned execution is unsettled; reconcile it before another admission")
    allowance = summary["remaining_nano"]
    required = MIN_SESSION + (COACH_RESERVE if coach else 0) + consultation_reserve(allowance, enabled=consultant)
    if allowance is not None and allowance < required:
        raise BudgetExhausted("Remaining allowance cannot fund a researcher and the configured reviews/consultations")
    return allowance
