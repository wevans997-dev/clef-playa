import csv
import itertools
import json
import statistics
from collections import defaultdict
from pathlib import Path

from . import geo
from .heatmap import load_results
from .tiles import Manifest

ANNULAR_ORDER = ["ESP", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]
SQFT_PER_ACRE = 43560


def street_radii(gis_dir: Path, g: geo.Georef) -> dict[str, float]:
    """Median distance from the Man to each annular street centerline, in feet."""
    by_name = defaultdict(list)
    for f in geo.load_geojson(gis_dir / "street_lines.geojson"):
        p = f["properties"]
        if p.get("kind") != "annular" or p.get("name") not in ANNULAR_ORDER:
            continue
        for lon, lat in f["geometry"]["coordinates"]:
            by_name[p["name"]].append(abs(g.enu_ft(lon, lat)))
    return {n: statistics.median(v) for n, v in by_name.items()}


def ring_for(r_ft: float, radii: dict[str, float]) -> str:
    names = [n for n in ANNULAR_ORDER if n in radii]
    if r_ft < radii[names[0]]:
        return "inner playa"
    for a, b in itertools.pairwise(names):
        if radii[a] <= r_ft < radii[b]:
            return f"{a}-{b}"
    return f"beyond {names[-1]}"


def twelve_bearing(gis_dir: Path, g: geo.Georef) -> float:
    cp = geo.cpn_lookup(gis_dir)
    return g.polar(*g.to_px(*cp["The Temple"]))[1]


def analyze(run_dir: Path, gis_dir: Path, clock_step_min: int = 30) -> dict:
    manifest = Manifest.load(run_dir / "manifest.json")
    results = load_results(run_dir)
    g = geo.Georef.load(run_dir / "georef.json")
    radii = street_radii(gis_dir, g)
    tw = twelve_bearing(gis_dir, g)
    tile_sqft = (manifest.tile_px * g.ft_per_px) ** 2
    tiles = {t.id: t for t in manifest.tiles}

    rows = []
    for tid, r in results.items():
        t = tiles[tid]
        cx, cy = t.x + t.w / 2, t.y + t.h / 2
        r_ft, brg = g.polar(cx, cy)
        hours = geo.bearing_to_clock(brg, tw)
        ring = ring_for(r_ft, radii)
        in_arc = 2.0 <= hours <= 10.0
        clock = geo.clock_label(hours, clock_step_min) if in_arc else "off-arc"
        m = r["metrics"]
        rows.append(
            {
                "id": tid,
                "x": t.x,
                "y": t.y,
                "r_ft": round(r_ft),
                "bearing": round(brg, 1),
                "clock_hours": round(hours, 2),
                "clock": clock,
                "ring": ring,
                "content": m["content"],
                "rv": m["rv_expected"],
                "car": m["car_expected"],
                "shade_sqft": m["shade_frac"] * tile_sqft,
            }
        )

    def agg(key):
        out = defaultdict(lambda: {"tiles": 0, "rv": 0.0, "car": 0.0, "shade_sqft": 0.0})
        for row in rows:
            a = out[row[key]]
            a["tiles"] += 1
            a["rv"] += row["rv"]
            a["car"] += row["car"]
            a["shade_sqft"] += row["shade_sqft"]
        for a in out.values():
            a["shade_acres"] = a["shade_sqft"] / SQFT_PER_ACRE
            a["shade_pct_of_tiles"] = 100 * a["shade_sqft"] / (a["tiles"] * tile_sqft) if a["tiles"] else 0
        return out

    return {
        "ft_per_px": g.ft_per_px,
        "tile_sqft": tile_sqft,
        "twelve_bearing": tw,
        "radii": radii,
        "rows": rows,
        "by_ring": agg("ring"),
        "by_clock": agg("clock"),
    }


def write_outputs(run_dir: Path, a: dict):
    with (run_dir / "tiles_sectors.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(a["rows"][0].keys()))
        w.writeheader()
        w.writerows(a["rows"])
    for key in ("by_ring", "by_clock"):
        with (run_dir / f"{key}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([key[3:], "tiles", "rv", "car", "shade_sqft", "shade_acres", "shade_pct_of_tiles"])
            for name, v in a[key].items():
                w.writerow(
                    [
                        name,
                        v["tiles"],
                        round(v["rv"], 1),
                        round(v["car"], 1),
                        round(v["shade_sqft"]),
                        round(v["shade_acres"], 2),
                        round(v["shade_pct_of_tiles"], 1),
                    ]
                )
    (run_dir / "sectors_meta.json").write_text(
        json.dumps({k: a[k] for k in ("ft_per_px", "tile_sqft", "twelve_bearing", "radii")}, indent=1)
    )


def ring_sort_key(name: str) -> int:
    if name == "inner playa":
        return -1
    if name.startswith("beyond"):
        return 99
    return ANNULAR_ORDER.index(name.split("-")[0])


def clock_sort_key(name: str) -> float:
    if name == "off-arc":
        return 99
    h, m = name.split(":")
    return int(h) + int(m) / 60


def print_tables(a: dict):
    print(
        f"scale {a['ft_per_px']:.4f} ft/px, tile {a['tile_sqft']:,.0f} sq ft, 12:00 at bearing {a['twelve_bearing']:.1f}"
    )
    print("street radii (ft): " + ", ".join(f"{n}={a['radii'][n]:.0f}" for n in ANNULAR_ORDER if n in a["radii"]))
    for key, sorter, title in (("by_ring", ring_sort_key, "ring"), ("by_clock", clock_sort_key, "clock")):
        print(f"\n{title:12s} {'tiles':>5s} {'RVs':>7s} {'cars':>7s} {'shade ac':>9s} {'shade %':>8s}")
        for name in sorted(a[key], key=sorter):
            v = a[key][name]
            print(
                f"{name:12s} {v['tiles']:5d} {v['rv']:7.0f} {v['car']:7.0f} {v['shade_acres']:9.1f} {v['shade_pct_of_tiles']:8.1f}"
            )
