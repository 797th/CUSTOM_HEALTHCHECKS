from __future__ import annotations

import logging

from collections.abc import Callable
from typing import cast

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import User
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpRequest, HttpResponse

from hc.accounts.http import AuthenticatedHttpRequest
from hc.accounts.models import Profile

# Imported lazily inside AutoLoginMiddleware._get_user to avoid a circular
# import (hc.accounts.views imports from hc.accounts.models).
logger = logging.getLogger(__name__)

MiddlewareFunc = Callable[[HttpRequest], HttpResponse]


class TeamAccessMiddleware:
    def __init__(self, get_response: MiddlewareFunc) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return self.get_response(request)

        request = cast(AuthenticatedHttpRequest, request)
        request.profile = Profile.objects.for_user(request.user)
        return self.get_response(request)


class AutoLoginMiddleware:
    """Auto-authenticate every request as a fixed local user.

    Enabled by setting AUTO_LOGIN_USER (a username or email). Intended for
    single-tenant, self-hosted deployments on trusted networks where the web
    dashboard should be reachable without a login form. Ping endpoints are
    unaffected (they never required authentication).

    If the configured user does not exist, it is created automatically
    (with a default project) so the deployment works on a fresh database.
    """

    # Re-lookup the user at most this often. The cache must not serve a
    # deleted/renamed user forever (the old cache lived for the process's
    # whole life); but a per-request DB lookup is wasteful for a fixed user.
    _USER_TTL_S = 300.0

    def __init__(self, get_response: MiddlewareFunc) -> None:
        if not settings.AUTO_LOGIN_USER:
            raise MiddlewareNotUsed()

        self.get_response = get_response
        self._user: User | None = None
        self._user_ts: float = 0.0

    def _get_user(self) -> User | None:
        # Cache the lookup: this runs on every request.
        import time as _time

        if self._user is not None and (_time.monotonic() - self._user_ts) < self._USER_TTL_S:
            return self._user

        spec = settings.AUTO_LOGIN_USER
        if not spec:
            return None
        assert isinstance(spec, str)

        user = User.objects.filter(username=spec).first() or User.objects.filter(
            email=spec
        ).first()
        if user is None:
            # Fresh deployment: create the user (plus default project/check)
            # so the dashboard works immediately.
            from hc.accounts.views import _make_user

            email = spec if "@" in spec else f"{spec}@localhost"
            try:
                user = _make_user(email)
                if "@" not in spec:
                    user.username = spec
                    user.save()
            except Exception:
                logger.exception("auto-login user creation failed")
                return None

        self._user = user
        self._user_ts = _time.monotonic()
        return user

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return self.get_response(request)

        user = self._get_user()
        if user is not None:
            # Annotate the user with its backend so auth.login() works
            # even when multiple AUTHENTICATION_BACKENDS are configured.
            backend = settings.AUTHENTICATION_BACKENDS[0]
            user.backend = backend  # type: ignore[attr-defined]
            request.user = user
            auth.login(request, user)

        return self.get_response(request)


class CustomHeaderMiddleware:
    """
    Middleware for utilizing Web-server-provided authentication.

    If request.user is not authenticated, then this middleware:
    - looks for an email address in request.META[settings.REMOTE_USER_HEADER]
    - looks up and automatically logs in the user with a matching email

    """

    def __init__(self, get_response: MiddlewareFunc) -> None:
        if not settings.REMOTE_USER_HEADER:
            raise MiddlewareNotUsed()

        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        assert settings.REMOTE_USER_HEADER
        # Make sure AuthenticationMiddleware is installed
        assert hasattr(request, "user")

        email = request.META.get(settings.REMOTE_USER_HEADER)
        if not email:
            # If specified header doesn't exist or is empty then log out any
            # authenticated user and return
            if request.user.is_authenticated:
                auth.logout(request)
            return self.get_response(request)

        # The email address from the external authentication system may be in
        # in upper case or mixed case. Convert it to lower case, as we do
        # elsewhere in the system (when registering new users, when inviting users
        # into projects, when changing email address) to avoid naming conflicts.
        email = email.lower()

        # If the user is already authenticated and that user is the user we are
        # getting passed in the headers, then the correct user is already
        # persisted in the session and we don't need to continue.
        if request.user.is_authenticated:
            if request.user.email == email:
                return self.get_response(request)
            else:
                # An authenticated user is associated with the request, but
                # it does not match the authorized user in the header.
                auth.logout(request)

        # We are seeing this user for the first time in this session, attempt
        # to authenticate the user.
        if user := auth.authenticate(request, remote_user_email=email):
            # User is valid.  Set request.user and persist user in the session
            # by logging the user in.
            request.user = user
            auth.login(request, user)

        return self.get_response(request)
