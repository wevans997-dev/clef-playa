"""Blind validation sample and oracle comparison.

`sample` copies a stratified set of judged tiles under neutral names so an annotator
(human or a frontier VLM) can count them without seeing Clef's answers or the tile
position. `compare` lines the annotations up against Clef and against each other.
"""

import json
import math
import random
import shutil
from pathlib import Path

from .heatmap import load_results

STRATA = {
    "rv_heavy": lambda m, std: m["rv_expected"] >= 8,
    "shade_heavy": lambda m, std: m["shade_frac"] >= 0.3 and m["rv_expected"] < 8,
    "camp_mid": lambda m, std: m["content"] == "camp" and 1 <= m["rv_expected"] < 8 and m["shade_frac"] < 0.3,
    "sparse": lambda m, std: m["content"] == "open_playa" and std >= 20,
}

ANNOTATOR_BRIEF = """Each tile is a 512x512 px nadir drone photo of Black Rock City at {ft_per_px} ft/px
(about {tile_ft} ft on a side). For each tile record:
- rv: count of RVs, motorhomes, camper trailers, fifth wheels, camper vans, box trucks or buses used as living space.
  Count a vehicle cut by the tile edge if more than half is visible.
- car: count of cars, SUVs, pickups, vans under about 20 ft. Not RVs, trailers, art cars, or bikes.
- shade: fraction of ground covered by man-made shade roofs (tarps, shade cloth, canopies, domes, tent roofs).
  Roof footprint only; not vehicles, cast shadows, or bare ground. Decimal 0-1.
Write a JSON array of {{"alias", "rv", "car", "shade", "note"}} objects to <name>.json in this directory.
"""


def sample(run_dir: Path, out_dir: Path, per_stratum: int = 10, seed: int = 7, ft_per_px: float = 0.433) -> list[dict]:
    rng = random.Random(seed)
    results = load_results(run_dir)
    pools = {name: [r for r in results.values() if pred(r["metrics"], r["std"])] for name, pred in STRATA.items()}
    picks = []
    for name, pool in pools.items():
        for r in rng.sample(pool, min(per_stratum, len(pool))):
            m = r["metrics"]
            picks.append(
                {
                    "stratum": name,
                    "id": r["id"],
                    "rv_expected": m["rv_expected"],
                    "car_expected": m["car_expected"],
                    "shade_frac": m["shade_frac"],
                    "content": m["content"],
                }
            )
    rng.shuffle(picks)
    (out_dir / "tiles").mkdir(parents=True, exist_ok=True)
    key = []
    for i, p in enumerate(picks, 1):
        alias = f"v{i:02d}.jpg"
        shutil.copy(run_dir / "tiles" / f"{p['id']}.jpg", out_dir / "tiles" / alias)
        key.append({"alias": alias, **p})
    (out_dir / "key.json").write_text(json.dumps(key, indent=1))
    (out_dir / "BRIEF.md").write_text(ANNOTATOR_BRIEF.format(ft_per_px=ft_per_px, tile_ft=round(512 * ft_per_px)))
    return key


def _stats(x: list[float], y: list[float]) -> tuple[float, float, float, float]:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    r = cov / math.sqrt(vx * vy) if vx and vy else float("nan")
    mae = sum(abs(a - b) for a, b in zip(x, y)) / n
    return sum(x), sum(y), r, mae


def compare(val_dir: Path, oracles: list[str]) -> None:
    key = {k["alias"]: k for k in json.loads((val_dir / "key.json").read_text())}
    ann = {name: {o["alias"]: o for o in json.loads((val_dir / f"{name}.json").read_text())} for name in oracles}
    series = {"clef": {"rv": {}, "car": {}, "shade": {}}}
    for a, k in key.items():
        series["clef"]["rv"][a] = k["rv_expected"]
        series["clef"]["car"][a] = k["car_expected"]
        series["clef"]["shade"][a] = k["shade_frac"]
    for name, rows in ann.items():
        series[name] = {m: {a: rows[a][m] for a in key} for m in ("rv", "car", "shade")}

    names = list(series)
    print(f"{'metric':7s} {'pair':14s} {'sum A':>7s} {'sum B':>7s} {'A/B':>5s} {'r':>5s} {'MAE':>6s}")
    for metric in ("rv", "car", "shade"):
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                x = [series[a][metric][al] for al in key]
                y = [series[b][metric][al] for al in key]
                sx, sy, r, mae = _stats(x, y)
                fmt = "{:7.2f}" if metric == "shade" else "{:7.0f}"
                print(
                    f"{metric:7s} {a + '/' + b:14s} {fmt.format(sx)} {fmt.format(sy)} {sx / sy if sy else float('nan'):5.2f} {r:5.2f} {mae:6.2f}"
                )

    print("\nper stratum, summed (clef then oracles):")
    for st in STRATA:
        als = [a for a, k in key.items() if k["stratum"] == st]
        if not als:
            continue
        parts = []
        for metric in ("rv", "car"):
            vals = "/".join(f"{sum(series[n][metric][a] for a in als):.0f}" for n in names)
            parts.append(f"{metric} {vals}")
        sh = "/".join(f"{sum(series[n]['shade'][a] for a in als) / len(als):.2f}" for n in names)
        print(f"  {st:12s} n={len(als):2d}  " + "   ".join(parts) + f"   shade {sh}")
