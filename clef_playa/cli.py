import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import auth, geo, sectors, validate
from .clef import ClefClient, cost_usd
from .heatmap import METRICS, load_results, render
from .questions import DEFAULT_SOURCE, QUESTIONS, summarize_answers, tile_state
from .sweep import run_sweep, select_tiles
from .tiles import Manifest, encode_for_clef, pretile


def _region(s: str | None):
    if not s:
        return None
    parts = [int(p) for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must be x0,y0,x1,y1")
    return tuple(parts)


def _client(args) -> ClefClient:
    creds = auth.resolve(args.account_id)
    return ClefClient(
        creds,
        model=args.model,
        refresh=lambda: auth.resolve(creds.account_id, force_refresh=True),
    )


GIS_LAYERS = ("city_blocks", "cpns", "plazas", "street_lines", "trash_fence", "gate_road")
GIS_URL = "https://raw.githubusercontent.com/burningmantech/innovate-GIS-data/master/{year}/GeoJSON/{layer}.geojson"


def cmd_gis_fetch(args):
    import httpx

    out = Path(args.out or f"gis/{args.year}")
    out.mkdir(parents=True, exist_ok=True)
    for layer in GIS_LAYERS:
        r = httpx.get(GIS_URL.format(year=args.year, layer=layer), follow_redirects=True, timeout=60)
        r.raise_for_status()
        (out / f"{layer}.geojson").write_bytes(r.content)
        print(f"  {layer}.geojson  {len(r.content) // 1024} KiB")
    print(f"BRC {args.year} GIS layers -> {out}")
    print("Terms: https://innovate.burningman.org/terms-of-service-for-burning-man-apis-and-datasets/")


def cmd_whoami(args):
    creds = auth.resolve(args.account_id)
    print(f"token source: {creds.source}")
    print(f"account id:   {creds.account_id}")


def cmd_pretile(args):
    run = Path(args.run)
    run.mkdir(parents=True, exist_ok=True)
    m = pretile(
        Path(args.image),
        run,
        tile_px=args.tile,
        region=_region(args.region),
        progress=lambda n, t: print(f"  {n}/{t} tiles", file=sys.stderr),
    )
    stds = sorted(t.std for t in m.tiles)
    print(f"{len(m.tiles)} tiles of {args.tile}px written to {run}/tiles")
    print(f"luminance std: min {stds[0]:.1f}  median {stds[len(stds) // 2]:.1f}  max {stds[-1]:.1f}")
    for thr in (10, 15, 20, 30):
        print(f"  tiles with std >= {thr}: {sum(s >= thr for s in stds)}")


def _fmt_answer(name, a):
    if a["type"] == "noul":
        return f"{name:16s} P(yes)={a['noul']:.2f}"
    if a["type"] == "choice":
        probs = ", ".join(f"{k}={v:.2f}" for k, v in sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[:3])
        return f"{name:16s} {a['choice']}  conf={a['confidence']:.2f}  [{probs}]"
    legend = a["legend"]
    probs = ", ".join(
        f"{legend[k].split(':')[0]}={v:.2f}" for k, v in sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[:3]
    )
    return f"{name:16s} score={a['score']:.2f}  conf={a['confidence']:.2f}  [{probs}]"


def cmd_probe(args):
    run = Path(args.run)
    manifest = Manifest.load(run / "manifest.json")
    if args.ids:
        tiles = select_tiles(manifest, 0, None, args.ids)
    else:
        tiles = sorted(manifest.tiles, key=lambda t: -t.std)[: args.n]
    client = _client(args)
    total_tokens = 0
    for t in tiles:
        img = encode_for_clef(run / t.path, upscale=args.upscale)
        res = client.ask(
            tile_state(args.ft_per_px, manifest.tile_px, t.col, t.row, args.source), QUESTIONS, images=[img]
        )
        total_tokens += res.usage.get("input_tokens", 0)
        m = summarize_answers(res.answers)
        print(
            f"\n== tile {t.id}  ({t.x},{t.y})  std={t.std}  {len(img) // 1024} KiB  "
            f"{res.usage.get('input_tokens')} tok  {res.latency_s:.1f}s  {run / t.path}"
        )
        for name, a in res.answers.items():
            print("  " + _fmt_answer(name, a))
        print(f"  -> shade={m['shade_frac']:.2f}  rv={m['rv_expected']:.1f}  cars={m['car_expected']:.1f}")
    print(f"\n{len(tiles)} tiles, {total_tokens} input tokens, ${cost_usd(args.model, total_tokens):.4f}")


def cmd_sweep(args):
    run = Path(args.run)
    manifest = Manifest.load(run / "manifest.json")
    tiles = select_tiles(manifest, args.min_std, args.limit, args.ids)
    client = _client(args)
    stats = run_sweep(
        client,
        run,
        tiles,
        upscale=args.upscale,
        ft_per_px=args.ft_per_px,
        concurrency=args.concurrency,
        source=args.source,
    )
    print(json.dumps(stats))


def cmd_heatmap(args):
    run = Path(args.run)
    metrics = list(METRICS) if args.metric == "all" else [args.metric]
    for m in metrics:
        print(
            render(
                run,
                m,
                render_px=args.render_px,
                alpha=args.alpha,
                vmax=args.vmax,
                gis_dir=Path(args.gis) if args.gis else None,
            )
        )


def cmd_summary(args):
    run = Path(args.run)
    manifest = Manifest.load(run / "manifest.json")
    results = load_results(run)
    if not results:
        sys.exit("no results yet")
    tile_px = manifest.tile_px
    ft = args.ft_per_px
    calib = run / "calibration.json"
    if ft is None and calib.exists():
        ft = json.loads(calib.read_text()).get("ft_per_px")
    tile_sqft = (tile_px * ft) ** 2 if ft else None

    content = Counter(r["metrics"]["content"] for r in results.values())
    shade_tiles = sum(r["metrics"]["shade_frac"] for r in results.values())
    rvs = sum(r["metrics"]["rv_expected"] for r in results.values())
    cars = sum(r["metrics"]["car_expected"] for r in results.values())
    tokens = sum(r["input_tokens"] or 0 for r in results.values())
    unreadable = sum(1 for r in results.values() if r["metrics"]["p_readable"] < 0.5)
    model = next(iter(results.values()))["model"]

    print(f"tiles judged:      {len(results)} of {len(manifest.tiles)}  ({tile_px}px, model {model})")
    print("content classes:   " + ", ".join(f"{k}={v}" for k, v in content.most_common()))
    print(f"low-readability:   {unreadable}")
    print(f"expected RVs:      {rvs:,.0f}")
    print(f"expected cars:     {cars:,.0f}")
    print(f"shade, tile-equiv: {shade_tiles:,.1f} full tiles")
    if tile_sqft:
        sqft = shade_tiles * tile_sqft
        print(f"shade area:        {sqft:,.0f} sq ft  ({sqft / 43560:,.1f} acres) at {ft} ft/px")
    print(f"tokens / cost:     {tokens:,} / ${cost_usd(model, tokens):.3f}")


def cmd_georef(args):
    run = Path(args.run)
    gis = Path(args.gis)
    cp = geo.cpn_lookup(gis)
    anchors = []
    for spec in args.anchor:
        name, _, px = spec.rpartition("=")
        if name not in cp:
            sys.exit(f"unknown CPN {name!r}; choices include {sorted(cp)[:10]}...")
        x, y = (float(v) for v in px.split(","))
        anchors.append((*cp[name], x, y))
    g = geo.fit(anchors, origin=cp[args.origin])
    g.save(run / "georef.json")
    tw = sectors.twelve_bearing(gis, g)
    print(f"ft/px {g.ft_per_px:.4f}   12:00 at bearing {tw:.1f}   saved {run / 'georef.json'}")
    for lon, lat, x, y in anchors:
        px, py = g.to_px(lon, lat)
        print(f"  residual {px - x:+.1f},{py - y:+.1f} px")


def cmd_sectors(args):
    a = sectors.analyze(Path(args.run), Path(args.gis), clock_step_min=args.step)
    sectors.write_outputs(Path(args.run), a)
    sectors.print_tables(a)


def cmd_compare(args):
    runs = [Path(r) for r in args.runs]
    totals = []
    for run in runs:
        rs = load_results(run)
        m = Manifest.load(run / "manifest.json")
        ft = (
            json.loads((run / "calibration.json").read_text())["ft_per_px"]
            if (run / "calibration.json").exists()
            else args.ft_per_px
        )
        sqft = (m.tile_px * ft) ** 2 if ft else None
        totals.append(
            {
                "run": run.name,
                "tiles": len(rs),
                "rv": sum(r["metrics"]["rv_expected"] for r in rs.values()),
                "car": sum(r["metrics"]["car_expected"] for r in rs.values()),
                "shade_acres": sum(r["metrics"]["shade_frac"] for r in rs.values()) * sqft / 43560
                if sqft
                else float("nan"),
            }
        )
    print(f"{'run':22s} {'tiles':>6s} {'RVs':>8s} {'cars':>8s} {'shade ac':>9s}")
    for t in totals:
        print(f"{t['run']:22s} {t['tiles']:6d} {t['rv']:8.0f} {t['car']:8.0f} {t['shade_acres']:9.1f}")
    if len(totals) > 1:
        for k in ("rv", "car", "shade_acres"):
            vals = [t[k] for t in totals]
            mean = sum(vals) / len(vals)
            spread = (max(vals) - min(vals)) / 2
            print(f"{k:12s} mean {mean:,.0f}  +/- {spread:,.0f}  ({100 * spread / mean:.1f}%)")


def cmd_validate_sample(args):
    key = validate.sample(Path(args.run), Path(args.out), per_stratum=args.per_stratum, seed=args.seed)
    print(f"{len(key)} tiles -> {Path(args.out) / 'tiles'}; brief in {Path(args.out) / 'BRIEF.md'}")


def cmd_validate_compare(args):
    validate.compare(Path(args.val), args.oracles)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="clef-playa", description="Ask Clef what is in each tile of a Black Rock City aerial"
    )
    p.add_argument("--account-id", help="Cloudflare account id (else CLOUDFLARE_ACCOUNT_ID or auto)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("whoami", help="show which credentials and account will be used")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("gis-fetch", help="download the BRC GIS GeoJSON layers for a year")
    s.add_argument("--year", type=int, default=2026)
    s.add_argument("--out", help="default gis/<year>")
    s.set_defaults(fn=cmd_gis_fetch)

    s = sub.add_parser("pretile", help="cut the gigapan into tiles once")
    s.add_argument("image")
    s.add_argument("--run", required=True, help="output directory for this run")
    s.add_argument("--tile", type=int, default=512, help="tile size in source pixels")
    s.add_argument("--region", help="x0,y0,x1,y1 in source pixels (default: whole image)")
    s.set_defaults(fn=cmd_pretile)

    def model_args(s):
        s.add_argument("--model", default="clef-flash", choices=["clef", "clef-flash"])
        s.add_argument("--upscale", type=int, default=2, help="resize factor before upload (cap 1024px)")
        s.add_argument("--ft-per-px", type=float, help="ground scale, passed to Clef as context")
        s.add_argument(
            "--source", default=DEFAULT_SOURCE, help="one-line description of the imagery, passed to Clef as context"
        )

    s = sub.add_parser("probe", help="judge a few tiles and print the full answers")
    s.add_argument("--run", required=True)
    s.add_argument("--ids", nargs="*", help="tile ids like 051_049 (default: busiest tiles)")
    s.add_argument("-n", type=int, default=3)
    model_args(s)
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("sweep", help="judge every selected tile, resumable")
    s.add_argument("--run", required=True)
    s.add_argument("--ids", nargs="*")
    s.add_argument("--min-std", type=float, default=0.0, help="skip tiles flatter than this")
    s.add_argument("--limit", type=int)
    s.add_argument("--concurrency", type=int, default=6)
    model_args(s)
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("heatmap", help="render per-tile metric overlays")
    s.add_argument("--run", required=True)
    s.add_argument("--metric", default="all", choices=["all", *METRICS])
    s.add_argument("--render-px", type=int, default=32)
    s.add_argument("--alpha", type=float, default=0.6)
    s.add_argument("--vmax", type=float)
    s.add_argument("--gis", default="gis/2026", help="draw street centerlines if georef.json exists; pass '' to skip")
    s.set_defaults(fn=cmd_heatmap)

    s = sub.add_parser("georef", help="fit lon/lat -> pixel from named CPN anchors")
    s.add_argument("--run", required=True)
    s.add_argument("--gis", default="gis/2026", help="directory with the BRC GeoJSON layers")
    s.add_argument(
        "--anchor", action="append", required=True, help='"CPN name=x,y" in source pixels; give at least two'
    )
    s.add_argument("--origin", default="The Man")
    s.set_defaults(fn=cmd_georef)

    s = sub.add_parser("sectors", help="totals by street ring and clock position")
    s.add_argument("--run", required=True)
    s.add_argument("--gis", default="gis/2026")
    s.add_argument("--step", type=int, default=30, help="clock bin size in minutes")
    s.set_defaults(fn=cmd_sectors)

    s = sub.add_parser("compare", help="totals across runs, e.g. offset grids")
    s.add_argument("runs", nargs="+")
    s.add_argument("--ft-per-px", type=float)
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("validate-sample", help="copy a blind, stratified sample of tiles for annotation")
    s.add_argument("--run", required=True)
    s.add_argument("--out", default="out/validation")
    s.add_argument("--per-stratum", type=int, default=10)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_validate_sample)

    s = sub.add_parser("validate-compare", help="compare annotator JSON files against Clef")
    s.add_argument("--val", default="out/validation")
    s.add_argument("oracles", nargs="+", help="annotator names, e.g. opus sol (reads <name>.json)")
    s.set_defaults(fn=cmd_validate_compare)

    s = sub.add_parser("summary", help="aggregate totals")
    s.add_argument("--run", required=True)
    s.add_argument("--ft-per-px", type=float)
    s.set_defaults(fn=cmd_summary)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except auth.AuthError as e:
        sys.exit(f"auth: {e}")


if __name__ == "__main__":
    main()
