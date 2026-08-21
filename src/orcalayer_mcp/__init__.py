"""orcalayer-mcp — Model Context Protocol server for the OrcaLayer API.

A thin stdio wrapper over the ``orcalayer`` Python SDK. Exposes OrcaLayer's
Polymarket whale and market analytics as MCP tools for clients such as
Claude Desktop.
"""

from .server import _MCP_VERSION as __version__  # single source: package metadata
from .server import main, mcp

__all__ = ["main", "mcp", "__version__"]
