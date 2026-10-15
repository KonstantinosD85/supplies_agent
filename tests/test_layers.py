"""Layering rules: which of our packages each layer may import.

Locally every dependency group is installed, so a cross-layer import would only fail
inside a service's container. This test catches it first.

  api         -> nothing else (the mock stands alone)
  mcp_server  -> nothing else (reaches the mock over HTTP)
  memory      -> nothing else (swappable for AgentCore Memory)
  agent       -> memory       (reaches the MCP server over MCP)
  all layers  -> logs
"""

import ast
from pathlib import Path

import pytest

import supplies_agent

PACKAGE_ROOT = Path(supplies_agent.__file__).parent

ALLOWED = {
    "api": {"api", "logs"},
    "mcp_server": {"mcp_server", "logs"},
    "memory": {"memory", "logs"},
    "agent": {"agent", "memory", "logs"},
}


def imported_layers(path: Path) -> set[str]:
    """Our top-level packages (e.g. 'memory') imported by one module."""
    layers = set()
    for node in ast.walk(ast.parse(path.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            parts = name.split(".")
            if parts[0] == "supplies_agent" and len(parts) > 1:
                layers.add(parts[1])
    return layers


def test_every_layer_has_a_rule():
    packages = {p.parent.name for p in PACKAGE_ROOT.glob("*/__init__.py")}
    assert packages == set(ALLOWED)


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports_only_what_it_is_allowed(layer):
    violations = {
        str(path.relative_to(PACKAGE_ROOT)): sorted(imported_layers(path) - ALLOWED[layer])
        for path in (PACKAGE_ROOT / layer).rglob("*.py")
        if imported_layers(path) - ALLOWED[layer]
    }
    assert violations == {}


def test_logs_imports_no_layer():
    assert imported_layers(PACKAGE_ROOT / "logs.py") == set()
