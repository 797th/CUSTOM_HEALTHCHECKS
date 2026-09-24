"""Auto-create checks on ping to unknown URLs.

This module powers the "ping to unknown URL" feature: pinging a ping URL
that does not correspond to an existing check can *create* the check on the
spot, so a monitoring URL pasted into infrastructure code never breaks.

Two flows are supported:

* Slug-based URLs (`/ping/<ping_key>/<slug>`): the default behavior is to
  auto-create the check on the first ping. The caller can tune the created
  check with query parameters (`name`, `period`, `grace`, `tags`, ...), or
  pass `create=0` to get the classic strict 404 behavior.
* UUID-based URLs (`/ping/<uuid>`): with `?create=1`, a check with exactly
  that UUID gets created, so the URL remains valid forever. This is aimed
  at setups where the UUID is baked into long-lived infrastructure.

Creation is race-safe: it happens inside a transaction that locks the
project row (serializing concurrent creators) and relies on the
(project_id, slug) unique constraint with an IntegrityError fallback, so
concurrent first-pings end up with one check and both pings recorded.
"""

from __future__ import annotations

from datetime import timedelta as td

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from hc.accounts.models import Profile, Project
from hc.api.models import Check

# Period/grace bounds: same as the web UI and the Management API
# (hc.front.forms uses 60..31536000 for both fields).
MIN_PERIOD_S = 60
MAX_PERIOD_S = 31536000  # 365 days

# Sanity cap on the number of extra query params a client can stuff into
# an auto-create ping: protects the endpoint from junk-parameter DoS.
MAX_QUERY_PARAMS = 50


def _coerce_int(value: str | None) -> int | None:
    """Parse an integer query value, or None if absent/invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_auto_create_params(request: HttpRequest) -> dict[str, object]:
    """Extract check-creation parameters from ping URL query string.

    Returns a dict with the recognized keys only ("unknown" keys are
    ignored, not errors — ping URLs must never become a failure point).
    Values that fail validation are dropped and replaced by defaults
    (a bad `period` must never break the ping).

    Query parameters:

    * name — display name for the check (defaults to the slug)
    * period — expected period in seconds (60..31536000, clamped)
    * grace — grace time in seconds (60..31536000, clamped)
    * tags — space-separated tag string
    * desc — description text
    * channels — "*" (all project channels), "" (none) or comma-separated
      channel names/codes (see the management API docs)
    """
    q = request.GET
    result: dict[str, object] = {}
    if len(q) > MAX_QUERY_PARAMS:
        return result

    name = q.get("name")
    if name:
        result["name"] = name[:100]

    period = _coerce_int(q.get("period"))
    if period is not None:
        result["timeout"] = td(seconds=min(max(period, MIN_PERIOD_S), MAX_PERIOD_S))

    grace = _coerce_int(q.get("grace"))
    if grace is not None:
        result["grace"] = td(seconds=min(max(grace, MIN_PERIOD_S), MAX_PERIOD_S))

    tags = q.get("tags")
    if tags:
        result["tags"] = tags[:500]

    desc = q.get("desc")
    if desc:
        result["desc"] = desc[:2000]

    channels = q.get("channels")
    if channels is not None:
        result["channels"] = channels[:500]

    return result


def _resolve_channels(project: Project, spec: str) -> list | None:
    """Resolve the `channels` query parameter to a list of channels.

    Mirrors the management API's Spec.channels semantics:
    "*" = all project's channels, "" = none, otherwise a comma-separated
    list of channel names or codes (unknown identifiers raise).
    """
    if spec == "*":
        return list(project.channel_set.all())
    if spec == "":
        return []
    available = list(project.channel_set.all())
    channels: dict[int, object] = {}
    for s in spec.split(","):
        if s == "":
            raise ValueError("empty channel identifier")
        matches = [c for c in available if str(c.code) == s or c.name == s]
        if len(matches) == 0:
            raise ValueError(f"invalid channel identifier: {s}")
        channels[matches[0].id] = matches[0]
    return list(channels.values())


def _check_limit_allows(profile: Profile, num_checks_used: int) -> bool:
    """The 2x over-limit grace for auto-provisioning, matching upstream docs."""
    return num_checks_used < profile.check_limit * 2

def create_check_for_ping(
    project: Project,
    *,
    code=None,
    slug: str | None = None,
    name: str | None = None,
    params: dict[str, object] | None = None,
) -> Check:
    """Create a new check in `project` for a ping-triggered auto-create.

    The caller must already hold a row lock on `project`
    (Project.objects.select_for_update().get(id=project.id)) — this
    serializes concurrent auto-creates and makes the check-count limit
    check race-free.

    If `code` is given, the check is created with that exact UUID
    (uuid-ping auto-create). Otherwise a fresh UUID is generated.

    `slug`, if given, is stored as the check's slug; `name` falls back to
    the slug, then to the code. Remaining check fields come from
    `params` (see parse_auto_create_params).

    Raises IntegrityError if a check with this (project, slug) or code
    already exists — callers treat that as "someone else created it
    first" and re-fetch.
    """
    params = params or {}
    check = Check(project=project)
    if code is not None:
        check.code = code
    if slug is not None:
        check.slug = slug
    check.name = str(params.get("name") or name or slug or str(check.code))[:100]
    if "timeout" in params:
        check.timeout = params["timeout"]  # type: ignore[assignment]
    if "grace" in params:
        check.grace = params["grace"]  # type: ignore[assignment]
    if "tags" in params:
        check.tags = str(params["tags"])
    if "desc" in params:
        check.desc = str(params["desc"])
    check.save()

    if "channels" in params:
        try:
            channels = _resolve_channels(project, str(params["channels"]))
        except ValueError:
            channels = None
        if channels is not None:
            check.channel_set.set(channels)
    else:
        # Default: the new check uses all the project's integrations.
        check.assign_all_channels()

    return check


def get_or_create_check_for_project(
    project: Project,
    *,
    slug: str,
    params: dict[str, object] | None = None,
) -> tuple[Check | None, bool]:
    """Fetch the check with `slug` in `project`, creating it if missing.

    Returns (check, created). Returns (None, False) when the project is at
    its auto-provisioning check limit.

    Race safety: takes a select_for_update() lock on the project row so
    concurrent first-pings serialize; the unique-ish (project_id, slug)
    index plus IntegrityError handling covers any remaining interleaving
    (the loser of the race re-fetches the winner's check).
    """
    created = False
    try:
        check = Check.objects.get(slug=slug, project=project)
        return check, False
    except Check.DoesNotExist:
        pass
    except Check.MultipleObjectsReturned:
        # Ambiguous slug: the caller (ping_by_slug) turns this into a 409.
        raise

    params = params or {}
    with transaction.atomic():
        # Lock the project row: serializes concurrent auto-creates.
        locked_project = Project.objects.select_for_update().get(id=project.id)
        profile = locked_project.owner_profile
        num_used = locked_project.check_set.count()
        if not _check_limit_allows(profile, num_used):
            return None, False

        try:
            check = create_check_for_ping(locked_project, slug=slug, params=params)
            created = True
        except IntegrityError:
            # A concurrent request created it first. Re-fetch outside the
            # transaction: the winner of the race owns the check.
            pass

    if not created:
        try:
            check = Check.objects.get(slug=slug, project=project)
            return check, False
        except Check.DoesNotExist:
            # Lost the race AND the winner rolled back (or limit raced):
            # report the limit failure rather than fabricating a check.
            return None, False

    return check, True