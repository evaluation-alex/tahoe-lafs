"""
Ported to Python 3.
"""

import attr

from zope.interface import (
    implementer,
)

from twisted.python.failure import (
    Failure,
)
from twisted.internet.defer import (
    succeed,
    fail,
)
from twisted.cred.credentials import (
    ICredentials,
)
from twisted.cred.portal import (
    IRealm,
    Portal,
)
from twisted.cred.checkers import (
    ANONYMOUS,
)
from twisted.cred.error import (
    UnauthorizedLogin,
)
from twisted.web.iweb import (
    ICredentialFactory,
)
from twisted.web.resource import (
    IResource,
    Resource,
)
from twisted.web.guard import (
    HTTPAuthSessionWrapper,
)

from ..util.hashutil import (
    timing_safe_compare,
)
from ..util.assertutil import (
    precondition,
)

from .freshness import (
    create_freshness_resources,
    REGISTRY_FILENAME,
)
from .logs import (
    create_log_resources,
)

SCHEME = b"tahoe-lafs"

class IToken(ICredentials):
    def check(auth_token):
        pass


# Workaround for Shoobx/mypy-zope#26, where without suitable
# stubs for twisted classes (ICredentials), IToken does not
# appear to be an Interface. The proper fix appears to be to
# create stubs for twisted
# (https://twistedmatrix.com/trac/ticket/9717). For now,
# bypassing the inline decorator syntax works around the issue.
_itoken_impl = implementer(IToken)


@_itoken_impl
@attr.s
class Token:
    proposed_token = attr.ib(type=bytes)

    def equals(self, valid_token):
        return timing_safe_compare(
            valid_token,
            self.proposed_token,
        )


@attr.s
class TokenChecker:
    get_auth_token = attr.ib()

    credentialInterfaces = [IToken]

    def requestAvatarId(self, credentials):
        required_token = self.get_auth_token()
        precondition(isinstance(required_token, bytes))
        if credentials.equals(required_token):
            return succeed(ANONYMOUS)
        return fail(Failure(UnauthorizedLogin()))


@implementer(ICredentialFactory)
@attr.s
class TokenCredentialFactory:
    scheme = SCHEME
    authentication_realm = b"tahoe-lafs"

    def getChallenge(self, request):
        return {b"realm": self.authentication_realm}

    def decode(self, response, request):
        return Token(response)


@implementer(IRealm)
@attr.s
class PrivateRealm:
    _root = attr.ib()

    def _logout(self):
        pass

    def requestAvatar(self, avatarId, mind, *interfaces):
        if IResource in interfaces:
            return (IResource, self._root, self._logout)
        raise NotImplementedError(
            "PrivateRealm supports IResource not {}".format(interfaces),
        )


def _create_vulnerable_tree(client):
    private = Resource()
    private.putChild(b"logs", create_log_resources())
    if client is not None:
        # The freshness endpoint has to turn capabilities into nodes, so
        # it is only meaningful when there is a client to do it with.
        config = getattr(client, "config", None)
        registry_path = None
        if config is not None:
            registry_path = config.get_private_path(
                REGISTRY_FILENAME,
            )
        private.putChild(
            b"freshness", create_freshness_resources(client, registry_path),
        )
    return private


def _create_private_tree(get_auth_token, vulnerable):
    realm = PrivateRealm(vulnerable)
    portal = Portal(realm, [TokenChecker(get_auth_token)])
    return HTTPAuthSessionWrapper(portal, [TokenCredentialFactory()])


def create_private_tree(get_auth_token, client=None):
    """
    Create a new resource tree that only allows requests if they include a
    correct `Authorization: tahoe-lafs <api_auth_token>` header (where
    `api_auth_token` matches the private configuration value).

    :param get_auth_token: a callable returning the expected token as
        ``bytes``.

    :param client: the ``_Client`` to build node-specific endpoints from.
        When this is ``None`` those endpoints are omitted, which is what
        callers that only want the token-protected logging endpoints
        should pass.
    """
    return _create_private_tree(
        get_auth_token,
        _create_vulnerable_tree(client),
    )
