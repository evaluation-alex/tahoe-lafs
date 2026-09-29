New ``/private/freshness/v1`` JSON endpoint, and a ``tahoe-mcp`` Model
Context Protocol server that exposes it. The endpoint reports, for a
watched mutable file or directory, what the grid currently says about
it and when the node last found out, so that a program -- or an AI
assistant -- can tell whether a capability is up to date, needs a
re-check, or is about to lose data to an unwritable revision. See
``docs/freshness-and-mcp.rst``.
