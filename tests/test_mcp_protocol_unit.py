# tests/test_mcp_protocol_unit.py
"""The pure MCP units: JSON-RPC envelopes and both protocol dialects, tool argument validation, and
the cleaning of text that comes from the public web (spec 2026-10-13, task 2)."""
from __future__ import annotations

import json

import pytest

from nexus.mcp import protocol, safety, schema


# ---- protocol --------------------------------------------------------------------------------

def _body(**kw) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **kw}).encode()


def test_a_batch_is_refused():
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse(b"[]")
    assert e.value.code == protocol.INVALID_REQUEST


def test_unparseable_json_is_a_parse_error():
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse(b"{")
    assert e.value.code == protocol.PARSE_ERROR


def test_a_request_needs_a_method():
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse(_body(id=1))
    assert e.value.code == protocol.INVALID_REQUEST


def test_a_message_without_an_id_is_a_notification():
    req = protocol.parse(_body(method="notifications/initialized"))
    assert req.is_notification and req.id is None


def test_params_must_be_an_object():
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse(_body(id=1, method="tools/list", params=[1, 2]))
    assert e.value.code == protocol.INVALID_REQUEST


def test_meta_version_selects_the_stateless_dialect():
    req = protocol.parse(_body(id=1, method="tools/list",
                               params={"_meta": {protocol.META_VERSION: "2026-07-28"}}))
    assert protocol.dialect(req, None) == ("stateless", "2026-07-28")


def test_the_header_version_selects_the_handshake_dialect():
    req = protocol.parse(_body(id=1, method="tools/list"))
    assert protocol.dialect(req, "2025-06-18") == ("handshake", "2025-06-18")


def test_no_version_at_all_is_the_oldest_handshake_version():
    req = protocol.parse(_body(id=1, method="tools/list"))
    assert protocol.dialect(req, None) == ("handshake", "2025-03-26")


def test_an_unknown_version_lists_what_is_supported():
    req = protocol.parse(_body(id=1, method="tools/list",
                               params={"_meta": {protocol.META_VERSION: "2099-01-01"}}))
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.dialect(req, None)
    assert e.value.code == protocol.UNSUPPORTED_VERSION
    assert "2026-07-28" in e.value.data["supported"]


def test_an_unknown_header_version_is_refused_too():
    req = protocol.parse(_body(id=1, method="tools/list"))
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.dialect(req, "1999-01-01")
    assert e.value.code == protocol.UNSUPPORTED_VERSION


def test_initialize_echoes_a_supported_version_and_otherwise_offers_the_newest():
    assert protocol.negotiate("2025-06-18") == "2025-06-18"
    assert protocol.negotiate("2024-01-01") == protocol.HANDSHAKE_VERSIONS[0]
    assert protocol.negotiate(None) == protocol.HANDSHAKE_VERSIONS[0]


def test_stateless_results_say_they_are_complete_and_who_answered():
    info = {"name": "nexus-gtm", "version": "1"}
    out = protocol.result(7, {"tools": []}, stateless=True, server_info=info)
    assert out["id"] == 7 and out["result"]["resultType"] == "complete"
    assert out["result"]["_meta"][protocol.META_SERVER_INFO] == info
    plain = protocol.result(7, {"tools": []}, stateless=False, server_info=info)
    assert "resultType" not in plain["result"] and "_meta" not in plain["result"]


def test_an_error_envelope():
    out = protocol.error(3, protocol.METHOD_NOT_FOUND, "no such method", {"m": "x"})
    assert out == {"jsonrpc": "2.0", "id": 3,
                   "error": {"code": -32601, "message": "no such method", "data": {"m": "x"}}}


# ---- schema ----------------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
        "kind": {"type": "string", "enum": ["account", "contact"]},
        "name": {"type": "string", "minLength": 1, "maxLength": 10},
        "ids": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "flag": {"type": "boolean"},
        "score": {"type": "number"},
    },
    "required": ["account_id"],
    "additionalProperties": False,
}


def test_defaults_are_applied():
    assert schema.validate(_SCHEMA, {"account_id": "abc"})["limit"] == 5


@pytest.mark.parametrize("args, fragment", [
    ({}, "account_id"),
    ({"account_id": "../x"}, "account_id"),
    ({"account_id": "a", "limit": 0}, "limit"),
    ({"account_id": "a", "limit": "3"}, "limit"),
    ({"account_id": "a", "limit": True}, "limit"),
    ({"account_id": "a", "kind": "deal"}, "kind"),
    ({"account_id": "a", "name": ""}, "name"),
    ({"account_id": "a", "name": "x" * 11}, "name"),
    ({"account_id": "a", "ids": ["1", "2", "3"]}, "ids"),
    ({"account_id": "a", "ids": [1]}, "ids"),
    ({"account_id": "a", "flag": "yes"}, "flag"),
    ({"account_id": "a", "surprise": 1}, "surprise"),
])
def test_bad_arguments_name_the_field(args, fragment):
    with pytest.raises(schema.SchemaError) as e:
        schema.validate(_SCHEMA, args)
    assert fragment in str(e.value)


def test_a_number_accepts_an_integer():
    assert schema.validate(_SCHEMA, {"account_id": "a", "score": 3})["score"] == 3


def test_arguments_must_be_an_object():
    with pytest.raises(schema.SchemaError):
        schema.validate(_SCHEMA, ["account_id"])  # type: ignore[arg-type]


# ---- safety ----------------------------------------------------------------------------------

def test_invisible_and_control_characters_are_removed():
    assert safety.clean("a​b\x07c‮d") == "abcd"


def test_newlines_and_tabs_survive():
    assert safety.clean("line one\n\tline two") == "line one\n\tline two"


def test_a_long_string_is_capped():
    out = safety.clean("x" * 10_000)
    assert len(out) <= safety.MAX_FIELD_CHARS + 1 and out.endswith("…")


def test_clean_walks_nested_structures_and_leaves_numbers():
    out = safety.clean({"a": ["b​", 1, True, None], "c": {"d": 2.5}})
    assert out == {"a": ["b", 1, True, None], "c": {"d": 2.5}}


def test_an_oversized_result_is_trimmed_and_says_so():
    data = {"items": [{"text": "y" * 1000} for _ in range(1000)], "total": 1000}
    out = safety.cap_result(data)
    assert out["truncated"] is True
    assert len(json.dumps(out)) <= safety.MAX_RESULT_BYTES
    assert out["total"] == 1000


def test_a_small_result_is_untouched():
    data = {"items": [1, 2, 3]}
    assert safety.cap_result(data) == data


def test_the_instructions_name_the_untrusted_fields():
    for word in ("data", "instructions", "summary", "title"):
        assert word in safety.INSTRUCTIONS
