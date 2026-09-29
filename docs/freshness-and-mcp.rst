=============================
Freshness and the MCP server
=============================

Tahoe-LAFS nodes can answer one question about mutable data: *for this
capability, what does the grid currently say, and when did this node
last find out?*  This chapter describes the ``/private/freshness/v1``
JSON endpoint that answers it, and the ``tahoe-mcp`` server that
exposes it to AI assistants over the Model Context Protocol.

.. contents:: On this page
   :depth: 2

Why "fresh" needs a definition
==============================

Immutable (``CHK``) data is content-addressed: the capability contains
the hash of the contents, so there is only ever one version, and if
this node can decrypt the capability then it has the right bytes.  There
is nothing about a ``CHK`` file that can go stale.

Mutable (``SSK`` and ``MDMF``) files and mutable directories are
different.  Somebody, somewhere, may have published a newer revision
that this node has never heard of.  Worse, a newer revision may exist
that this node *cannot reconstruct* -- because too few of its shares
survive, or because a fork means no single revision has enough shares.
In that situation a write to the capability would destroy the only
remaining evidence of the newer data, and there would be no second copy.

So "fresh" here is not a timestamp.  It is a comparison between a report
this node took at some point in the past and what the grid says now,
plus a verdict on the data itself.  Every response carries:

``status``
    ``fresh``, ``stale``, ``unknown``, or ``untracked``.

``stale_reasons``
    When the status is ``stale``, this says why, as a list of short
    tokens.  The important one is
    ``newer-version-is-not-recoverable``: a newer revision exists that
    cannot be read, and writing would lose it.

``checked_at`` and ``age_seconds``
    When the node last asked the grid, and how long ago that was.  A
    report can be perfectly healthy and still be too old to trust,
    which is what the ``stale-after`` argument is for.

``versions``
    Every revision the grid mentioned on that visit, with its sequence
    number, share count, and the servers holding them.

The ``stale_reasons`` vocabulary
--------------------------------

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Token
     - Meaning
   * - ``never-checked``
     - The node has never asked the grid about this.
   * - ``report-is-older-than-stale-after``
     - Somebody may have published since the report was taken.
   * - ``no-recoverable-version``
     - Not enough shares survive to reconstruct anything.
   * - ``multiple-recoverable-versions-with-same-seqnum``
     - A fork. A write would lose a branch.
   * - ``newer-version-is-not-recoverable``
     - There is a newer revision that cannot be reconstructed.
   * - ``corrupt-shares``
     - A share failed verification.
   * - ``unhealthy``
     - A check ran and reported the object unhealthy.

The last three are about the data.  The first four are about how much
the report can be trusted.  Both matter, and they mean different things
to different kinds of caller: an agent that only wants to avoid
data loss should look for ``newer-version-is-not-recoverable``, while
one that only wants to avoid a surprise write conflict should look for
``multiple-recoverable-versions-with-same-seqnum``.

.. _freshness-endpoint:

The ``/private/freshness/v1`` endpoint
======================================

The endpoint lives under ``/private``, so it requires the node's
``api_auth_token``.  Every request must carry the header::

    Authorization: tahoe-lafs <api_auth_token>

The token is written to ``private/api_auth_token`` in the node
directory when the node starts, and it is regenerated on every restart.
Anyone holding it already has full control of the node, so this is
authentication rather than authorization: it keeps other users of the
same machine out of the answer, and nothing more.

Reading
-------

``GET /private/freshness/v1``
    Every watched capability, with its freshness.  This reads a cached
    JSON file and never contacts the grid.

``GET /private/freshness/v1?cap=<cap>``
    The most recent report for one capability.  ``404`` if the node is
    not watching it.

Both accept ``stale-after=<seconds>``, which overrides how old a report
may be before it counts as stale.  The default is one hour.

Writing
-------

All of these are ``POST`` with form-encoded arguments.

``t=track``
    Start watching ``cap``, optionally with a ``label``.  The report
    recorded is only what the capability itself says -- its kind, its
    size -- and its status is ``unknown``, because nothing has asked
    the grid yet.  Watching is cheap on purpose: it is the first thing
    any caller does.

``t=untrack``
    Stop watching, and discard the cached report.  The data is
    untouched.

``t=refresh``
    Ask the grid which versions of ``cap`` exist right now, and record
    the answer.  Contacts every storage server that could be holding a
    share, but downloads no file contents and changes nothing.

``t=check``
    As ``t=refresh``, and additionally health-check the object:
    how many shares exist out of how many are required, and whether any
    are corrupt.  Takes ``verify`` (download and validate every share),
    ``add-lease`` (renew the shares' leases), and ``repair``
    (re-upload what is missing).  ``repair`` is the only argument here
    that modifies data on the grid, and it needs a writable
    capability.

``t=children``
    List a directory's children, each with its own freshness, as seen
    by the directory's watch-list.  Takes ``limit`` (how many children
    to describe; the default is 25) and ``refresh`` (also re-check each
    watched mutable child).

Both ``t=refresh`` and ``t=check`` require the capability to already be
watched, and answer ``404`` if it is not.  This is deliberate: if asking
a question about a capability could also start watching it, then
``t=untrack`` would be silently undone by the next question.

Listing a directory does not add its children to the watch-list.  An
unwatched child is reported with status ``untracked``, which means "we
know nothing about this", as distinct from ``stale``, which means "we
know something and it is not right".

A worked example
----------------

::

    $ tahoe run curl -s \
        -H "Authorization: tahoe-lafs $(cat $NODEDIR/private/api_auth_token)" \
        -d t=track -d "cap=$MDMF" \
        http://127.0.0.1:8888/private/freshness/v1
    {
     "label": null,
     "report": {
      "best_seqnum": null,
      "check": null,
      "checked_at": null,
      "kind": "mutable-file",
      "size": 0,
      "stale_reasons": ["never-checked"],
      "status": "unknown",
      "uri": "URI:MDMF:..."
     },
     "tracked": true,
     "uri": "URI:MDMF:..."
    }

Note that tracking alone tells you nothing, and says so.  Then::

    $ tahoe run curl -s -H "..." -d t=refresh -d "cap=$MDMF" \
        http://127.0.0.1:8888/private/freshness/v1
    ...
     "best_seqnum": 7,
     "status": "fresh",
     "stale_reasons": [],
     "versions": [
      {"seqnum": 7, "shares": 3, "required_shares": 3, "recoverable": true, ...},
      ...
     ]

The watch-list itself is stored in ``private/freshness.json`` inside the
node directory, written atomically.  It is a cache: deleting it costs
only the time to re-check.

Talking to an AI assistant
==========================

``tahoe-mcp`` serves the same information over the Model Context
Protocol, which is what opencode, Claude Desktop, and other MCP clients
speak.  It uses only the standard library, so it ships with
Tahoe-LAFS and needs nothing else installed.

Point it at a node, either with its directory::

    tahoe-mcp --node-directory $NODEDIR

or with an endpoint and a token::

    tahoe-mcp --endpoint http://127.0.0.1:8888/ \
        --auth-token-file $NODEDIR/private/api_auth_token

Prefer ``--auth-token-file`` over ``--auth-token``: a token on the
command line lands in your shell history and shows up in ``ps`` output
for as long as the server is running.

Three environment variables provide defaults, which is usually the
tidier choice for a client that spawns the server itself:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Variable
     - Replaces
   * - ``TAHOE_LAPS_MCP_NODE_DIRECTORY``
     - ``--node-directory``
   * - ``TAHOE_LAPS_MCP_ENDPOINT``
     - ``--endpoint``
   * - ``TAHOE_LAPS_MCP_AUTH_TOKEN``
     - ``--auth-token``

``tahoe-mcp --list-tools`` prints the tool list as JSON and exits, which
is a quick way to check that the server starts at all.

Registering it with opencode.  With no arguments, ``tahoe-mcp`` takes
everything from the environment, which keeps the config free of both
secrets and machine-specific paths::

    {
      "$schema": "https://opencode.ai/config.json",
      "mcp": {
        "tahoe-freshness": {
          "type": "local",
          "command": ["tahoe-mcp"],
          "enabled": true,
          "timeout": 120000
        }
      }
    }

Save that as ``.opencode/opencode.json`` in your project, export
``TAHOE_LAPS_MCP_NODE_DIRECTORY``, and restart opencode -- configuration
is read at startup and is not hot-reloaded.  The generous ``timeout`` is
there because ``tahoe_check_capability`` and ``tahoe_refresh_capability``
talk to storage servers, and opencode's five-second default would cut
them off mid-operation.

The tools
---------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Tool
     - What it does
   * - ``tahoe_freshness_overview``
     - List everything the node watches.  Cheap.
   * - ``tahoe_watch_capability``
     - Start watching a capability.  Cheap.
   * - ``tahoe_unwatch_capability``
     - Stop watching.  Cheap.
   * - ``tahoe_capability_freshness``
     - The cached report for one capability.  Cheap.
   * - ``tahoe_refresh_capability``
     - Ask the grid what exists now.
   * - ``tahoe_check_capability``
     - Health-check, and optionally repair.
   * - ``tahoe_directory_children``
     - List a directory with its children's freshness.

Each tool's description tells the model what the operation costs and
what its result means, because a model has no way to work that out from
the name.  The server also publishes the ``stale_reasons`` vocabulary
as a resource, at ``tahoe://freshness/report-format``.

The intended order of work is: read the overview, read the specific
capability, then refresh it if you need to know about revisions
published since the last report.  Refreshing is the expensive step, and
it is deliberately not something the server does on its own.

Security
--------

The MCP server is a client of the node's web API, not a way around it.
It holds the node's ``api_auth_token`` for as long as it is running,
which means anything that can run the server can already drive the
node.  Treat the token as a password.

The one tool that modifies anything is ``tahoe_check_capability`` with
``repair`` set, and it is annotated as such.  A model that has been told
which tools are read-only can be trusted to ask before writing.
