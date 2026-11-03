import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .clef import ClefClient, ClefError, cost_usd
from .questions import DEFAULT_SOURCE, QUESTIONS, summarize_answers, tile_state
from .tiles import Manifest, Tile, encode_for_clef


def load_done(results_path: Path) -> set[str]:
    if not results_path.exists():
        return set()
    done = set()
    with results_path.open() as f:
        for line in f:
            try:
                done.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue
    return done


def select_tiles(manifest: Manifest, min_std: float, limit: int | None, ids: list[str] | None) -> list[Tile]:
    tiles = manifest.tiles
    if ids:
        wanted = set(ids)
        tiles = [t for t in tiles if t.id in wanted]
    tiles = [t for t in tiles if t.std >= min_std]
    if limit:
        tiles = tiles[:limit]
    return tiles


def judge_tile(
    client: ClefClient,
    run_dir: Path,
    manifest: Manifest,
    tile: Tile,
    upscale: int,
    ft_per_px: float | None,
    source: str = DEFAULT_SOURCE,
) -> dict:
    img = encode_for_clef(run_dir / tile.path, upscale=upscale)
    state = tile_state(ft_per_px, manifest.tile_px, tile.col, tile.row, source)
    res = client.ask(state, QUESTIONS, images=[img])
    return {
        "id": tile.id,
        "col": tile.col,
        "row": tile.row,
        "x": tile.x,
        "y": tile.y,
        "std": tile.std,
        "model": res.model,
        "input_tokens": res.usage.get("input_tokens"),
        "latency_s": round(res.latency_s, 2),
        "attempts": res.attempts,
        "metrics": summarize_answers(res.answers),
        "answers": res.answers,
    }


def run_sweep(
    client: ClefClient,
    run_dir: Path,
    tiles: list[Tile],
    upscale: int = 2,
    ft_per_px: float | None = None,
    concurrency: int = 6,
    source: str = DEFAULT_SOURCE,
    log=print,
) -> dict:
    manifest = Manifest.load(run_dir / "manifest.json")
    results_path = run_dir / "results.jsonl"
    done = load_done(results_path)
    todo = [t for t in tiles if t.id not in done]
    log(f"{len(tiles)} tiles selected, {len(done)} already done, {len(todo)} to run")

    lock = threading.Lock()
    stats = {"ok": 0, "failed": 0, "input_tokens": 0}
    t0 = time.monotonic()

    with results_path.open("a") as out, ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(judge_tile, client, run_dir, manifest, t, upscale, ft_per_px, source): t for t in todo}
        for fut in as_completed(futures):
            tile = futures[fut]
            try:
                rec = fut.result()
            except ClefError as e:
                with lock:
                    stats["failed"] += 1
                log(f"  {tile.id} failed: {e}")
                continue
            with lock:
                out.write(json.dumps(rec) + "\n")
                out.flush()
                stats["ok"] += 1
                stats["input_tokens"] += rec["input_tokens"] or 0
                n = stats["ok"] + stats["failed"]
            if n % 25 == 0 or n == len(todo):
                rate = n / max(time.monotonic() - t0, 1e-6)
                eta = (len(todo) - n) / rate if rate else 0
                log(
                    f"  {n}/{len(todo)}  {rate:.1f} tiles/s  eta {eta / 60:.1f} min  "
                    f"${cost_usd(client.model, stats['input_tokens']):.3f}"
                )
    stats["cost_usd"] = round(cost_usd(client.model, stats["input_tokens"]), 4)
    stats["elapsed_s"] = round(time.monotonic() - t0, 1)
    return stats
