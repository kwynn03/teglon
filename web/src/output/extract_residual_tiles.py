"""Suggest pointings for one telescope from the probability that previous observations have NOT covered.

Same ranking as `extract_tiles` (the detector's StaticTile grid, summed 4D NetPixelProb or 2D Prob),
except each map pixel's probability is first multiplied by the chance that a counterpart there would
have been missed by every observation already in ObservedTile for this event:

    residual_i = P_i * prod_obs (1 - f_i,obs)

  --coverage depth  (default) f is the fraction of the pixel's distance posterior (Gaussian in D, D > 0,
                    as in the paper's Eq. 14) within which a source of absolute magnitude --abs-mag would
                    be brighter than the observation's limiting magnitude after Milky Way extinction.
                    Shallow images only remove the nearby part of a pixel's probability.
  --coverage binary f = 1 for any pixel inside any observed footprint (Treasure Map-style masking).

Observations loaded against an earlier map of the same event are re-projected onto this map, so
skymap updates don't lose earlier coverage.

    python web/src/output/extract_residual_tiles.py --gw_id S230529ay --tele SWOPE --band r
"""

import argparse
import json
import os
import sys
import time
from collections import OrderedDict, defaultdict

import numpy as np
from astropy.table import Table
from scipy.special import erf

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from web.src.utilities.Database_Helpers import query_db  # noqa: E402

# Same short flags / bands as Teglon.extract_tiles.
DETECTOR_FLAGS = {"s": "SWOPE", "t": "THACHER", "n": "NICKEL", "t80": "T80S_T80S-Cam", "nf": "NEWFIRM"}
BAND_MAPPING = {"g": "SDSS g", "r": "SDSS r", "i": "SDSS i", "z": "SDSS z", "I": "Landolt I", "J": "UKIRT J"}

IN_CHUNK = 5000  # max ids per SQL IN (...) list


# --- pure helpers (no DB; unit-testable) --------------------------------------------------------
def _phi(x):
    return 0.5 * (1.0 + erf(np.asarray(x, dtype=float) / np.sqrt(2.0)))


def detection_fraction(mag_lim, a_lambda, abs_mag, mean_dist, sigma_dist):
    """Fraction of each pixel's distance posterior (Gaussian truncated at D=0) inside which a source
    of `abs_mag` would be brighter than `mag_lim` after `a_lambda` of extinction. Vectorized over
    pixels. Pixels without usable distance info get 0 (no coverage is claimed)."""
    mean_dist = np.asarray(mean_dist, dtype=float)
    sigma_dist = np.asarray(sigma_dist, dtype=float)
    d_max = 10.0 ** (0.2 * (mag_lim - abs_mag - a_lambda - 25.0))  # Mpc

    out = np.zeros_like(mean_dist)
    ok = np.isfinite(mean_dist) & (mean_dist > 0)
    gauss = ok & np.isfinite(sigma_dist) & (sigma_dist > 0)
    step = ok & ~gauss

    if np.any(gauss):
        mu, sig = mean_dist[gauss], sigma_dist[gauss]
        lo = _phi(-mu / sig)
        denom = np.clip(1.0 - lo, 1e-300, None)
        out[gauss] = (_phi((d_max - mu) / sig) - lo) / denom
    out[step] = (d_max >= mean_dist[step]).astype(float)
    return np.clip(out, 0.0, 1.0)


def select_tiles(tile_ids, residual, cum_prob, remaining_total, num_tiles):
    """Rank tiles by residual probability and keep them until their cumulative residual reaches
    cum_prob of the remaining (uncovered) probability, or num_tiles. Returns (order, cumulative)."""
    residual = np.asarray(residual, dtype=float)
    order = np.argsort(-residual, kind="stable")
    order = order[residual[order] > 0.0]
    cumulative = np.cumsum(residual[order])
    keep = cumulative <= cum_prob * remaining_total
    # Always include the tile that crosses the threshold so the target is actually reached.
    n = min(int(np.sum(keep)) + 1, len(order), num_tiles)
    return [tile_ids[i] for i in order[:n]], cumulative[:n]


# --- DB access ---------------------------------------------------------------------------------------
def _rows(sql):
    res = query_db([sql])
    return res[0] if res else []


def _chunked_rows(sql_template, ids):
    """Run sql_template (containing `{ids}`) over ids in IN-list chunks."""
    ids = list(ids)
    out = []
    for i in range(0, len(ids), IN_CHUNK):
        out += _rows(sql_template.format(ids=",".join(str(int(x)) for x in ids[i:i + IN_CHUNK])))
    return out


def _sql_str(s):
    return str(s).replace("\\", "\\\\").replace("'", "''")


def _observation_pixels(gw_id, map_id, nside):
    """{ObservedTile.id: [HealpixPixel.id on this map]} plus observation metadata for every observation
    of this event, re-projecting those loaded against other maps of the same event."""
    obs = _rows("""
        SELECT ot.id, ot.HealpixMap_id, ot.Detector_id, ot.RA, ot._Dec, ot.PositionAngle,
               ot.Mag_Lim, ot.EBV, b.F99_Coefficient
        FROM ObservedTile ot
        JOIN Band b ON b.id = ot.Band_id
        JOIN HealpixMap hm ON hm.id = ot.HealpixMap_id
        WHERE hm.GWID = '%s';""" % _sql_str(gw_id))
    meta = {int(r[0]): {"map_id": int(r[1]), "detector_id": int(r[2]), "ra": float(r[3]), "dec": float(r[4]),
                        "pa": float(r[5] or 0.0),
                        "mag_lim": None if r[6] is None else float(r[6]),
                        "a_lambda": float(r[7] or 0.0) * float(r[8] or 0.0)}
            for r in obs}

    pixels = defaultdict(list)
    for ot_id, hp_id in _rows("SELECT ObservedTile_id, HealpixPixel_id FROM ObservedTile_HealpixPixel "
                              "WHERE HealpixMap_id = %d;" % map_id):
        if int(ot_id) in meta:
            pixels[int(ot_id)].append(int(hp_id))

    stale = [ot_id for ot_id, m in meta.items() if m["map_id"] != map_id]
    if stale:
        from web.src.objects.Detector import Detector
        from web.src.objects.Tile import Tile

        index_to_id = {int(r[1]): int(r[0]) for r in
                       _rows("SELECT id, Pixel_Index FROM HealpixPixel WHERE HealpixMap_id = %d;" % map_id)}
        detectors = {}
        for ot_id in stale:
            m = meta[ot_id]
            if m["detector_id"] not in detectors:
                d = _rows("SELECT id, Name, ST_AsText(Poly) FROM Detector WHERE id = %d;" % m["detector_id"])[0]
                detectors[m["detector_id"]] = Detector(d[1], Detector.get_detector_vertices_from_teglon_db(d[2]),
                                                       detector_id=int(d[0]))
            t = Tile(m["ra"], m["dec"], detectors[m["detector_id"]], nside, position_angle_deg=m["pa"])
            pixels[ot_id] = [index_to_id[int(p)] for p in t.enclosed_pixel_indices if int(p) in index_to_id]

    return meta, pixels, len(stale)


# --- main entry point --------------------------------------------------------------------------------
def extract_residual_tiles(gw_id, tele, band="r", healpix_dir="./web/events/{GWID}",
                           healpix_file="bayestar.fits.gz", prob_type="4D", cum_prob=0.9, num_tiles=1000,
                           extinct=0.5, coverage="depth", abs_mag=-16.0, log=print):
    """Write a ranked residual-probability tile list for one telescope. Returns a summary dict."""
    if prob_type not in ("4D", "2D"):
        raise ValueError("prob_type must be 4D or 2D")
    if coverage not in ("depth", "binary"):
        raise ValueError("coverage must be depth or binary")
    if band not in BAND_MAPPING:
        raise ValueError("band must be one of %s" % list(BAND_MAPPING))
    if not 0.0 < cum_prob <= 1.0:
        raise ValueError("cum_prob must be in (0, 1]")

    t_start = time.time()
    prob_col = "hpc.NetPixelProb" if prob_type == "4D" else "hp.Prob"
    event_dir = healpix_dir.replace("{GWID}", gw_id)

    # Map, detector, band
    m = _rows("SELECT id, RescaledNSIDE FROM HealpixMap WHERE GWID = '%s' AND Filename = '%s';"
              % (_sql_str(gw_id), _sql_str(healpix_file)))
    if not m:
        raise ValueError("No HealpixMap for %s / %s" % (gw_id, healpix_file))
    map_id, nside = int(m[0][0]), int(m[0][1])

    detector_name = DETECTOR_FLAGS.get(tele, tele)
    if detector_name == "NICKEL":
        raise ValueError("NICKEL is galaxy-targeted, not tiled; residual tiling needs a StaticTile grid.")
    d = _rows("SELECT id, Name FROM Detector WHERE Name = '%s';" % _sql_str(detector_name))
    if not d:
        raise ValueError("Unknown detector `%s`" % detector_name)
    detector_id, detector_name = int(d[0][0]), d[0][1]

    b = _rows("SELECT F99_Coefficient FROM Band WHERE Name = '%s';" % _sql_str(BAND_MAPPING[band]))
    band_f99 = float(b[0][0])

    # Per-tile original probability (same aggregation as extract_tiles, without the cumulative cut)
    tiles = _rows("""
        SELECT st.id, st.FieldName, st.RA, st._Dec, SUM({prob}), AVG(hp.Mean), sp_ebv.EBV
        FROM StaticTile st
        JOIN StaticTile_HealpixPixel st_hp ON st_hp.StaticTile_id = st.id
        JOIN HealpixPixel hp ON hp.id = st_hp.HealpixPixel_id
        JOIN HealpixPixel_Completeness hpc ON hpc.HealpixPixel_id = hp.id
        JOIN SkyPixel_EBV sp_ebv ON sp_ebv.N128_SkyPixel_id = st.N128_SkyPixel_id
        WHERE st.Detector_id = {det} AND hp.HealpixMap_id = {map}
        GROUP BY st.id, st.FieldName, st.RA, st._Dec, sp_ebv.EBV;""".format(prob=prob_col, det=detector_id, map=map_id))
    tiles = [t for t in tiles if float(t[6]) * band_f99 <= extinct and float(t[4]) > 0.0]
    tile_info = {int(t[0]): t for t in tiles}
    log("%s: %d candidate %s tiles on map %d." % (gw_id, len(tiles), detector_name, map_id))

    total = _rows("SELECT SUM({prob}) FROM HealpixPixel hp JOIN HealpixPixel_Completeness hpc "
                  "ON hpc.HealpixPixel_id = hp.id WHERE hp.HealpixMap_id = {map};".format(prob=prob_col, map=map_id))
    total_prob = float(total[0][0] or 0.0) if total else 0.0

    # Previous observations -> per-pixel probability of having been missed
    obs_meta, obs_pixels, n_reprojected = _observation_pixels(gw_id, map_id, nside)
    covered_ids = sorted({p for pix in obs_pixels.values() for p in pix})
    log("%s: %d observations (%d re-projected from older maps) touching %d map pixels."
        % (gw_id, len(obs_meta), n_reprojected, len(covered_ids)))

    pix_rows = _chunked_rows("""
        SELECT hp.id, {prob}, hp.Mean, hp.Stddev
        FROM HealpixPixel hp JOIN HealpixPixel_Completeness hpc ON hpc.HealpixPixel_id = hp.id
        WHERE hp.HealpixMap_id = %d AND hp.id IN ({{ids}});""".format(prob=prob_col) % map_id, covered_ids)
    pix_pos = {int(r[0]): i for i, r in enumerate(pix_rows)}
    pix_prob = np.array([float(r[1]) for r in pix_rows])
    pix_mean = np.array([float(r[2]) for r in pix_rows])
    pix_sigma = np.array([float(r[3]) for r in pix_rows])

    missed = np.ones(len(pix_rows))
    n_used = 0
    for ot_id, pix in obs_pixels.items():
        idx = np.array([pix_pos[p] for p in pix if p in pix_pos], dtype=int)
        if len(idx) == 0:
            continue
        o = obs_meta[ot_id]
        if coverage == "binary":
            f = np.ones(len(idx))
        else:
            if o["mag_lim"] is None:
                continue  # no depth -> can't say what it would have caught
            f = detection_fraction(o["mag_lim"], o["a_lambda"], abs_mag, pix_mean[idx], pix_sigma[idx])
        missed[idx] *= (1.0 - f)
        n_used += 1

    removed_by_pixel = pix_prob * (1.0 - missed)
    removed_total = float(np.sum(removed_by_pixel))
    remaining_total = max(total_prob - removed_total, 0.0)

    # Subtract covered probability from each tile that contains covered pixels
    removed_by_tile = defaultdict(float)
    tile_pix = _chunked_rows("""
        SELECT st_hp.StaticTile_id, st_hp.HealpixPixel_id
        FROM StaticTile_HealpixPixel st_hp JOIN StaticTile st ON st.id = st_hp.StaticTile_id
        WHERE st.Detector_id = %d AND st_hp.HealpixMap_id = %d AND st_hp.HealpixPixel_id IN ({ids});"""
                             % (detector_id, map_id), covered_ids)
    for st_id, hp_id in tile_pix:
        i = pix_pos.get(int(hp_id))
        if i is not None:
            removed_by_tile[int(st_id)] += removed_by_pixel[i]

    tile_ids = list(tile_info)
    orig = np.array([float(tile_info[t][4]) for t in tile_ids])
    residual = np.clip(orig - np.array([removed_by_tile.get(t, 0.0) for t in tile_ids]), 0.0, None)
    chosen, cumulative = select_tiles(tile_ids, residual, cum_prob, remaining_total, num_tiles)
    resid_lookup = dict(zip(tile_ids, residual))

    # Output: same columns as extract_tiles, plus original probability and covered fraction
    out = Table(names=["Field_Name", "RA", "Dec", "Prob", "Percentile", "EBV", "A_lambda", "Lum_Dist",
                       "B_mag", "K_mag", "Orig_Prob", "Covered_Frac"],
                dtype=["U64", "f8", "f8", "f8", "f8", "f8", "f8", "f8", "f8", "f8", "f8", "f8"])
    for st_id, cum in zip(chosen, cumulative):
        t = tile_info[st_id]
        o = float(t[4])
        r = float(resid_lookup[st_id])
        out.add_row([t[1], float(t[2]), float(t[3]), r, float(cum), float(t[6]), float(t[6]) * band_f99,
                     float(t[5]), np.nan, np.nan, o, 1.0 - r / o if o > 0 else 0.0])

    out.meta["comments"] = OrderedDict([
        ("GW_ID", gw_id), ("HPX_FILE", healpix_file), ("TELESCOPE", detector_name),
        ("A_LAMBDA_BAND", BAND_MAPPING[band]), ("F99_BAND_COEF", band_f99), ("EXTINCT_THRESH", extinct),
        ("PROB_TYPE", prob_type), ("CUM_PROB_OF_REMAINING", cum_prob), ("NUM_TILES", num_tiles),
        ("COVERAGE", coverage), ("ABS_MAG", abs_mag if coverage == "depth" else None),
        ("N_OBSERVATIONS_USED", n_used), ("TOTAL_PROB", total_prob),
        ("PROB_ALREADY_COVERED", removed_total), ("PROB_REMAINING", remaining_total),
    ])

    os.makedirs(event_dir, exist_ok=True)
    out_path = os.path.join(event_dir, "%s_%s_%s_%s_residual_%s.txt"
                            % (gw_id, detector_name, prob_type, cum_prob, healpix_file))
    out.write(out_path, format="ascii.ecsv", overwrite=True)

    summary = {
        "gw_id": gw_id, "telescope": detector_name, "healpix_file": healpix_file, "prob_type": prob_type,
        "coverage": coverage, "abs_mag": abs_mag if coverage == "depth" else None,
        "observations_used": n_used, "prob_total": round(total_prob, 6),
        "prob_already_covered": round(removed_total, 6), "prob_remaining": round(remaining_total, 6),
        "num_tiles": len(out), "prob_in_suggested_tiles": round(float(cumulative[-1]) if len(cumulative) else 0.0, 6),
        "tile_file": out_path,
    }
    log("%s: %.3f of %.3f probability already covered; %d %s tiles enclose %.3f of what remains. -> %s (%.1fs)"
        % (gw_id, removed_total, total_prob, len(out), detector_name, summary["prob_in_suggested_tiles"],
           out_path, time.time() - t_start))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description="Suggest pointings from the probability not yet covered.")
    p.add_argument("--gw_id", required=True)
    p.add_argument("--tele", required=True, help="Short flag (s, t, t80, nf) or exact Detector.Name.")
    p.add_argument("--band", default="r", choices=list(BAND_MAPPING))
    p.add_argument("--healpix_dir", default="./web/events/{GWID}")
    p.add_argument("--healpix_file", default="bayestar.fits.gz")
    p.add_argument("--prob_type", default="4D", choices=["4D", "2D"])
    p.add_argument("--cum_prob", type=float, default=0.9, help="Fraction of the REMAINING probability to enclose.")
    p.add_argument("--num_tiles", type=int, default=1000)
    p.add_argument("--extinct", type=float, default=0.5)
    p.add_argument("--coverage", default="depth", choices=["depth", "binary"])
    p.add_argument("--abs_mag", type=float, default=-16.0,
                   help="Assumed counterpart absolute magnitude for --coverage depth. Default: -16.0")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    summary = extract_residual_tiles(a.gw_id, a.tele, a.band, a.healpix_dir, a.healpix_file, a.prob_type,
                                     a.cum_prob, a.num_tiles, a.extinct, a.coverage, a.abs_mag, log=log)
    if a.json:
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())