"""Per-tile question schema for Clef.

Clef answers typed questions with probabilities rather than detecting objects, so counts
and areas are phrased as ordered buckets. Each bucket carries a representative value so
we can turn the returned per-level probabilities into an expected value per tile and sum
those across the city.
"""

COUNT_LEVELS = [
    ("0", 0.0),
    ("1", 1.0),
    ("2", 2.0),
    ("3", 3.0),
    ("4 to 5", 4.5),
    ("6 to 8", 7.0),
    ("9 to 12", 10.5),
    ("13 to 20", 16.5),
    ("21 to 35", 28.0),
    ("36 or more", 45.0),
]

COVERAGE_LEVELS = [
    ("none: no shade structures at all", 0.0),
    ("trace: under 10% of the ground is covered", 0.05),
    ("light: 10% to 25% covered", 0.175),
    ("moderate: 25% to 50% covered", 0.375),
    ("heavy: 50% to 75% covered", 0.625),
    ("nearly full: over 75% covered", 0.875),
]

CONTENT_OPTIONS = {
    "open_playa": "Bare desert floor with at most tire tracks, a few people, or bikes",
    "camp": "Theme camp or open camping: tents, shade structures, RVs, parked vehicles",
    "road": "Mostly a dirt street or promenade with little else in frame",
    "art": "An art installation or sculpture standing on open playa, not a camp",
    "infrastructure": "Service area: fuel, sanitation trucks, rows of portable toilets, airport, depot",
    "artifact": "Image border, vignette, watermark, blank, or unreadable",
}

QUESTIONS = {
    "content": {
        "type": "choice",
        "instructions": "Which option best describes what fills most of this aerial tile?",
        "criteria": CONTENT_OPTIONS,
    },
    "shade_coverage": {
        "type": "score",
        "instructions": (
            "What fraction of the ground in this tile is covered by man-made shade: tarps, "
            "shade cloth, aluminet, canopies, carports, domes, tent roofs, or awnings? Count "
            "the roof footprint only. Do not count vehicles, cast shadows, or bare ground."
        ),
        "criteria": [label for label, _ in COVERAGE_LEVELS],
    },
    "rv_count": {
        "type": "score",
        "instructions": (
            "How many RVs are in this tile? RVs are motorhomes, camper trailers, fifth wheels, "
            "camper vans, and box trucks or buses used as living space: long rectangular "
            "vehicles with white or light roofs, usually 20 to 45 feet long. Do not count "
            "passenger cars, pickups, or shade structures."
        ),
        "criteria": [label for label, _ in COUNT_LEVELS],
    },
    "car_count": {
        "type": "score",
        "instructions": (
            "How many passenger vehicles are in this tile? Count cars, SUVs, pickups, and "
            "vans under about 20 feet long. Do not count RVs, trailers, art cars, bikes, or "
            "shade structures."
        ),
        "criteria": [label for label, _ in COUNT_LEVELS],
    },
    "has_rv": {
        "type": "noul",
        "instructions": "Is at least one RV, motorhome, or camper trailer visible in this tile?",
    },
    "has_shade": {
        "type": "noul",
        "instructions": "Is at least one man-made shade structure or tent roof visible in this tile?",
    },
    "readable": {
        "type": "noul",
        "instructions": (
            "Is this tile sharp and clear enough that a person could tell cars apart from RVs "
            "and see the edges of shade structures?"
        ),
    },
}


DEFAULT_SOURCE = "Nadir drone orthomosaic of Black Rock City (Burning Man)"


def tile_state(ft_per_px: float | None, tile_px: int, col: int, row: int, source: str = DEFAULT_SOURCE) -> dict:
    state = {
        "source": source,
        "tile": {"col": col, "row": row, "size_px": tile_px},
        "guidance": (
            "Shade structures are flat or peaked roofs of white, silver, tan, black, or "
            "colored fabric. RVs are long white rectangles with rounded corners and roof "
            "vents or AC units. Cars are small rectangles roughly half the length of an RV."
        ),
    }
    if ft_per_px:
        tile_ft = round(tile_px * ft_per_px)
        state["scale"] = {
            "ft_per_px": round(ft_per_px, 3),
            "tile_covers_ft": f"{tile_ft} x {tile_ft}",
            "typical_car_len_px": round(15 / ft_per_px),
            "typical_rv_len_px": round(32 / ft_per_px),
        }
    return state


NOISE_FLOOR = 0.05


def expected(probabilities: dict[str, float], levels: list[tuple[str, float]], floor: float = NOISE_FLOOR) -> float:
    """Probability-weighted value over the bucket midpoints.

    Levels below the noise floor are dropped before weighting. Without that, a few
    percent of mass smeared over the large buckets adds a phantom RV or two to every
    empty tile, which dominates the city-wide sum.
    """
    kept = {int(i): float(p) for i, p in probabilities.items() if float(p) >= floor}
    if not kept:
        top = max(probabilities.items(), key=lambda kv: kv[1])[0]
        kept = {int(top): 1.0}
    total = sum(kept.values())
    return sum(p * levels[i][1] for i, p in kept.items()) / total


def summarize_answers(answers: dict) -> dict:
    """Flatten a Clef response into the per-tile metrics we aggregate on."""
    content = answers["content"]
    return {
        "content": content["choice"],
        "content_conf": content["confidence"],
        "p_camp": content["probabilities"].get("camp", 0.0),
        "shade_frac": expected(answers["shade_coverage"]["probabilities"], COVERAGE_LEVELS),
        "shade_conf": answers["shade_coverage"]["confidence"],
        "rv_expected": expected(answers["rv_count"]["probabilities"], COUNT_LEVELS),
        "rv_conf": answers["rv_count"]["confidence"],
        "car_expected": expected(answers["car_count"]["probabilities"], COUNT_LEVELS),
        "car_conf": answers["car_count"]["confidence"],
        "p_rv": answers["has_rv"]["noul"],
        "p_shade": answers["has_shade"]["noul"],
        "p_readable": answers["readable"]["noul"],
    }
