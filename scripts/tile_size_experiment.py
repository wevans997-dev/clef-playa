"""Why the production sweep uses 256 px tiles.

Re-judges the blind validation tiles two alternative ways and prints the sums next to
the oracle annotations:

- quadrants: split each 512 px tile into four 256 px tiles (upscaled 4x) and sum
- choice:    ask for the exact count as a `choice` over 0..40 at 512 px

Run after `clef-playa validate-sample` and at least one annotator JSON exists:

    uv run python scripts/tile_size_experiment.py out/validation opus sol
"""

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from clef_playa import auth
from clef_playa.clef import ClefClient
from clef_playa.questions import COUNT_LEVELS, COVERAGE_LEVELS, QUESTIONS, expected, tile_state

val = Path(sys.argv[1] if len(sys.argv) > 1 else "out/validation")
oracles = sys.argv[2:] or ["opus", "sol"]
key = json.loads((val / "key.json").read_text())
ann = {n: {o["alias"]: o for o in json.loads((val / f"{n}.json").read_text())} for n in oracles}

creds = auth.resolve()
client = ClefClient(creds, model="clef", refresh=lambda: auth.resolve(creds.account_id, force_refresh=True))
COUNT_QS = {k: QUESTIONS[k] for k in ("rv_count", "car_count", "shade_coverage")}
EXACT = {str(i): None for i in range(41)}


def jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def quadrants(alias: str) -> tuple[str, dict]:
    im = Image.open(val / "tiles" / alias).convert("RGB")
    tot = {"rv": 0.0, "car": 0.0, "shade": 0.0}
    for qx in (0, 256):
        for qy in (0, 256):
            q = im.crop((qx, qy, qx + 256, qy + 256)).resize((1024, 1024), Image.LANCZOS)
            a = client.ask(tile_state(0.433, 256, 0, 0), COUNT_QS, images=[jpeg(q)]).answers
            tot["rv"] += expected(a["rv_count"]["probabilities"], COUNT_LEVELS)
            tot["car"] += expected(a["car_count"]["probabilities"], COUNT_LEVELS)
            tot["shade"] += expected(a["shade_coverage"]["probabilities"], COVERAGE_LEVELS) / 4
    return alias, tot


def exact_choice(alias: str) -> tuple[str, dict]:
    im = Image.open(val / "tiles" / alias).convert("RGB").resize((1024, 1024), Image.LANCZOS)
    qs = {
        f"{k}_n": {
            "type": "choice",
            "instructions": QUESTIONS[f"{k}_count"]["instructions"] + " Answer with the exact number.",
            "criteria": EXACT,
        }
        for k in ("rv", "car")
    }
    answers = client.ask(tile_state(0.433, 512, 0, 0), qs, images=[jpeg(im)]).answers
    out = {}
    for k in ("rv", "car"):
        p = answers[f"{k}_n"]["probabilities"]
        kept = {i: v for i, v in p.items() if v >= 0.03} or p
        out[k] = sum(int(i) * v for i, v in kept.items()) / sum(kept.values())
    return alias, out


with ThreadPoolExecutor(6) as pool:
    quad = dict(pool.map(quadrants, [k["alias"] for k in key]))
    choice = dict(pool.map(exact_choice, [k["alias"] for k in key]))
(val / "tile_size_experiment.json").write_text(json.dumps({"quadrants": quad, "choice": choice}, indent=1))

strata = ["rv_heavy", "shade_heavy", "camp_mid", "sparse", "ALL"]
print(f"{'':12s} {'oracle avg':>10s} {'512 score':>9s} {'256 quad':>8s} {'512 choice':>10s}")
for metric, ckey in (("rv", "rv_expected"), ("car", "car_expected")):
    print(f"-- {metric}")
    for st in strata:
        ks = [k for k in key if st == "ALL" or k["stratum"] == st]
        orc = sum(sum(ann[n][k["alias"]][metric] for n in oracles) / len(oracles) for k in ks)
        print(
            f"{st:12s} {orc:10.0f} {sum(k[ckey] for k in ks):9.0f} "
            f"{sum(quad[k['alias']][metric] for k in ks):8.0f} {sum(choice[k['alias']][metric] for k in ks):10.0f}"
        )
orc_sh = sum(sum(ann[n][k["alias"]]["shade"] for n in oracles) / len(oracles) for k in key)
print(f"\nshade sum  oracle {orc_sh:.2f}  512 {sum(k['shade_frac'] for k in key):.2f}  256 quad {sum(q['shade'] for q in quad.values()):.2f}")
