# unit-converter: setup and notes

Offline unit conversion. No API, no key, no configuration.

## Notes

- Ingredient densities (60+ entries, grams per US cup) live in the
  `INGREDIENTS` table in `scripts/convert.py`; add your own there.
- Spoon-and-level densities, except brown sugar which assumes packed.
- Purely on-demand; no cron jobs.
