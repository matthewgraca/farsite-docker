"""Axis-order-robust WGS84 <-> projected coordinate transforms.

pyproj's `always_xy=True` is honored on a healthy install, but on some
Windows conda builds it is effectively broken: a valid CONUS (lon, lat)
reprojects to (inf, inf) in BOTH argument orders, while the plain
(always_xy=False) transformer with EPSG-native (lat, lon) input still works.
`GeoProj` therefore probes the Always-XY transformer first and falls back to
the plain transformer (native axis order), so `.transform(lon, lat)` and
`.to_lonlat(x, y)` are correct on a healthy install, on an always_xy-ignoring
build, and on an always_xy-broken build alike.

Resolution is grounded in a fact that holds on every build: the *wrong* input
order feeds a >90 |latitude| into the geographic axis, so PROJ returns
(nan, nan)/(inf, inf) - the first probe that yields finite output for an
in-domain point is the correct (transformer, order) pair. The reverse output
order is fixed by requiring a self-consistent round trip back to that probe.
"""

import math
import os

# pyproj snapshots PROJ_LIB/PROJ_DATA at import time; a stray value pointing
# at an old bundled proj.db makes valid WGS84 coordinates reproject to
# (inf, inf) / non-finite. Neutralize before `import pyproj`; the native exes
# locate their own proj.db by DLL-relative path.
for _proj_k in ("PROJ_LIB", "PROJ_DATA"):
    os.environ.pop(_proj_k, None)

import pyproj

# A WGS84 point whose lon (< -90) makes the wrong-order probe call non-finite
# on any build - the deterministic anchor for the finiteness probe.
_CONUS = (-118.85, 34.79)
_TOL = 5.0   # round-trip agreement, m


def _aou_center(crs):
    """(lon, lat) center of a projected CRS's area of use, else the CONUS
    anchor - the probe point for the self-consistent reverse resolution."""
    aou = crs.area_of_use
    if aou is not None and aou.west <= aou.east:
        return ((aou.west + aou.east) / 2.0, (aou.south + aou.north) / 2.0)
    return _CONUS


def _resolve_ll_to(proj_crs):
    """A working 4326 -> proj_crs transformer + input-swap flag.

    Tries always_xy=True first (healthy installs), then always_xy=False with
    the EPSG-native (lat, lon) input order. Returns (transformer, swap_in)
    where swap_in is True when the transformer consumes (lat, lon)."""
    for always_xy in (True, False):
        try:
            tr = pyproj.Transformer.from_crs("EPSG:4326", proj_crs,
                                             always_xy=always_xy)
        except Exception:  # noqa: BLE001
            continue
        for swap, (a, b) in ((False, _CONUS), (True, (_CONUS[1], _CONUS[0]))):
            try:
                x, y = tr.transform(a, b)
            except Exception:  # noqa: BLE001
                continue
            if math.isfinite(x) and math.isfinite(y):
                return tr, swap
    raise pyproj.exceptions.CRSError(
        f"no finite WGS84->{proj_crs} transform probe {_CONUS}: pyproj is "
        "reading a proj.db that cannot project valid WGS84 coordinates (a "
        "stray PROJ_LIB/PROJ_DATA env var pointing at an old bundled "
        "proj.db, or a broken conda pyproj install)")


def _resolve_proj_to_ll(proj_crs, fwd_t, fwd_swap):
    """A working proj_crs -> 4326 transformer + output-swap flag.

    Output order is validated by round-tripping an in-domain probe back
    through the resolved forward - self-consistent, no ground truth needed."""
    plon, plat = _aou_center(proj_crs)
    a, b = (plon, plat) if not fwd_swap else (plat, plon)
    px, py = fwd_t.transform(a, b)
    for always_xy in (True, False):
        try:
            rev = pyproj.Transformer.from_crs(proj_crs, "EPSG:4326",
                                              always_xy=always_xy)
        except Exception:  # noqa: BLE001
            continue
        ra, rb = rev.transform(px, py)
        for out_swap, (lo, la) in ((False, (ra, rb)), (True, (rb, ra))):
            ia, ib = (lo, la) if not fwd_swap else (la, lo)
            x2, y2 = fwd_t.transform(ia, ib)
            if (math.isfinite(x2) and math.isfinite(y2)
                    and abs(x2 - px) < _TOL and abs(y2 - py) < _TOL):
                return rev, out_swap
    raise pyproj.exceptions.CRSError(
        f"cannot resolve {proj_crs}->WGS84 axis order")


class GeoProj:
    """A from_crs -> to_crs transform with deterministic (lon, lat) semantics.

    `transform(lon, lat)` returns (x, y) in `to_crs` (geographic->projected);
    `to_lonlat(x, y)` returns (lon, lat) (projected->geographic). Use the
    method matching the pair's geographic endpoint.
    """

    def __init__(self, from_crs, to_crs):
        self.frm = pyproj.CRS.from_user_input(from_crs)
        self.to = pyproj.CRS.from_user_input(to_crs)
        if not (self.frm.is_geographic or self.to.is_geographic):
            raise pyproj.exceptions.CRSError(
                "GeoProj needs at least one geographic endpoint")
        # anchor: the working 4326 -> projected-CRS transform (probe-resolved).
        proj = self.to if self.frm.is_geographic else self.frm
        self._anchor_t, self._anchor_swap = _resolve_ll_to(proj)
        self._fwd_t, self._fwd_swap = self._anchor_t, self._anchor_swap
        self._rev_t, self._rev_swap = None, False
        if self.to.is_geographic:
            self._rev_t, self._rev_swap = \
                _resolve_proj_to_ll(proj, self._anchor_t, self._anchor_swap)

    def transform(self, lon, lat):
        """(lon, lat) in a geographic `from_crs` -> (x, y) in `to_crs`."""
        if not self.frm.is_geographic:
            raise pyproj.exceptions.CRSError(
                f"{self.frm} is not geographic; use to_lonlat()")
        a, b = (lon, lat) if not self._fwd_swap else (lat, lon)
        x, y = self._fwd_t.transform(a, b)
        return float(x), float(y)

    def to_lonlat(self, x, y):
        """(x, y) in `from_crs` -> (lon, lat) in a geographic `to_crs`."""
        if not self.to.is_geographic:
            raise pyproj.exceptions.CRSError(
                f"{self.to} is not geographic; use transform()")
        ra, rb = self._rev_t.transform(x, y)
        lon, lat = (ra, rb) if not self._rev_swap else (rb, ra)
        return float(lon), float(lat)
