#!/usr/bin/env python3
"""
Unit converter with ingredient-aware density conversions.

Standard conversions (3-arg):
  convert.py 100 kg lb
  convert.py 72 f c
  convert.py 500 gb tb

Ingredient conversions (4-arg):
  convert.py 2 cups g flour
  convert.py 250 g cups sugar
  convert.py 1 cups g butter

Usage:
  convert.py <value> <from_unit> <to_unit> [ingredient]
"""
import sys

# --- Ingredient density table ---
# Grams per US cup (236.588 ml), sourced from King Arthur Baking,
# FAO Density Database, and consensus across major baking references.
# "spoon and level" method unless noted.
INGREDIENTS = {
    # Flours
    "flour":              125,   # all-purpose, spoon & level
    "all-purpose-flour":  125,
    "ap-flour":           125,
    "bread-flour":        130,
    "cake-flour":         115,
    "pastry-flour":       115,
    "whole-wheat-flour":  128,
    "ww-flour":           128,
    "almond-flour":       96,
    "coconut-flour":      128,
    "rye-flour":          102,
    "semolina":           167,
    "cornstarch":         128,
    "cornflour":          128,

    # Sugars
    "sugar":              200,   # granulated white
    "granulated-sugar":   200,
    "caster-sugar":       200,
    "brown-sugar":        220,   # packed
    "powdered-sugar":     120,
    "icing-sugar":        120,
    "confectioners-sugar":120,
    "demerara-sugar":     220,
    "coconut-sugar":      180,
    "maple-syrup":        312,
    "honey":              340,
    "molasses":           328,
    "golden-syrup":       340,
    "corn-syrup":         328,

    # Fats
    "butter":             227,
    "margarine":          227,
    "coconut-oil":        218,
    "vegetable-oil":      218,
    "olive-oil":          216,
    "lard":               205,
    "shortening":         191,
    "ghee":               224,

    # Dairy
    "milk":               245,
    "cream":              238,
    "heavy-cream":        238,
    "sour-cream":         230,
    "yogurt":             245,
    "cream-cheese":       232,
    "ricotta":            246,

    # Grains & starches
    "rice":               195,   # uncooked white
    "white-rice":         195,
    "brown-rice":         190,
    "oats":               90,    # rolled/old-fashioned
    "rolled-oats":        90,
    "quick-oats":         80,
    "quinoa":             170,
    "couscous":           173,
    "breadcrumbs":        120,
    "panko":              60,
    "cornmeal":           163,
    "polenta":            163,

    # Nuts & seeds
    "almonds":            143,   # whole
    "walnuts":            120,   # halves
    "pecans":             110,   # halves
    "cashews":            137,
    "peanuts":            146,
    "pine-nuts":          135,
    "sunflower-seeds":    140,
    "sesame-seeds":       144,
    "chia-seeds":         170,
    "flax-seeds":         150,
    "pumpkin-seeds":      130,
    "peanut-butter":      258,
    "almond-butter":      256,

    # Cocoa & chocolate
    "cocoa":              85,
    "cocoa-powder":       85,
    "chocolate-chips":    170,

    # Dried fruit
    "raisins":            165,
    "dried-cranberries":  160,
    "dates":              178,   # chopped

    # Misc
    "salt":               288,
    "baking-powder":      230,
    "baking-soda":        230,
    "yeast":              150,   # instant dry
    "gelatin":            150,
    "protein-powder":     120,
    "matcha":             120,
    "water":              237,
}

# Ingredient aliases — normalise common names
INGREDIENT_ALIASES = {
    "plain flour": "flour", "plain-flour": "flour", "ap flour": "ap-flour",
    "self-raising flour": "flour", "self-rising flour": "flour",
    "wholemeal flour": "whole-wheat-flour", "wholewheat flour": "whole-wheat-flour",
    "icing sugar": "icing-sugar", "powdered sugar": "powdered-sugar",
    "confectioners sugar": "confectioners-sugar",
    "brown sugar": "brown-sugar", "caster sugar": "caster-sugar",
    "maple syrup": "maple-syrup", "golden syrup": "golden-syrup",
    "corn syrup": "corn-syrup",
    "coconut oil": "coconut-oil", "vegetable oil": "vegetable-oil",
    "olive oil": "olive-oil",
    "coconut flour": "coconut-flour", "almond flour": "almond-flour",
    "rye flour": "rye-flour", "bread flour": "bread-flour",
    "cake flour": "cake-flour", "pastry flour": "pastry-flour",
    "cream cheese": "cream-cheese", "sour cream": "sour-cream",
    "heavy cream": "heavy-cream",
    "peanut butter": "peanut-butter", "almond butter": "almond-butter",
    "rolled oats": "rolled-oats", "quick oats": "quick-oats",
    "chocolate chips": "chocolate-chips", "cocoa powder": "cocoa-powder",
    "baking powder": "baking-powder", "baking soda": "baking-soda",
    "coconut sugar": "coconut-sugar", "protein powder": "protein-powder",
    "chia seeds": "chia-seeds", "flax seeds": "flax-seeds",
    "sunflower seeds": "sunflower-seeds", "sesame seeds": "sesame-seeds",
    "pumpkin seeds": "pumpkin-seeds", "pine nuts": "pine-nuts",
    "white rice": "white-rice", "brown rice": "brown-rice",
    "dried cranberries": "dried-cranberries",
}


def normalise_ingredient(name):
    """Normalise ingredient name to table key."""
    n = name.lower().strip().replace("_", " ")
    # Check aliases first
    if n in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[n]
    # Try hyphenated version
    hyphenated = n.replace(" ", "-")
    if hyphenated in INGREDIENTS:
        return hyphenated
    # Try as-is
    if n in INGREDIENTS:
        return n
    return None


# --- Standard unit conversions (unchanged) ---

CONVERSIONS = {
    # Distance
    ("km", "mi"): lambda x: x * 0.621371,
    ("mi", "km"): lambda x: x * 1.60934,
    ("m", "ft"): lambda x: x * 3.28084,
    ("ft", "m"): lambda x: x * 0.3048,
    ("cm", "in"): lambda x: x * 0.393701,
    ("in", "cm"): lambda x: x * 2.54,
    ("m", "yd"): lambda x: x * 1.09361,
    ("yd", "m"): lambda x: x * 0.9144,

    # Weight
    ("kg", "lb"): lambda x: x * 2.20462,
    ("lb", "kg"): lambda x: x * 0.453592,
    ("g", "oz"): lambda x: x * 0.035274,
    ("oz", "g"): lambda x: x * 28.3495,
    ("kg", "st"): lambda x: x * 0.157473,
    ("st", "kg"): lambda x: x * 6.35029,

    # Temperature
    ("c", "f"): lambda x: (x * 9 / 5) + 32,
    ("f", "c"): lambda x: (x - 32) * 5 / 9,
    ("c", "k"): lambda x: x + 273.15,
    ("k", "c"): lambda x: x - 273.15,
    ("f", "k"): lambda x: (x - 32) * 5 / 9 + 273.15,
    ("k", "f"): lambda x: (x - 273.15) * 9 / 5 + 32,

    # Volume
    ("l", "gal"): lambda x: x * 0.264172,
    ("gal", "l"): lambda x: x * 3.78541,
    ("ml", "floz"): lambda x: x * 0.033814,
    ("floz", "ml"): lambda x: x * 29.5735,
    ("l", "ml"): lambda x: x * 1000,
    ("ml", "l"): lambda x: x / 1000,

    # Cooking
    ("cups", "ml"): lambda x: x * 236.588,
    ("ml", "cups"): lambda x: x / 236.588,
    ("tbsp", "ml"): lambda x: x * 14.7868,
    ("ml", "tbsp"): lambda x: x / 14.7868,
    ("tsp", "ml"): lambda x: x * 4.92892,
    ("ml", "tsp"): lambda x: x / 4.92892,
    ("cups", "tbsp"): lambda x: x * 16,
    ("tbsp", "cups"): lambda x: x / 16,
    ("tbsp", "tsp"): lambda x: x * 3,
    ("tsp", "tbsp"): lambda x: x / 3,

    # Data storage
    ("gb", "tb"): lambda x: x / 1024,
    ("tb", "gb"): lambda x: x * 1024,
    ("mb", "gb"): lambda x: x / 1024,
    ("gb", "mb"): lambda x: x * 1024,
    ("kb", "mb"): lambda x: x / 1024,
    ("mb", "kb"): lambda x: x * 1024,
    ("tb", "pb"): lambda x: x / 1024,
    ("pb", "tb"): lambda x: x * 1024,

    # Speed
    ("kmh", "mph"): lambda x: x * 0.621371,
    ("mph", "kmh"): lambda x: x * 1.60934,
    ("ms", "kmh"): lambda x: x * 3.6,
    ("kmh", "ms"): lambda x: x / 3.6,
    ("knots", "kmh"): lambda x: x * 1.852,
    ("kmh", "knots"): lambda x: x / 1.852,
}

# Unit aliases
ALIASES = {
    "celsius": "c", "fahrenheit": "f", "kelvin": "k",
    "kilometers": "km", "kilometres": "km", "miles": "mi",
    "meters": "m", "metres": "m", "feet": "ft", "inches": "in",
    "yards": "yd", "centimeters": "cm", "centimetres": "cm",
    "kilograms": "kg", "pounds": "lb", "grams": "g",
    "ounces": "oz", "stone": "st", "stones": "st",
    "liters": "l", "litres": "l", "gallons": "gal",
    "milliliters": "ml", "millilitres": "ml",
    "cup": "cups", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp",
    "gigabytes": "gb", "terabytes": "tb", "megabytes": "mb",
    "kilobytes": "kb", "petabytes": "pb",
    "kph": "kmh", "km/h": "kmh", "mi/h": "mph",
    "m/s": "ms", "knot": "knots",
    "fluidounces": "floz", "fl_oz": "floz", "fluid_oz": "floz",
}

# Weight units that can participate in ingredient conversions
WEIGHT_UNITS = {"g", "kg", "oz", "lb"}
VOLUME_UNITS = {"cups", "tbsp", "tsp", "ml", "l"}


def normalise(unit):
    u = unit.lower().strip().replace(" ", "")
    return ALIASES.get(u, u)


def convert_ingredient(value, from_unit, to_unit, ingredient_key):
    """
    Convert between volume and weight using ingredient density.
    Returns (result_value, result_str) or (None, error_msg).
    """
    grams_per_cup = INGREDIENTS[ingredient_key]
    f = normalise(from_unit)
    t = normalise(to_unit)

    # Convert source to cups (if volume) or grams (if weight)
    cups_value = None
    grams_value = None

    # Source is volume → convert to cups first
    if f == "cups":
        cups_value = value
    elif f == "tbsp":
        cups_value = value / 16
    elif f == "tsp":
        cups_value = value / 48
    elif f == "ml":
        cups_value = value / 236.588
    elif f == "l":
        cups_value = value * 1000 / 236.588
    # Source is weight → convert to grams first
    elif f == "g":
        grams_value = value
    elif f == "kg":
        grams_value = value * 1000
    elif f == "oz":
        grams_value = value * 28.3495
    elif f == "lb":
        grams_value = value * 453.592
    else:
        return None, f"Unsupported unit for ingredient conversion: {f}"

    # Cross-convert via density
    if cups_value is not None and grams_value is None:
        grams_value = cups_value * grams_per_cup
    elif grams_value is not None and cups_value is None:
        cups_value = grams_value / grams_per_cup

    # Now convert grams/cups to target unit
    if t == "g":
        result = grams_value
    elif t == "kg":
        result = grams_value / 1000
    elif t == "oz":
        result = grams_value / 28.3495
    elif t == "lb":
        result = grams_value / 453.592
    elif t == "cups":
        result = cups_value
    elif t == "tbsp":
        result = cups_value * 16
    elif t == "tsp":
        result = cups_value * 48
    elif t == "ml":
        result = cups_value * 236.588
    elif t == "l":
        result = cups_value * 236.588 / 1000
    else:
        return None, f"Unsupported target unit for ingredient conversion: {t}"

    # Format
    if abs(result) >= 100:
        return result, f"{result:,.1f}"
    elif abs(result) >= 1:
        return result, f"{result:.2f}"
    else:
        return result, f"{result:.4f}"


def convert(value, from_unit, to_unit):
    """Standard unit conversion (no ingredient)."""
    f = normalise(from_unit)
    t = normalise(to_unit)
    key = (f, t)
    if key in CONVERSIONS:
        result = CONVERSIONS[key](value)
        if abs(result) >= 100:
            return f"{result:,.1f}"
        elif abs(result) >= 1:
            return f"{result:.2f}"
        else:
            return f"{result:.4f}"
    return None


def list_ingredients():
    """Print all known ingredients grouped by category."""
    categories = {
        "Flours": ["flour", "bread-flour", "cake-flour", "pastry-flour", "whole-wheat-flour",
                    "almond-flour", "coconut-flour", "rye-flour", "semolina", "cornstarch"],
        "Sugars & Syrups": ["sugar", "brown-sugar", "powdered-sugar", "coconut-sugar",
                            "honey", "maple-syrup", "molasses", "golden-syrup", "corn-syrup"],
        "Fats": ["butter", "coconut-oil", "vegetable-oil", "olive-oil", "lard", "shortening", "ghee"],
        "Dairy": ["milk", "heavy-cream", "sour-cream", "yogurt", "cream-cheese"],
        "Grains": ["rice", "brown-rice", "oats", "quinoa", "couscous", "cornmeal", "breadcrumbs", "panko"],
        "Nuts & Seeds": ["almonds", "walnuts", "pecans", "cashews", "peanut-butter",
                         "chia-seeds", "flax-seeds", "sunflower-seeds"],
        "Cocoa": ["cocoa", "chocolate-chips"],
        "Misc": ["salt", "baking-powder", "baking-soda", "water"],
    }
    for cat, items in categories.items():
        print(f"\n{cat}:")
        for item in items:
            if item in INGREDIENTS:
                print(f"  {item:25s} {INGREDIENTS[item]:>4}g per cup")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  convert.py <value> <from> <to>              — standard conversion")
        print("  convert.py <value> <from> <to> <ingredient> — ingredient-aware")
        print("  convert.py ingredients                       — list all ingredients")
        print()
        print("Examples:")
        print("  convert.py 100 kg lb")
        print("  convert.py 2 cups g flour")
        print("  convert.py 250 g cups sugar")
        print("  convert.py 3 tbsp g butter")
        sys.exit(1)

    if sys.argv[1] == "ingredients":
        list_ingredients()
        sys.exit(0)

    try:
        value = float(sys.argv[1])
    except ValueError:
        print(f"Not a number: {sys.argv[1]}")
        sys.exit(1)

    if len(sys.argv) < 4:
        print("Need at least: <value> <from_unit> <to_unit>")
        sys.exit(1)

    from_unit = sys.argv[2]
    to_unit = sys.argv[3]

    # Ingredient mode (4th arg)
    if len(sys.argv) >= 5:
        ingredient_raw = " ".join(sys.argv[4:])
        ingredient_key = normalise_ingredient(ingredient_raw)
        if ingredient_key is None:
            print(f"Unknown ingredient: {ingredient_raw}")
            print("Run 'convert.py ingredients' to see all supported ingredients.")
            sys.exit(1)

        result_val, result_str = convert_ingredient(value, from_unit, to_unit, ingredient_key)
        if result_val is None:
            print(result_str)  # error message
            sys.exit(1)
        f = normalise(from_unit)
        t = normalise(to_unit)
        print(f"{value:g} {f} {ingredient_key} = {result_str} {t}")
        print(f"  (density: {INGREDIENTS[ingredient_key]}g per cup)")

    else:
        # Standard conversion
        result = convert(value, from_unit, to_unit)
        if result is None:
            f = normalise(from_unit)
            t = normalise(to_unit)

            # Hint: maybe they meant an ingredient conversion?
            if (f in VOLUME_UNITS and t in WEIGHT_UNITS) or (f in WEIGHT_UNITS and t in VOLUME_UNITS):
                print(f"No direct {f} → {t} conversion (need ingredient density).")
                print(f"Try: convert.py {value} {from_unit} {to_unit} flour")
                print(f"     convert.py {value} {from_unit} {to_unit} sugar")
            else:
                print(f"No conversion found: {f} → {t}")
                print(f"Available from {f}: {[k[1] for k in CONVERSIONS if k[0] == f]}")
            sys.exit(1)

        print(f"{value:g} {normalise(from_unit)} = {result} {normalise(to_unit)}")
