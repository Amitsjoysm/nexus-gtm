"""Test data for a run: synthetic tenants (default), existing personas, or an anonymised copy.

``synth`` (default) — the Quality foundation's ``scripts/testdata/synth.py`` creates tagged tenants
before the run and deletes them after, whatever happened in between. Adapter contract (the one
function to adjust if the merged CLI differs):

    synth.py --prefix qa-synth-lt-<run> --tenants N --accounts M --json
        -> last stdout line: {"tenants": [...], "personas": [{"role", "email", "password", "tenant"}]}
    synth.py --cleanup --prefix qa-synth-lt-<run> --json

On staging it has to run inside the VNet (Postgres has no public access), so the command is
configurable: ``LT_SYNTH_COMMAND="az containerapp exec -n gtm-staging-app -g gtm-staging-rg
--command 'python scripts/testdata/synth.py'"`` or ``docker exec -i nexus-gtm-app-1 python ...``.

``existing`` — personas supplied by the environment (LT_<PERSONA>_EMAIL / _PASSWORD, or
LT_PERSONAS_FILE outside the repo). Nothing is created or deleted; results say so.

``anon`` — opt-in: an anonymised production copy restored into staging (tenants ``qa-anon-``),
for realistic skew. Requires LT_ANON_CONFIRM=yes and LT_ANON_TENANT.

Every persona email must end in ``@qa.example.com`` for synth; every tenant must carry its prefix.
A generator that returned anything else is refused before a single request is sent.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPO_ROOT  # noqa: E402

MODES = ("synth", "existing", "anon")
SYNTH_SCRIPT = REPO_ROOT / "scripts" / "testdata" / "synth.py"
SYNTH_PREFIX = "qa-synth-"
ANON_PREFIX = "qa-anon-"
QA_DOMAIN = "@qa.example.com"


class DataError(RuntimeError):
    """Test data could not be prepared safely. The run does not start."""


@dataclass
class DataSet:
    mode: str
    personas: list[dict]
    tenant_prefix: str | None = None
    notes: list[str] = field(default_factory=list)
    cleanup_fn: Callable[[], dict] | None = None

    def cleanup(self) -> dict | None:
        return self.cleanup_fn() if self.cleanup_fn else None

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "tenant_prefix": self.tenant_prefix,
            "personas": dict(Counter(p["persona"] for p in self.personas)),
            "notes": list(self.notes),
        }


def _persona_for_role(catalogue: dict, role: str) -> str | None:
    for pid, persona in catalogue["personas"].items():
        if persona["role"] == role:
            return pid
    return None


def personas_from_env(catalogue: dict, persona_ids: list[str],
                      environ: dict | os._Environ = os.environ) -> list[dict]:
    path = environ.get("LT_PERSONAS_FILE")
    if path:
        resolved = Path(path).resolve()
        if REPO_ROOT in resolved.parents:
            raise DataError("LT_PERSONAS_FILE is inside the repository; keep credentials outside it")
        data = json.loads(resolved.read_text(encoding="utf-8"))
        pool = data.get("personas", data) if isinstance(data, dict) else data
        creds = [p for p in pool if p.get("persona") in persona_ids]
        missing = sorted(set(persona_ids) - {p["persona"] for p in creds})
        if missing:
            raise DataError(f"LT_PERSONAS_FILE has no credential for persona(s) {missing}")
        return creds
    creds, missing = [], []
    for pid in persona_ids:
        prefix = catalogue["personas"][pid]["env_prefix"]
        email, password = environ.get(f"{prefix}_EMAIL"), environ.get(f"{prefix}_PASSWORD")
        if email and password:
            creds.append({"persona": pid, "email": email, "password": password})
        else:
            missing.append(f"{prefix}_EMAIL/{prefix}_PASSWORD")
    if missing:
        raise DataError("persona credentials missing from the environment: " + ", ".join(missing))
    return creds


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise DataError("synthetic data generator printed no JSON result line")


def validate_synth(result: dict, prefix: str) -> None:
    tenants = result.get("tenants") or []
    if not tenants or any(not str(t).startswith(prefix) for t in tenants):
        raise DataError(f"generator returned tenants outside prefix {prefix!r}: {tenants}")
    for p in result.get("personas") or []:
        if not str(p.get("email", "")).endswith(QA_DOMAIN):
            raise DataError(f"generator returned a persona outside {QA_DOMAIN}; refusing to use it")
        if p.get("tenant") and not str(p["tenant"]).startswith(prefix):
            raise DataError(f"generator returned a persona in tenant {p['tenant']!r} outside {prefix!r}")


def prepare(mode: str, catalogue: dict, persona_ids: list[str], run_id: str, *,
            tenants: int = 1, accounts: int = 200,
            environ: dict | os._Environ = os.environ,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> DataSet:
    if mode not in MODES:
        raise DataError(f"--data {mode!r}: expected one of {MODES}")

    if mode == "existing":
        return DataSet("existing", personas_from_env(catalogue, persona_ids, environ), notes=[
            "existing personas: nothing was created or cleaned up; results reflect whatever data "
            "those tenants hold, not the synthetic distribution",
        ])

    if mode == "anon":
        if environ.get("LT_ANON_CONFIRM") != "yes":
            raise DataError("--data anon is opt-in: set LT_ANON_CONFIRM=yes after restoring the "
                            "anonymised copy (scripts/testdata/anonymise.py) into staging")
        tenant = environ.get("LT_ANON_TENANT", "")
        if not tenant.startswith(ANON_PREFIX):
            raise DataError(f"LT_ANON_TENANT must name a {ANON_PREFIX}* tenant, got {tenant!r}")
        return DataSet("anon", personas_from_env(catalogue, persona_ids, environ), tenant,
                       notes=["anonymised production copy: realistic skew, not cleaned up"])

    command = environ.get("LT_SYNTH_COMMAND")
    if not command and not SYNTH_SCRIPT.exists():
        raise DataError(
            "scripts/testdata/synth.py has not merged yet (Quality foundation task). Run with "
            "--data existing and LT_<PERSONA>_EMAIL/_PASSWORD, or set LT_SYNTH_COMMAND."
        )
    base = shlex.split(command) if command else [sys.executable, str(SYNTH_SCRIPT)]
    short = run_id.rsplit("-", 1)[-1]
    prefix = f"{SYNTH_PREFIX}lt-{short}"
    proc = runner(base + ["--prefix", prefix, "--tenants", str(tenants), "--accounts", str(accounts),
                          "--json"], capture_output=True, text=True, timeout=1800, cwd=REPO_ROOT)
    if proc.returncode != 0:
        raise DataError(f"synthetic data generation failed (exit {proc.returncode}): "
                        f"{(proc.stderr or '')[-500:]}")
    result = _last_json(proc.stdout)
    validate_synth(result, prefix)
    personas = []
    for p in result.get("personas") or []:
        pid = _persona_for_role(catalogue, p.get("role", ""))
        if pid in persona_ids:
            personas.append({"persona": pid, "email": p["email"], "password": p["password"]})
    missing = sorted(set(persona_ids) - {p["persona"] for p in personas})
    if missing:
        raise DataError(f"synthetic data has no persona for {missing}")

    def cleanup() -> dict:
        done = runner(base + ["--cleanup", "--prefix", prefix, "--json"], capture_output=True,
                      text=True, timeout=1800, cwd=REPO_ROOT)
        detail: dict = {"exit": done.returncode, "prefix": prefix}
        try:
            detail["result"] = _last_json(done.stdout)
        except DataError:
            detail["stderr_tail"] = (done.stderr or "")[-300:]
        return detail

    return DataSet("synth", personas, prefix,
                   notes=[f"{len(result.get('tenants') or [])} synthetic tenant(s), {accounts} accounts each"],
                   cleanup_fn=cleanup)
