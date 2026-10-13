# nexus/mcp/__init__.py
"""NEXUS as an MCP server: AI assistants (Claude, ChatGPT, Cursor, the model APIs) use NEXUS through
the Model Context Protocol. Spec: docs/superpowers/specs/2026-10-13-mcp-server-design.md.

``protocol``, ``schema`` and ``safety`` know nothing about NEXUS. ``credentials`` turns a bearer into
an ``Actor``; ``gateway`` calls the app's own ``/api`` as that actor; ``tools`` is the catalogue;
``server`` dispatches one JSON-RPC request; ``oauth`` and ``cimd`` are the authorization server.
"""
