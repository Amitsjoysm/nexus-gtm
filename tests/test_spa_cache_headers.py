"""Cache policy for the built SPA when FastAPI serves it directly (no Caddy in front).

Production and staging answer with ``server: uvicorn``, so ``deploy/Caddyfile``'s cache rules never
apply there. Without an explicit ``Cache-Control`` a browser falls back to heuristic freshness
(~10% of the time since ``Last-Modified``) and can reuse an ``index.html`` fetched hours after a
build without revalidating — which keeps loading the previous release's hashed bundles, so a live
deploy looks like "the fix is not in the build".
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import nexus.main as main

IMMUTABLE = "public, max-age=31536000, immutable"
SHELL_HTML = "<!doctype html><html><body><div id=\"root\"></div></body></html>"


@pytest_asyncio.fixture
async def spa(tmp_path, monkeypatch):
    """An app serving a throwaway dist directory instead of nexus/web/dist."""
    (tmp_path / "index.html").write_text(SHELL_HTML, encoding="utf-8")
    (tmp_path / "favicon-32.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1);", encoding="utf-8")

    monkeypatch.setattr(main, "_DIST_DIR", tmp_path)
    app = main.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/accounts/123"])
async def test_shell_always_revalidates(spa, path):
    """index.html — directly or as the fallback for a client route — must be revalidated on every
    load, so the next page load after a deploy picks up the new bundle names."""
    r = await spa.get(path)
    assert r.status_code == 200
    assert r.text == SHELL_HTML
    assert r.headers["Cache-Control"] == "no-cache"
    # The policy rides alongside the security headers, never instead of them.
    assert r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/", "/accounts/123"])
async def test_shell_revalidation_is_a_cheap_304(spa, path):
    first = await spa.get(path)
    etag = first.headers["ETag"]

    r = await spa.get(path, headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    # A 304 carries the same policy as the 200 it stands in for.
    assert r.headers["Cache-Control"] == "no-cache"


async def test_hashed_assets_are_immutable(spa):
    r = await spa.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.text == "console.log(1);"
    assert r.headers["Cache-Control"] == IMMUTABLE
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_root_static_files_are_never_immutable(spa):
    """public/ files (favicons, logos) are copied to the dist root with stable, unhashed names."""
    r = await spa.get("/favicon-32.png")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("path", ["/assets/missing.js", "/api/does-not-exist"])
async def test_asset_and_api_misses_still_404_without_the_shell(spa, path):
    r = await spa.get(path)
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")
    assert "<div id=\"root\">" not in r.text
    # Nothing here is a cacheable file, so the static policy must not leak onto it.
    assert "Cache-Control" not in r.headers


async def test_api_responses_carry_no_static_cache_policy(spa):
    r = await spa.get("/health")
    assert r.status_code == 200
    assert "Cache-Control" not in r.headers


def test_nothing_unhashed_can_land_in_assets():
    """The immutable policy is keyed on the assets/ directory, which is only safe while every file
    Vite writes there has a content hash in its name. These are the two ways that stops being true:
    a public/assets/ folder (copied verbatim, unhashed) or a filename/dir override in the build
    config. If you need either, key the policy on the hash instead of the directory first."""
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    assert not (frontend / "public" / "assets").exists()
    config = (frontend / "vite.config.ts").read_text(encoding="utf-8")
    for override in ("entryFileNames", "chunkFileNames", "assetFileNames", "assetsDir"):
        assert override not in config, f"vite.config.ts sets {override}; see this test's docstring"
