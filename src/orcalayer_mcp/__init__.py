"""orcalayer-mcp — Model Context Protocol server for the OrcaLayer API.

A thin wrapper over the ``orcalayer`` Python SDK. Exposes OrcaLayer's
Polymarket whale and market analytics as MCP tools, over stdio for local
clients such as Claude Desktop, or over Streamable HTTP (``--http``) for the
hosted server at https://orcalayer.com/mcp and for self-hosting.
"""

from .server import _MCP_VERSION as __version__  # single source: package metadata
from .server import main, mcp

__all__ = ["main", "mcp", "__version__"]
