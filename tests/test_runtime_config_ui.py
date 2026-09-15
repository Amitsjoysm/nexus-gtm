# tests/test_runtime_config_ui.py
"""The Superadmin Runtime settings tab.

Reported 2026-09-15: an IP could not be typed into the admin allowlist and `apify` could not be
chosen for personalization, because every non-switch setting without options was drawn as a number
box. The server accepted both; the browser refused the text before it was ever sent.

There is no frontend test runner here, so these read the source, the same approach as
`test_plan_picker_ui.py`.
"""
from __future__ import annotations

import pathlib
import re

FRONTEND = pathlib.Path("frontend/src")
TAB = FRONTEND / "pages/admin/RuntimeConfigTab.tsx"
TYPES = FRONTEND / "lib/types.ts"
API = FRONTEND / "lib/api.ts"


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"{path} is missing; was it moved?"
    return path.read_text(encoding="utf-8")


def test_a_number_box_is_only_drawn_for_a_numeric_setting():
    src = _read(TAB)
    assert 'const isNumber = row.kind === "int" || row.kind === "float";' in src
    assert src.count('type="number"') == 1, "a second number input would reintroduce the bug"
    number_at = src.index('type="number"')
    branch = src.rfind("isNumber ?", 0, number_at)
    assert branch != -1 and number_at - branch < 200, "the number input is not behind isNumber"


def test_free_text_gets_a_text_input_with_the_servers_placeholder():
    src = _read(TAB)
    assert 'type={row.key.endsWith("_url") ? "url" : "text"}' in src
    assert "placeholder={row.placeholder || undefined}" in src


def test_options_are_shown_by_their_label():
    src = _read(TAB)
    assert "labels[o] ?? (o || \"(default)\")" in src


def test_the_client_type_carries_the_fields_the_server_sends():
    """A field the server adds and the client type lacks compiles fine and renders nothing."""
    import asyncio

    block = re.search(r"export interface RuntimeSetting \{(.*?)\n\}", _read(TYPES), re.S)
    assert block, "RuntimeSetting not found in types.ts"
    client = set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))

    from nexus.runtime_config import service

    async def one_row():
        async def no_rows():
            return {}

        class _Rows:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def scalars(self, _):
                class _All:
                    def all(self):
                        return []
                return _All()

        original_stored, original_maker = service.stored_overrides, service.get_platform_sessionmaker
        service.stored_overrides = no_rows
        service.get_platform_sessionmaker = lambda: (lambda: _Rows())
        try:
            return (await service.current_values())[0]
        finally:
            service.stored_overrides = original_stored
            service.get_platform_sessionmaker = original_maker

    server = set(asyncio.run(one_row()))
    assert client == server, f"client {sorted(client)} vs server {sorted(server)}"


def test_the_tab_can_be_searched_filtered_and_navigated():
    src = _read(TAB)
    assert 'aria-label="Search settings"' in src
    assert "aria-pressed={changedOnly}" in src
    assert 'aria-label="Settings sections"' in src
    assert 'aria-live="polite"' in src
    # Groups keep the server's order: nothing re-sorts them in the browser.
    assert ".sort(" not in src.split("const groups = useMemo")[1].split("}, [rows")[0]


def test_check_connection_calls_the_verifier_check():
    assert "checkEmailVerifier()" in _read(TAB)
    assert '"/admin/runtime/email-verifier/check"' in _read(API)


def test_the_webhook_panel_is_not_a_card_inside_a_card():
    src = _read(TAB)
    panel = src[src.index("function WebhookPanel"): src.index("function VerifierResult")]
    assert "<Card" not in panel
