"""Prospector: search a third-party B2B database of contacts and companies, and add what you find.

Spec: ``docs/superpowers/specs/2026-10-16-prospect-engine-design.md`` (decisions D1-D25). The
package is a pipeline of single-purpose modules, each importable on its own:

``fields`` (what can be filtered) -> ``filters`` (validated input) -> ``client`` (the vendor call,
never raises) -> ``records`` / ``identity`` / ``masking`` (what came back, who it is, what the browser
may see) -> ``cache`` / ``fetch`` (bought once, shared, sealed) -> ``holdings`` / ``limits`` (what
the workspace already has, how much it may browse) -> ``search`` / ``saved`` (the three tabs) ->
``selection`` / ``pricing`` / ``charges`` / ``writers`` / ``deliver`` / ``export`` (paying for and
taking records) -> ``verify`` and ``hold`` (the vendor email's path to a campaign).
"""
