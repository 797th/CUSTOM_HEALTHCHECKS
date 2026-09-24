# Auto Provisioning

You can instruct SITE_NAME to automatically create missing checks on the first
received ping. With slug-based ping endpoints, auto provisioning is **on by default**
in this fork: just send the ping, and the check will be created on the spot:

```bash
# Do some work
sleep 5
# Send success signal to SITE_NAME
curl -m 10 --retry 5 PING_ENDPOINTmy-ping-key/srv01
```

In this example, SITE_NAME will look up project with the Ping Key `my-ping-key`,
and check if a check with a slug `srv01` exists there.

* If the check does not exist yet, SITE_NAME will create it, ping it, and return
  an HTTP 201 response.
* If the check exists, SITE_NAME will ping it and return an HTTP 200 response.

Pass `create=0` on the ping URL if you want the classic strict behavior (HTTP 404
for unknown slugs, nothing created).

Auto provisioning works with all slug-based ping endpoints:

* [Success](../http_api/#success-slug)
* [Start](../http_api/#start-slug)
* [Failure](../http_api/#fail-slug)
* [Log](../http_api/#log-slug)
* [Exit status](../http_api/#exitcode-slug)

Auto provisioning is handy when working with dynamic infrastructure: if you distribute
the Ping Key to your monitoring clients, each client can pick its own slug
(for example, derived from the server's hostname), construct a ping URL, and
register with SITE_NAME "on the fly" while sending its first ping.

## Customizing Auto Provisioned Checks

You can tune the newly created check with query parameters. They are applied
**only at creation time** — they never modify an existing check, so existing ping
URLs keep working no matter what parameters they carry:

* `name` — display name (defaults to the slug)
* `period` — expected period in seconds, 60..31536000 (default: 1 day)
* `grace` — grace time in seconds, 60..31536000 (default: 1 hour)
* `tags` — space-separated tag string
* `desc` — description text
* `channels` — `*` (all project channels), empty (none), or comma-separated
  channel names/codes

Example:

```bash
curl -m 10 --retry 5 "PING_ENDPOINTmy-ping-key/srv01?name=SRV01%20backup&period=300&grace=120&tags=prod"
```

This creates a check named "SRV01 backup", expected to be pinged every 5 minutes,
with a 2-minute grace time, tagged "prod".

## UUID-based Ping URLs

The check's UUID-based ping URL (`/ping/<uuid>`) does not carry any project
information, so auto provisioning for UUID URLs is opt-in and requires extra
configuration: set the `AUTO_PROVISION_USER` environment variable to the username
of the account that should own auto-created checks (typically a single dedicated
monitoring account).

With that in place, append `?create=1` to the UUID ping URL:

```bash
curl -m 10 --retry 5 "PING_ENDPOINT<uuid>?create=1&name=nightly-job&period=86400&grace=3600"
```

SITE_NAME will create a check with exactly that UUID, so the ping URL baked into
your infrastructure stays valid forever.

## Auto Provisioning and Account Limits

Each SITE_NAME account has a specific limit of how many checks it is allowed to
create: 20 checks for free accounts; 100 or 1000 checks for paid accounts. To reduce
friction and the risk of silent failures, the auto provisioning functionality
**is allowed to temporarily exceed the account's check limit up to two times**.
Meaning, if your account is already maxed out, auto provisioning will still be able to
create new checks until you hit two times the limit.