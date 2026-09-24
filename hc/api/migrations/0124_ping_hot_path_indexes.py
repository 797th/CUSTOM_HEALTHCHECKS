from django.db import migrations, models


class Migration(migrations.Migration):
    """Composite indexes for the hot ping/flip paths.

    The ping log endpoints filter by (owner, n) and (owner, created); the
    pruning logic scans by (owner, created). Without composite indexes,
    Postgres falls back to the owner_id index + filter, which degrades
    once a check has tens of thousands of pings.
    """

    dependencies = [
        ("api", "0123_alter_channel_kind"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="ping",
            index=models.Index(
                fields=["owner", "n"], name="api_ping_owner_n_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="ping",
            index=models.Index(
                fields=["owner", "created"], name="api_ping_owner_created_idx"
            ),
        ),
    ]