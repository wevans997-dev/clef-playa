# clef-playa

Count what is on the playa with [Clef](https://developers.cloudflare.com/workers-ai/models/clef/), Cloudflare's decision model.

Clef is not an object detector. It takes a state (here: one aerial tile plus some JSON context) and a schema of typed questions, and returns a calibrated probability for every option. That shape fits a gigapixel orthomosaic well: cut the image into tiles, ask the same questions of every tile, and sum the expected values. Shade area becomes `sum(expected coverage fraction) x tile area`; RVs become `sum(expected count)`.

Built against Jamen Percy's 2026 Black Rock City gigapan, but nothing in the code is year-specific.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a Cloudflare account with Workers AI.

```sh
npm install --global cf   # the Cloudflare CLI; Homebrew's `cf` formula is an unrelated tool
cf auth login
uv sync
uv run clef-playa whoami
```

Credentials are resolved in this order:

- `CLOUDFLARE_API_TOKEN` (needs Workers AI: Edit)
- the [Cloudflare CLI](https://developers.cloudflare.com/cf/) login in `~/Library/Preferences/cloudflare/config/default.json`. Its tokens last an hour; the script runs `cf auth whoami` to refresh when one is near expiry or a request comes back 401
- a wrangler login with the `ai:write` scope

Set `CLOUDFLARE_ACCOUNT_ID` if the token can see more than one account.

The same request works straight from the CLI, which is handy for poking at question wording:

```sh
cf ai run @cf/cloudflare/clef --body @request.json
```

## Workflow

```sh
# 1. Cut the gigapan into tiles once. Region is x0,y0,x1,y1 in source pixels.
uv run clef-playa pretile gigapan.jpg --run out/brc2026-256 --tile 256 --region 5000,13000,34000,38000

# 2. Sanity-check the questions on the busiest tiles, with full answers printed.
uv run clef-playa probe --run out/brc2026-256 -n 3 --upscale 4

# 3. Judge everything that is not flat playa. Resumable; appends to results.jsonl.
uv run clef-playa sweep --run out/brc2026-256 --upscale 4 --min-std 15 --concurrency 8 --ft-per-px 0.433

# 4. Fetch the BRC GIS layers and georeference against them, using two CPNs
#    whose pixel positions you have read off the image.
uv run clef-playa gis-fetch --year 2026
uv run clef-playa georef --run out/brc2026-256 --anchor "The Man=20000,22010" --anchor "Center Camp=20000,29000"

# 5. Render overlays and totals, by street ring and clock position.
uv run clef-playa heatmap --run out/brc2026-256
uv run clef-playa summary --run out/brc2026-256
uv run clef-playa sectors --run out/brc2026-256

# 6. Validate: hand a blind sample to annotators (people or other models), then compare.
uv run clef-playa validate-sample --run out/brc2026-256
uv run clef-playa validate-compare opus sol      # reads out/validation/opus.json, sol.json

# 7. Check grid sensitivity with a half-tile offset run.
uv run clef-playa pretile gigapan.jpg --run out/brc2026-256-off --tile 256 --region 5128,13128,34000,38000
uv run clef-playa compare out/brc2026-256 out/brc2026-256-off
```

`summary` reads `calibration.json` in the run directory for the scale if `--ft-per-px` is omitted; `georef` writes `georef.json`, which `sectors` and the heatmap street overlay use.

## Design notes

- **Tile size is the accuracy lever.** Clef sees at most 1,024 patches of 32 x 32 px per image. A 256 px source tile upscaled 4x gives each patch 8 source pixels, so a car is ~4 patches and an RV ~12, and a tile holds single-digit counts. At 512 px, counts in dense tiles saturate low (see Validation).
- **Counts are buckets.** `rv_count` and `car_count` are `score` questions over ordered ranges. We convert the per-level probabilities to an expected value with a midpoint per bucket, after dropping levels under a 5% noise floor; without the floor, a few percent of mass on the large buckets adds a phantom RV to every empty tile. Aggregates are sound; per-tile values are noisy.
- **Confidence is not a quality gate.** Clef's `confidence` measures how peaked the distribution is. Real counts straddle buckets, so it reads low even when the expected value is right.
- **Blank filter.** `pretile` records luminance std per tile. Open playa sits under 15; `sweep --min-std` skips it without an API call.
- **Scale and orientation** come from a similarity fit (`geo.py`) between CPN coordinates in the GIS layers and their pixel positions. Two anchors suffice for a nadir mosaic; verify by rendering the street overlay.
- **Model choice.** `clef-flash` undercounted dense tiles by 2-3x in a side-by-side probe. At ~2.5k tokens per tile, full `clef` costs about $0.0006 per tile, so there is little reason to economize.

## Results: BRC 2026

Imagery: 40k x 40k px (35% of the original), nadir, aligned to the city's 12:00-6:00 axis. 0.433 ft/px from the GIS fit, verified against the street grid and the full trash-fence pentagon. Region covers the city, Gate, Walk-in camping, and the airport.

| | 512 px, grid A | 512 px, grid B | **256 px** |
|---|---|---|---|
| tiles judged | 2,299 | 2,271 | 8,785 |
| expected RVs | 9,521 | 9,479 | **14,385** |
| expected cars | 10,660 | 10,654 | **18,084** |
| shade | 261.6 acres | 260.6 acres | **256.8 acres** |
| cost | $1.38 | $1.37 | $5.29 |

The 256 px run is the one to quote. The two 512 px grids agree with each other to 0.2% but both undercount dense tiles (see Validation); the 256 px totals land where the quadrant experiment predicted (1.5-1.7x on counts, shade unchanged).

By street ring, 256 px run (shade % is the share of judged tile area under a roof):

| ring | RVs | cars | shade acres | shade % |
|---|---|---|---|---|
| ESP-A | 1,215 | 1,526 | 21.9 | 16.4 |
| A-B | 823 | 1,146 | 16.1 | 16.8 |
| B-C | 893 | 1,153 | 17.2 | 15.4 |
| C-D | 1,002 | 1,307 | 18.9 | 14.8 |
| D-E | 1,231 | 1,515 | 22.3 | 15.5 |
| E-F (double block) | 2,168 | 2,581 | 37.6 | 14.8 |
| F-G | 1,373 | 1,581 | 20.6 | 13.5 |
| G-H | 1,403 | 1,515 | 17.7 | 11.4 |
| H-I | 1,412 | 1,550 | 17.3 | 10.7 |
| I-J | 870 | 986 | 10.0 | 9.5 |
| J-K | 869 | 1,058 | 11.0 | 9.8 |
| beyond K | 994 | 1,649 | 41.2 | 5.7 |
| inner playa | 131 | 518 | 5.0 | 2.5 |

Shade density falls steadily from the Esplanade (~16%) to K (~10%). RV density per tile is fairly even across rings but not around the clock: the 2:00-3:00 and 9:00-10:00 wings run about 2.5 RVs per tile against 1.4 at 4:30-5:30. Full tables are written by `sectors` as `by_ring.csv`, `by_clock.csv`, and `tiles_sectors.csv`.

Known biases: vehicles under carports or shade are invisible from above, so vehicle counts are a floor. Coverage buckets make shade an estimate, not a measurement.

## Validation

40 tiles, stratified by what Clef saw, were counted blind by Claude Opus 5.5 and GPT-6.1 Sol as independent oracles (`scripts/tile_size_experiment.py` reproduces the comparison).

- Clef **ranks** tiles as well as the oracles do (r = 0.95-0.97 on RVs and shade) and its shade estimate sits between them.
- At 512 px, Clef **undercounts** dense tiles: 0.6x the oracles on RVs, 0.5-0.7x on cars. The oracles agree with each other within 5% on RVs.
- Splitting each tile into four 256 px quadrants and summing closes the gap (RVs 386 vs 394, cars 444 vs 452). An exact-count `choice` over 0..40 did not (0.8x). The lever is object density per image, not question wording.
- The half-tile offset grid reproduces the 512 px totals to within 0.2%, so grid placement adds no meaningful variance. The error is systematic, which is why a validation set matters more than repeat runs.

## Data and attribution

- Imagery: the annual Black Rock City gigapan by [Jamen Percy](https://www.instagram.com/jamenpercy.burn/). Not included here; obtain it from the photographer.
- GIS layers: [burningmantech/innovate-GIS-data](https://github.com/burningmantech/innovate-GIS-data), fetched by `gis-fetch` and not committed. Use is subject to the [Burning Man dataset terms](https://innovate.burningman.org/terms-of-service-for-burning-man-apis-and-datasets/).
- City measurements: the yearly "BRC Measurements" PDF on [Burning Man Innovate](https://innovate.burningman.org/datasets-page/).

## Layout

- `clef_playa/auth.py` credential resolution (env, `cf`, wrangler)
- `clef_playa/clef.py` minimal Workers AI REST client with retries and token rotation
- `clef_playa/questions.py` the per-tile question schema and expected-value mapping
- `clef_playa/tiles.py` pretiling and tile encoding
- `clef_playa/sweep.py` concurrent, resumable judging
- `clef_playa/geo.py` lon/lat to pixel similarity fit and polar helpers
- `clef_playa/sectors.py` totals by street ring and clock position
- `clef_playa/heatmap.py` overlay rendering with optional street centerlines
- `clef_playa/validate.py` blind sampling and oracle comparison
- `scripts/tile_size_experiment.py` the 256 px vs 512 px vs exact-choice experiment
