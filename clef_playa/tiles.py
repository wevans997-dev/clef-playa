import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageStat

Image.MAX_IMAGE_PIXELS = None

# Clef caps each image at 1,024 tokens, one per 32x32 patch, so anything above 1024 px on
# a side is downscaled server-side and just wastes upload bytes.
CLEF_MAX_SIDE = 1024


@dataclass
class Tile:
    id: str
    col: int
    row: int
    x: int
    y: int
    w: int
    h: int
    std: float
    path: str


@dataclass
class Manifest:
    image: str
    image_w: int
    image_h: int
    tile_px: int
    region: tuple[int, int, int, int]
    tiles: list[Tile]

    def save(self, path: Path):
        path.write_text(json.dumps({**asdict(self), "tiles": [asdict(t) for t in self.tiles]}, indent=1))

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        d = json.loads(path.read_text())
        d["tiles"] = [Tile(**t) for t in d["tiles"]]
        d["region"] = tuple(d["region"])
        return cls(**d)


def pretile(
    image_path: Path,
    out_dir: Path,
    tile_px: int = 512,
    region: tuple[int, int, int, int] | None = None,
    quality: int = 90,
    progress=None,
) -> Manifest:
    im = Image.open(image_path)
    im.load()
    W, H = im.size
    x0, y0, x1, y1 = region or (0, 0, W, H)
    x1, y1 = min(x1, W), min(y1, H)
    tiles_dir = out_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    tiles: list[Tile] = []
    rows = range(y0, y1, tile_px)
    cols = range(x0, x1, tile_px)
    total = len(rows) * len(cols)
    n = 0
    for r, y in enumerate(rows):
        for c, x in enumerate(cols):
            box = (x, y, min(x + tile_px, x1), min(y + tile_px, y1))
            crop = im.crop(box)
            # Luminance std is a cheap proxy for "something is here": bare playa sits
            # around 5-15, camps and vehicles push well past 30.
            std = ImageStat.Stat(crop.convert("L")).stddev[0]
            tid = f"{c:03d}_{r:03d}"
            p = tiles_dir / f"{tid}.jpg"
            crop.save(p, quality=quality)
            tiles.append(
                Tile(
                    tid,
                    c,
                    r,
                    box[0],
                    box[1],
                    box[2] - box[0],
                    box[3] - box[1],
                    round(std, 2),
                    str(p.relative_to(out_dir)),
                )
            )
            n += 1
            if progress and n % 200 == 0:
                progress(n, total)

    m = Manifest(str(image_path), W, H, tile_px, (x0, y0, x1, y1), tiles)
    m.save(out_dir / "manifest.json")
    return m


def encode_for_clef(tile_path: Path, upscale: int = 1, quality: int = 90) -> bytes:
    im = Image.open(tile_path).convert("RGB")
    if upscale > 1:
        side = min(CLEF_MAX_SIDE, max(im.size) * upscale)
        im = im.resize((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
