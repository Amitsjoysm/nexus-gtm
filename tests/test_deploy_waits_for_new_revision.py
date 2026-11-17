"""A deploy reports success only when the NEW revision serves, and the app has time to boot.

Staging, 2026-11-06: v1.13.0's revision was killed by its liveness probe mid-boot on every attempt
(the entrypoint migrates and applies RLS before uvicorn listens, and Azure's default liveness gave
the whole boot about 20 s). The old revision kept serving, the pipeline's wait passed because
"a revision is ready", the worker moved to the new image, and staging validation passed against the
old code. These read the pipeline template and the Terraform (no runner for either here) so the
two fixes cannot quietly go back.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _wait_step() -> str:
    """The wait step's text, read without a YAML parser (none is a test dependency). Steps are
    the `- task:` / `- bash:` items at the template's top level."""
    template = (ROOT / "deploy" / "pipelines" / "deploy-containerapp.yml").read_text(
        encoding="utf-8")
    steps = re.split(r"\n  - (?=task:|bash:)", template.split("\nsteps:\n", 1)[1])
    names = [m.group(1) if (m := re.search(r'displayName:\s*"([^"]+)"', s)) else ""
             for s in steps]
    waits = [i for i, n in enumerate(names) if "Wait for" in n]
    assert len(waits) == 1, "exactly one wait step, between the app and the worker"
    wait_at = waits[0]
    assert any("Update app" in n for n in names[:wait_at])
    assert any("Update worker" in n for n in names[wait_at + 1:])
    return steps[wait_at]


def test_the_wait_names_the_revision_it_waits_for():
    script = _wait_step()
    # The latest revision must carry the image being deployed, be the latest READY one, and be
    # Healthy. "latestReadyRevisionName is set" alone is true of the old revision.
    assert "latestRevisionName" in script and "latestReadyRevisionName" in script
    assert re.search(r'\$REV_IMAGE"? = "?\$IMAGE', script)
    assert re.search(r'\$READY"? = "?\$LATEST', script)
    assert '"Healthy"' in script
    assert "exit 1" in script


def test_the_app_has_a_startup_probe():
    tf = (ROOT / "deploy" / "cloud" / "azure" / "container_apps.tf").read_text(encoding="utf-8")
    app = tf[tf.index('resource "azurerm_container_app" "app"'):
             tf.index('resource "azurerm_container_app" "worker"')]
    probe = re.search(r"startup_probe\s*\{(.*?)\}", app, re.S)
    assert probe, "the app container needs a startup probe"
    body = probe.group(1)
    assert '"/health"' in body and "8000" in body
    interval = int(re.search(r"interval_seconds\s*=\s*(\d+)", body).group(1))
    failures = int(re.search(r"failure_count_threshold\s*=\s*(\d+)", body).group(1))
    assert interval * failures >= 90, "migrations plus RLS plus imports need well over 20 s"
