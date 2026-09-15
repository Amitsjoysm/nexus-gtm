"""Shared paths and small helpers for the load-test runner.

Standard library plus PyYAML only. The runner has to work from a laptop, Azure Cloud Shell and a
Microsoft-hosted ADO agent, none of which have the app's virtualenv, so it deliberately imports
nothing from ``nexus``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

QUALITY_LOAD = REPO_ROOT / "quality" / "load"
SCENARIOS_FILE = QUALITY_LOAD / "scenarios.yaml"
ENVIRONMENTS_FILE = QUALITY_LOAD / "environments.yaml"
GENERATED_DIR = QUALITY_LOAD / "generated"
CATALOGUE_JSON = GENERATED_DIR / "catalogue.json"
SLO_JSON = GENERATED_DIR / "slo.json"

LOADTEST_DIR = REPO_ROOT / "deploy" / "loadtest"
K6_DIR = LOADTEST_DIR / "k6"
GATLING_DIR = LOADTEST_DIR / "gatling"

RESULTS_DIR = REPO_ROOT / "docs" / "quality" / "load-results"
LOAD_DOC = REPO_ROOT / "docs" / "quality" / "load.md"


class ConfigError(ValueError):
    """A catalogue, environment or SLO file that cannot be used as written."""


def rel(path: Path) -> str:
    """Repo-relative POSIX path, for messages and generated files that must not embed a machine."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise SystemExit("PyYAML is required by the load runner: pip install pyyaml") from exc
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def dump_json(data: Any) -> str:
    """Deterministic JSON: sorted keys, trailing newline. Generated files are diffed by `check`."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration_s(value: Any) -> float:
    """Seconds from ``90``, ``"90s"``, ``"15m"`` or ``"1h"``. Bare numbers are seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION.match(str(value))
    if not match:
        raise ConfigError(f"not a duration: {value!r} (use 90, 90s, 15m or 1h)")
    return float(match.group(1)) * _UNIT_S[match.group(2)]


def k6_duration(seconds: float) -> str:
    """k6 wants a string; whole seconds are enough for every stage we generate."""
    return f"{int(round(seconds))}s"


def number(mapping: dict, key: str, where: str, *, minimum: float | None = None,
           integer: bool = False) -> float:
    """Fetch a required number with a message that names the file position, not a KeyError."""
    if key not in mapping:
        raise ConfigError(f"{where}: missing `{key}`")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}: expected a number, got {value!r}")
    if integer and int(value) != value:
        raise ConfigError(f"{where}.{key}: expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}.{key}: must be >= {minimum}, got {value!r}")
    return value
