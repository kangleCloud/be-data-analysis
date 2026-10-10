"""普通故障与确认风控使用不同控制键；旧键原因未知，等待自然过期。"""

import json

PREFIX = "stock:source-control:v1:"


def risk_key(group):
    return f"{PREFIX}risk:{group}"


def ordinary_key(key):
    return f"{key}:ordinary"


def protected_keys(keys, group, mode):
    return (*keys, risk_key(group), *(ordinary_key(key) for key in keys)) if mode == "auto" else (*keys, risk_key(group))


def risk_rejection(metadata):
    return (metadata.get("http_status") in {401, 403, 429}
            or metadata.get("category") in {"SOURCE_REJECTED", "AUTH_REJECTED"}
            or metadata.get("exception_type") == "RateLimitError")


def remaining(client, keys, group, mode):
    for key in protected_keys(keys, group, mode):
        if client.exists(key):
            return client.ttl(key)
    return None


def record(client, keys, group, mode, metadata, *, ordinary_seconds=300):
    risk = risk_rejection(metadata)
    if risk:
        key, seconds = risk_key(group), 7200
    elif mode == "auto" and keys:
        key, seconds = ordinary_key(keys[-1]), ordinary_seconds
    else:
        return
    value = json.dumps({"reason": "RISK" if risk else "ORDINARY", "httpStatus": metadata.get("http_status")})
    client.set(key, value, ex=seconds, nx=True)
