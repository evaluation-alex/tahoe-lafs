"""
Tests for the ``/private/freshness/v1`` endpoint.
"""

from __future__ import annotations

from twisted.internet import defer
from twisted.trial import unittest
from twisted.web.test.requesthelper import DummyRequest
from zope.interface import directlyProvides
import os
import shutil
import tempfile

from allmydata.interfaces import (
    CapConstraintError,
    IDirectoryNode,
)
from allmydata.mutable.servermap import ServerMap
from allmydata.util import base32
from allmydata.web.freshness import (
    API_VERSION,
    DEFAULT_CHILD_LIMIT,
    DEFAULT_STALE_AFTER,
    FreshnessRegistry,
    FreshnessResource,
    create_freshness_resources,
    stale_reasons,
    status_for,
    summarize_servermap,
)

CAP = "URI:CHK:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbbbbbbbbbbbb:1234:1:1"
MDMF = "URI:MDMF:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbbbbbbbbbbbb"
DIRCAP = "URI:DIR2:cccccccccccccccccccccccccccccc:dddddddddddddddddddddddddd"
NEWCAP = "URI:SSK:eeeeeeeeeeeeeeeeeeeeeeeeeeeee:fffffffffffffffffffffffffffffff"


def verinfo(seqnum, root_hash_char="a", datalen=100, k=3, n=10):
    """
    Build the 9-tuple that a ``ServerMap`` uses to identify a version.
    """
    return (
        seqnum,
        base32.a2b((root_hash_char * 32).encode("ascii")),
        b"\x00" * 16,   # salt
        128 * 1024,     # segment size
        datalen,
        k,
        n,
        b"\x00" * 16,   # prefix
        (0, datalen),   # offsets
    )


class FakeServer:
    """
    Stands in for a storage server, which a ``ServerMap`` only ever
    asks for its serverid.
    """
    def __init__(self, serverid):
        self._serverid = serverid

    def get_serverid(self):
        return self._serverid


def make_servermap(versions):
    """
    Build a real ``ServerMap`` holding the given versions.

    :param versions: a list of ``(verinfo, number_of_shares)`` pairs.
    """
    smap = ServerMap()
    for (vinfo, count) in versions:
        for shnum in range(count):
            server = FakeServer(b"server-%d-%d" % (shnum, vinfo[0]))
            smap.add_new_share(server, shnum, vinfo, 1600000000.0)
    smap.reachable_servers.add(FakeServer(b"reachable"))
    smap.unreachable_servers.add(FakeServer(b"unreachable"))
    return smap


class FakeCheckResults:
    """
    Just enough of ``ICheckResults`` for the summarizer to work.
    """
    def __init__(self, healthy=True, corrupt=0):
        self._healthy = healthy
        self._corrupt = corrupt

    def is_healthy(self):
        return self._healthy

    def is_recoverable(self):
        return self._healthy

    def get_summary(self):
        return b"healthy" if self._healthy else b"not healthy"

    def get_report(self):
        return [b"a share problem"]

    def as_dict(self):
        return {
            "count-shares-needed": 3,
            "count-shares-expected": 10,
            "list-corrupt-shares": [
                (b"server-%d" % shnum, b"si", shnum)
                for shnum in range(self._corrupt)
            ],
        }


class FakeCheckAndRepairResults:
    def __init__(self, before, after=None):
        self._before = before
        self._after = after

    def get_repair_attempted(self):
        return self._after is not None

    def get_repair_successful(self):
        return self._after is not None and self._after.is_healthy()

    def get_pre_repair_results(self):
        return self._before

    def get_post_repair_results(self):
        return self._after


class FakeNode:
    """
    A stand-in for a file node that the endpoint can interrogate.
    """
    def __init__(self, uri, smap=None, results=None, repair_results=None,
                 error=None, unknown=False):
        self._uri = uri.encode("utf-8") if isinstance(uri, str) else uri
        self._smap = smap
        self._results = results
        self._repair_results = repair_results
        self._error = error
        self._unknown = unknown
        self.servermaps_requested = []
        self.checks = []
        self.repairs = []

    #
    # The bits of INode that every node has
    #

    def is_mutable(self):
        return not self._uri.startswith(b"URI:CHK:")

    def is_readonly(self):
        return True

    def is_unknown(self):
        return self._unknown

    def raise_error(self):
        if self._error is not None:
            raise self._error

    def get_readonly_uri(self):
        return self._uri

    def get_storage_index(self):
        return base32.a2b(b"a" * 32)

    def get_size(self):
        return 100

    #
    # Mutable nodes
    #

    def get_servermap(self, mode):
        self.servermaps_requested.append(mode)
        return defer.succeed(self._smap)

    def check(self, monitor, verify, add_lease):
        self.checks.append((verify, add_lease))
        return defer.succeed(self._results)

    def check_and_repair(self, monitor, verify, add_lease):
        self.repairs.append((verify, add_lease))
        return defer.succeed(self._repair_results)


class FakeDirectoryNode(FakeNode):
    """
    A directory, which the endpoint can list and which exposes an inner
    file node.

    Only the handful of ``IDirectoryNode`` members the endpoint touches
    are implemented, so the interface is declared for this instance
    rather than for the class: pretending to the whole interface would
    be a lie that a typo elsewhere could rely on.
    """
    def __init__(self, uri, inner, children=None, **kwargs):
        super().__init__(uri, **kwargs)
        self._node = inner
        self._children = children or {}
        self.listed = 0
        directlyProvides(self, IDirectoryNode)

    def is_mutable(self):
        return True

    def list(self):
        self.listed += 1
        return defer.succeed(self._children)


class FakeClient:
    """
    Stands in for the ``_Client`` that turns capabilities into nodes.
    """
    nickname = b"fake-node"

    def __init__(self, nodes):
        self._nodes = nodes

    def get_long_nodeid(self):
        return b"nodeid-0123456789"

    def create_node_from_uri(self, uri):
        node = self._nodes.get(uri.decode("utf-8"))
        if node is None:
            raise CapConstraintError("no such cap")
        return node


class FakeRequest(DummyRequest):
    """
    A ``DummyRequest`` that behaves like a real connection.

    ``DummyRequest`` replaces its own list of ``notifyFinish`` observers
    with ``None`` once ``finish()`` runs, and ``render_exception`` asks
    for a ``notifyFinish`` Deferred *after* a synchronous render has
    already replied.  Keeping a Deferred of our own around sidesteps
    that, and gives the test something to wait on.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.finished_deferred = defer.Deferred()
        self._finishedDeferreds = [self.finished_deferred]

    def notifyFinish(self):
        return self.finished_deferred


class FreshnessRegistryTests(unittest.TestCase):
    """
    The watch-list, on its own.
    """
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "freshness.json")
        self.registry = FreshnessRegistry(self.path, now=lambda: 100.0)

    def test_starts_empty(self):
        self.assertEqual(self.registry.list(), [])
        self.assertIsNone(self.registry.get(CAP))

    def test_track_then_get(self):
        self.registry.track(CAP, "a label")
        entry = self.registry.get(CAP)
        self.assertEqual(entry["uri"], CAP)
        self.assertEqual(entry["label"], "a label")
        self.assertEqual(entry["added_at"], 100.0)
        self.assertIsNone(entry["report"])

    def test_track_is_idempotent_and_keeps_the_report(self):
        self.registry.track(CAP)
        self.registry.set_report(CAP, {"best_seqnum": 4})
        self.registry.track(CAP)
        entry = self.registry.get(CAP)
        self.assertEqual(entry["report"], {"best_seqnum": 4})
        # A second track must not reset added_at.
        self.assertEqual(entry["added_at"], 100.0)

    def test_track_with_a_label_updates_the_label(self):
        self.registry.track(CAP, "first")
        self.registry.track(CAP, "second")
        self.assertEqual(self.registry.get(CAP)["label"], "second")

    def test_track_with_no_label_keeps_the_old_one(self):
        self.registry.track(CAP, "first")
        self.registry.track(CAP)
        self.assertEqual(self.registry.get(CAP)["label"], "first")

    def test_untrack(self):
        self.registry.track(CAP)
        self.assertTrue(self.registry.untrack(CAP))
        self.assertIsNone(self.registry.get(CAP))
        # Untracking something we do not watch is not an error.
        self.assertFalse(self.registry.untrack(CAP))

    def test_set_report_needs_a_watched_cap(self):
        # Otherwise un-watching would be undone by the next question.
        self.assertRaises(
            KeyError, self.registry.set_report, CAP, {"best_seqnum": 1},
        )

    def test_list_is_sorted_and_copies(self):
        self.registry.track(DIRCAP)
        self.registry.track(CAP)
        self.assertEqual(
            [entry["uri"] for entry in self.registry.list()],
            sorted([CAP, DIRCAP]),
        )
        self.registry.list()[0]["uri"] = "mutated"
        self.assertIn(CAP, [e["uri"] for e in self.registry.list()])

    def test_survives_a_reload(self):
        self.registry.track(CAP, "a label")
        self.registry.set_report(CAP, {"best_seqnum": 7})
        reloaded = FreshnessRegistry(self.path, now=lambda: 200.0)
        entry = reloaded.get(CAP)
        self.assertEqual(entry["label"], "a label")
        self.assertEqual(entry["report"], {"best_seqnum": 7})

    def test_a_corrupt_file_is_not_fatal(self):
        with open(self.path, "w") as f:
            f.write("this is not json")
        self.assertEqual(FreshnessRegistry(self.path).list(), [])

    def test_a_json_file_of_the_wrong_shape_is_not_fatal(self):
        with open(self.path, "w") as f:
            f.write('["not", "a", "registry"]')
        self.assertEqual(FreshnessRegistry(self.path).list(), [])

    def test_in_memory_when_there_is_no_path(self):
        registry = FreshnessRegistry(None)
        registry.track(CAP)
        self.assertIsNotNone(registry.get(CAP))
        # And nothing was written anywhere.
        self.assertFalse(os.path.exists(self.path))

    def test_default_stale_after(self):
        self.assertEqual(self.registry.stale_after, DEFAULT_STALE_AFTER)


class StaleReasonTests(unittest.TestCase):
    """
    The vocabulary that the endpoint reports with.
    """
    def report(self, **kwargs):
        base = {
            "mutable": True,
            "checked_at": 100.0,
            "best_seqnum": 3,
            "needs_merge": False,
            "newer_unrecoverable_seqnums": [],
            "check": None,
        }
        base.update(kwargs)
        return base

    def reasons(self, **kwargs):
        return stale_reasons(self.report(**kwargs), 1000.0, 200.0)

    def test_fresh(self):
        self.assertEqual(self.reasons(), [])

    def test_never_checked(self):
        self.assertEqual(self.reasons(checked_at=None), ["never-checked"])

    def test_older_than_stale_after(self):
        # 200 - 100 = 100 seconds old, and we only tolerate 10.
        self.assertEqual(
            stale_reasons(self.report(checked_at=100.0), 10.0, 200.0),
            ["report-is-older-than-stale-after"],
        )

    def test_within_stale_after(self):
        self.assertEqual(
            stale_reasons(self.report(checked_at=100.0), 1000.0, 200.0), [],
        )

    def test_no_recoverable_version(self):
        self.assertEqual(self.reasons(best_seqnum=None),
                         ["no-recoverable-version"])

    def test_needs_merge(self):
        self.assertEqual(
            self.reasons(needs_merge=True),
            ["multiple-recoverable-versions-with-same-seqnum"],
        )

    def test_newer_unrecoverable(self):
        self.assertEqual(
            self.reasons(newer_unrecoverable_seqnums=[5]),
            ["newer-version-is-not-recoverable"],
        )

    def test_corrupt_shares(self):
        self.assertEqual(
            self.reasons(check={"healthy": True, "corrupt_shares": 1}),
            ["corrupt-shares"],
        )

    def test_unhealthy(self):
        self.assertEqual(
            self.reasons(check={"healthy": False, "corrupt_shares": 0}),
            ["unhealthy"],
        )

    def test_grid_reasons_are_withheld_until_the_grid_was_asked(self):
        # A tracked-but-never-refreshed mutable file has no servermap
        # data, so we must not claim to know that it is unrecoverable.
        report = {"mutable": True, "checked_at": None, "check": None}
        self.assertEqual(stale_reasons(report, 1000.0, 200.0),
                         ["never-checked"])

    def test_immutable_data_cannot_be_superseded(self):
        report = {"mutable": False, "checked_at": 100.0, "check": None}
        self.assertEqual(stale_reasons(report, 1000.0, 100000.0), [])

    def test_immutable_data_can_still_be_corrupt(self):
        # Skipping the grid questions for immutable data must not also
        # skip the corruption answer.
        report = {
            "mutable": False,
            "checked_at": 100.0,
            "check": {"healthy": False, "corrupt_shares": 2},
        }
        self.assertEqual(
            sorted(stale_reasons(report, 1000.0, 200.0)),
            ["corrupt-shares", "unhealthy"],
        )

    def test_status(self):
        self.assertEqual(status_for([], None), "unknown")
        self.assertEqual(status_for([], 100.0), "fresh")
        self.assertEqual(status_for(["unhealthy"], 100.0), "stale")


class SummarizeServermapTests(unittest.TestCase):
    """
    Turning grid state into JSON.
    """
    def test_versions_are_ordered_by_seqnum(self):
        smap = make_servermap([(verinfo(5, "b"), 3), (verinfo(2, "a"), 3)])
        summary = summarize_servermap(smap)
        self.assertEqual(
            [version["seqnum"] for version in summary["versions"]],
            [2, 5],
        )

    def test_recoverability(self):
        smap = make_servermap([
            (verinfo(1, "a", k=3), 3),   # recoverable
            (verinfo(2, "b", k=3), 1),   # not
        ])
        summary = summarize_servermap(smap)
        self.assertEqual(summary["best_seqnum"], 1)
        self.assertEqual(summary["highest_seqnum"], 2)
        self.assertEqual(summary["newer_unrecoverable_seqnums"], [2])

    def test_nothing_recoverable_at_all(self):
        summary = summarize_servermap(make_servermap([(verinfo(1), 1)]))
        self.assertIsNone(summary["best_seqnum"])
        # With nothing recoverable, ServerMap counts every unrecoverable
        # version as a newer one -- which is right, because any write
        # would destroy the only evidence of the file.
        self.assertEqual(summary["newer_unrecoverable_seqnums"], [1])

    def test_servers(self):
        summary = summarize_servermap(make_servermap([(verinfo(1), 3)]))
        # ensure_str, so these are text on the way out.
        self.assertEqual(summary["servers_responding"], ["reachable"])
        self.assertEqual(summary["servers_unreachable"], ["unreachable"])

    def test_needs_merge(self):
        smap = make_servermap([(verinfo(1, "a"), 3), (verinfo(1, "b"), 3)])
        self.assertTrue(summarize_servermap(smap)["needs_merge"])

    def test_share_counts(self):
        smap = make_servermap([(verinfo(1, "a", datalen=42, k=3, n=10), 4)])
        version = summarize_servermap(smap)["versions"][0]
        self.assertEqual(version["shares"], 4)
        self.assertEqual(version["required_shares"], 3)
        self.assertEqual(version["total_shares"], 10)
        self.assertEqual(version["size"], 42)
        self.assertTrue(version["recoverable"])

    def test_no_shares(self):
        summary = summarize_servermap(ServerMap())
        self.assertEqual(summary["versions"], [])
        self.assertEqual(summary["highest_seqnum"], 0)
        self.assertIsNone(summary["best_seqnum"])


class EndpointTestMixin:
    """
    A resource wired to fake nodes, and a way to make requests.
    """
    def build(self, nodes, registry_path=None):
        self.client = FakeClient(nodes)
        self.registry = FreshnessRegistry(registry_path)
        self.resource = FreshnessResource(self.client, self.registry)
        return self.resource

    def request(self, method, **args):
        """
        Make a request and wait for the response.

        :return FakeRequest: the finished request.
        """
        req = FakeRequest([b"freshness", b"v1"])
        req.method = method.encode("utf-8")
        req.args = {
            key.encode("utf-8"): [
                str(value).encode("utf-8")
                for value in (value if isinstance(value, list) else [value])
            ]
            for (key, value) in args.items()
        }
        render = (
            self.resource.render_GET
            if method == "GET"
            else self.resource.render_POST
        )
        render(req)
        return self._response(req)

    def _response(self, req):
        # (Not _wait: twisted.trial's TestCase uses that name for its
        # own purposes, and overriding it breaks the test runner.)
        from twisted.internet import reactor
        if not req.finished_deferred.called:
            # Everything in the fake layer is synchronous, so this is
            # only here to catch a mistake rather than to wait.
            reactor.iterate(0)
        self.assertTrue(
            req.finished_deferred.called,
            "the request never finished",
        )
        return req

    def body(self, req):
        """
        The JSON body of a response.
        """
        from allmydata.util import jsonbytes
        raw = b"".join(req.written)
        return jsonbytes.loads(raw.decode("utf-8"))

    def text(self, req):
        return b"".join(req.written).decode("utf-8")

    def get(self, **args):
        return self.request("GET", **args)

    def post(self, **args):
        return self.request("POST", **args)


class OverviewTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.build({})

    def test_empty(self):
        response = self.body(self.get())
        self.assertEqual(response["version"], API_VERSION)
        self.assertEqual(response["caps"], [])
        self.assertEqual(response["stale_after"], DEFAULT_STALE_AFTER)
        self.assertEqual(response["node"], {
            "nickname": "fake-node",
            "nodeid": "nodeid-0123456789",
        })

    def test_content_type_is_json(self):
        req = self.get()
        self.assertEqual(
            req.responseHeaders.getRawHeaders(b"content-type"),
            [b"application/json; charset=utf-8"],
        )

    def test_lists_watched_caps(self):
        self.registry.track(MDMF, "notes")
        response = self.body(self.get())
        self.assertEqual(
            [cap["uri"] for cap in response["caps"]],
            [MDMF],
        )
        self.assertEqual(response["caps"][0]["label"], "notes")

    def test_a_tracked_but_unchecked_cap_is_unknown(self):
        self.registry.track(MDMF)
        cap = self.body(self.get())["caps"][0]
        self.assertEqual(cap["status"], "unknown")
        self.assertIsNone(cap["checked_at"])
        self.assertIsNone(cap["age_seconds"])

    def test_stale_after_override(self):
        response = self.body(self.get(**{"stale-after": 5}))
        self.assertEqual(response["stale_after"], 5.0)


class ReportTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.node = FakeNode(MDMF, smap=make_servermap([(verinfo(3), 3)]))
        self.build({MDMF: self.node})

    def test_unwatched(self):
        req = self.get(cap=MDMF)
        self.assertEqual(req.responseCode, 404)
        self.assertIn("not being watched", self.text(req))

    def test_tracked_but_unchecked(self):
        self.post(t="track", cap=MDMF)
        response = self.body(self.get(cap=MDMF))
        self.assertEqual(response["uri"], MDMF)
        self.assertEqual(response["status"], "unknown")
        self.assertIsNone(response["report"]["checked_at"])
        # The capability itself told us its kind, size and storage index,
        # and that is worth reporting even before we ask the grid.
        self.assertEqual(response["report"]["kind"], "mutable-file")
        self.assertEqual(response["report"]["size"], 100)

    def test_stale_after_is_reported(self):
        self.registry.track(MDMF)
        response = self.body(self.get(cap=MDMF, **{"stale-after": 30}))
        self.assertEqual(response["stale_after"], 30.0)

    def test_unwatched_fields(self):
        # Used for children the node is not watching.
        from allmydata.web.freshness import unwatched_fields
        self.assertEqual(unwatched_fields()["status"], "untracked")
        self.assertFalse(unwatched_fields()["watched"])


class TrackTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.node = FakeNode(MDMF, smap=make_servermap([(verinfo(3), 3)]))
        self.build({MDMF: self.node, CAP: FakeNode(CAP)})

    def test_track(self):
        response = self.body(self.post(t="track", cap=MDMF, label="notes"))
        self.assertTrue(response["tracked"])
        self.assertEqual(response["label"], "notes")
        self.assertEqual(self.registry.get(MDMF)["uri"], MDMF)

    def test_tracking_does_not_ask_the_grid(self):
        # It must be cheap, because it is the first thing anybody does.
        self.post(t="track", cap=MDMF)
        self.assertEqual(self.node.servermaps_requested, [])
        self.assertEqual(self.node.checks, [])

    def test_tracking_reports_unknown_not_fresh(self):
        # We have not asked anybody, so we must not claim to know.
        response = self.body(self.post(t="track", cap=MDMF))
        self.assertEqual(response["report"]["status"], "unknown")
        self.assertEqual(response["report"]["stale_reasons"],
                         ["never-checked"])

    def test_the_report_is_remembered(self):
        self.post(t="track", cap=MDMF)
        entry = self.registry.get(MDMF)
        self.assertIsNotNone(entry["report"])
        self.assertEqual(entry["report"]["kind"], "mutable-file")

    def test_cap_is_required(self):
        req = self.post(t="track")
        self.assertEqual(req.responseCode, 400)
        self.assertIn("cap", self.text(req))

    def test_a_bad_cap_is_rejected(self):
        req = self.post(t="track", cap="not-a-cap")
        self.assertEqual(req.responseCode, 400)
        self.assertIn("not a valid", self.text(req))

    def test_a_cap_the_node_cannot_use(self):
        node = FakeNode(CAP, error=CapConstraintError("this node is happy"))
        self.build({CAP: node})
        req = self.post(t="track", cap=CAP)
        self.assertEqual(req.responseCode, 400)
        self.assertIn("can examine", self.text(req))

    def test_an_unknown_cap(self):
        self.build({CAP: FakeNode(CAP, unknown=True)})
        req = self.post(t="track", cap=CAP)
        self.assertEqual(req.responseCode, 400)
        self.assertIn("does not understand", self.text(req))

    def test_untrack(self):
        self.registry.track(MDMF)
        response = self.body(self.post(t="untrack", cap=MDMF))
        self.assertFalse(response["tracked"])
        self.assertTrue(response["removed"])
        self.assertIsNone(self.registry.get(MDMF))

    def test_untrack_something_unwatched(self):
        response = self.body(self.post(t="untrack", cap=MDMF))
        self.assertFalse(response["removed"])

    def test_unknown_action(self):
        req = self.post(t="teleport", cap=MDMF)
        self.assertEqual(req.responseCode, 400)
        self.assertIn("accepts t=track", self.text(req))

    def test_no_action(self):
        req = self.post(cap=MDMF)
        self.assertEqual(req.responseCode, 400)


class RefreshTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.node = FakeNode(MDMF, smap=make_servermap([(verinfo(3), 3)]))
        self.build({MDMF: self.node, CAP: FakeNode(CAP)})
        self.registry.track(MDMF)

    def report_of(self, **args):
        return self.body(self.post(t="refresh", cap=MDMF, **args))["report"]

    def test_refresh_records_the_servermap(self):
        report = self.report_of()
        self.assertEqual(report["best_seqnum"], 3)
        self.assertEqual(report["highest_seqnum"], 3)
        self.assertEqual(report["status"], "fresh")
        self.assertIsNotNone(report["checked_at"])
        self.assertEqual(report["age_seconds"], 0.0)
        self.assertEqual(len(report["versions"]), 1)

    def test_refresh_asks_the_grid(self):
        self.post(t="refresh", cap=MDMF)
        from allmydata.mutable.common import MODE_CHECK
        self.assertEqual(self.node.servermaps_requested, [MODE_CHECK])
        # A refresh must not download anything.
        self.assertEqual(self.node.checks, [])

    def test_refresh_saves_the_report(self):
        self.post(t="refresh", cap=MDMF)
        self.assertEqual(
            self.registry.get(MDMF)["report"]["best_seqnum"],
            3,
        )

    def test_an_unrecoverable_newer_version_is_alarming(self):
        self.node._smap = make_servermap([
            (verinfo(3, "a"), 3),
            (verinfo(4, "b"), 1),
        ])
        report = self.report_of()
        self.assertEqual(report["status"], "stale")
        self.assertEqual(report["stale_reasons"],
                         ["newer-version-is-not-recoverable"])

    def test_nothing_recoverable(self):
        self.node._smap = make_servermap([(verinfo(3), 1)])
        # Nothing at all is recoverable, and a write would destroy the
        # only evidence of the file.
        self.assertEqual(
            self.report_of()["stale_reasons"],
            ["no-recoverable-version", "newer-version-is-not-recoverable"],
        )

    def test_an_old_report_goes_stale_by_itself(self):
        # No new information, just time passing: this is what
        # stale-after is for.
        self.post(t="refresh", cap=MDMF)
        entry = self.registry.get(MDMF)
        entry["report"]["checked_at"] = 0.0
        self.registry.set_report(MDMF, entry["report"])
        response = self.body(self.get(cap=MDMF, **{"stale-after": 1}))
        self.assertEqual(response["status"], "stale")
        self.assertEqual(
            response["stale_reasons"],
            ["report-is-older-than-stale-after"],
        )

    def test_refresh_needs_a_watched_cap(self):
        # Otherwise un-watching would be undone by the next question.
        self.registry.untrack(MDMF)
        req = self.post(t="refresh", cap=MDMF)
        self.assertEqual(req.responseCode, 404)
        self.assertIn("t=track", self.text(req))
        self.assertEqual(self.node.servermaps_requested, [])

    def test_immutable_data_is_not_asked_about(self):
        self.registry.track(CAP)
        report = self.body(self.post(t="refresh", cap=CAP))["report"]
        # There is no question to ask: a CHK cap names its own content.
        self.assertIsNone(report["checked_at"])
        self.assertEqual(report["status"], "unknown")
        self.assertFalse(report["mutable"])

    def test_a_fork(self):
        self.node._smap = make_servermap([
            (verinfo(1, "a"), 3), (verinfo(1, "b"), 3),
        ])
        self.assertEqual(
            self.report_of()["stale_reasons"],
            ["multiple-recoverable-versions-with-same-seqnum"],
        )


class CheckTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.node = FakeNode(
            MDMF,
            smap=make_servermap([(verinfo(3), 3)]),
            results=FakeCheckResults(),
            repair_results=FakeCheckAndRepairResults(
                FakeCheckResults(healthy=False, corrupt=1),
                FakeCheckResults(),
            ),
        )
        self.build({MDMF: self.node})
        self.registry.track(MDMF)

    def test_check(self):
        report = self.body(self.post(t="check", cap=MDMF))["report"]
        self.assertEqual(report["check"]["healthy"], True)
        self.assertEqual(report["status"], "fresh")
        self.assertEqual(self.node.checks, [(False, False)])
        self.assertEqual(self.node.repairs, [])

    def test_verify_and_lease_are_passed_through(self):
        self.post(t="check", cap=MDMF, verify="true", **{"add-lease": "1"})
        self.assertEqual(self.node.checks, [(True, True)])

    def test_a_bad_boolean_is_a_bad_request(self):
        req = self.post(t="check", cap=MDMF, verify="perhaps")
        self.assertEqual(req.responseCode, 400)

    def test_corrupt_shares(self):
        self.node._results = FakeCheckResults(healthy=False, corrupt=2)
        report = self.body(self.post(t="check", cap=MDMF))["report"]
        self.assertEqual(report["check"]["corrupt_shares"], 2)
        self.assertEqual(report["status"], "stale")
        self.assertEqual(
            sorted(report["stale_reasons"]),
            ["corrupt-shares", "unhealthy"],
        )

    def test_repair(self):
        report = self.body(self.post(t="check", cap=MDMF, repair="true"))[
            "report"
        ]
        self.assertEqual(self.node.repairs, [(False, False)])
        self.assertEqual(self.node.checks, [])
        self.assertTrue(report["check_results"]["repair_attempted"])
        self.assertTrue(report["check_results"]["repair_successful"])
        # After a repair the "after" results describe the object, so the
        # report is about the repaired object, not the broken one.
        self.assertEqual(report["check"]["healthy"], True)
        self.assertEqual(report["status"], "fresh")

    def test_a_repair_that_did_nothing_reports_the_before_results(self):
        self.node._repair_results = FakeCheckAndRepairResults(
            FakeCheckResults(healthy=False, corrupt=1),
        )
        report = self.body(self.post(t="check", cap=MDMF, repair="true"))[
            "report"
        ]
        self.assertFalse(report["check_results"]["repair_attempted"])
        self.assertEqual(report["check"]["healthy"], False)


class ChildrenTests(EndpointTestMixin, unittest.TestCase):
    def setUp(self):
        self.a = FakeNode(
            NEWCAP, smap=make_servermap([(verinfo(1, "a"), 3)]),
        )
        self.b = FakeNode(
            "URI:SSK:ggggggggggggggggggggggggggggg:hhhhhhhhhhhhhhhhhhhhhhhhhhhh",
            smap=make_servermap([(verinfo(1, "b"), 1)]),
        )
        self.inner = FakeNode(DIRCAP)
        self.directory = FakeDirectoryNode(
            "URI:DIR2:iiiiiiiiiiiiiiiiiiiiiiiiiiiiii:jjjjjjjjjjjjjjjjjjjjjjjjjj",
            self.inner,
            children={
                b"b-file": (self.b, {}),
                b"a-file": (self.a, {}),
                b"static": (FakeNode(CAP), {}),
            },
        )
        self.build({
            self.cap_of(self.directory): self.directory,
            self.cap_of(self.a): self.a,
            self.cap_of(self.b): self.b,
        })

    def cap_of(self, node):
        return node.get_readonly_uri().decode("utf-8")

    def post_children(self, **args):
        return self.body(self.post(t="children",
                                   cap=self.dircap(), **args))

    def dircap(self):
        return self.cap_of(self.directory)

    def test_lists_children_in_name_order(self):
        response = self.post_children()
        self.assertEqual(
            [child["name"] for child in response["children"]],
            ["a-file", "b-file", "static"],
        )

    def test_unwatched_children_are_untracked(self):
        response = self.post_children()
        by_name = {child["name"]: child for child in response["children"]}
        self.assertEqual(by_name["a-file"]["status"], "untracked")
        self.assertFalse(by_name["a-file"]["watched"])

    def test_child_kinds(self):
        response = self.post_children()
        by_name = {child["name"]: child for child in response["children"]}
        self.assertEqual(by_name["a-file"]["kind"], "mutable-file")
        self.assertEqual(by_name["static"]["kind"], "immutable-file")
        self.assertFalse(by_name["static"]["mutable"])

    def test_a_watched_child_reports_its_own_freshness(self):
        self.registry.track(self.cap_of(self.a))
        self.registry.set_report(
            self.cap_of(self.a),
            {
                "uri": self.cap_of(self.a),
                "kind": "mutable-file",
                "mutable": True,
                "size": 100,
                "checked_at": 1000.0,
                "check": None,
            },
        )
        response = self.post_children(**{"stale-after": 1})
        by_name = {child["name"]: child for child in response["children"]}
        self.assertEqual(by_name["a-file"]["status"], "stale")
        self.assertEqual(
            by_name["a-file"]["stale_reasons"],
            ["report-is-older-than-stale-after"],
        )

    def test_listing_does_not_watch_anything(self):
        # Adding to the watch-list as a side effect of looking around
        # would make the overview useless.
        self.post_children(refresh="true")
        self.assertEqual(self.registry.list(), [])

    def test_refresh_only_touches_watched_children(self):
        self.registry.track(self.cap_of(self.a))
        response = self.post_children(refresh="true")
        self.assertEqual(
            response["refreshed"],
            [self.cap_of(self.a)],
        )
        self.assertEqual(len(self.a.servermaps_requested), 1)
        self.assertEqual(self.b.servermaps_requested, [])
        # And the refreshed child is now known.
        self.assertEqual(
            self.registry.get(
                self.cap_of(self.a),
            )["report"]["best_seqnum"],
            1,
        )

    def test_no_refresh_by_default(self):
        self.registry.track(self.cap_of(self.a))
        response = self.post_children()
        self.assertEqual(response["refreshed"], [])
        self.assertEqual(self.a.servermaps_requested, [])

    def test_limit_truncates_the_listing(self):
        response = self.post_children(limit=1)
        self.assertEqual(
            [child["name"] for child in response["children"]],
            ["a-file"],
        )
        self.assertEqual(response["limit"], 1)
        self.assertEqual(response["child_count"], 3)
        # A caller must be able to tell that they are not seeing
        # everything, rather than assuming an empty directory.
        self.assertTrue(response["truncated"])

    def test_no_truncation_when_it_all_fits(self):
        response = self.post_children(limit=99)
        self.assertFalse(response["truncated"])
        self.assertEqual(response["child_count"], 3)

    def test_the_default_limit(self):
        self.assertEqual(self.post_children()["limit"],
                         DEFAULT_CHILD_LIMIT)

    def test_a_negative_limit_is_rejected(self):
        req = self.post(t="children", cap=self.dircap(), limit="-1")
        self.assertEqual(req.responseCode, 400)

    def test_a_non_directory(self):
        req = self.post(t="children", cap=self.cap_of(self.a))
        self.assertEqual(req.responseCode, 400)
        self.assertIn("not a directory", self.text(req))

    def test_an_unreachable_child_does_not_break_the_listing(self):
        self.registry.track(self.cap_of(self.a))
        self.a.get_servermap = lambda mode: defer.fail(
            RuntimeError("the grid is down"),
        )
        response = self.post_children(refresh="true")
        # The listing is still useful without it.
        self.assertEqual(len(response["children"]), 3)
        self.assertEqual(response["refreshed"], [])


class WiringTests(unittest.TestCase):
    def test_the_subtree_is_v1(self):
        client = FakeClient({})
        subtree = create_freshness_resources(client)
        v1 = subtree.children[b"v1"]
        self.assertIsInstance(v1, FreshnessResource)

    def test_the_registry_persists_where_it_is_told(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "freshness.json")
        subtree = create_freshness_resources(FakeClient({}), path)
        subtree.children[b"v1"]._registry.track(CAP)
        self.assertTrue(os.path.exists(path))

    def test_no_registry_path_stays_in_memory(self):
        subtree = create_freshness_resources(FakeClient({}))
        registry = subtree.children[b"v1"]._registry
        registry.track(CAP)
        self.assertIsNotNone(registry.get(CAP))
