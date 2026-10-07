"""Copilot credit units and accounting from immutable owned launch receipts."""

from decimal import Decimal, ROUND_CEILING
import json

from labgoblin.protocol import BudgetExhausted, UncertainExecution, integer, number


NANO = 1_000_000_000
MIN_SESSION = 30 * NANO


def parse_limit(value, name):
    if value is None or value == "unlimited":
        return None
    number(value, name)
    if value < 30:
        raise ValueError(f"{name} must be at least 30 AI credits, or 'unlimited'")
    if Decimal(str(value)) * NANO != (Decimal(str(value)) * NANO).to_integral_value():
        raise ValueError(f"{name} supports at most nine decimal places")
    return value


def nano_limit(value):
    return None if value is None else int(Decimal(str(value)) * NANO)


def estimate_nano(value):
    number(value, "estimated credits", zero=True)
    scaled = Decimal(str(value)) * NANO
    if scaled != scaled.to_integral_value():
        raise ValueError("Estimated credits support at most nine decimal places")
    return int(scaled)


def argument(nano):
    return format(Decimal(nano) / NANO, "f")


def usage_nano(usage):
    if not isinstance(usage, dict) or "totalNanoAiu" not in usage:
        raise ValueError("Copilot usage lacks totalNanoAiu; premium-request cost is not AI credits")
    value = number(usage["totalNanoAiu"], "Copilot totalNanoAiu", zero=True)
    return int(Decimal(str(value)).to_integral_value(rounding=ROUND_CEILING))


def summarize(rows, limit, *, reconciliations=None, estimates=None):
    used, reserved, unknown = 0, 0, []
    partial_used, reconciled = 0, []
    estimated_used, estimated, uncovered = 0, [], []
    reconciliations = reconciliations or {}
    estimates = estimates or {}
    for row in rows:
        if row["phase"] == "revoked":
            continue
        envelope = json.loads(row["envelope"])
        if not row["receipt"]:
            allowance = envelope["metadata"].get("credit_allowance_nano")
            if type(allowance) is int and allowance >= MIN_SESSION:
                reserved += allowance
            else:
                unknown.append(envelope["key"]["work_id"])
                uncovered.append(envelope["key"]["work_id"])
            continue
        receipt = json.loads(row["receipt"])
        if receipt.get("executed") is False:
            continue
        try:
            used += usage_nano(receipt.get("usage"))
        except ValueError:
            replacement = reconciliations.get(envelope["key"]["nonce"])
            if replacement is not None:
                used += usage_nano(replacement)
                reconciled.append(envelope["key"]["work_id"])
            else:
                partial = receipt.get("metadata", {}).get("partial_usage")
                value = 0
                if partial is not None:
                    value = usage_nano(partial)
                    used += value
                    partial_used += value
                unknown.append(envelope["key"]["work_id"])
                estimate = estimates.get(envelope["key"]["nonce"])
                if estimate is None:
                    uncovered.append(envelope["key"]["work_id"])
                else:
                    amount = integer(estimate["estimatedNanoAiu"], "estimated nano-credits", zero=True)
                    if amount < value:
                        raise ValueError("Credit estimate is below retained partial usage")
                    estimated_used += amount - value
                    estimated.append(envelope["key"]["work_id"])
    configured = nano_limit(limit)
    accounted = used + estimated_used
    remaining = None if configured is None or uncovered else max(0, configured - accounted - reserved)
    return {
        "configured": limit, "unlimited": limit is None,
        "used": used / NANO, "reserved": reserved / NANO,
        "remaining": None if remaining is None else remaining / NANO,
        "used_nano": used, "reserved_nano": reserved, "remaining_nano": remaining,
        "unknown": len(unknown), "unknown_ids": unknown[:20],
        "uncovered_unknown": len(uncovered), "uncovered_unknown_ids": uncovered[:20],
        "soft_cap": True, "minimum_session_credits": 30,
        "partial_used": partial_used / NANO,
        "reconciled": len(reconciled), "reconciled_ids": reconciled[:20],
        "estimated": len(estimated), "estimated_ids": estimated[:20],
        "estimated_used": estimated_used / NANO, "estimated_nano": estimated_used,
        "accounted_used": accounted / NANO, "accounted_nano": accounted,
        "remaining_is_estimated": bool(estimated),
    }


def available(summary, *, reserve=0):
    if summary["unlimited"]:
        return None
    if summary["uncovered_unknown"]:
        raise UncertainExecution("AI credit usage is unknown for an owned invocation; further inference is blocked")
    remaining = summary["remaining_nano"] - reserve
    if remaining < MIN_SESSION:
        raise BudgetExhausted("ai_credits", "AI credit allowance cannot fund another 30-credit session after reservations")
    return remaining
