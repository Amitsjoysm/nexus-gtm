"""SLO thresholds for load tests: read ``quality/slo.yaml``, validate it, export resolved JSON.

The scripts never hard-code a latency or error budget. k6 and Gatling read the JSON this module
writes, so promoting the system from ``baseline`` to ``strict`` is a one-word change in the SLO file
and nothing in ``deploy/loadtest`` moves.

Source, in order:

1. ``LT_SLO_EXPORT`` — a JSON file already exported by the Quality foundation's own exporter. Used
   verbatim once it validates, so the two exporters cannot disagree about what a profile means.
2. ``quality/slo.yaml`` — the real file, owned by the Quality foundation task.
3. ``quality/load/slo.fixture.yaml`` — same schema, used ONLY while (2) has not merged. Every export
   produced from it says ``"is_fixture": true`` so a result file can never pass one off as the other.

    python scripts/load/slo.py export [--profile strict] [--out path]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    QUALITY_LOAD,
    REPO_ROOT,
    ConfigError,
    dump_json,
    load_yaml,
    number,
    rel,
)

REAL_SLO = REPO_ROOT / "quality" / "slo.yaml"
FIXTURE_SLO = QUALITY_LOAD / "slo.fixture.yaml"
EXPORT_SCHEMA = "nexus.slo.export/v1"

_ROUTE_KEY = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) /\S*$")
_ROUTE_FIELDS = {"p95_ms", "p99_ms", "error_rate_pct"}


def source_path() -> tuple[Path, bool]:
    """The SLO file to read and whether it is the fixture."""
    if REAL_SLO.exists():
        return REAL_SLO, False
    return FIXTURE_SLO, True


def _validate_api(api: Any, where: str) -> dict:
    if not isinstance(api, dict):
        raise ConfigError(f"{where}: expected a mapping")
    p95 = number(api, "p95_ms", where, minimum=1)
    p99 = number(api, "p99_ms", where, minimum=1)
    err = number(api, "error_rate_pct", where, minimum=0)
    if p99 < p95:
        raise ConfigError(f"{where}: p99_ms ({p99}) is below p95_ms ({p95})")
    if err > 100:
        raise ConfigError(f"{where}.error_rate_pct: a percentage, got {err}")
    return {"p95_ms": p95, "p99_ms": p99, "error_rate_pct": err}


def _validate_routes(routes: Any, where: str) -> dict:
    if routes in (None, {}):
        return {}
    if not isinstance(routes, dict):
        raise ConfigError(f"{where}: expected a mapping of \"<METHOD> <path>\" to overrides")
    out: dict[str, dict] = {}
    for key, override in routes.items():
        if not _ROUTE_KEY.match(str(key)):
            raise ConfigError(f"{where}: route key {key!r} is not \"<METHOD> <path>\"")
        if not isinstance(override, dict) or not override:
            raise ConfigError(f"{where}.{key}: expected a non-empty mapping")
        unknown = set(override) - _ROUTE_FIELDS
        if unknown:
            raise ConfigError(f"{where}.{key}: unknown keys {sorted(unknown)}")
        out[str(key)] = {k: number(override, k, f"{where}.{key}", minimum=0) for k in override}
    return out


def validate(raw: Any, *, where: str = "slo") -> None:
    """Fail loudly on a bad profile name or a missing key, never fall back to a default."""
    resolve(raw, None, where=where)


def resolve(raw: Any, profile: str | None, *, where: str = "slo") -> dict:
    """The selected profile with route overrides merged (profile-level beats top-level)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping at the top level")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError(f"{where}: missing `profiles`")
    selected = profile or raw.get("profile")
    if not selected:
        raise ConfigError(f"{where}: missing top-level `profile`")
    top_routes = _validate_routes(raw.get("routes"), f"{where}.routes")

    resolved: dict[str, dict] = {}
    for name, body in profiles.items():
        pw = f"{where}.profiles.{name}"
        if not isinstance(body, dict):
            raise ConfigError(f"{pw}: expected a mapping")
        window = body.get("alert_window")
        if not isinstance(window, dict):
            raise ConfigError(f"{pw}: missing `alert_window`")
        if not re.fullmatch(r"\d+[smh]", str(window.get("bucket", ""))):
            raise ConfigError(f"{pw}.alert_window.bucket: expected e.g. 1m, got {window.get('bucket')!r}")
        availability = number(body, "availability_pct", pw, minimum=0)
        if availability > 100:
            raise ConfigError(f"{pw}.availability_pct: a percentage, got {availability}")
        resolved[name] = {
            "api": _validate_api(body.get("api"), f"{pw}.api"),
            "availability_pct": availability,
            "queue_lag_minutes": number(body, "queue_lag_minutes", pw, minimum=0),
            "alert_window": {
                "bucket": str(window["bucket"]),
                "consecutive": int(number(window, "consecutive", f"{pw}.alert_window",
                                          minimum=1, integer=True)),
            },
            "routes": {**top_routes, **_validate_routes(body.get("routes"), f"{pw}.routes")},
        }
    if selected not in resolved:
        raise ConfigError(
            f"{where}: profile {selected!r} is not defined (have: {', '.join(sorted(resolved))})"
        )
    return {"profile": selected, **resolved[selected]}


def _validate_export(data: Any, where: str) -> dict:
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a JSON object")
    if not data.get("profile"):
        raise ConfigError(f"{where}: missing `profile`")
    return {
        "profile": data["profile"],
        "api": _validate_api(data.get("api"), f"{where}.api"),
        "availability_pct": number(data, "availability_pct", where, minimum=0),
        "queue_lag_minutes": number(data, "queue_lag_minutes", where, minimum=0),
        "alert_window": data.get("alert_window") or {},
        "routes": _validate_routes(data.get("routes"), f"{where}.routes"),
    }


def export(profile: str | None = None, *, path: Path | None = None) -> dict:
    """Resolved SLO JSON for the tools. ``profile`` overrides the file's own selection."""
    external = os.environ.get("LT_SLO_EXPORT")
    if external and path is None:
        data = json.loads(Path(external).read_text(encoding="utf-8"))
        body = _validate_export(data, f"LT_SLO_EXPORT={external}")
        if profile and body["profile"] != profile:
            raise ConfigError(
                f"LT_SLO_EXPORT is profile {body['profile']!r} but {profile!r} was requested"
            )
        return {"schema": EXPORT_SCHEMA, "source": external, "is_fixture": False, **body}

    src, is_fixture = (path, path == FIXTURE_SLO) if path else source_path()
    body = resolve(load_yaml(src), profile, where=rel(src))
    return {"schema": EXPORT_SCHEMA, "source": rel(src), "is_fixture": is_fixture, **body}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="print or write the resolved SLO JSON")
    exp.add_argument("--profile", help="override the file's `profile` (e.g. strict)")
    exp.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        text = dump_json(export(args.profile))
    except ConfigError as exc:
        print(f"SLO config error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
