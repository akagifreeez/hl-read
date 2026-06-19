"""hl-read - a key-free, read-only toolkit for Hyperliquid.

Imports only the read side of the Hyperliquid SDK, so it is structurally
incapable of placing orders or moving funds. Use it as a library, a CLI
(``hl-read``), or an MCP server (``hl-read-mcp``).
"""
from .info import HLRead, HLReadError, ResilientStream

__version__ = "0.4.0"
__all__ = ["HLRead", "HLReadError", "ResilientStream", "__version__"]
