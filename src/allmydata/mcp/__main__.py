"""
Allow ``python -m allmydata.mcp`` to start the server.
"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
