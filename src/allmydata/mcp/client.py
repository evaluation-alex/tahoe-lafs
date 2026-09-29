"""
A blocking HTTP client for the Tahoe-LAFS freshness endpoint.

The MCP server runs as its own process with nothing else on its event
loop, so this uses ``urllib`` rather than Twisted: the request/response
cycle is a single blocking call, and pulling a reactor in would buy
nothing.
"""

from __future__ import annotations

from urllib.parse import (
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from urllib.request import (
    Request,
    urlopen,
)
from urllib.error import (
    HTTPError,
    URLError,
)
import json

#: The web API listens here by default.
DEFAULT_ENDPOINT = "http://127.0.0.1:8888/"

#: The ``Authorization`` scheme that the private resource tree expects.
AUTH_SCHEME = "tahoe-lafs"

#: How long to wait for an ordinary request.
DEFAULT_TIMEOUT = 60.0

#: How long to wait for something that talks to storage servers.  A
#: ``MODE_CHECK`` servermap update queries every storage server we know
#: about, and a verifying check additionally downloads shares, so these
#: can legitimately take minutes.
CHECK_TIMEOUT = 900.0

#: Where the endpoint lives, relative to the node's web API root.
ENDPOINT_PATH = "private/freshness/v1"


class FreshnessError(Exception):
    """
    The Tahoe-LAFS node could not answer a freshness question.

    :ivar status: the HTTP status code, when the node answered at all.
    :ivar body: the response body, when there was one.
    """
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class FreshnessClient:
    """
    Ask a running Tahoe-LAFS node how fresh its data is.

    :param str endpoint: the node's web API root, e.g.
        ``http://127.0.0.1:8888/``.

    :param str auth_token: the node's ``api_auth_token``, which is
        required because the endpoint lives in the token-protected
        ``/private`` tree.

    :param float timeout: how long to wait, in seconds, for requests that
        do not talk to storage servers.

    :param float check_timeout: how long to wait, in seconds, for
        requests that do.
    """
    def __init__(self, endpoint, auth_token, timeout=DEFAULT_TIMEOUT,
                 check_timeout=CHECK_TIMEOUT):
        if not endpoint:
            raise ValueError("an endpoint is required")
        if not auth_token:
            raise ValueError("an api_auth_token is required")
        parts = urlsplit(endpoint)
        if not parts.scheme or not parts.netloc:
            raise ValueError(
                "endpoint must look like http://host:port/ , not {!r}".format(
                    endpoint,
                ),
            )
        self._base = endpoint if endpoint.endswith("/") else endpoint + "/"
        self._auth_token = auth_token
        self._timeout = timeout
        self._check_timeout = check_timeout

    def _url(self, params):
        parts = urlsplit(self._base)
        base_path = parts.path if parts.path.endswith("/") else parts.path + "/"
        query = urlencode(
            {key: value for (key, value) in params.items() if value is not None},
        )
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            urljoin(base_path, ENDPOINT_PATH.lstrip("/")),
            query,
            "",
        ))

    def _request(self, method, params=None, form=None, timeout=None):
        url = self._url(params or {})
        headers = {
            "Accept": "application/json",
            "Authorization": "{} {}".format(AUTH_SCHEME, self._auth_token),
        }
        body = None
        if form is not None:
            body = urlencode(
                {key: value for (key, value) in form.items() if value is not None},
            ).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout or self._timeout) as response:
                raw = response.read()
        except HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise FreshnessError(
                "the Tahoe-LAFS node answered {} with {}: {}".format(
                    url, e.code, detail.strip(),
                ),
                status=e.code,
                body=detail,
            ) from e
        except URLError as e:
            raise FreshnessError(
                "could not reach the Tahoe-LAFS node at {}: {}".format(url, e.reason),
            ) from e

        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError as e:
            raise FreshnessError(
                "the Tahoe-LAFS node returned something that is not JSON",
                body=text,
            ) from e

    #
    # Reading
    #

    def overview(self, stale_after=None):
        """
        List every capability the node watches, with its freshness.
        """
        return self._request("GET", {"stale-after": stale_after})

    def report(self, cap, stale_after=None):
        """
        Return the most recent freshness report for ``cap``.
        """
        return self._request("GET", {"cap": cap, "stale-after": stale_after})

    #
    # Managing the watch-list
    #

    def track(self, cap, label=None):
        """
        Start watching ``cap``.
        """
        return self._request("POST", form={"t": "track", "cap": cap, "label": label})

    def untrack(self, cap):
        """
        Stop watching ``cap``.
        """
        return self._request("POST", form={"t": "untrack", "cap": cap})

    #
    # Doing work
    #

    def refresh(self, cap, stale_after=None):
        """
        Ask the grid what versions of ``cap`` exist right now.
        """
        return self._request(
            "POST",
            form={"t": "refresh", "cap": cap, "stale-after": stale_after},
            timeout=self._check_timeout,
        )

    def check(self, cap, verify=None, add_lease=None, repair=None,
              stale_after=None):
        """
        Health-check ``cap``, optionally repairing it.
        """
        return self._request(
            "POST",
            form={
                "t": "check",
                "cap": cap,
                "verify": _bool_arg(verify),
                "add-lease": _bool_arg(add_lease),
                "repair": _bool_arg(repair),
                "stale-after": stale_after,
            },
            timeout=self._check_timeout,
        )

    def children(self, cap, refresh=None, limit=None, stale_after=None):
        """
        List ``cap``'s children and what is known about their freshness.
        """
        return self._request(
            "POST",
            form={
                "t": "children",
                "cap": cap,
                "refresh": _bool_arg(refresh),
                "limit": limit,
                "stale-after": stale_after,
            },
            timeout=self._check_timeout,
        )


def _bool_arg(value):
    """
    Render a tri-state boolean the way the endpoint expects to read it.
    """
    if value is None:
        return None
    return "true" if value else "false"
