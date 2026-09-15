"""Run a capped, windowed load test against local or staging, and record the result.

    python scripts/load/run.py --profile smoke --env local --data existing
    python scripts/load/run.py --profile load --env staging --yes
    python scripts/load/run.py --profile breakpoint --env staging --duration-scale 0.5 --dry-run

Order of operations. Every step can refuse, and nothing after a refusal runs:

  1. catalogue drift check (generated JSON fresh, both interpreters support every feature)
  2. plan + HARD caps from quality/load/environments.yaml (no flag raises a cap)
  3. run window (outside Indian working hours on staging) and target allowlist / production denylist
  4. confirmation (staging, or any paid-provider budget)
  5. pre-flight: /health, /ready (database), /metrics, readiness latency baseline
  6. test data (synthetic by default) and rate-limited persona logins, platform access check
  7. notice: staging is under load
  8. the tool in Docker, with the canary guard, the worker probe and a hard timeout beside it
  9. cleanup of test data — always, including after a failure or abort
 10. result JSON (docs/quality/load-results/) and the trend table in docs/quality/load.md

Exit codes: 0 ok · 1 SLO breach on a gating (k6) profile · 2 refused before load ·
3 aborted by a guard · 4 the tool itself failed.
"""
from __future__ import annotations

import argparse
import copy
import json
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalogue as catalogue_mod  # noqa: E402
import notify  # noqa: E402
import preflight  # noqa: E402
import probes  # noqa: E402
import results  # noqa: E402
import slo as slo_mod  # noqa: E402
import testdata  # noqa: E402
import tools  # noqa: E402
from common import LOADTEST_DIR, REPO_ROOT, ConfigError, dump_json, rel  # noqa: E402
from safety import SafetyError, check_caps, check_target, window_verdict, zone  # noqa: E402

EXIT_OK, EXIT_SLO, EXIT_REFUSED, EXIT_GUARD, EXIT_TOOL = 0, 1, 2, 3, 4
RUNS_DIR = LOADTEST_DIR / ".runs"


class Refused(RuntimeError):
    pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--profile", required=True, help="smoke|load|soak (k6) or stress|spike|breakpoint|capacity (Gatling)")
    p.add_argument("--tool", choices=["k6", "gatling"], help="optional; must match the profile's tool")
    p.add_argument("--env", required=True, help="an environment from quality/load/environments.yaml")
    p.add_argument("--data", choices=testdata.MODES, default="synth")
    p.add_argument("--synth-tenants", type=int, default=1)
    p.add_argument("--synth-accounts", type=int, default=200)
    p.add_argument("--slo-profile", help="override the SLO file's profile (baseline|strict)")
    p.add_argument("--vus", type=int)
    p.add_argument("--duration-s", type=float)
    p.add_argument("--iterations", type=int)
    p.add_argument("--peak-rate", type=float, help="peak users/sec for open-model profiles")
    p.add_argument("--levels", type=int, help="stairs levels for breakpoint/capacity")
    p.add_argument("--duration-scale", type=float, help="0.01..1: shorten every stage proportionally")
    p.add_argument("--budget", action="append", metavar="PAID_JOURNEY=CALLS",
                   help="enable a paid-provider journey with a max call count (off by default)")
    p.add_argument("--exclude-journey", action="append", default=[])
    p.add_argument("--ignore-window", action="store_true")
    p.add_argument("--reason", help="required with --ignore-window; recorded and announced")
    p.add_argument("--dry-run", action="store_true", help="everything up to the load, then stop")
    rec = p.add_mutually_exclusive_group()
    rec.add_argument("--record", dest="record", action="store_true", default=None)
    rec.add_argument("--no-record", dest="record", action="store_false")
    p.add_argument("--yes", action="store_true", help="confirm a staging or paid run non-interactively")
    return p.parse_args(argv)


def new_run_id(tool: str, profile: str) -> str:
    return f"lt-{datetime.now(timezone.utc):%Y%m%dT%H%MZ}-{tool}-{profile}-{secrets.token_hex(2)}"


def git_info() -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO_ROOT,
                                  timeout=10).stdout.strip()
        except Exception:
            return ""
    return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain"))}


def confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise Refused(f"{prompt} — re-run with --yes to confirm non-interactively")
    answer = input(f"{prompt}\nType 'yes' to continue: ").strip().lower()
    if answer != "yes":
        raise Refused("not confirmed")


def build_plan(cat: dict, args: argparse.Namespace, excluded: set[str]) -> dict:
    working = copy.deepcopy(cat)
    for jid in excluded:
        if jid not in working["journeys"]:
            raise ConfigError(f"--exclude-journey {jid}: not a journey")
        working["journeys"][jid]["weight"] = 0
    overrides = {"vus": args.vus, "duration_s": args.duration_s, "iterations": args.iterations,
                 "peak_users_per_sec": args.peak_rate, "levels": args.levels,
                 "duration_scale": args.duration_scale}
    return catalogue_mod.build_plan(working, args.profile, overrides=overrides,
                                    budget=catalogue_mod.parse_budget(args.budget))


def main(argv: list[str] | None = None) -> int:
    # k6 and Gatling print non-ASCII (✓, ✗, box drawing); a cp1252 Windows console must not be the
    # thing that kills a run halfway through, leaving the container loading staging unsupervised.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        return run(args)
    except (Refused, SafetyError, ConfigError, preflight.PreflightError, testdata.DataError) as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED


def run(args: argparse.Namespace) -> int:
    cat = catalogue_mod.load()
    drift = catalogue_mod.check(cat)
    if drift:
        raise Refused("the catalogue and the tools have drifted:\n  - " + "\n  - ".join(drift))
    if args.profile not in cat["profiles"]:
        raise Refused(f"unknown profile {args.profile!r}")
    profile = cat["profiles"][args.profile]
    if args.tool and args.tool != profile["tool"]:
        raise Refused(f"profile {args.profile} runs on {profile['tool']}: k6 owns smoke/load/soak "
                      "(the release gate), Gatling owns stress/spike/breakpoint/capacity")
    if args.env not in cat["environments"]:
        raise Refused(f"unknown environment {args.env!r}")
    env = cat["environments"][args.env]
    for url in {env["base_url"], env["host_url"]}:
        check_target(args.env, env, cat["forbidden_hosts"], url)

    excluded = set(args.exclude_journey)
    plan = build_plan(cat, args, excluded)
    violations = check_caps(plan["envelope"], env["caps"])
    if violations:
        raise Refused(f"plan exceeds {args.env} hard caps:\n  - " + "\n  - ".join(violations))

    started = datetime.now(timezone.utc)
    allowance = tools.GATLING_SETUP_ALLOWANCE_S if plan["tool"] == "gatling" else 60
    window_ok, window_reason = window_verdict(env["window"], cat["timezone"], started,
                                              plan["envelope"]["duration_s"] + allowance)
    window_override = None
    if not window_ok:
        if not args.ignore_window:
            raise Refused(window_reason)
        if not args.reason:
            raise Refused("--ignore-window needs --reason; it is recorded and announced")
        window_override = args.reason

    run_id = new_run_id(plan["tool"], args.profile)
    tz = zone(cat["timezone"])
    ends_at = started + timedelta(seconds=plan["envelope"]["duration_s"] + allowance)
    print(f"run {run_id}\n  target  {env['base_url']} ({args.env})\n  plan    {plan['tool']} "
          f"{args.profile}: {json.dumps(plan['envelope'])}\n  caps    {json.dumps(env['caps'])}\n"
          f"  window  {window_reason}{' — OVERRIDDEN: ' + window_override if window_override else ''}\n"
          f"  ends by {ends_at.astimezone(tz):%H:%M} {cat['timezone']}")

    if plan["paid"]:
        spend = ", ".join(f"{j}: {p['max_paid_calls']} call(s) to {'/'.join(p['providers'])}"
                          for j, p in plan["paid"].items())
        confirm(f"This run SPENDS MONEY through paid providers — {spend}.", args.yes)
    if env["window"].get("enforce") and not args.dry_run:
        confirm(f"Load {args.env} ({env['base_url']}) for up to "
                f"{plan['envelope']['duration_s'] // 60} min at <= {plan['envelope']['peak_vus']} VUs?",
                args.yes)

    slo = slo_mod.export(args.slo_profile)
    headers = {cat["run_header"]: run_id, "User-Agent": f"nexus-loadtest/runner {run_id}"}
    insecure = env["insecure_tls"]
    pre = preflight.check_health(env["host_url"], headers, insecure=insecure)
    for c in pre["checks"]:
        print(f"  pre-flight {c['name']:<15} {'ok' if c['ok'] else 'FAIL'}  {c['detail']}")
    if not pre["ok"]:
        raise Refused("pre-flight failed: the target is not healthy enough to load")

    data = testdata.prepare(args.data, cat, plan["personas"], run_id, tenants=args.synth_tenants,
                            accounts=args.synth_accounts)
    summary: dict = {}
    execution = None
    canary_summary: dict = {}
    worker_summary: dict = {"status": "not started"}
    notes: list[str] = list(data.notes)
    run_dir = RUNS_DIR / run_id
    try:
        minted = preflight.login_personas(env["host_url"], cat, data.personas, headers, insecure=insecure)
        for persona_id, persona in cat["personas"].items():
            if persona["platform"] and persona_id in plan["personas"]:
                token = next(t["token"] for t in minted if t["persona"] == persona_id)
                ok, why = preflight.platform_access(env["host_url"], token, headers, insecure=insecure)
                if not ok:
                    dropped = {j for j, jd in cat["journeys"].items() if jd["persona"] == persona_id}
                    notes.append(f"excluded {sorted(dropped)}: {why}")
                    print(f"  note: excluding {sorted(dropped)} — {why}")
                    excluded |= dropped
                    plan = build_plan(cat, args, excluded)
        if args.dry_run:
            print(dump_json({"run_id": run_id, "plan": plan, "slo": slo, "data": data.describe(),
                             "preflight": pre, "notes": notes}))
            return EXIT_OK

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "plan.json").write_text(dump_json(plan), encoding="utf-8")
        (run_dir / "slo.json").write_text(dump_json(slo), encoding="utf-8")
        secrets_env = {"LT_TOKENS_JSON": json.dumps([
            {k: t[k] for k in ("persona", "token", "role", "issued_ms")}
            for t in minted if t["persona"] in plan["personas"]
        ])}
        if plan["relogin"]:
            # Passwords reach the container only when the run outlives the tokens.
            secrets_env["LT_PERSONAS_JSON"] = json.dumps(data.personas)
        launcher = tools.k6_launch if plan["tool"] == "k6" else tools.gatling_launch
        launch = launcher(run_id, run_dir, env, plan, base_url=env["base_url"], slo=slo,
                          secrets=secrets_env)

        info = {"run_id": run_id, "env": args.env, "tool": plan["tool"], "profile": args.profile,
                "ends_at": f"{ends_at.astimezone(tz):%H:%M} {cat['timezone']}",
                "peak_vus": plan["envelope"]["peak_vus"], "est_rps": plan["envelope"]["est_peak_rps"],
                "window_override": window_override}
        notices = notify.notice("start", info)

        canary_breach: list[str] = []

        def on_breach(reason: str) -> None:
            canary_breach.append(reason)
            print(f"\nGUARD: {reason} — stopping {launch.container}")
            tools.stop_container(launch.container)

        canary = probes.Canary(env["host_url"], headers, env["guards"], on_breach, insecure=insecure)
        worker_cfg = cat["probes"].get("worker_health", {})
        worker = probes.WorkerProbe(env["worker_probe"], worker_cfg.get("metrics", []),
                                    worker_cfg.get("interval_s", 60))
        canary.start()
        worker.start()
        try:
            execution = tools.execute(launch, run_dir / "tool.log")
        except BaseException:
            # Supervision failed (or Ctrl+C): never leave a container loading the target unwatched.
            tools.stop_container(launch.container, grace_s=10)
            raise
        finally:
            canary_summary = canary.stop()
            worker_summary = worker.stop()
    finally:
        cleanup = data.cleanup()
        if cleanup is not None:
            print(f"  test data cleanup: {json.dumps(cleanup)}")

    ended = datetime.now(timezone.utc)
    log_text = (run_dir / "tool.log").read_text(encoding="utf-8", errors="replace")
    metrics: dict = {}
    tool_error = None
    try:
        if plan["tool"] == "k6":
            metrics = tools.parse_k6(run_dir / "k6-summary.json")
        else:
            metrics = tools.parse_gatling(log_text, tools.latest_gatling_report(run_dir / "gatling"))
            if not metrics["requests"]:
                raise ValueError("the simulation logged no request samples")
    except (OSError, ValueError, KeyError) as exc:
        tool_error = f"could not read {plan['tool']} results: {exc}"

    verdict = results.slo_verdict(metrics, slo) if metrics else {"passed": False, "breaches": [tool_error]}
    guard_reasons = canary_breach + execution.markers["guard"] + execution.markers["k6_abort"]
    guard = {"aborted": bool(guard_reasons) or execution.timed_out,
             "reason": "; ".join(guard_reasons) or ("hard timeout" if execution.timed_out else None),
             "source": ("canary" if canary_breach else "tool" if guard_reasons else
                        "timeout" if execution.timed_out else None),
             "abort_is_result": plan["abort_is_result"], "canary": canary_summary}
    if execution.markers["bootstrap_failed"]:
        tool_error = tool_error or "Gatling bootstrap could not log a persona in: " + "; ".join(
            execution.markers["bootstrap_failed"])

    summary = {
        "schema": results.RESULT_SCHEMA,
        "run_id": run_id,
        "started_at": started.isoformat(timespec="seconds"),
        "ended_at": ended.isoformat(timespec="seconds"),
        "environment": {"name": args.env, "base_url": env["base_url"]},
        "tool": {"name": plan["tool"], "image": tools.K6_IMAGE if plan["tool"] == "k6" else
                 f"{tools.NODE_IMAGE} + @gatling.io/cli 3.15.105"},
        "profile": args.profile,
        "gate": plan["gate"],
        "git": git_info(),
        "plan": {k: plan[k] for k in ("kind", "envelope", "overrides", "journeys", "paid", "relogin",
                                      "think_time_scale")},
        "caps": env["caps"],
        "guards": env["guards"],
        "window": {"enforced": bool(env["window"].get("enforce")), "verdict": window_reason,
                   "override_reason": window_override},
        "slo": {k: slo[k] for k in ("profile", "is_fixture", "source", "api", "routes")},
        "data": {**data.describe(), "cleanup": cleanup},
        "preflight": pre,
        "notices": notices,
        "metrics": metrics,
        "slo_verdict": verdict,
        "guard": guard,
        "worker_probe": worker_summary,
        "exit": {"tool_returncode": execution.returncode, "timed_out": execution.timed_out,
                 "tool_error": tool_error, "wall_s": execution.duration_s},
        "artifacts": {"run_dir": rel(run_dir)},
        "notes": notes,
    }
    (run_dir / "summary.json").write_text(dump_json(summary), encoding="utf-8")

    record = args.record if args.record is not None else args.env == "staging"
    if record:
        path = results.write_result(summary)
        results.update_trend()
        print(f"recorded {rel(path)} and updated the trend table in docs/quality/load.md")

    if tool_error:
        code, outcome = EXIT_TOOL, f"tool error: {tool_error}"
    elif guard["aborted"] and not plan["abort_is_result"]:
        code, outcome = EXIT_GUARD, f"ABORTED by guard: {guard['reason']}"
    elif plan["gate"] and not verdict["passed"]:
        code, outcome = EXIT_SLO, "SLO breached: " + "; ".join(verdict["breaches"])
    else:
        code = EXIT_OK
        outcome = ("SLO met" if verdict["passed"] else "SLO not met (exploratory profile): "
                   + "; ".join(verdict["breaches"]))
        if guard["aborted"]:
            outcome += f"; guard stopped the run at the knee: {guard['reason']}"
    notify.notice("end", {**info, "outcome": outcome})
    print(f"\n{outcome}\nsummary: {rel(run_dir / 'summary.json')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
