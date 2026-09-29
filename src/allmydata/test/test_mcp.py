"""
Tests for the MCP server and its freshness HTTP client.
"""

from __future__ import annotations

from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from threading import Thread
from urllib.parse import (
    parse_qsl,
    urlsplit,
)
import io
import json
import os
import tempfile

from twisted.trial import unittest

from allmydata.mcp.client import (
    FreshnessClient,
    FreshnessError,
)
from allmydata.mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    MCPServer,
    main,
    read_node_directory,
    serve,
)

CAP = "URI:CHK:abcdefghijklmnopqrstuvwxyz:234567abcdefghij:1234"
MDMF = "URI:MDMF:aaaa:bbbb"


class FakeFreshnessClient:
    """
    Records the calls tools make, so that tests can assert on them.

    The signatures deliberately mirror :class:`FreshnessClient`, so that
    a change to one and not the other shows up here.
    """
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result
        self.error = error

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def overview(self, stale_after=None):
        return self._record("overview", stale_after=stale_after)

    def report(self, cap, stale_after=None):
        return self._record("report", cap=cap, stale_after=stale_after)

    def track(self, cap, label=None):
        return self._record("track", cap=cap, label=label)

    def untrack(self, cap):
        return self._record("untrack", cap=cap)

    def refresh(self, cap, stale_after=None):
        return self._record("refresh", cap=cap, stale_after=stale_after)

    def check(self, cap, verify=None, add_lease=None, repair=None,
              stale_after=None):
        return self._record("check", cap=cap, verify=verify,
                            add_lease=add_lease, repair=repair,
                            stale_after=stale_after)

    def children(self, cap, refresh=None, limit=None, stale_after=None):
        return self._record("children", cap=cap, refresh=refresh,
                            limit=limit, stale_after=stale_after)


def request(request_id, method, params=None):
    """
    Build a JSON-RPC request message.
    """
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


class ProtocolTests(unittest.TestCase):
    """
    The JSON-RPC plumbing.
    """
    def setUp(self):
        self.client = FakeFreshnessClient()
        self.server = MCPServer(self.client)

    def test_initialize(self):
        response = self.server.handle(request(1, "initialize", {
            "protocolVersion": "2025-06-18",
        }))
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "tahoe-lafs-freshness")
        # A model that has just connected needs to be told what to do.
        self.assertIn("tahoe_freshness_overview", result["instructions"])

    def test_initialize_negotiates_an_older_version(self):
        # The spec says: answer with the client's version if we speak it.
        response = self.server.handle(request(1, "initialize", {
            "protocolVersion": "2024-11-05",
        }))
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")

    def test_initialize_falls_back_for_an_unknown_version(self):
        response = self.server.handle(request(1, "initialize", {
            "protocolVersion": "1998-01-01",
        }))
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")

    def test_notification_gets_no_response(self):
        # Even one that does work.
        self.assertIs(
            self.server.handle({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }),
            None,
        )
        self.assertTrue(self.server.initialized)

    def test_unknown_notification_is_silently_dropped(self):
        self.assertIs(
            self.server.handle({
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 4},
            }),
            None,
        )

    def test_unknown_method(self):
        response = self.server.handle(request(7, "tools/nope"))
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], METHOD_NOT_FOUND)

    def test_ping(self):
        self.assertEqual(
            self.server.handle(request(2, "ping"))["result"],
            {},
        )

    def test_bad_jsonrpc_version(self):
        response = self.server.handle({
            "jsonrpc": "1.0", "id": 1, "method": "ping",
        })
        self.assertEqual(response["error"]["code"], INVALID_REQUEST)

    def test_bad_params(self):
        response = self.server.handle(request(1, "tools/list", params=[]))
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)

    def test_not_an_object(self):
        response = self.server.handle(["not", "a", "message"])
        self.assertEqual(response["error"]["code"], INVALID_REQUEST)
        self.assertIsNone(response["id"])


class ToolTests(unittest.TestCase):
    """
    The advertised tools, and the calls behind them.
    """
    def setUp(self):
        self.client = FakeFreshnessClient({"status": "fresh"})
        self.server = MCPServer(self.client)

    def call(self, name, arguments=None):
        return self.server.handle(request(1, "tools/call", {
            "name": name,
            "arguments": arguments or {},
        }))["result"]

    def test_tools_list(self):
        tools = self.server.handle(request(1, "tools/list"))["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(names, {
            "tahoe_freshness_overview",
            "tahoe_watch_capability",
            "tahoe_unwatch_capability",
            "tahoe_capability_freshness",
            "tahoe_refresh_capability",
            "tahoe_check_capability",
            "tahoe_directory_children",
        })

    def test_every_tool_is_fully_described(self):
        # A schema with no description of a required argument is how a
        # model ends up guessing, so require prose everywhere.
        tools = self.server.handle(request(1, "tools/list"))["result"]["tools"]
        for tool in tools:
            self.assertTrue(tool["description"].strip(), tool["name"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            for key in schema["required"]:
                self.assertIn(key, schema["properties"])
                self.assertTrue(
                    schema["properties"][key].get("description"),
                    "{}.{}".format(tool["name"], key),
                )

    def test_annotations_distinguish_read_only_tools(self):
        tools = {
            tool["name"]: tool
            for tool in
            self.server.handle(request(1, "tools/list"))["result"]["tools"]
        }
        self.assertTrue(tools["tahoe_capability_freshness"]
                        ["annotations"]["readOnlyHint"])
        self.assertFalse(tools["tahoe_check_capability"]
                         ["annotations"]["readOnlyHint"])

    def test_overview(self):
        result = self.call("tahoe_freshness_overview", {"stale-after": 60})
        self.assertFalse(result["isError"])
        self.assertEqual(
            self.client.calls,
            [("overview", {"stale_after": 60})],
        )

    def test_overview_defaults(self):
        self.call("tahoe_freshness_overview")
        self.assertEqual(self.client.calls, [("overview", {"stale_after": None})])

    def test_watch(self):
        self.call("tahoe_watch_capability", {"cap": CAP, "label": "notes"})
        self.assertEqual(
            self.client.calls,
            [("track", {"cap": CAP, "label": "notes"})],
        )

    def test_unwatch(self):
        self.call("tahoe_unwatch_capability", {"cap": CAP})
        self.assertEqual(self.client.calls, [("untrack", {"cap": CAP})])

    def test_report(self):
        self.call("tahoe_capability_freshness", {"cap": CAP})
        self.assertEqual(
            self.client.calls,
            [("report", {"cap": CAP, "stale_after": None})],
        )

    def test_refresh(self):
        self.call("tahoe_refresh_capability", {"cap": CAP})
        self.assertEqual(
            self.client.calls,
            [("refresh", {"cap": CAP, "stale_after": None})],
        )

    def test_check_defaults_to_not_changing_anything(self):
        result = self.call("tahoe_check_capability", {"cap": CAP})
        self.assertFalse(result["isError"])
        self.assertEqual(self.client.calls, [("check", {
            "cap": CAP,
            "verify": False,
            "add_lease": False,
            "repair": False,
            "stale_after": None,
        })])

    def test_check_with_repair(self):
        self.call("tahoe_check_capability", {
            "cap": CAP, "verify": True, "repair": True, "add-lease": True,
        })
        self.assertEqual(self.client.calls, [("check", {
            "cap": CAP,
            "verify": True,
            "add_lease": True,
            "repair": True,
            "stale_after": None,
        })])

    def test_children(self):
        self.call("tahoe_directory_children", {
            "cap": CAP, "refresh": True, "limit": 3,
        })
        self.assertEqual(self.client.calls, [("children", {
            "cap": CAP, "refresh": True, "limit": 3, "stale_after": None,
        })])

    def test_result_is_json_text(self):
        result = self.call("tahoe_capability_freshness", {"cap": CAP})
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            {"status": "fresh"},
        )

    def test_missing_required_argument(self):
        result = self.call("tahoe_capability_freshness", {})
        self.assertTrue(result["isError"])
        self.assertIn("cap", result["content"][0]["text"])
        self.assertEqual(self.client.calls, [])

    def test_wrongly_typed_argument(self):
        result = self.call("tahoe_capability_freshness", {"cap": 17})
        self.assertTrue(result["isError"])
        self.assertEqual(self.client.calls, [])

    def test_unknown_tool(self):
        result = self.call("tahoe_teleport_capability", {"cap": CAP})
        self.assertTrue(result["isError"])
        # The message should be actionable, so it names what is available.
        self.assertIn("tahoe_capability_freshness",
                      result["content"][0]["text"])

    def test_node_error_becomes_a_tool_error(self):
        # A tool-level error keeps the conversation going, which a
        # JSON-RPC error would not.
        self.client.error = FreshnessError(
            "could not reach the Tahoe-LAFS node",
        )
        result = self.call("tahoe_freshness_overview")
        self.assertTrue(result["isError"])
        self.assertIn("could not reach", result["content"][0]["text"])
        self.assertIn("api_auth_token", result["content"][0]["text"])


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer(FakeFreshnessClient())

    def test_list(self):
        resources = self.server.handle(
            request(1, "resources/list"),
        )["result"]["resources"]
        self.assertEqual(
            [resource["uri"] for resource in resources],
            ["tahoe://freshness/report-format"],
        )

    def test_read(self):
        contents = self.server.handle(request(1, "resources/read", {
            "uri": "tahoe://freshness/report-format",
        }))["result"]["contents"]
        text = contents[0]["text"]
        # The statuses are the part a model cannot guess.
        self.assertIn("newer-version-is-not-recoverable", text)
        self.assertIn("untracked", text)

    def test_read_unknown(self):
        response = self.server.handle(request(1, "resources/read", {
            "uri": "tahoe://freshness/nope",
        }))
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)


class TransportTests(unittest.TestCase):
    """
    The newline-delimited stdio loop.
    """
    def run_messages(self, messages, client=None):
        """
        Feed newline-delimited messages through ``serve``.
        """
        stdin = io.BytesIO(("\n".join(messages) + "\n").encode("utf-8"))
        stdout = _Sink()
        serve(client or FakeFreshnessClient(), stdin, stdout)
        return [json.loads(line) for line in stdout.lines()]

    def test_responses_are_newline_delimited_json(self):
        responses = self.run_messages([
            json.dumps(request(1, "initialize", {"protocolVersion": "2025-06-18"})),
            json.dumps(request(2, "tools/list")),
        ])
        self.assertEqual([r["id"] for r in responses], [1, 2])

    def test_notifications_produce_no_output(self):
        responses = self.run_messages([
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(request(1, "ping")),
        ])
        self.assertEqual([r["id"] for r in responses], [1])

    def test_blank_lines_are_skipped(self):
        responses = self.run_messages(["", "   ", json.dumps(request(1, "ping"))])
        self.assertEqual(len(responses), 1)

    def test_bad_json_is_reported_and_the_loop_survives(self):
        responses = self.run_messages([
            "{not json",
            json.dumps(request(1, "ping")),
        ])
        self.assertEqual(responses[0]["error"]["code"], PARSE_ERROR)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1]["id"], 1)

    def test_input_ending_without_a_newline(self):
        stdin = io.BytesIO(json.dumps(request(1, "ping")).encode("utf-8"))
        stdout = _Sink()
        serve(FakeFreshnessClient(), stdin, stdout)
        self.assertEqual(
            [json.loads(line)["id"] for line in stdout.lines()],
            [1],
        )


class _Sink:
    """
    A writable file-like object that remembers whole lines.
    """
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        pass

    def lines(self):
        return [
            chunk.decode("utf-8").rstrip("\n")
            for chunk in self.chunks
        ]


class _Handler(BaseHTTPRequestHandler):
    """
    Records the requests it receives and answers with a canned payload.
    """
    def _record(self, method, body=None):
        self.server.requests.append({
            "method": method,
            "path": self.path,
            "body": body,
            "authorization": self.headers.get("Authorization"),
        })

    def _respond(self):
        status = self.server.status
        if status == 200:
            body = self.server.body
            if body is None:
                body = json.dumps(self.server.payload).encode("utf-8")
        else:
            body = b"the answer is no"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record("GET")
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record("POST", self.rfile.read(length).decode("utf-8"))
        self._respond()

    def log_message(self, *args):
        # Keep the test output clean.
        pass


class HTTPClientTests(unittest.TestCase):
    """
    The client, against a real HTTP server, so that the query strings,
    form encodings and auth header are all actually exercised.
    """
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.payload = {"ok": True}
        self.httpd.body = None
        self.httpd.status = 200
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:{}/".format(
            self.httpd.server_address[1],
        )
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)
        self.client = FreshnessClient(self.endpoint, "sekrit")

    def query_of(self, request):
        return dict(parse_qsl(urlsplit(request["path"]).query))

    def form_of(self, request):
        return dict(parse_qsl(request["body"]))

    def last(self):
        return self.httpd.requests[-1]

    def test_endpoint_path_and_auth_header(self):
        self.client.overview()
        request = self.last()
        self.assertEqual(
            urlsplit(request["path"]).path,
            "/private/freshness/v1",
        )
        self.assertEqual(request["authorization"], "tahoe-lafs sekrit")

    def test_endpoint_with_a_path_prefix(self):
        client = FreshnessClient(
            "http://127.0.0.1:{}/some/prefix".format(
                self.httpd.server_address[1],
            ),
            "sekrit",
        )
        client.overview()
        self.assertEqual(
            urlsplit(self.last()["path"]).path,
            "/some/prefix/private/freshness/v1",
        )

    def test_read_arguments_become_query_parameters(self):
        self.client.report(CAP, stale_after=90)
        request = self.last()
        self.assertEqual(request["method"], "GET")
        self.assertEqual(self.query_of(request), {
            "cap": CAP, "stale-after": "90",
        })

    def test_write_arguments_become_a_form(self):
        self.client.check(CAP, verify=True, repair=True, add_lease=False)
        request = self.last()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(self.form_of(request), {
            "t": "check",
            "cap": CAP,
            "verify": "true",
            "add-lease": "false",
            "repair": "true",
        })

    def test_omitted_optional_arguments_are_left_out(self):
        # The endpoint distinguishes "not given" from "false", because
        # absent means "keep the stored setting" and false means "no".
        self.client.children(CAP)
        self.assertEqual(self.form_of(self.last()), {
            "t": "children", "cap": CAP,
        })

    def test_capabilities_survive_the_round_trip(self):
        # A capability has slashes, colons and plus signs in it.
        weird = "URI:SSK:+/abc:def:1234%20"
        self.client.track(weird, label="a label & an = sign")
        self.assertEqual(self.form_of(self.last()), {
            "t": "track", "cap": weird, "label": "a label & an = sign",
        })

    def test_node_errors_are_reported(self):
        self.httpd.status = 404
        e = self.assertRaises(FreshnessError, self.client.overview)
        self.assertEqual(e.status, 404)
        self.assertIn("the answer is no", str(e))

    def test_non_json_is_reported(self):
        # A proxy that answers with an HTML error page is exactly the
        # case where a bare json.loads() would produce a baffling error.
        self.httpd.body = b"<html>not the node</html>"
        e = self.assertRaises(FreshnessError, self.client.overview)
        self.assertIn("not JSON", str(e))

    def test_unreachable_node_is_reported(self):
        # Port 1 on loopback is not listening.
        client = FreshnessClient("http://127.0.0.1:1/", "sekrit")
        e = self.assertRaises(FreshnessError, client.overview)
        self.assertIn("could not reach", str(e))

    def test_bad_endpoint_is_rejected_up_front(self):
        self.assertRaises(ValueError, FreshnessClient, "localhost:8888", "x")
        self.assertRaises(ValueError, FreshnessClient, "http://x/", "")


class CommandLineTests(unittest.TestCase):
    def test_read_node_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "private"))
            with open(os.path.join(directory, "node.url"), "w") as f:
                f.write("http://127.0.0.1:8888/\n")
            with open(
                os.path.join(directory, "private", "api_auth_token"), "w",
            ) as f:
                f.write("sekrit\n")
            self.assertEqual(
                read_node_directory(directory),
                ("http://127.0.0.1:8888/", "sekrit"),
            )

    def test_no_token_is_a_usage_error(self):
        # A token is required; running without one would mean every tool
        # call fails, which is much harder to debug than a refusal.
        from allmydata.mcp import server as server_module
        self.patch(server_module.os, "environ", {})
        self.assertEqual(main(["--list-tools"]), 2)

    def test_list_tools_prints_the_tools(self):
        from allmydata.mcp import server as server_module
        capture = _Capture()
        self.patch(server_module.sys, "stdout", capture)
        self.patch(server_module.os, "environ", {
            "TAHOE_LAPS_MCP_AUTH_TOKEN": "sekrit",
        })
        self.assertEqual(main(["--list-tools"]), 0)
        tools = json.loads(capture.getvalue())
        self.assertIn("tahoe_freshness_overview", tools)
        self.assertEqual(
            tools["tahoe_freshness_overview"]["inputSchema"]["type"],
            "object",
        )

    def test_auth_token_file(self):
        # Reading the token from a file keeps it out of shell history and
        # out of ps(1) output.
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "token")
            with open(path, "w") as f:
                f.write("sekrit\n")
            endpoint = self.build_client(["--auth-token-file", path])
        self.assertEqual(endpoint, "http://127.0.0.1:8888/")

    def build_client(self, argv):
        """
        Run ``main`` far enough to see the client it built.
        """
        from allmydata.mcp import server as server_module

        built = []

        def capture(client, stdin=None, stdout=None, server=None):
            built.append(client)
            return 0

        self.patch(server_module, "serve", capture)
        self.patch(server_module.os, "environ", {})
        self.assertEqual(main(argv), 0)
        self.assertEqual(len(built), 1)
        return built[0]._base

    def test_node_directory_from_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "private"))
            with open(os.path.join(directory, "node.url"), "w") as f:
                f.write("http://127.0.0.1:1234/\n")
            with open(
                os.path.join(directory, "private", "api_auth_token"), "w",
            ) as f:
                f.write("sekrit\n")
            endpoint = self.build_client([
                "--node-directory", directory,
            ])
        self.assertEqual(endpoint, "http://127.0.0.1:1234/")

    def test_node_directory_default_from_the_environment(self):
        from allmydata.mcp import server as server_module
        self.patch(server_module.os, "environ", {
            "TAHOE_LAPS_MCP_NODE_DIRECTORY": "/nonexistent",
        })
        options = server_module.build_parser().parse_args([])
        self.assertEqual(options.node_directory, "/nonexistent")
        self.assertIsNone(options.auth_token)


class _Capture:
    """
    Collects everything written to it.
    """
    def __init__(self):
        self.value = ""

    def write(self, data):
        self.value += data

    def flush(self):
        pass

    def getvalue(self):
        return self.value
