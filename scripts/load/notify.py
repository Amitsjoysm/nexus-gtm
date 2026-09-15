"""The "staging is under load" notice, posted when a run starts and when it ends.

Sinks, all optional except stdout:

* stdout, always — and an Azure DevOps warning annotation when running in a pipeline (TF_BUILD);
* ``LT_NOTICE_WEBHOOK`` — any incoming webhook that accepts ``{"text": ...}`` (Slack, Teams via a
  workflow, Google Chat). The Operations task owns which channel that is;
* ``LT_GRAFANA_URL`` + ``LT_GRAFANA_TOKEN`` — a Grafana annotation tagged ``load-test``, so every
  dashboard shows the run as a shaded region instead of an unexplained latency bump.

A sink that fails is reported, not fatal: the notice exists to inform people, and refusing to run
because Slack is down would only move the test to a worse hour.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable


def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def message(phase: str, info: dict) -> str:
    if phase == "start":
        text = (
            f"[load test] {info['env']} is UNDER LOAD from now until about {info['ends_at']}: "
            f"{info['tool']} {info['profile']}, up to {info['peak_vus']} virtual users / "
            f"~{info['est_rps']} req/s. Requests carry X-Load-Test: {info['run_id']}."
        )
        if info.get("window_override"):
            text += f" Run-window override: {info['window_override']}."
        return text
    return (
        f"[load test] {info['env']} load finished ({info['tool']} {info['profile']}, "
        f"run {info['run_id']}): {info.get('outcome', 'see results')}."
    )


def notice(phase: str, info: dict, *, environ: dict | os._Environ = os.environ,
           printer: Callable[[str], None] = print,
           post: Callable[..., tuple[bool, str]] = _post) -> list[dict]:
    text = message(phase, info)
    bar = "=" * min(len(text), 100)
    printer(f"\n{bar}\n{text}\n{bar}\n")
    sinks = [{"sink": "stdout", "ok": True}]
    if environ.get("TF_BUILD"):
        printer(f"##vso[task.logissue type=warning]{text}")
        sinks.append({"sink": "azure-devops", "ok": True})
    webhook = environ.get("LT_NOTICE_WEBHOOK")
    if webhook:
        ok, detail = post(webhook, {"text": text})
        sinks.append({"sink": "webhook", "ok": ok, "detail": detail})
        if not ok:
            printer(f"warning: notice webhook failed ({detail})")
    grafana, token = environ.get("LT_GRAFANA_URL"), environ.get("LT_GRAFANA_TOKEN")
    if grafana and token:
        ok, detail = post(f"{grafana.rstrip('/')}/api/annotations",
                          {"text": text, "tags": ["load-test", info["env"], info["tool"],
                                                  info["profile"], info["run_id"], phase]},
                          headers={"Authorization": f"Bearer {token}"})
        sinks.append({"sink": "grafana-annotation", "ok": ok, "detail": detail})
    return sinks
