#!/usr/bin/env python3
"""
Currency converter using open.er-api.com (free, no key, ECB daily rates).

Usage:
  currency.py 100 USD VND           — convert amount
  currency.py rates USD              — show common rates from USD
  currency.py rates USD VND,GBP,JPY  — show specific rates
"""
import json
import sys
import urllib.error
import urllib.request

BASE_URL = "https://open.er-api.com/v6"
USER_AGENT = "faffmonkey"

# Default targets for rate display
DEFAULT_TARGETS = ["USD", "EUR", "GBP", "JPY", "VND", "THB", "HKD", "SGD", "AUD", "TWD"]

# Common name aliases
ALIASES = {
    "dong": "VND", "vnd": "VND",
    "dollar": "USD", "dollars": "USD", "usd": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "gbp": "GBP",
    "yen": "JPY", "jpy": "JPY",
    "baht": "THB", "thb": "THB",
    "rmb": "CNY", "yuan": "CNY", "renminbi": "CNY", "cny": "CNY",
    "won": "KRW", "krw": "KRW",
    "rupee": "INR", "rupees": "INR", "inr": "INR",
    "ringgit": "MYR", "myr": "MYR",
    "peso": "MXN", "mxn": "MXN",
    "franc": "CHF", "chf": "CHF",
    "aud": "AUD", "aussie": "AUD",
    "cad": "CAD", "loonie": "CAD",
    "hkd": "HKD",
    "sgd": "SGD",
    "twd": "TWD", "ntd": "TWD",
    "rub": "RUB", "ruble": "RUB",
}


def resolve_currency(name):
    """Resolve a currency name/alias to ISO code."""
    upper = name.strip().upper()
    lower = name.strip().lower()
    if lower in ALIASES:
        return ALIASES[lower]
    return upper


def _fetch_rates(base_cur):
    """Fetch all rates for a base currency. Returns (rates_dict, date_str) or raises."""
    url = f"{BASE_URL}/latest/{base_cur}"
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("result") != "success":
        error_type = data.get("error-type", "unknown")
        raise ValueError(f"API error: {error_type}")

    rates = data.get("rates", {})
    date_str = data.get("time_last_update_utc", "unknown")
    return rates, date_str


def convert(amount, from_cur, to_cur):
    """Convert amount between currencies. Returns result dict."""
    from_code = resolve_currency(from_cur)
    to_code = resolve_currency(to_cur)

    try:
        rates, date_str = _fetch_rates(from_code)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": f"Unknown currency: {from_code}"}
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

    if to_code not in rates:
        return {"error": f"Unknown target currency: {to_code}"}

    rate = rates[to_code]
    result = amount * rate
    return {
        "amount": amount,
        "from": from_code,
        "to": to_code,
        "rate": rate,
        "reverse_rate": 1 / rate if rate else 0,
        "result": result,
        "date": date_str,
    }


def get_rates(base_cur, targets=None):
    """Get rates from base currency to specified targets."""
    base_code = resolve_currency(base_cur)
    if targets is None:
        targets = DEFAULT_TARGETS

    # Resolve target aliases
    target_codes = [resolve_currency(t) for t in targets]
    # Remove base from targets
    target_codes = [t for t in target_codes if t != base_code]

    try:
        all_rates, date_str = _fetch_rates(base_code)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": f"Unknown currency: {base_code}"}
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

    # Filter to requested targets only
    filtered = {}
    missing = []
    for code in target_codes:
        if code in all_rates:
            filtered[code] = all_rates[code]
        else:
            missing.append(code)

    return {
        "base": base_code,
        "rates": filtered,
        "date": date_str,
        "missing": missing if missing else None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  currency.py <amount> <from> <to>          — convert")
        print("  currency.py rates <base>                   — common rates")
        print("  currency.py rates <base> VND,GBP,JPY       — specific rates")
        print()
        print("Examples:")
        print("  currency.py 100 USD VND")
        print("  currency.py 5000000 dong pounds")
        print("  currency.py rates GBP")
        sys.exit(1)

    if sys.argv[1] == "rates":
        base = sys.argv[2] if len(sys.argv) > 2 else "USD"
        targets = None
        if len(sys.argv) > 3:
            targets = [t.strip() for t in sys.argv[3].split(",")]

        result = get_rates(base, targets)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)

        print(f"Rates from {result['base']} ({result['date']}):")
        for cur, rate in sorted(result["rates"].items()):
            reverse = 1 / rate if rate else 0
            # Format: show more decimals for large rates (VND), fewer for small
            if rate >= 100:
                print(f"  {cur}: {rate:>12,.2f}  (1 {cur} = {reverse:.6f} {result['base']})")
            elif rate >= 1:
                print(f"  {cur}: {rate:>12,.4f}  (1 {cur} = {reverse:.4f} {result['base']})")
            else:
                print(f"  {cur}: {rate:>12,.6f}  (1 {cur} = {reverse:,.2f} {result['base']})")

        if result.get("missing"):
            print(f"\nNot found: {', '.join(result['missing'])}")

    else:
        try:
            amount = float(sys.argv[1])
        except ValueError:
            print(f"Not a number: {sys.argv[1]}")
            sys.exit(1)

        if len(sys.argv) < 4:
            print("Need: currency.py <amount> <from> <to>")
            sys.exit(1)

        from_cur = sys.argv[2]
        to_cur = sys.argv[3]
        result = convert(amount, from_cur, to_cur)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)

        # Smart formatting based on result magnitude
        if result['result'] >= 1000:
            result_str = f"{result['result']:,.0f}"
        elif result['result'] >= 1:
            result_str = f"{result['result']:,.2f}"
        else:
            result_str = f"{result['result']:.4f}"

        amount_str = f"{result['amount']:,.2f}" if result['amount'] >= 1 else f"{result['amount']:.4f}"

        print(f"{amount_str} {result['from']} = {result_str} {result['to']}")
        print(f"  Rate: 1 {result['from']} = {result['rate']:,.4f} {result['to']}")
        print(f"  Rate: 1 {result['to']} = {result['reverse_rate']:.6f} {result['from']}")
        print(f"  Updated: {result['date']}")
