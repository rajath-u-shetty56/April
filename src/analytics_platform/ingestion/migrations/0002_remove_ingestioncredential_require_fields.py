from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ingestion", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ingestioncredential",
            name="require_account",
        ),
        migrations.RemoveField(
            model_name="ingestioncredential",
            name="require_product",
        ),
    ]
