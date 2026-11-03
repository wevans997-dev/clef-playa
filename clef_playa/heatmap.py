import itertools
import json
from pathlib import Path

from PIL import Image, ImageDraw

from . import geo
from .tiles import Manifest

# Approximation of matplotlib's inferno: dark -> purple -> orange -> yellow.
INFERNO = [
    (0.0, (0, 0, 4)),
    (0.25, (87, 16, 110)),
    (0.5, (188, 55, 84)),
    (0.75, (249, 142, 9)),
    (1.0, (252, 255, 164)),
]

METRICS = {
    "shade_frac": ("Expected shade coverage (fraction of tile)", 0.0, 0.6),
    "rv_expected": ("Expected RV count per tile", 0.0, None),
    "car_expected": ("Expected car count per tile", 0.0, None),
    "p_camp": ("P(tile is camp)", 0.0, 1.0),
    "p_rv": ("P(at least one RV)", 0.0, 1.0),
    "p_readable": ("P(tile is readable)", 0.0, 1.0),
}


def colormap(v: float) -> tuple[int, int, int]:
    v = max(0.0, min(1.0, v))
    for (a, ca), (b, cb) in itertools.pairwise(INFERNO):
        if v <= b:
            t = (v - a) / (b - a)
            return tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return INFERNO[-1][1]


def load_results(run_dir: Path) -> dict[str, dict]:
    out = {}
    with (run_dir / "results.jsonl").open() as f:
        for line in f:
            rec = json.loads(line)
            out[rec["id"]] = rec
    return out


def base_mosaic(run_dir: Path, manifest: Manifest, render_px: int) -> tuple[Image.Image, int, int]:
    cols = max(t.col for t in manifest.tiles) + 1
    rows = max(t.row for t in manifest.tiles) + 1
    base = Image.new("RGB", (cols * render_px, rows * render_px), (40, 40, 40))
    for t in manifest.tiles:
        im = Image.open(run_dir / t.path).convert("RGB")
        im = im.resize((render_px, render_px), Image.BOX)
        base.paste(im, (t.col * render_px, t.row * render_px))
    return base, cols, rows


def _draw_streets(img: Image.Image, run_dir: Path, manifest: Manifest, gis_dir: Path, render_px: int):
    g = geo.Georef.load(run_dir / "georef.json")
    x0, y0 = manifest.region[0], manifest.region[1]
    k = render_px / manifest.tile_px
    d = ImageDraw.Draw(img)
    for f in geo.load_geojson(gis_dir / "street_lines.geojson"):
        pts = [
            ((px - x0) * k, (py - y0) * k)
            for px, py in (g.to_px(lon, lat) for lon, lat in f["geometry"]["coordinates"])
        ]
        d.line(pts, fill=(255, 255, 255), width=1)


def render(
    run_dir: Path,
    metric: str,
    render_px: int = 32,
    alpha: float = 0.6,
    vmax: float | None = None,
    gis_dir: Path | None = None,
) -> Path:
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    title, lo, hi = METRICS[metric]
    manifest = Manifest.load(run_dir / "manifest.json")
    results = load_results(run_dir)

    values = {tid: r["metrics"][metric] for tid, r in results.items()}
    if vmax is not None:
        hi = vmax
    if hi is None:
        hi = max(values.values(), default=1.0) or 1.0

    base, _cols, _rows = base_mosaic(run_dir, manifest, render_px)
    # Desaturate the imagery so the colour layer reads as data, not as playa dust.
    base = base.convert("L").convert("RGB")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    a = round(255 * alpha)
    for t in manifest.tiles:
        if t.id not in values:
            continue
        v = (values[t.id] - lo) / (hi - lo) if hi > lo else 0.0
        v = max(0.0, min(1.0, v))
        x, y = t.col * render_px, t.row * render_px
        # Fade low values so judged-but-empty tiles show the imagery instead of a dark slab.
        d.rectangle((x, y, x + render_px - 1, y + render_px - 1), fill=(*colormap(v), round(a * min(1.0, 0.15 + v))))
    out = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    if gis_dir and (run_dir / "georef.json").exists():
        _draw_streets(out, run_dir, manifest, gis_dir, render_px)

    # Legend strip along the bottom.
    strip_h = 40
    canvas = Image.new("RGB", (out.width, out.height + strip_h), (20, 20, 20))
    canvas.paste(out, (0, 0))
    d = ImageDraw.Draw(canvas)
    bar_w = min(400, out.width - 20)
    for i in range(bar_w):
        d.line((10 + i, out.height + 8, 10 + i, out.height + 22), fill=colormap(i / bar_w))
    d.text((10, out.height + 25), f"{lo:g}", fill="white")
    d.text((10 + bar_w - 30, out.height + 25), f"{hi:.2g}", fill="white")
    d.text((10 + bar_w + 15, out.height + 12), f"{title}  (n={len(values)} tiles)", fill="white")

    path = run_dir / f"heatmap_{metric}.png"
    canvas.save(path)
    return path
