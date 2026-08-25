# currency: setup and notes

Currency conversion via open.er-api.com (ECB daily reference rates).

## Setup

None. No API key, no configuration. Install and it works.

## Notes

- Rates update weekdays around 16:00 CET and lag live forex by up to a day.
- Default rate targets: USD, EUR, GBP, JPY, VND, THB, HKD, SGD, AUD, TWD.
  Edit `DEFAULT_TARGETS` in `scripts/currency.py` to change them.
- Purely on-demand; no cron jobs.
