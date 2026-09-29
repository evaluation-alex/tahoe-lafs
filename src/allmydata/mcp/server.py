"""
An MCP (Model Context Protocol) server over stdio, backed by the
Tahoe-LAFS freshness endpoint.

The Model Context Protocol is JSON-RPC 2.0 with newline-delimited
messages, so implementing a server for it is small enough to do here
without pulling in a dependency that the rest of Tahoe-LAFS does not
already have.

What this server deliberately does *not* do is decide anything on the
model's behalf.  Every tool is a thin, faithful wrapper around one call
to ``/private/freshness/v1``, and the tool descriptions carry the
context that would otherwise live in a prompt: what "fresh" means, what
the statuses are, and which operations cost a round trip to the grid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from allmydata import __version__

from .client import (
    CHECK_TIMEOUT,
    DEFAULT_ENDPOINT,
    DEFAULT_TIMEOUT,
    FreshnessClient,
    FreshnessError,
)

SERVER_NAME = "tahoe-lafs-freshness"

#: The protocol revision this server implements by default.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: Revisions we will also speak if the client asks for one of them.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


SCHEMA_DOCUMENTATION = """\
# Tahoe-LAFS freshness reports

`/private/freshness/v1` reports, for one capability at a time, what the
grid currently says about it and when this node last found out.

## status

Every report and every watch-list summary carries exactly one of these:

* `fresh` -- the report is recent and the object is in good shape.
* `stale` -- something about the report or the object needs attention.
  The `stale_reasons` list says what, using these tokens:
  * `never-checked` -- we have never asked the grid about this.
  * `report-is-older-than-stale-after` -- the report predates the
    `stale_after` you asked for, so somebody else may have published
    since.
  * `no-recoverable-version` -- not enough shares to reconstruct the
    object at all.
  * `multiple-recoverable-versions-with-same-seqnum` -- a fork exists;
    a write would lose one of the branches.
  * `newer-version-is-not-recoverable` -- there is evidence of a newer
    revision that we cannot reconstruct. This is the one to treat as
    urgent: a write here would destroy data.
  * `corrupt-shares` -- at least one share failed verification.
  * `unhealthy` -- a check ran and reported the object as unhealthy.
* `unknown` -- never checked, so nothing is known.
* `untracked` -- this node is not watching that capability, so nothing
  is known about it.

Immutable data is content-addressed, so a report for it has no stale
reasons unless a check found corrupt shares. That is not an oversight:
there is no newer revision of a CHK file for anybody to publish.

## Cost

`overview`, `report`, `watch` and `unwatch` are cheap: they read a
cached JSON file. `refresh`, `check` and `children` talk to storage
servers and can take seconds to minutes. Prefer reading first and
acting second.

## Mutating operations

`check` with `repair` set is the only tool that changes anything on the
grid. It re-uploads shares. It needs a writable capability; on a
read-only capability it will report what it found and change nothing.
"""


def _object_schema(properties, required=None):
    """
    Build a JSON Schema for a tool that takes named arguments.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": False,
    }


STALE_AFTER = {
    "type": "number",
    "minimum": 0,
    "description": (
        "How many seconds a report may be old before it counts as stale. "
        "Defaults to the node's own setting (one hour)."
    ),
}

CAP = {
    "type": "string",
    "description": (
        "A Tahoe-LAFS capability (URI:SSK:, URI:MDMF:, or a directory "
        "cap). Watch it first with tahoe_watch_capability if you have "
        "not already."
    ),
}


class ToolError(Exception):
    """
    A tool was called with something we cannot act on.
    """
    def __init__(self, message, kind="invalid_arguments"):
        super().__init__(message)
        self.kind = kind


class MCPTool:
    """
    One MCP tool: its advertised shape and the code behind it.
    """
    def __init__(self, name, description, schema, handler, mutating=False):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler
        self.mutating = mutating

    def describe(self):
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": not self.mutating,
                "destructiveHint": self.mutating,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        }

    def call(self, client, arguments):
        return self.handler(client, arguments or {})


def _need_str(arguments, key):
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError("the {!r} argument is required".format(key))
    return value


def _optional_str(arguments, key):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("the {!r} argument must be a string".format(key))
    return value


def _optional_number(arguments, key):
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("the {!r} argument must be a number".format(key))
    return value


def _optional_int(arguments, key):
    value = _optional_number(arguments, key)
    if value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        raise ToolError("the {!r} argument must be a whole number".format(key))
    return int(value)


def _optional_bool(arguments, key, default=False):
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolError("the {!r} argument must be true or false".format(key))
    return value


def _overview_tool():
    schema = _object_schema({"stale-after": STALE_AFTER})
    return MCPTool(
        "tahoe_freshness_overview",
        "List every capability this Tahoe-LAFS node is watching for "
        "freshness, each with a status of fresh, stale, or unknown and "
        "the reasons behind it. This reads a local cache and never talks "
        "to storage servers. Start here to find out what is being "
        "tracked, then read a specific capability with "
        "tahoe_capability_freshness. See the "
        "tahoe://freshness/report-format resource for what the statuses "
        "mean.",
        schema,
        lambda client, arguments: client.overview(
            stale_after=_optional_number(arguments, "stale-after"),
        ),
    )


def _watch_tool():
    schema = _object_schema(
        {
            "cap": CAP,
            "label": {
                "type": "string",
                "description": "An optional human-readable name to "
                               "remember this capability by.",
            },
        },
        required=["cap"],
    )
    return MCPTool(
        "tahoe_watch_capability",
        "Start tracking the freshness of a capability. Reporting on a "
        "capability requires tracking it first. Cheap: it does not "
        "contact the grid.",
        schema,
        lambda client, arguments: client.track(
            _need_str(arguments, "cap"),
            label=_optional_str(arguments, "label"),
        ),
        mutating=True,
    )


def _unwatch_tool():
    schema = _object_schema({"cap": CAP}, required=["cap"])
    return MCPTool(
        "tahoe_unwatch_capability",
        "Stop tracking the freshness of a capability and discard its "
        "cached report. Does not affect the data itself.",
        schema,
        lambda client, arguments: client.untrack(_need_str(arguments, "cap")),
        mutating=True,
    )


def _report_tool():
    schema = _object_schema(
        {"cap": CAP, "stale-after": STALE_AFTER},
        required=["cap"],
    )
    return MCPTool(
        "tahoe_capability_freshness",
        "Return the cached freshness report for a tracked capability: "
        "every version the node last saw on the grid, with sequence "
        "numbers, share counts, and the servers holding them. Cheap: it "
        "never contacts the grid, so it cannot tell you about a revision "
        "published since the report was taken. Use "
        "tahoe_refresh_capability for that.",
        schema,
        lambda client, arguments: client.report(
            _need_str(arguments, "cap"),
            stale_after=_optional_number(arguments, "stale-after"),
        ),
    )


def _refresh_tool():
    schema = _object_schema(
        {"cap": CAP, "stale-after": STALE_AFTER},
        required=["cap"],
    )
    return MCPTool(
        "tahoe_refresh_capability",
        "Ask the storage servers what versions of a capability exist "
        "right now, and record the answer as this node's freshest view. "
        "Talks to every server that could be holding a share, so it "
        "costs a round trip and can take a while. Does not download file "
        "contents and does not modify anything. Use this before "
        "concluding that a mutable file or directory is up to date.",
        schema,
        lambda client, arguments: client.refresh(
            _need_str(arguments, "cap"),
            stale_after=_optional_number(arguments, "stale-after"),
        ),
    )


def _check_tool():
    schema = _object_schema(
        {
            "cap": CAP,
            "verify": {
                "type": "boolean",
                "description": "Download every share and validate every "
                               "bit. Expensive in bandwidth; the only "
                               "way to detect a corrupted share.",
            },
            "add-lease": {
                "type": "boolean",
                "description": "Renew the lease on each share so the "
                               "storage servers do not delete it.",
            },
            "repair": {
                "type": "boolean",
                "description": "Re-upload missing or corrupt shares. "
                               "This modifies data on the grid, and "
                               "needs a writable capability.",
            },
            "stale-after": STALE_AFTER,
        },
        required=["cap"],
    )
    return MCPTool(
        "tahoe_check_capability",
        "Health-check a capability: how many shares exist out of how many "
        "are required, whether any are corrupt, and whether the object "
        "is still healthy. Set verify to download and validate every "
        "share (expensive), add-lease to renew the shares' leases, and "
        "repair to re-upload what is missing -- repair is the only "
        "option here that changes anything on the grid.",
        schema,
        lambda client, arguments: client.check(
            _need_str(arguments, "cap"),
            verify=_optional_bool(arguments, "verify"),
            add_lease=_optional_bool(arguments, "add-lease"),
            repair=_optional_bool(arguments, "repair"),
            stale_after=_optional_number(arguments, "stale-after"),
        ),
        mutating=True,
    )


def _children_tool():
    schema = _object_schema(
        {
            "cap": CAP,
            "refresh": {
                "type": "boolean",
                "description": "Also re-check each mutable child's "
                               "freshness against the grid. Off by "
                               "default because it costs a round trip "
                               "per child.",
            },
            "limit": {
                "type": "integer",
                "minimum": 0,
                "description": "How many children to describe. Defaults "
                               "to 25.",
            },
            "stale-after": STALE_AFTER,
        },
        required=["cap"],
    )
    return MCPTool(
        "tahoe_directory_children",
        "List a directory's children with what is known about each "
        "child's freshness. Reading the directory itself already costs a "
        "download of the latest revision; set refresh to additionally "
        "query the grid about every mutable child.",
        schema,
        lambda client, arguments: client.children(
            _need_str(arguments, "cap"),
            refresh=_optional_bool(arguments, "refresh"),
            limit=_optional_int(arguments, "limit"),
            stale_after=_optional_number(arguments, "stale-after"),
        ),
    )


def default_tools():
    """
    The complete tool list, in the order they are advertised.
    """
    return [
        _overview_tool(),
        _watch_tool(),
        _unwatch_tool(),
        _report_tool(),
        _refresh_tool(),
        _check_tool(),
        _children_tool(),
    ]


class MCPServer:
    """
    A JSON-RPC 2.0 dispatcher that speaks the Model Context Protocol.

    The transport is deliberately dumb: read one JSON message per line,
    write one JSON message per line.  Everything that requires a reply
    goes through :meth:`handle`.

    :ivar client: the ``FreshnessClient`` that tools call.
    :ivar tools: the advertised tools, keyed by name.
    """
    def __init__(self, client, tools=None):
        self.client = client
        self.tools = {
            tool.name: tool
            for tool in (default_tools() if tools is None else tools)
        }
        self.protocol_version = DEFAULT_PROTOCOL_VERSION
        self.initialized = False

    #
    # Dispatch
    #

    def handle(self, message):
        """
        Handle one JSON-RPC message.

        :return: the response message, or ``None`` for notifications.

        :raises ToolError: never; tool problems come back in the result.
        """
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "request must be an object")
        if message.get("jsonrpc") != "2.0":
            return self._error(
                _message_id(message),
                INVALID_REQUEST,
                'the "jsonrpc" member must be exactly "2.0"',
            )
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(
                _message_id(message),
                INVALID_REQUEST,
                'the "method" member must be a string',
            )
        request_id = message.get("id")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(request_id, INVALID_PARAMS, "params must be an object")

        try:
            handler = getattr(self, "_rpc_" + method.replace("/", "_"), None)
            if handler is None:
                if request_id is None:
                    # An unknown notification is not an error: we simply
                    # have nothing to do about it.
                    return None
                return self._error(
                    request_id,
                    METHOD_NOT_FOUND,
                    "unknown method: {}".format(method),
                )
            result = handler(params)
        except ToolError as e:
            if request_id is None:
                return None
            return self._error(request_id, INVALID_PARAMS, str(e))
        except Exception as e:
            if request_id is None:
                return None
            return self._error(request_id, INTERNAL_ERROR, str(e))

        if request_id is None:
            # A notification that we handled; nothing to send back.
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    #
    # MCP methods
    #

    def _rpc_initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": str(__version__),
            },
            "instructions": (
                "These tools report how fresh mutable Tahoe-LAFS data is. "
                "Read tahoe_freshness_overview first, then "
                "tahoe_capability_freshness for a specific capability, "
                "then tahoe_refresh_capability to confirm it against the "
                "grid. The tahoe://freshness/report-format resource "
                "explains the statuses."
            ),
        }

    def _rpc_notifications_initialized(self, params):
        self.initialized = True
        return {}

    def _rpc_ping(self, params):
        return {}

    def _rpc_tools_list(self, params):
        return {"tools": [tool.describe() for tool in self.tools.values()]}

    def _rpc_tools_call(self, params):
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _text_result(
                "There is no tool called {!r}. Available tools: {}".format(
                    name, ", ".join(sorted(self.tools)),
                ),
                is_error=True,
            )
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _text_result(
                "The 'arguments' member must be an object.",
                is_error=True,
            )
        try:
            payload = tool.call(self.client, arguments)
        except ToolError as e:
            return _text_result(
                "{} (from tahoe://freshness/report-format: {})".format(e, e.kind),
                is_error=True,
            )
        except FreshnessError as e:
            return _text_result(
                "{}\n\nThe node may be stopped, on a different port, or "
                "need a fresh api_auth_token (it is regenerated every "
                "time the node starts).".format(e),
                is_error=True,
            )
        return _text_result(json.dumps(payload, indent=1, sort_keys=True))

    def _rpc_resources_list(self, params):
        return {
            "resources": [{
                "uri": "tahoe://freshness/report-format",
                "name": "Tahoe-LAFS freshness report format",
                "description": "What the statuses and stale_reasons mean, "
                               "what each operation costs, and which "
                               "operation changes data.",
                "mimeType": "text/markdown",
            }],
        }

    def _rpc_resources_read(self, params):
        uri = params.get("uri")
        if uri != "tahoe://freshness/report-format":
            raise ToolError("no such resource: {!r}".format(uri))
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/markdown",
                "text": SCHEMA_DOCUMENTATION,
            }],
        }


def _message_id(message):
    """
    The id to quote in an error response, if the message had a usable
    one.
    """
    request_id = message.get("id")
    if isinstance(request_id, (str, int, float)) and not isinstance(request_id, bool):
        return request_id
    return None


def _text_result(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


#
# The stdio transport
#

def _read_message(stream):
    """
    Read one newline-delimited JSON message, or ``None`` at end of input.
    """
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        return json.loads(line)


def serve(client, stdin=None, stdout=None, server=None):
    """
    Read requests from ``stdin`` and write responses to ``stdout`` until
    the input ends.

    Nothing but protocol messages may ever reach ``stdout``: that is the
    channel the client is parsing.  Diagnostics go to ``stderr``.
    """
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    server = MCPServer(client) if server is None else server

    while True:
        try:
            message = _read_message(stdin)
        except ValueError:
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": "invalid JSON"},
                },
            )
            continue
        if message is None:
            return 0
        try:
            response = server.handle(message)
        except Exception as e:
            response = {
                "jsonrpc": "2.0",
                "id": _message_id(message) if isinstance(message, dict) else None,
                "error": {"code": INTERNAL_ERROR, "message": str(e)},
            }
        if response is None:
            continue
        _write_message(stdout, response)


def _write_message(stream, message):
    stream.write(json.dumps(message).encode("utf-8") + b"\n")
    stream.flush()


#
# The command line
#

def read_node_directory(directory):
    """
    Pull the endpoint and auth token out of a Tahoe-LAFS node directory.

    :return (str, str): the web API URL and the ``api_auth_token``.

    The token is regenerated every time the node starts, so this is only
    as good as the last time the node ran.
    """
    with open(os.path.join(directory, "node.url"), "r", encoding="utf-8") as f:
        url = f.read().strip()
    with open(
        os.path.join(directory, "private", "api_auth_token"),
        "r",
        encoding="utf-8",
    ) as f:
        token = f.read().strip()
    return url, token


def build_parser():
    """
    Build the ``tahoe-mcp`` argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="tahoe-mcp",
        description="Serve Tahoe-LAFS freshness data over the Model "
                    "Context Protocol on stdio.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("TAHOE_LAPS_MCP_ENDPOINT", DEFAULT_ENDPOINT),
        help="the node's web API root (default: %(default)s, or the "
             "TAHOE_LAPS_MCP_ENDPOINT environment variable)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TAHOE_LAPS_MCP_AUTH_TOKEN"),
        help="the node's api_auth_token (default: the "
             "TAHOE_LAPS_MCP_AUTH_TOKEN environment variable)",
    )
    parser.add_argument(
        "--auth-token-file",
        help="read the api_auth_token from this file, rather than putting "
             "it on the command line where it would land in your shell "
             "history and in ps(1) output",
    )
    parser.add_argument(
        "--node-directory",
        default=os.environ.get("TAHOE_LAPS_MCP_NODE_DIRECTORY"),
        help="read the endpoint and token from this Tahoe-LAFS node "
             "directory instead (default: the "
             "TAHOE_LAPS_MCP_NODE_DIRECTORY environment variable)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds to wait for a request that does not contact "
             "storage servers (default: %(default)s)",
    )
    parser.add_argument(
        "--check-timeout",
        type=float,
        default=CHECK_TIMEOUT,
        help="seconds to wait for an operation that does contact storage "
             "servers (default: %(default)s)",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print this server's tools as JSON and exit, without "
             "speaking the protocol. Useful for checking configuration.",
    )
    return parser


def main(argv=None):
    """
    The ``tahoe-mcp`` entry point.
    """
    options = build_parser().parse_args(argv)

    endpoint = options.endpoint
    token = options.auth_token
    if options.node_directory:
        endpoint, token = read_node_directory(options.node_directory)
    elif options.auth_token_file:
        with open(options.auth_token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        sys.stderr.write(
            "tahoe-mcp: no api_auth_token. Pass --node-directory, "
            "--auth-token, --auth-token-file, or set "
            "TAHOE_LAPS_MCP_AUTH_TOKEN.\n",
        )
        return 2

    client = FreshnessClient(
        endpoint,
        token.strip(),
        timeout=options.timeout,
        check_timeout=options.check_timeout,
    )

    if options.list_tools:
        json.dump(
            {
                tool.name: tool.describe()
                for tool in default_tools()
            },
            sys.stdout,
            indent=1,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 0

    return serve(client)


if __name__ == "__main__":
    sys.exit(main())
