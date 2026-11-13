"""Provider-reported token accounting; absent receipts never imply zero cost."""


def receipt_usage(data):
    """Read the two per-request event formats without inferring missing usage."""
    if isinstance(data.get("usage"), dict) or "usage_returned" in data:
        return data.get("usage"), data.get("usage_returned", True) is True
    return data, data.get("usage_received") is True


def private_receipt(data):
    """Retain only accounting fields from a possibly private usage event."""
    import hashlib

    usage, returned = receipt_usage(data)
    usage = usage if isinstance(usage, dict) else {}
    fields = ("total_tokens", "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    result = dict(usage={key: usage[key] for key in fields
                         if type(usage.get(key)) is int and usage[key] >= 0},
                  usage_returned=returned)
    if "request_count" in data:
        count = data["request_count"]
        result["request_count"] = count if type(count) is int and count > 0 else None
    if isinstance(data.get("request_id"), str):
        result["request_id"] = hashlib.sha256(data["request_id"].encode()).hexdigest()
    # Private references and reasoning remain excluded; completion validity is
    # needed to distinguish an unavailable check from a genuine verdict.
    reasons = {"stop", "length", "tool_calls", "content_filter", "function_call"}
    if isinstance(data.get("finish_reasons"), list):
        result["finish_reasons"] = [value if value in reasons else "unknown"
                                    for value in data["finish_reasons"] if isinstance(value, str)][:8]
    for key in ("empty_content", "verdict_valid"):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key in ("effective_max_tokens", "visible_output_chars"):
        if type(data.get(key)) is int and data[key] >= 0:
            result[key] = data[key]
    return result


def token_count(usage):
    if not isinstance(usage, dict):
        return None
    def valid(value):
        return type(value) is int and value >= 0
    total = usage.get("total_tokens")
    if valid(total):
        return total
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if valid(input_tokens) and valid(output_tokens):
        # Cached/reasoning detail fields are subsets, not extra token charges.
        return input_tokens + output_tokens
    return None


def summarize(business_usage, capability_calls):
    business = [token_count(usage) for usage in business_usage]
    capability, unknown_calls = [], 0
    for call in capability_calls:
        receipts, seen = [], set()
        for event in call.get("model_usage", []):
            data = event.get("data") or {}
            request_id = data.get("request_id")
            if request_id is not None:
                key = (event.get("call_id"), request_id)
                if key in seen:
                    continue
                seen.add(key)
            # A batch/last-attempt receipt cannot establish the total cost of
            # several provider requests. Emit one usage event per request.
            usage, returned = receipt_usage(data)
            count = data.get("request_count", 1)
            receipts.append(token_count(usage)
                            if returned and type(count) is int and count == 1 else None)
        if call.get("uses_model_service") and not receipts:
            unknown_calls += 1
        capability.extend(receipts)
    counts = business + capability
    missing = sum(value is None for value in counts)
    observed = sum(value for value in counts if value is not None)
    return dict(tokens=None if missing or unknown_calls else observed,
                observed_tokens=observed, unknown_requests=missing,
                unreported_capability_calls=unknown_calls,
                business_requests=len(business), capability_requests=len(capability),
                source="provider_reported_tokens", unit="tokens")
