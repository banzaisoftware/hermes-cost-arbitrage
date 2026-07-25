"""Hermes cost-arbitrage dashboard plugin package.

This directory is loaded by the host as a uniquely-named package
(``hermes_cost_arbitrage_dashboard``) rather than as bare top-level modules,
so its sibling modules (``cost_engine``, ``pricing``, ``store``,
``plugin_config``) can never collide with a same-named module owned by
another plugin or by the host itself. See ``plugin_api.py`` for the
bootstrap that sets this up — it is the one module the host loads directly
by file path, so it cannot rely on relative imports itself.
"""
