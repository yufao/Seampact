"""Provider-reported token accounting; absent receipts never imply zero cost."""


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
            receipts.append(token_count(data.get("usage"))
                            if data.get("usage_returned", True) and data.get("request_count", 1) == 1 else None)
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
