---
name: unit-converter
description: Convert between units offline - temperature, distance, weight, volume, cooking, data storage, speed. Ingredient-aware for baking (cups of flour to grams). Use for any unit conversion question.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: convert
---

## When to use

- "Convert 100 kg to lb" → `convert 100 kg lb`
- "How many miles is 10km?" → `convert 10 km mi`
- "What's 72°F in Celsius?" → `convert 72 f c`
- "How many grams is 2 cups of flour?" → `convert 2 cups g flour`
- "250g of sugar in cups?" → `convert 250 g cups sugar`

## Commands

```
convert <amount> <from> <to>               standard conversion
convert <amount> <from> <to> <ingredient>  volume <-> weight for cooking
convert ingredients                        list the 60+ known ingredients
```

Categories: temperature (c, f, k), distance (km, mi, m, ft, cm, in, yd), weight (kg, lb, g, oz, st), volume (l, gal, ml, floz), cooking (cups, tbsp, tsp, ml), data (kb, mb, gb, tb, pb), speed (kmh, mph, ms, knots).

## Rules

- Volume to weight (or back) for food is meaningless without the ingredient: a cup of flour is 125g, a cup of sugar 200g, a cup of butter 227g. If the user asks for such a conversion without naming the ingredient, ask which ingredient; do not guess.
- If an ingredient is not recognised, run `convert ingredients` and offer the closest matches.
- Densities are spoon-and-level (brown sugar assumes packed); US cup (236.588 ml) throughout. Mention this only when the user cares about precision.
