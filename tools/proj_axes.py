"""Axis-order-robust WGS84 <-> projected coordinate transforms.

pyproj's `always_xy=True` is honored on a healthy install, but has been
observed ignored on some Windows builds: a valid CONUS (lon, lat) then
reprojects to (inf, inf) because the input is consumed as EPSG-native
(lat, lon), and returned geographic pairs can come back (lat, lon)-ordered.
Rather than depend on which behavior a given build has, `GeoProj` resolves
the effective axis order at construction and guarantees `.transform(lon, lat)
-> (x, y)` and `.to_lonlat(x, y) -> (lon, lat)` on both kinds of build.

Resolution is grounded in one fact that holds on every build: the *wrong*
input order feeds a >90 |latitude| into the geographic axis, so PROJ returns
(nan, nan)/(inf, inf). The forward (lon, lat) order is therefore fixed by
finiteness; the reverse output order is fixed by requiring a self-consistent
round trip back to the forward probe - no ground truth needed.
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

# A WGS84 point whose lon (< -90) makes the swapped forward call non-finite
# on any build - the deterministic anchor for the finiteness probe.
_CONUS = (-118.85, 34.79)
_TOL = 5.0   # round-trip agreement, m


class GeoProj:
    """A from_crs -> to_crs transform with deterministic (lon, lat) semantics.

    `transform(lon, lat)` returns (x, y) in `to_crs` (meant for
    geographic->projected pairs); `to_lonlat(x, y)` returns (lon, lat) (meant
    for projected->geographic pairs). Callers must use the method matching
    the pair's endpoint that is geographic.
    """

    def __init__(self, from_crs, to_crs):
        self.frm = pyproj.CRS.from_user_input(from_crs)
        self.to = pyproj.CRS.from_user_input(to_crs)
        if not (self.frm.is_geographic or self.to.is_geographic):
            raise pyproj.exceptions.CRSError(
                "GeoProj needs at least one geographic endpoint")
        self._tr = pyproj.Transformer.from_crs(self.frm, self.to,
                                               always_xy=True)
        self._swap_in = False
        if self.frm.is_geographic:
            self._swap_in = self._resolve_input_order(self._tr)
        self._swap_out = False
        if self.to.is_geographic:
            self._swap_out = self._resolve_output_order()

    # ------------------------------------------------------------------ #
    def _resolve_input_order(self, tr):
        """True when `tr` consumes (lat, lon) despite always_xy: the wrong
        call puts a >90 |latitude| on the geographic axis -> non-finite."""
        for swap, (a, b) in ((False, _CONUS), (True, (_CONUS[1], _CONUS[0]))):
            try:
                x, y = tr.transform(a, b)
            except Exception:  # noqa: BLE001
                continue
            if math.isfinite(x) and math.isfinite(y):
                return swap
        raise pyproj.exceptions.CRSError(
            f"no finite transform probe {_CONUS} in {self.frm}->{self.to}: "
            "pyproj is reading a proj.db that cannot project valid WGS84 "
            "coordinates (a stray PROJ_LIB/PROJ_DATA env var pointing at an "
            "old bundled proj.db, or a broken conda pyproj install)")

    def _resolve_output_order(self):
        """Which output ordering of the reverse transform round-trips an
        in-domain probe through a finiteness-resolved forward."""
        aou = self.frm.area_of_use
        if aou is not None and aou.west <= aou.east:
            plon = (aou.west + aou.east) / 2.0
            plat = (aou.south + aou.north) / 2.0
        else:
            plon, plat = _CONUS
        fwd = pyproj.Transformer.from_crs("EPSG:4326", self.frm,
                                          always_xy=True)
        swap_in = self._resolve_input_order(fwd)
        a, b = (plon, plat) if not swap_in else (plat, plon)
        x, y = fwd.transform(a, b)
        ra, rb = self._tr.transform(x, y)   # frm -> to (to is geographic)
        for out_swap, (lon, lat) in ((False, (ra, rb)), (True, (rb, ra))):
            ia, ib = (lon, lat) if not swap_in else (lat, lon)
            x2, y2 = fwd.transform(ia, ib)
            if (math.isfinite(x2) and math.isfinite(y2)
                    and abs(x2 - x) < _TOL and abs(y2 - y) < _TOL):
                return out_swap
        raise pyproj.exceptions.CRSError(
            f"cannot resolve reverse axis order for {self.frm}->{self.to}")

    # ------------------------------------------------------------------ #
    def transform(self, lon, lat):
        """(lon, lat) in a geographic `from_crs` -> (x, y) in `to_crs`."""
        if not self.frm.is_geographic:
            raise pyproj.exceptions.CRSError(
                f"{self.frm} is not geographic; use to_lonlat()")
        a, b = (lon, lat) if not self._swap_in else (lat, lon)
        x, y = self._tr.transform(a, b)
        return float(x), float(y)

    def to_lonlat(self, x, y):
        """(x, y) in `from_crs` -> (lon, lat) in a geographic `to_crs`."""
        if not self.to.is_geographic:
            raise pyproj.exceptions.CRSError(
                f"{self.to} is not geographic; use transform()")
        ra, rb = self._tr.transform(x, y)
        lon, lat = (ra, rb) if not self._swap_out else (rb, ra)
        return float(lon), float(lat)
