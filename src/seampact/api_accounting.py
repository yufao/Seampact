"""Provider-reported token accounting; absent receipts never imply zero cost."""

import math


PROGRESS_RECORD_LIMIT = 128
_PHASES = {"starting", "waiting_response", "waiting_stream_with_usage", "waiting_stream_without_usage_options",
           "waiting_nonstream_fallback", "receiving", "response_completed", "failed_partial_text_not_returned",
           "unknown"}
_ERROR_TYPES = {"TimeoutError", "APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
                "APIConnectionError", "ConnectionError", "ConnectError", "ReadError", "WriteError",
                "RemoteProtocolError", "APIStatusError", "BadRequestError", "AuthenticationError",
                "PermissionDeniedError", "NotFoundError", "RateLimitError", "InternalServerError",
                "UnprocessableEntityError", "ValueError", "TypeError", "RuntimeError", "CancelledError",
                "KeyboardInterrupt", "unknown"}
_ERROR_KINDS = {"timeout", "transport", "http_status", "protocol", "cancelled", "unknown"}
_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call", "error", "other", "unknown"}


def _private_diagnostics(data):
    """Only bounded-vocabulary failure labels and finite elapsed times."""
    result = {}
    for key in ("elapsed_s", "elapsed_ms"):
        value = data.get(key)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[key] = value
    for key, allowed in (("phase", _PHASES), ("finish_reason", _FINISH_REASONS),
                         ("timeout_source", {"connection_explicit", "sdk_default", "unknown"})):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = value if value in allowed else "unknown"
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    error_type = data.get("error_type", error.get("type"))
    if isinstance(error_type, str):
        result["error_type"] = error_type if error_type in _ERROR_TYPES else "unknown"
    kind = data.get("error_kind")
    if isinstance(kind, str):
        result["error_kind"] = kind if kind in _ERROR_KINDS else "unknown"
    elif "error_type" in result:
        result["error_kind"] = ("timeout" if "Timeout" in result["error_type"] else "unknown")
    return result


def private_progress(data):
    """Progress is separate from usage: no text, request IDs or token estimates."""
    data = data if isinstance(data, dict) else {}
    result = _private_diagnostics(data)
    for key in ("request_index", "transport_attempt", "received_chunks", "generated_characters", "reasoning_characters",
                "completed_lines", "unique_nonempty_completed_lines", "repeated_nonempty_completed_lines",
                "max_line_occurrences", "pending_line_characters", "progress_emit_errors"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    return result


def receipt_usage(data):
    """Read the two per-request event formats without inferring missing usage."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if "usage_returned" in data:
        returned = data["usage_returned"] is True
    elif "usage_received" in data:
        returned = data["usage_received"] is True
    else:
        returned = isinstance(data.get("usage"), dict)
    return usage, returned


def local_inference(data):
    """Only explicitly identified local classification is outside API tokens."""
    return (data.get("metering_scope") == "local_inference" or
            data.get("provider") == "transformers" and data.get("request_type") == "sequence_classification")


def private_receipt(data):
    """Retain only accounting fields from a possibly private usage event."""
    import hashlib

    usage, returned = receipt_usage(data)
    usage = usage if isinstance(usage, dict) else {}
    fields = ("total_tokens", "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    result = dict(usage={key: usage[key] for key in fields
                         if type(usage.get(key)) is int and usage[key] >= 0},
                  usage_returned=returned)
    if local_inference(data):
        result["metering_scope"] = "local_inference"
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
    result.update(_private_diagnostics(data))
    temperature = data.get("effective_temperature", data.get("temperature"))
    if type(temperature) in (int, float) and 0 <= temperature <= 2 and math.isfinite(temperature):
        result["effective_temperature"] = temperature
        sources = {"service.parameters.temperature": "connection_explicit",
                   "service.parameters.extra_body.temperature": "connection_explicit",
                   "connection_explicit": "connection_explicit",
                   "upstream_triplet_default": "upstream_triplet_default",
                   "upstream.RailsConfig.lowest_temperature": "upstream_rail_default",
                   "upstream_rail_default": "upstream_rail_default", "service_default": "service_default"}
        source = data.get("temperature_source")
        result["temperature_source"] = sources.get(source, "unknown") if isinstance(source, str) else "unknown"
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
            if local_inference(data):
                continue
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
