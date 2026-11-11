"""Load Treasure Map pointings into Teglon as ObservedTiles.

Meant to run from cron (usually via GetTreasureMapData.py). Per event this makes one Treasure Map
`pointings` query, maps each TM instrument to its Teglon Detector (Detector.TM_id), and loads every
detector's new pointings in a single pass against the event's latest map (load_observed_tiles.load_tables).

    completed pointings -> ObservedTile.Source = 'TreasureMap'          (only new ones are loaded)
    planned pointings   -> ObservedTile.Source = 'TreasureMap-planned'  (replaced wholesale on every sync)

A planned pointing that later shows up as completed has its planned row removed. `extract --exclude-planned`
also skips pixels covered by planned pointings; `--clear_planned` removes them.

    python web/src/utilities/get_TM_pointings.py                    # completed, events from the last 30 days
    python web/src/utilities/get_TM_pointings.py --grace_id S230529ay --status planned
    python web/src/utilities/get_TM_pointings.py --lookback_days 0  # every event in the DB
    python web/src/utilities/get_TM_pointings.py --grace_id S230529ay --clear_planned
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from configparser import RawConfigParser
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from web.src.utilities.Database_Helpers import query_db, delete_rows  # noqa: E402
from web.src.utilities.tm_bands import teglon_band_name, vega_to_ab_offset  # noqa: E402

logger = logging.getLogger("tm_pointings")

# Treasure Map instruments Teglon can't tile (non-UVOIR), skipped even if a Detector row exists.
SKIP_TM_INSTRUMENT_IDS = {13}  # Swift/XRT

# GW ids go straight into SQL strings, so only accept plain identifiers.
SAFE_ID = re.compile(r"^[A-Za-z0-9_]+$")

# Must match load_observed_tiles.TM_SOURCE / TM_SOURCE_PLANNED (not imported here to keep this module light).
TM_SOURCE = "TreasureMap"
TM_SOURCE_PLANNED = "TreasureMap-planned"


# --- Treasure Map --------------------------------------------------------------------------------
def treasuremap_settings():
    """(token, endpoint) from TM_API_TOKEN / TM_ENDPOINT, falling back to [treasuremap] in Settings.ini."""
    token = os.environ.get("TM_API_TOKEN", "")
    endpoint = os.environ.get("TM_ENDPOINT", "")
    if not token or not endpoint:
        cfg = RawConfigParser()
        cfg.read([os.environ.get("TEGLON_SETTINGS", ""), os.path.join(REPO_ROOT, "Settings.ini")])
        if cfg.has_section("treasuremap"):
            token = token or cfg.get("treasuremap", "TM_API_TOKEN", fallback="")
            endpoint = endpoint or cfg.get("treasuremap", "TM_ENDPOINT", fallback="")
    return token, (endpoint or "https://treasuremap.space/api/v1").rstrip("/")


def tm_get(endpoint, path, params):
    """GET a Treasure Map endpoint and return a list of dicts (handles the old list-of-JSON-strings shape)."""
    try:
        r = requests.get("%s/%s" % (endpoint, path), params=params, timeout=120)
    except requests.RequestException as e:
        # requests' message includes the full URL, i.e. the api_token query param; don't propagate it.
        raise RuntimeError("Treasure Map `%s` request failed: %s" % (path, type(e).__name__)) from None
    if r.status_code != 200:
        raise RuntimeError("Treasure Map `%s` failed (HTTP %s): %s" % (path, r.status_code, r.text[:300]))
    data = r.json()
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = [data]
    return [json.loads(d) if isinstance(d, str) else d for d in data]


def unloadable_reason(pointing, band_names):
    """Why load_observed_tiles.py couldn't ingest this pointing, or None if it can. Checked up front so
    unloadable pointings don't make an instrument look out of date on every run."""
    if not (pointing.get("position") and pointing.get("time")):
        return "no position/time"
    unit = str(pointing.get("depth_unit") or "").lower()
    band = teglon_band_name(pointing.get("band"))
    if pointing.get("depth") is None:
        return "depth missing"
    if "vega_mag" in unit:
        if vega_to_ab_offset(band) is None:
            return "Vega depth in band %s (no Vega->AB offset)" % (band or "missing")
    elif not ("ab_mag" in unit or "flux_jy" in unit):
        return "depth unit %s" % (pointing.get("depth_unit") or "missing")
    if band not in band_names:
        return "band %s" % (band or "missing")
    return None


def is_loadable(pointing, band_names):
    return unloadable_reason(pointing, band_names) is None


# --- Teglon DB ---------------------------------------------------------------------------------------
def _rows(sql):
    res = query_db([sql])
    return res[0] if res else []


def event_date(gw_id):
    """Date encoded in a GW id (S230529ay, MS230529a, GW170817), or None."""
    m = re.match(r"^(?:M?S|GW)(\d{2})(\d{2})(\d{2})", gw_id)
    if not m:
        return None
    try:
        return datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def events_to_process(grace_ids, lookback_days):
    if grace_ids:
        return list(dict.fromkeys(grace_ids))
    ids = sorted({r[0] for r in _rows("SELECT DISTINCT GWID FROM HealpixMap;")})
    if lookback_days <= 0:
        return ids
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    # Keep ids we can't date rather than silently dropping them.
    return [g for g in ids if event_date(g) is None or event_date(g) >= cutoff]


def latest_map(gw_id):
    """(HealpixMap.id, Filename) of the most recently loaded map for an event, or (None, None)."""
    rows = _rows("SELECT id, Filename FROM HealpixMap WHERE GWID = '%s' ORDER BY id DESC LIMIT 1;" % gw_id)
    return (int(rows[0][0]), rows[0][1]) if rows else (None, None)


def teglon_detectors_by_tm_id():
    """{TM instrument id: (Detector.id, Detector.Name)} for detectors registered from Treasure Map."""
    return {int(r[0]): (int(r[1]), r[2])
            for r in _rows("SELECT TM_id, id, Name FROM Detector WHERE TM_id IS NOT NULL;")}


def teglon_band_names():
    return {r[0] for r in _rows("SELECT `Name` FROM Band;")}


def loaded_field_names(map_id, source):
    """{Detector.id: FieldNames} already loaded against this map with this Source."""
    loaded = defaultdict(set)
    for r in _rows("SELECT Detector_id, FieldName FROM ObservedTile WHERE HealpixMap_id = %d AND Source = '%s';"
                   % (map_id, source)):
        loaded[int(r[0])].add(r[1])
    return loaded


def delete_planned(map_ids, field_names=None):
    """Delete planned ObservedTiles (and their pixel links) for these maps; optionally only these FieldNames.
    Returns the number of tiles deleted."""
    if not map_ids:
        return 0
    sql = "SELECT id FROM ObservedTile WHERE Source = '%s' AND HealpixMap_id IN (%s)" % (
        TM_SOURCE_PLANNED, ",".join(str(int(m)) for m in map_ids))
    if field_names is not None:
        # FieldNames are the `TM_<pointing id>` values this module writes; anything else can't match.
        safe = sorted(f for f in field_names if re.match(r"^TM_\d+$", str(f)))
        if not safe:
            return 0
        sql += " AND FieldName IN (%s)" % ",".join("'%s'" % f for f in safe)
    ids = _rows(sql + ";")
    if not ids:
        return 0
    # Pixel links first: they reference the tiles.
    delete_rows("DELETE FROM ObservedTile_HealpixPixel WHERE ObservedTile_id IN (%s)", ids)
    delete_rows("DELETE FROM ObservedTile WHERE id IN (%s)", ids)
    return len(ids)


def clear_planned(gw_id, healpix_file=None):
    """Remove an event's planned Treasure Map pointings (all of its maps, or just `healpix_file`)."""
    if not SAFE_ID.match(gw_id):
        raise ValueError("Unexpected GW id %r" % gw_id)
    sql = "SELECT id FROM HealpixMap WHERE GWID = '%s'" % gw_id
    if healpix_file:
        sql += " AND Filename = '%s'" % healpix_file.replace("'", "''")
    map_ids = [int(r[0]) for r in _rows(sql + ";")]
    n = delete_planned(map_ids)
    logger.info("%s: cleared %d planned pointing(s) across %d map(s).", gw_id, n, len(map_ids))
    return n


# --- loading -----------------------------------------------------------------------------------------
def sync_pointings(grace_ids=None, lookback_days=30, status="completed", updated=None):
    """Load Treasure Map pointings (`completed` or `planned`) for each event's latest map.
    Returns 0 on success, 1 if anything failed.

    If `updated` (a set) is given, the GW ids whose pointings changed are added to it, so the
    caller can re-extract tiles for just those events."""
    # Heavy (healpy, shapely, ...); only needed when actually loading.
    from web.src.ingestion.load_observed_tiles import load_tables, pointings_to_table

    if status not in ("completed", "planned"):
        logger.error("Unsupported status %r (use completed or planned).", status)
        return 1
    planned = status == "planned"
    source = TM_SOURCE_PLANNED if planned else TM_SOURCE

    token, endpoint = treasuremap_settings()
    if not token:
        logger.error("No Treasure Map API token (TM_API_TOKEN env var or [treasuremap] in Settings.ini).")
        return 1

    detectors = teglon_detectors_by_tm_id()
    if not detectors:
        logger.error("No Detector rows have a TM_id; nothing to load pointings into.")
        return 1
    band_names = teglon_band_names()

    failures = 0
    events = events_to_process(grace_ids, lookback_days)
    logger.info("Syncing %s Treasure Map pointings for %d event(s).", status, len(events))

    for gw_id in events:
        if not SAFE_ID.match(gw_id):
            logger.warning("Skipping unexpected GW id %r", gw_id)
            continue

        map_id, healpix_file = latest_map(gw_id)
        if map_id is None:
            logger.warning("%s: no map loaded in Teglon yet; skipping pointings.", gw_id)
            continue

        try:
            pointings = tm_get(endpoint, "pointings", {"api_token": token, "graceid": gw_id, "status": status})
        except Exception as e:
            logger.error("%s: Treasure Map pointings query failed: %s", gw_id, e)
            failures += 1
            continue

        by_instrument = defaultdict(list)
        for p in pointings:
            if p.get("instrumentid") is not None:
                by_instrument[int(p["instrumentid"])].append(p)

        logger.info("%s: %d %s pointings across %d instrument(s) (map %s, %s).",
                    gw_id, len(pointings), status, len(by_instrument), map_id, healpix_file)

        # Planned pointings are replaced wholesale, so cancelled/changed plans don't linger.
        changed = False
        if planned:
            cleared = delete_planned([map_id])
            if cleared:
                logger.info("%s: cleared %d previously loaded planned pointing(s).", gw_id, cleared)
                changed = True
            existing = {}
        else:
            existing = loaded_field_names(map_id, source)

        # Keyed by Detector.id: several TM instruments can map to detectors that share a Name (e.g. MASTER).
        new_by_detector = defaultdict(list)
        for tm_id in sorted(by_instrument):
            pts = by_instrument[tm_id]
            if tm_id in SKIP_TM_INSTRUMENT_IDS:
                continue
            if tm_id not in detectors:
                logger.info("%s: TM instrument %s has %d pointings but no Teglon detector "
                            "(add it with `teglon add-telescope --tm-detector-id %s`).",
                            gw_id, tm_id, len(pts), tm_id)
                continue

            detector_id, detector_name = detectors[tm_id]
            label = "%s (TM %s)" % (detector_name, tm_id)
            loaded = existing.get(detector_id, ())
            reasons = defaultdict(int)
            new = []
            for p in pts:
                reason = unloadable_reason(p, band_names)
                if reason:
                    reasons[reason] += 1
                elif "TM_%s" % p.get("id") not in loaded:
                    new.append(p)
            n_loaded = len(pts) - len(new) - sum(reasons.values())
            if reasons:
                logger.info("%s: %s: %d pointing(s) can't be loaded: %s.", gw_id, label, sum(reasons.values()),
                            ", ".join("%d %s" % (n, r) for r, n in sorted(reasons.items(), key=lambda x: -x[1])))
            if not new:
                logger.info("%s: %s: nothing new (%d already loaded).", gw_id, label, n_loaded)
                continue
            logger.info("%s: %s: %d new %s pointing(s) to load.", gw_id, label, len(new), status)
            new_by_detector[detector_id].extend(new)

        tables = {}
        for detector_id, new in new_by_detector.items():
            table, skipped = pointings_to_table(new, source=source)
            if skipped:
                logger.info("%s: detector id %s: skipped %d pointing(s) that couldn't be converted.",
                            gw_id, detector_id, len(skipped))
            tables[detector_id] = table

        if tables:
            tile_dir = os.path.join(REPO_ROOT, "web", "events", gw_id, "observed_tiles")
            rc, inserted = load_tables(gw_id, healpix_file, tile_dir, tables)
            if rc != 0:
                logger.error("%s: loading pointings failed for at least one detector (%s).", gw_id, inserted)
                failures += 1
            if any(inserted.values()):
                changed = True

        if not planned:
            # A planned pointing that's now completed keeps its TM id; drop the stale planned copy.
            completed = set().union(*loaded_field_names(map_id, source).values())
            superseded = delete_planned([map_id], completed)
            if superseded:
                logger.info("%s: removed %d planned pointing(s) now completed.", gw_id, superseded)
                changed = True

        if changed and updated is not None:
            updated.add(gw_id)

    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load Treasure Map pointings into Teglon.")
    parser.add_argument("--grace_id", action="append", default=None,
                        help="Event to sync (repeatable). Default: every event in the DB within --lookback_days.")
    parser.add_argument("--lookback_days", type=int, default=30,
                        help="Only sync events from the last N days (0 = all). Default: 30")
    parser.add_argument("--status", default="completed", choices=["completed", "planned"])
    parser.add_argument("--clear_planned", action="store_true",
                        help="Remove the planned pointings of each --grace_id instead of syncing.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start = time.time()
    if args.clear_planned:
        if not args.grace_id:
            parser.error("--clear_planned needs at least one --grace_id")
        for gw_id in args.grace_id:
            clear_planned(gw_id)
        rc = 0
    else:
        rc = sync_pointings(args.grace_id, args.lookback_days, args.status)
    logger.info("Done in %.1fs (rc=%s).", time.time() - start, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
