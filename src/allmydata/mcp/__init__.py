"""
An MCP server for Tahoe-LAFS.

This package exposes the node's ``/private/freshness/v1`` endpoint as
Model Context Protocol tools, so that an AI assistant can ask how fresh a
mutable file or directory is, and can act on the answer.

The server speaks MCP over stdio and has no dependencies beyond the
standard library, so it can be run straight out of a Tahoe-LAFS
checkout.
"""

from .client import (
    FreshnessClient,
    FreshnessError,
)
from .server import (
    MCPServer,
    main,
)

__all__ = [
    "FreshnessClient",
    "FreshnessError",
    "MCPServer",
    "main",
]
