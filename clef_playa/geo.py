"""Georeference the orthomosaic against the BRC GIS layers.

The gigapan is a nadir orthomosaic, so lon/lat -> pixel is a 2D similarity transform
(uniform scale, rotation, translation). We express it with complex numbers: a point in
local east/north feet is z, its pixel position is w = a*z + b. Two or more anchors give
a closed-form least-squares fit without any linear-algebra dependency.
"""

import cmath
import json
import math
from dataclasses import dataclass
from pathlib import Path

FT_PER_M = 1 / 0.3048


def _m_per_deg(lat_deg: float) -> tuple[float, float]:
    p = math.radians(lat_deg)
    lat = 111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p)
    lon = 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p)
    return lat, lon


@dataclass
class Georef:
    lon0: float
    lat0: float
    a: complex  # scale and rotation, px per ft
    b: complex  # pixel position of the origin

    def enu_ft(self, lon: float, lat: float) -> complex:
        m_lat, m_lon = _m_per_deg(self.lat0)
        return complex((lon - self.lon0) * m_lon * FT_PER_M, (lat - self.lat0) * m_lat * FT_PER_M)

    def to_px(self, lon: float, lat: float) -> tuple[float, float]:
        w = self.a * self.enu_ft(lon, lat) + self.b
        return w.real, w.imag

    def px_to_enu(self, x: float, y: float) -> complex:
        return (complex(x, y) - self.b) / self.a

    def polar(self, x: float, y: float) -> tuple[float, float]:
        """Distance from the origin in feet and compass bearing in degrees."""
        z = self.px_to_enu(x, y)
        return abs(z), math.degrees(math.atan2(z.real, z.imag)) % 360

    @property
    def ft_per_px(self) -> float:
        return 1 / abs(self.a)

    @property
    def rotation_deg(self) -> float:
        # Screen angle (y down) of the east axis; -45 means east points up-right.
        return math.degrees(cmath.phase(self.a))

    def save(self, path: Path):
        path.write_text(
            json.dumps(
                {
                    "lon0": self.lon0,
                    "lat0": self.lat0,
                    "a": [self.a.real, self.a.imag],
                    "b": [self.b.real, self.b.imag],
                    "ft_per_px": self.ft_per_px,
                    "rotation_deg": self.rotation_deg,
                },
                indent=1,
            )
        )

    @classmethod
    def load(cls, path: Path) -> "Georef":
        d = json.loads(path.read_text())
        return cls(d["lon0"], d["lat0"], complex(*d["a"]), complex(*d["b"]))


def fit(anchors: list[tuple[float, float, float, float]], origin: tuple[float, float]) -> Georef:
    """anchors: (lon, lat, px_x, px_y); origin: (lon, lat) that ENU is measured from."""
    g = Georef(origin[0], origin[1], 1, 0)
    z = [g.enu_ft(lon, lat) for lon, lat, _, _ in anchors]
    w = [complex(x, y) for _, _, x, y in anchors]
    zm = sum(z) / len(z)
    wm = sum(w) / len(w)
    num = sum((wi - wm) * (zi - zm).conjugate() for zi, wi in zip(z, w))
    den = sum(abs(zi - zm) ** 2 for zi in z)
    a = num / den
    b = wm - a * zm
    return Georef(origin[0], origin[1], a, b)


def load_geojson(path: Path) -> list[dict]:
    return json.loads(path.read_text())["features"]


def cpn_lookup(gis_dir: Path) -> dict[str, tuple[float, float]]:
    return {
        f["properties"]["NAME"]: tuple(f["geometry"]["coordinates"]) for f in load_geojson(gis_dir / "cpns.geojson")
    }


def bearing_to_clock(bearing: float, twelve_bearing: float) -> float:
    """Clock position in hours (0, 12], with 12:00 at twelve_bearing."""
    h = ((bearing - twelve_bearing) % 360) / 30
    return 12.0 if h == 0 else h


def clock_label(hours: float, step_min: int = 30) -> str:
    total = round(hours * 60 / step_min) * step_min
    h, m = divmod(total % 720, 60)
    return f"{12 if h == 0 else h}:{m:02d}"
