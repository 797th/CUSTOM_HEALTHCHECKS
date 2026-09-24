from __future__ import annotations

from django.contrib.auth.models import User
from django.test.utils import override_settings

from hc.accounts.models import Project
from hc.test import BaseTestCase


@override_settings(AUTO_LOGIN_USER="autouser")
class AutoLoginMiddlewareTestCase(BaseTestCase):
    """The web UI auto-authenticates as AUTO_LOGIN_USER when set."""

    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create(username="autouser", email="auto@example.org")

    def test_it_logs_in_as_configured_user(self) -> None:
        r = self.client.get("/")
        # The index page renders the projects list for the auto-logged-in user:
        self.assertContains(r, "My Projects", status_code=200)
        # The account menu shows the auto-login user's email:
        self.assertContains(r, "auto@example.org", status_code=200)

    def test_it_matches_by_username_then_email(self) -> None:
        # An existing user matched by email works too
        with override_settings(AUTO_LOGIN_USER="auto@example.org"):
            r = self.client.get("/accounts/profile/")
            self.assertEqual(r.status_code, 200)

    def test_it_creates_missing_user_on_fresh_deployment(self) -> None:
        self.user.delete()
        self.assertFalse(User.objects.filter(username="autouser").exists())

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        user = User.objects.get(username="autouser")
        # The created user gets a default project:
        self.assertTrue(Project.objects.filter(owner=user).exists())

    def test_it_does_not_hijack_an_authenticated_session(self) -> None:
        # Login as alice (BaseTestCase's default user), visit the site:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get("/")
        # Alice stays alice -- the middleware does not switch users:
        self.assertContains(r, "alice@example.org", status_code=200)
        # And the auto-login user was not additionally created/used:
        self.assertEqual(User.objects.filter(email="auto@example.org").count(), 1)

    def test_it_is_not_installed_when_unset(self) -> None:
        from hc.accounts.middleware import AutoLoginMiddleware

        with override_settings(AUTO_LOGIN_USER=None):
            with self.assertRaises(Exception) as ctx:
                AutoLoginMiddleware(lambda request: None)
            self.assertIn("MiddlewareNotUsed", str(type(ctx.exception)))


@override_settings(AUTO_LOGIN_USER=None)
class AutoLoginDisabledTestCase(BaseTestCase):
    def test_login_form_still_required(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("accounts/login", r["Location"])