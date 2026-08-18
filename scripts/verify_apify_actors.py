#!/usr/bin/env python
"""Check every registered Apify actor: is the key valid, is the actor approved, does it parse?

Three separate failures hide behind "enrichment returns nothing", and they need different fixes:

1. **No key.** Inert by design; `ApifyNotConfigured` is raised rather than returning [].
2. **Key valid, actor not approved.** Several actors demand FULL ACCOUNT ACCESS and 403 with
   ``full-permission-actor-not-approved`` until somebody clicks approve in the Apify console.
   Approval is per ACCOUNT, so rotating keys does nothing. The code used to report this as a rate
   limit, which sent operators to rotate credentials that were never wrong.
3. **Actor runs, parser misses.** The one that cost the most: `phone_finder` returned the correct
   number under ``first_mobile_number`` / ``mobile_numbers``, neither of which was in the key list,
   so a working actor extracted NOTHING — silently, reading as "this person has no phone".

So a green "key is valid" tells you almost nothing. This checks all three, per actor, per key.

    python scripts/verify_apify_actors.py                    # metadata + approval only (free)
    python scripts/verify_apify_actors.py --run              # also RUN the wired actors (costs)

``--run`` is opt-in because each run is billed and the free tiers exhaust quickly.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

# Runnable as `python scripts/verify_apify_actors.py` from the repo root, which does not put the
# root on sys.path the way `python -m` does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Profile used for the live run. Public, and its expected phone is known, so a wrong answer is
# visible rather than merely plausible.
PROBE_PROFILE = "https://www.linkedin.com/in/walterbenvenuto"
PROBE_EXPECTED_PHONE = "+17148033540"


def keys_from_env(env_file: Path) -> list[str]:
    found: list[str] = []
    for name, raw in (os.environ.items() if not env_file.exists() else _file_env(env_file).items()):
        if name == "NEXUS_APIFY_API_KEYS":
            found = [k.strip() for k in raw.split(",") if k.strip()] + found
        elif name == "NEXUS_APIFY_API_KEY" and raw.strip():
            found.append(raw.strip())
    # De-duplicate, preserving order.
    seen, out = set(), []
    for k in found:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _file_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="deploy/.env")
    ap.add_argument("--run", action="store_true", help="actually run the wired actors (billed)")
    args = ap.parse_args()

    import httpx

    from nexus.integrations.apify import ACTORS

    keys = keys_from_env(Path(args.env))
    if not keys:
        print(f"no NEXUS_APIFY_API_KEY[S] in {args.env} or the environment — nothing to check")
        return 2

    # Which actors have a consumer. An unwired actor that works is still doing nothing.
    consumers = {
        "phone_finder": "people/enrich.py",
        "linkedin_profile": "personalization/apify_provider.py",
    }

    exit_code = 0
    async with httpx.AsyncClient(timeout=120) as c:
        for key in keys:
            auth = {"Authorization": f"Bearer {key}"}
            me = await c.get("https://api.apify.com/v2/users/me", headers=auth)
            if me.status_code != 200:
                print(f"key ...{key[-4:]}  INVALID ({me.status_code})\n")
                exit_code = 1
                continue
            data = me.json().get("data") or {}
            print(f"account {data.get('username')}  plan={(data.get('plan') or {}).get('id')}"
                  f"  (key ...{key[-4:]})")
            print(f"  {'actor':<18}{'permission':<22}{'approved':<18}{'consumer'}")
            print("  " + "-" * 76)

            for name, actor_id in ACTORS.items():
                meta = await c.get(f"https://api.apify.com/v2/acts/{actor_id}", headers=auth)
                if meta.status_code != 200:
                    print(f"  {name:<18}{'unreachable':<22}{'-':<18}{consumers.get(name, 'none')}")
                    exit_code = 1
                    continue
                level = (meta.json().get("data") or {}).get("actorPermissionLevel") or "unset"

                approved = "n/a"
                if level == "FULL_PERMISSIONS":
                    # The ONLY reliable test is to ask for a run: metadata never says whether THIS
                    # account has approved it. An empty input makes an approved actor fail on
                    # validation (cheap) instead of doing paid work.
                    probe = await c.post(
                        f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items",
                        json={}, params={"token": key},
                    )
                    kind = ""
                    try:
                        kind = ((probe.json() or {}).get("error") or {}).get("type", "")
                    except Exception:
                        pass
                    blocked = kind == "full-permission-actor-not-approved"
                    approved = "NO - approve it" if blocked else "yes"
                    if blocked and name in consumers:
                        exit_code = 1
                print(f"  {name:<18}{level:<22}{approved:<18}{consumers.get(name, 'none')}")
                if approved.startswith("NO"):
                    print(f"  {'':<18}https://console.apify.com/actors/{actor_id}"
                          f"?approvePermissions=true")
            print()

    if not args.run:
        print("Re-run with --run to actually exercise the wired actors end to end (billed).")
        return exit_code

    # ---- end-to-end, through the product's own parsers -----------------------------------------
    print("running the wired actors through the real extraction code...\n")
    from nexus.contacts.phone import normalise_phone
    from nexus.integrations.apify import get_apify_client
    from nexus.people.enrich import extract_phone
    from nexus.personalization.apify_provider import parse_profile

    client = get_apify_client()

    try:
        items = await client.run_actor("phone_finder", {"linkedin_url": [PROBE_PROFILE]})
        got = extract_phone(items, expect_linkedin_url=PROBE_PROFILE)
        e164 = normalise_phone(got).e164 if got else ""
        ok = e164 == PROBE_EXPECTED_PHONE
        print(f"phone_finder      rows={len(items)}  extracted={got!r}  e164={e164!r}  "
              f"{'PASS' if ok else 'MISMATCH (expected ' + PROBE_EXPECTED_PHONE + ')'}")
        exit_code |= 0 if ok else 1
    except Exception as exc:
        print(f"phone_finder      FAILED: {type(exc).__name__}: {str(exc)[:160]}")
        exit_code = 1

    try:
        items = await client.run_actor("linkedin_profile", {"profileUrls": [PROBE_PROFILE]})
        ins = parse_profile(items, expect_linkedin_url=PROBE_PROFILE)
        # `rows > 0` proves the actor answered; `is_empty` proves the PARSER understood it. They
        # fail separately, and the second is the one that shipped broken for phone_finder.
        print(f"linkedin_profile  rows={len(items)}  headline={ins.headline[:44]!r}  "
              f"posts={len(ins.recent_posts)}  interests={len(ins.interests)}")
        if items and ins.is_empty():
            print("                  *** the actor returned rows and the PARSER found nothing — "
                  "the keys it uses are not the keys we read ***")
            exit_code = 1
    except Exception as exc:
        print(f"linkedin_profile  FAILED: {type(exc).__name__}: {str(exc)[:160]}")
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
