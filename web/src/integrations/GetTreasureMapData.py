"""Cron entry point: pull GW events and their pointings from Treasure Map, then plan tiles.

    1. Events:    each recent significant event not yet in Teglon -> load_map.py (newest alert's skymap)
    2. Pointings: every recent event in Teglon -> new Treasure Map pointings loaded as ObservedTiles
                  (get_TM_pointings.sync_pointings; with --planned, other groups' planned pointings too)
    3. Plan:      events that are new or whose pointings changed -> `teglon extract` -> `teglon plot`
                  (with --planned, also `teglon extract --exclude-planned`)

Extract runs after the pointings are loaded, so already-imaged pixels are excluded from the tiles.

    python web/src/integrations/GetTreasureMapData.py                   # cron default
    python web/src/integrations/GetTreasureMapData.py --gw_id S230529ay # one event
    python web/src/integrations/GetTreasureMapData.py --planned         # also avoid others' planned fields
    python web/src/integrations/GetTreasureMapData.py --role test --lookback_days 1   # LIGO mock (MS...) events
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from web.src.utilities.Database_Helpers import query_db  # noqa: E402
from web.src.utilities.get_TM_pointings import (  # noqa: E402
    SAFE_ID, latest_map, sync_pointings, tm_get, treasuremap_settings,
)

logger = logging.getLogger("tm_events")

DEFAULT_TELE = "a"  # all telescopes, same as `teglon run`


def _run(argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    logger.info("+ %s", " ".join(argv))
    return subprocess.call(argv, cwd=REPO_ROOT, env=env)


def latest_alerts(endpoint, token, gw_id=None, lookback_days=30, only_with_pointings=False, role="observation"):
    """{graceid: newest alert} from Treasure Map's query_alerts.

    `role` is the alert role to ingest: "observation" (real events), "test" (LIGO mock events, MS... ids) or
    "all". Ignored with `gw_id`, which fetches that event whatever its role."""
    params = {"api_token": token}
    if gw_id:
        params["graceid"] = gw_id
    else:
        params["far"] = "Significant"
        if role != "all":
            params["role"] = role
        if lookback_days > 0:
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            params["timesent_after"] = since.strftime("%Y-%m-%dT%H:%M:%S")
        if only_with_pointings:
            params["has_pointings"] = "True"

    latest = {}
    for alert in tm_get(endpoint, "query_alerts", params):
        graceid = alert.get("graceid")
        if not graceid:
            continue
        sent = str(alert.get("timesent") or alert.get("datecreated") or "")  # ISO strings sort correctly
        if graceid not in latest or sent >= latest[graceid][0]:
            latest[graceid] = (sent, alert)
    return {g: a for g, (_, a) in latest.items()}


def ingest_event(gw_id, alert):
    """load_map for one event. Returns True on success."""
    load_map = [sys.executable, os.path.join("web", "src", "ingestion", "load_map.py"), "--gw_id", gw_id]
    url = alert.get("skymap_fits_url")
    if url:
        # load_map downloads the file from GraceDB by name.
        load_map += ["--healpix_file", url.rstrip("/").split("/")[-1]]
    if _run(load_map) != 0:
        logger.error("%s: load_map failed.", gw_id)
        return False

    if latest_map(gw_id)[0] is None:
        logger.error("%s: load_map reported success but no HealpixMap row exists.", gw_id)
        return False
    return True


def plan_event(gw_id, planned=False):
    """extract -> plot on the event's latest map (plus an --exclude-planned extract). Returns True on success."""
    # Use the filename load_map actually stored, so extract/plot find the right map.
    map_id, healpix_file = latest_map(gw_id)
    if map_id is None:
        logger.error("%s: no map loaded; can't extract tiles.", gw_id)
        return False

    for stage in ("extract", "plot"):
        rc = _run([sys.executable, "-m", "web.src.services.teglon_cli", stage, gw_id,
                   "--healpix-file", healpix_file, "--tele", DEFAULT_TELE])
        if rc != 0:
            logger.error("%s: teglon %s failed (rc=%s).", gw_id, stage, rc)
            return False

    if planned:
        rc = _run([sys.executable, "-m", "web.src.services.teglon_cli", "extract", gw_id,
                   "--healpix-file", healpix_file, "--tele", DEFAULT_TELE, "--exclude-planned"])
        if rc != 0:
            logger.error("%s: teglon extract --exclude-planned failed (rc=%s).", gw_id, rc)
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest Treasure Map events and pointings into Teglon.")
    parser.add_argument("--gw_id", default="", help="Only this event. Default: all recent significant events.")
    parser.add_argument("--lookback_days", type=int, default=30,
                        help="Alerts/events from the last N days (0 = all). Default: 30")
    parser.add_argument("--only_with_pointings", action="store_true",
                        help="Only ingest events that already have Treasure Map pointings.")
    parser.add_argument("--skip_pointings", action="store_true", help="Ingest events only.")
    parser.add_argument("--role", default="observation", choices=["observation", "test", "all"],
                        help="Alert role to ingest: real events (observation), LIGO mock MS... events (test), "
                             "or both. Mock alerts are frequent, so pair `test` with a short --lookback_days. "
                             "Ignored with --gw_id. Default: observation")
    parser.add_argument("--planned", action="store_true",
                        help="Also load other groups' planned pointings and extract tiles that avoid them "
                             "(*_exclplanned.txt). Clear them with `teglon clear-planned <GWID>`.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start = time.time()

    token, endpoint = treasuremap_settings()
    if not token:
        logger.error("No Treasure Map API token (TM_API_TOKEN env var or [treasuremap] in Settings.ini).")
        return 1

    rc = 0
    try:
        alerts = latest_alerts(endpoint, token, args.gw_id or None, args.lookback_days,
                               args.only_with_pointings, args.role)
    except Exception as e:
        logger.error("Treasure Map query_alerts failed: %s", e)
        alerts, rc = {}, 1

    res = query_db(["SELECT DISTINCT GWID FROM HealpixMap;"])
    archived = {r[0] for r in (res[0] if res else [])}

    new, failed = [], []
    for gw_id in sorted(alerts):
        alert = alerts[gw_id]
        if not SAFE_ID.match(gw_id):
            logger.warning("Skipping unexpected graceid %r", gw_id)
            continue
        if str(alert.get("alert_type", "")).lower() == "retraction":
            logger.info("%s: retracted; skipping.", gw_id)
            continue
        if gw_id in archived:
            continue
        logger.info("%s: new event, ingesting.", gw_id)
        (new if ingest_event(gw_id, alert) else failed).append(gw_id)

    logger.info("Events: %d alerts, %d new ingested %s, %d failed %s.",
                len(alerts), len(new), new, len(failed), failed)
    logger.info("Event ingestion took %.1fs.", time.time() - start)

    updated = set()
    if not args.skip_pointings:
        t = time.time()
        grace_ids = [args.gw_id] if args.gw_id else None
        if sync_pointings(grace_ids, args.lookback_days, updated=updated) != 0:
            rc = 1
        if args.planned and sync_pointings(grace_ids, args.lookback_days, status="planned", updated=updated) != 0:
            rc = 1
        logger.info("Pointing sync took %.1fs.", time.time() - t)

    # Plan after the pointings are in, so the tiles skip what's already been imaged.
    to_plan = sorted(set(new) | updated)
    plan_failed = [g for g in to_plan if not plan_event(g, planned=args.planned)]
    logger.info("Planned %d event(s) %s, %d failed %s.", len(to_plan), to_plan, len(plan_failed), plan_failed)

    return 1 if (rc or failed or plan_failed) else 0


if __name__ == "__main__":
    sys.exit(main())