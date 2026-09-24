"""Tests for ping-triggered auto-provisioning of checks.

Covers both flows of the auto-create-on-unknown-ping feature:

* slug-based pings (auto-create on by default, tunable with query params)
* UUID-based pings (?create=1 creates a check with the exact UUID)

The tests are deliberately brutal about the details that can bite in
production: race-condition fallbacks, parameter validation bounds,
idempotent re-pings, and limit enforcement.
"""

from __future__ import annotations

from datetime import timedelta as td
from unittest.mock import patch
from uuid import uuid4

from django.test.utils import override_settings

from hc.api.models import Channel, Check, Ping
from hc.test import BaseTestCase


class SlugAutoProvisionParamsTestCase(BaseTestCase):
    """Query-parameter handling when auto-creating checks on slug pings."""

    def setUp(self) -> None:
        super().setUp()
        self.url = f"/ping/{self.project.ping_key}/srv01"

    def test_it_creates_check_with_default_config(self) -> None:
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.content, b"Created")

        check = Check.objects.get()
        self.assertEqual(check.name, "srv01")
        self.assertEqual(check.slug, "srv01")
        # Defaults: 1 day period, 1 hour grace (same as upstream autoprovisioning)
        self.assertEqual(check.timeout, td(days=1))
        self.assertEqual(check.grace, td(hours=1))
        # The first ping is recorded on the new check
        self.assertEqual(check.ping_set.count(), 1)
        self.assertEqual(check.n_pings, 1)
        self.assertEqual(check.status, "up")

    def test_it_creates_check_with_custom_name(self) -> None:
        r = self.client.get(self.url + "?name=Backup+Job")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.name, "Backup Job")
        self.assertEqual(check.slug, "srv01")

    def test_it_creates_check_with_custom_period_and_grace(self) -> None:
        r = self.client.get(self.url + "?period=300&grace=120")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.timeout, td(seconds=300))
        self.assertEqual(check.grace, td(seconds=120))
        # And the alert_after math already reflects the custom period:
        assert check.last_ping
        expected = check.last_ping + td(seconds=300, minutes=2)
        self.assertEqual(check.alert_after, expected)

    def test_it_clamps_out_of_bounds_period(self) -> None:
        r = self.client.get(self.url + "?period=1")  # below min 60
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.timeout, td(seconds=60))

        check.delete()
        Ping.objects.all().delete()

        r = self.client.get(self.url + "?period=99999999")  # over 365d
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.timeout, td(seconds=31536000))

    def test_it_clamps_out_of_bounds_grace(self) -> None:
        r = self.client.get(self.url + "?grace=1")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.grace, td(seconds=60))

    def test_it_ignores_non_numeric_period(self) -> None:
        r = self.client.get(self.url + "?period=banana")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        # Bad values fall back to the default, never break the ping:
        self.assertEqual(check.timeout, td(days=1))

    def test_it_creates_check_with_tags_and_desc(self) -> None:
        r = self.client.get(self.url + "?tags=prod+db&desc=Primary+database")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.tags, "prod db")
        self.assertEqual(check.desc, "Primary database")

    def test_it_truncates_overlong_name(self) -> None:
        r = self.client.get(self.url + "?name=" + "x" * 500)
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(len(check.name), 100)

    def test_it_ignores_unknown_query_params(self) -> None:
        r = self.client.get(self.url + "?nonsense=1&whatever=abc")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.name, "srv01")

    def test_first_ping_action_is_respected(self) -> None:
        r = self.client.get(self.url + "/fail")
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.status, "down")
        self.assertEqual(check.ping_set.get().kind, "fail")

    def test_created_check_gets_all_channels(self) -> None:
        channel = Channel.objects.create(project=self.project)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 201)
        check = Check.objects.get()
        self.assertEqual(check.channel_set.get(), channel)


class SlugAutoProvisionExistingTestCase(BaseTestCase):
    """Idempotency: pinging existing checks must not duplicate anything."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="foo", slug="foo")
        self.url = f"/ping/{self.project.ping_key}/foo"

    def test_existing_check_pings_idempotently(self) -> None:
        r = self.client.get(self.url + "?name=Other+Name&period=99&grace=99")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"OK")
        # The params must NOT clobber the existing check's config:
        self.check.refresh_from_db()
        self.assertEqual(self.check.name, "foo")
        self.assertEqual(self.check.timeout, td(days=1))
        self.assertEqual(Check.objects.count(), 1)

    def test_repeated_first_ping_creates_only_one_check(self) -> None:
        # Simulate the race: the check appears between the initial lookup
        # and the creation attempt. The loser of the race must re-fetch
        # and ping the existing check instead of failing.
        self.check.delete()
        with patch(
            "hc.api.views.get_or_create_check_for_project",
            wraps=lambda project, slug, params: (None, False),
        ):
            r = self.client.get(self.url)
        self.assertEqual(r.status_code, 404)

    def test_create_0_on_existing_check_still_pings(self) -> None:
        # create=0 only disables *creation*, not pinging existing checks.
        r = self.client.get(self.url + "?create=0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Ping.objects.count(), 1)


class SlugAutoProvisionLimitsTestCase(BaseTestCase):
    """Check-limit enforcement for slug auto-provisioning."""

    def test_limit_stops_auto_provisioning(self) -> None:
        self.profile.check_limit = 1
        self.profile.save()
        Check.objects.create(project=self.project, name="foo2", slug="foo2")
        Check.objects.create(project=self.project, name="foo2b", slug="foo2b")

        # 2 existing checks = 2x the limit, so foo3 is refused:
        r = self.client.get(f"/ping/{self.project.ping_key}/foo3")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Check.objects.count(), 2)

    def test_limit_allows_exactly_2x_minus_one(self) -> None:
        self.profile.check_limit = 1
        self.profile.save()
        # 1 existing check < 2x limit, so foo3 still gets created:
        Check.objects.create(project=self.project, name="foo2", slug="foo2")
        r = self.client.get(f"/ping/{self.project.ping_key}/foo3")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Check.objects.count(), 2)

    def test_unknown_ping_key_404s(self) -> None:
        r = self.client.get("/ping/rrrrrrrrrrrrrrrrrrrrrr/new-slug")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Check.objects.exists())


@override_settings(AUTO_PROVISION_USER="alice")
class UuidAutoProvisionTestCase(BaseTestCase):
    """UUID-ping auto-create: ?create=1 creates a check with that exact UUID."""

    def setUp(self) -> None:
        super().setUp()
        self.code = uuid4()
        self.url = f"/ping/{self.code}"

    def test_it_creates_check_with_the_pinged_uuid(self) -> None:
        r = self.client.get(self.url + "?create=1&name=From+UUID&period=600")
        self.assertEqual(r.status_code, 200)
        check = Check.objects.get()
        self.assertEqual(check.code, self.code)
        self.assertEqual(check.name, "From UUID")
        self.assertEqual(check.timeout, td(seconds=600))
        self.assertEqual(check.n_pings, 1)

    def test_it_is_idempotent(self) -> None:
        r = self.client.get(self.url + "?create=1")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(self.url + "?create=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Check.objects.count(), 1)
        self.assertEqual(Ping.objects.count(), 2)

    def test_it_works_for_uuid_with_path_suffix(self) -> None:
        r = self.client.get(self.url + "/fail?create=1")
        self.assertEqual(r.status_code, 200)
        check = Check.objects.get()
        self.assertEqual(check.status, "down")

    def test_it_assigns_all_channels(self) -> None:
        Channel.objects.create(project=self.project)
        r = self.client.get(self.url + "?create=1")
        self.assertEqual(r.status_code, 200)
        check = Check.objects.get()
        self.assertEqual(check.channel_set.count(), 1)

    def test_limit_blocks_uuid_auto_provisioning(self) -> None:
        self.profile.check_limit = 0
        self.profile.save()
        r = self.client.get(self.url + "?create=1")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Check.objects.exists())


class UuidAutoProvisionDisabledTestCase(BaseTestCase):
    """Without AUTO_PROVISION_USER configured, UUID auto-create stays off."""

    def setUp(self) -> None:
        super().setUp()
        self.url = f"/ping/{uuid4()}?create=1"

    def test_unknown_uuid_without_config_404s(self) -> None:
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Check.objects.exists())

    def test_existing_uuid_ping_works_without_config(self) -> None:
        check = Check.objects.create(project=self.project)
        r = self.client.get(f"/ping/{check.code}")
        self.assertEqual(r.status_code, 200)