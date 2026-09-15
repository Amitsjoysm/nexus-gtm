"""Pre-flight checks and persona logins, done by the runner BEFORE any load starts.

Nothing here generates load: a handful of GETs, and one login per persona credential, spaced for
the auth rate limit (nexus/core/ratelimit.py: 10 per 60 s per client IP, shared across replicas).
The tokens minted here are handed to the tool, so its virtual users never burst the login endpoint
— which would 429 and read as an application error rate.
"""
from __future__ import annotations

import json
import ssl
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


class PreflightError(RuntimeError):
    """The target or a persona is not in a state worth loading. The run does not start."""


@dataclass
class Response:
    status: int
    latency_ms: float
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


def request(method: str, url: str, *, headers: dict | None = None, payload: Any = None,
            timeout: float = 15.0, insecure: bool = False) -> Response:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    context = ssl._create_unverified_context() if insecure else None  # local self-signed CA only
    started = time.perf_counter()

    def elapsed() -> float:
        return (time.perf_counter() - started) * 1000

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read()
            return Response(resp.status, elapsed(), body, dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, elapsed(), exc.read(), dict(exc.headers or {}))
    except Exception as exc:  # refused, DNS, TLS, timeout — all "not reachable"
        return Response(0, elapsed(), error=f"{type(exc).__name__}: {exc}")


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def check_health(host_url: str, headers: dict, *, insecure: bool = False,
                 probe: Callable[..., Response] = request) -> dict:
    """/health, /ready (database up), /metrics, and a 5-probe readiness latency baseline."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    r = probe("GET", f"{host_url}/health", headers=headers, insecure=insecure)
    body = r.json() or {}
    add("health", r.status == 200 and body.get("status") == "ok",
        f"HTTP {r.status}{' ' + r.error if r.error else ''}")
    r = probe("GET", f"{host_url}/ready", headers=headers, insecure=insecure)
    body = r.json() or {}
    add("ready", r.status == 200 and body.get("db") == "up",
        f"HTTP {r.status} db={body.get('db')}{' ' + r.error if r.error else ''}")
    r = probe("GET", f"{host_url}/metrics", headers=headers, insecure=insecure)
    add("metrics", r.status == 200, f"HTTP {r.status}", required=False)

    samples = [probe("GET", f"{host_url}/ready", headers=headers, insecure=insecure) for _ in range(5)]
    latencies = [s.latency_ms for s in samples if s.status == 200]
    baseline = round(statistics.median(latencies), 1) if latencies else None
    add("ready_baseline", len(latencies) == len(samples),
        f"{len(latencies)}/{len(samples)} ok, median {baseline} ms")
    return {
        "ok": all(c["ok"] for c in checks if c["required"]),
        "checks": checks,
        "ready_baseline_ms": baseline,
    }


def login_personas(host_url: str, catalogue: dict, credentials: list[dict], headers: dict, *,
                   insecure: bool = False, probe: Callable[..., Response] = request,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.time) -> list[dict]:
    """One login per credential. Fails fast on bad credentials, MFA, or the wrong role."""
    auth = catalogue["auth"]
    limit = auth["login_rate_limit"]
    burst = max(1, limit["max"] - 2)
    spacing = limit["window_s"] / burst if len(credentials) > burst else 0.0
    minted = []
    for index, cred in enumerate(credentials):
        if spacing and index >= burst:
            sleep(spacing)
        persona = cred["persona"]
        response = None
        for _ in range(4):
            response = probe("POST", f"{host_url}{auth['login_path']}", headers=headers,
                             payload={"email": cred["email"], "password": cred["password"]},
                             insecure=insecure)
            if response.status != 429:
                break
            sleep(min(float(response.headers.get("Retry-After") or limit["window_s"]), 90.0))
        assert response is not None
        who = f"persona {persona} ({mask_email(cred['email'])})"
        if response.status != 200:
            raise PreflightError(f"{who} could not log in: HTTP {response.status}"
                                 f"{' ' + response.error if response.error else ''}")
        body = response.json() or {}
        if not body.get("access_token"):
            raise PreflightError(f"{who} is MFA-enrolled (login returned a challenge). "
                                 "Load-test personas must not have a confirmed second factor.")
        expected = catalogue["personas"][persona]["role"]
        if expected != "platform_admin" and body.get("role") != expected:
            raise PreflightError(f"{who} logged in as {body.get('role')!r}, expected {expected!r}; "
                                 "its journeys would measure the wrong permissions")
        minted.append({"persona": persona, "email": cred["email"], "token": body["access_token"],
                       "role": body.get("role"), "issued_ms": int(clock() * 1000)})
    return minted


def platform_access(host_url: str, token: str, headers: dict, *, insecure: bool = False,
                    probe: Callable[..., Response] = request) -> tuple[bool, str]:
    """Whether the platform persona can reach the staff surface from this load generator."""
    r = probe("GET", f"{host_url}/api/admin/billing/whoami",
              headers={**headers, "Authorization": f"Bearer {token}"}, insecure=insecure)
    if r.status == 200:
        return True, "platform admin surface reachable"
    if r.status == 404:
        return False, "persona is not a platform admin (404 — the staff surface hides itself)"
    if r.status == 403:
        detail = (r.json() or {}).get("detail", "")
        return False, f"403 from the control plane: {detail or 'admin_ip_allowlist refuses this address'}"
    return False, f"HTTP {r.status}{' ' + r.error if r.error else ''}"
