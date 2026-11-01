"""TND through Konnect, Tunisian VAT and stamp duty (TN-02, `payments-tunisia.md`).

The dinar has three decimals and is priced by conversion, rounded to the whole
dinar (BUILD-PLAN L-4, amended 2026-11-01). The rate is left blank on purpose:
TND is not on sale until staff type it in admin. Visitors connected from
Tunisia are shown it (`market_country`). `get_or_create` only, so an edited row
is never overwritten. Off under test settings, like every seed.
"""

import datetime as dt
from decimal import Decimal

from django.db import migrations

from common.seeding import seeding_enabled

SINCE = dt.date(2018, 1, 1)


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    apps.get_model("billing", "Currency").objects.get_or_create(
        code="TND",
        defaults={
            "name": "Tunisian dinar",
            "symbol": "DT",
            "minor_units": 3,
            "symbol_first": False,
            "gateway": "konnect",
            "round_to_minor": 1000,
            "market_country": "TN",
        },
    )
    apps.get_model("billing", "TaxRate").objects.get_or_create(
        country="TN", valid_from=SINCE, defaults={"rate": Decimal("0.1900"), "label": "TVA"}
    )
    apps.get_model("billing", "TaxStamp").objects.get_or_create(
        country="TN", valid_from=SINCE, defaults={"amount_minor": 1000, "label": "Droit de timbre"}
    )


class Migration(migrations.Migration):
    dependencies = [("billing", "0030_tn_payments")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
