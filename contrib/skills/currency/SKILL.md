---
name: currency
description: Convert between currencies using ECB daily rates via open.er-api.com. Supports common name aliases (dong, yen, pound). No API key needed. Use for any currency conversion or exchange rate question.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: currency
---

## When to use

- "Convert 100 USD to VND" → `currency 100 USD VND`
- "How much is 5 million dong in pounds?" → `currency 5000000 dong pounds`
- "Dollar to dong rate" → `currency rates USD VND`
- "What's the exchange rate for GBP?" → `currency rates GBP`

## Commands

```
currency <amount> <from> <to>       convert (aliases resolve: dong, yen, baht, ...)
currency rates <base>               rates against the default target set
currency rates <base> VND,GBP,JPY   rates against specific currencies
```

Common names resolve automatically: dong/dollar/euro/pound/sterling/yen/baht/yuan/rmb/won/rupee/ringgit/peso/franc/loonie and the ISO codes. Unknown names pass through as codes, so obscure ISO codes work too.

## Interpreting output

Conversions show the rate in both directions and the update date. Quote the converted amount plainly, and include the rate when the user asked about rates rather than an amount.

Accuracy caveat to pass on when it matters: rates are ECB daily reference rates, updated weekdays around 16:00 CET, so they lag live forex by up to a day. Fine for "how much is dinner in pounds", not for trading.
