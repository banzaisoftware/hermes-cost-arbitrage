"""Load ``dashboard/plugin_api.py`` the same way the host does.

The host mounts the plugin by loading ``plugin_api.py`` directly by file path
(the manifest's ``"api"`` entry) rather than through a package import. That
load is what triggers ``plugin_api``'s own bootstrap of its sibling modules
into the ``hermes_cost_arbitrage_dashboard`` package (see
``dashboard/plugin_api.py``). Tests must exercise that same route instead of
a second, parallel import mechanism (e.g. putting ``dashboard/`` on
``sys.path``) — a parallel route is exactly how a bare-name collision risk
in the bootstrap could stay invisible to the test suite.
"""
import importlib.util
import sys
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
_PLUGIN_API_PATH = _DASHBOARD / "plugin_api.py"


def _load_plugin_api():
    name = "plugin_api"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_API_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_PLUGIN_API_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_api()
