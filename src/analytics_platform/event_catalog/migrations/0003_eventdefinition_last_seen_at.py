from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("event_catalog", "0002_alter_eventdefinition_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="eventdefinition",
            name="last_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
