import os


def required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


def base_url_from_request(headers) -> str:
    proto = headers.get("X-Forwarded-Proto") or "https"
    host = headers.get("X-Forwarded-Host") or headers.get("Host")
    if not host:
        raise RuntimeError("no Host header")
    return f"{proto}://{host}"
