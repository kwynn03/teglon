"""Unit tests for the Treasure Map -> Teglon pointing pipeline.

Covers:
  - tm_bands            (TM band -> Teglon Band.Name)
  - get_TM_pointings    (sync logic, planned/completed handling, clear-planned, token scrubbing)
  - GetTreasureMapData  (newest alert per event, cron step order)
  - load_observed_tiles (TM pointing -> tile-row conversion; skipped without healpy)
  - teglon_cli wiring   (load-obs, extract, sync-pointings, tm-ingest, clear-planned, list-telescopes)

Like test_new_functions.py, nothing here touches the real database or network: query_db, the
Treasure Map API and the loader are replaced with in-memory fakes.
"""
import importlib
import importlib.util
import io
import json
import re
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

import web.src.services.teglon_cli as tc
from web.src.utilities.tm_bands import teglon_band_name

HAS_HEALPY = importlib.util.find_spec("healpy") is not None


def _import_without_db(module_name):
    """Import a module that does `from Database_Helpers import ...` even when the MySQL drivers aren't
    installed (the tests patch query_db/delete_rows on the module anyway)."""
    stub = types.ModuleType("web.src.utilities.Database_Helpers")
    stub.query_db = stub.delete_rows = stub.batch_insert = stub.bulk_upload = \
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unpatched DB call"))
    try:
        return importlib.import_module(module_name)
    except ImportError:
        with mock.patch.dict(sys.modules, {"web.src.utilities.Database_Helpers": stub}):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)


g = _import_without_db("web.src.utilities.get_TM_pointings")
gtm = _import_without_db("web.src.integrations.GetTreasureMapData")


def pointing(pid, inst, band="r", unit="ab_mag", depth=21.0):
    return {"id": pid, "instrumentid": inst, "position": "POINT(10 20)", "time": "2025-02-06T10:00:00",
            "depth": depth, "depth_unit": unit, "band": band}


# --- tm_bands -------------------------------------------------------------------------------------
class TestTMBands(unittest.TestCase):
    def test_maps_known_bands(self):
        self.assertEqual(teglon_band_name("r"), "SDSS r")
        self.assertEqual(teglon_band_name("J"), "UKIRT J")
        self.assertEqual(teglon_band_name("open"), "Clear")

    def test_vega_to_ab_offsets(self):
        from web.src.utilities.tm_bands import vega_to_ab_offset
        # Blanton & Roweis 2007, Table 1
        self.assertEqual(vega_to_ab_offset("Landolt R"), 0.21)
        self.assertEqual(vega_to_ab_offset("SDSS g"), -0.08)
        self.assertEqual(vega_to_ab_offset("UKIRT K"), 1.85)
        self.assertEqual(vega_to_ab_offset(teglon_band_name("clear")), 0.21)  # unfiltered approximated as R
        self.assertIsNone(vega_to_ab_offset("w"))

    def test_enum_repr_and_passthrough(self):
        self.assertEqual(teglon_band_name("bandpass.g"), "SDSS g")
        self.assertEqual(teglon_band_name("SDSS r"), "SDSS r")  # unknown -> used as-is
        self.assertEqual(teglon_band_name(None), "")


# --- get_TM_pointings: pure helpers -----------------------------------------------------------------
class TestPointingHelpers(unittest.TestCase):
    def test_event_date(self):
        self.assertEqual(g.event_date("S250206dm").strftime("%Y-%m-%d"), "2025-02-06")
        self.assertEqual(g.event_date("MS230529a").strftime("%Y-%m-%d"), "2023-05-29")
        self.assertEqual(g.event_date("GW170817").strftime("%Y-%m-%d"), "2017-08-17")
        self.assertIsNone(g.event_date("S991399x"))  # invalid month
        self.assertIsNone(g.event_date("foo"))

    def test_explicit_events_deduplicated_in_order(self):
        self.assertEqual(g.events_to_process(["S2", "S1", "S2"], 30), ["S2", "S1"])

    def test_safe_id(self):
        self.assertTrue(g.SAFE_ID.match("S250206dm"))
        self.assertFalse(g.SAFE_ID.match("S1'; DROP TABLE Detector;--"))

    def test_is_loadable(self):
        bands = {"SDSS r", "UKIRT J"}
        self.assertTrue(g.is_loadable(pointing(1, 47), bands))
        self.assertTrue(g.is_loadable(pointing(1, 47, unit="flux_jy", depth=1e-5), bands))
        self.assertTrue(g.is_loadable(pointing(1, 47, unit="vega_mag"), bands))   # r has a Vega->AB offset
        self.assertFalse(g.is_loadable(pointing(1, 47, unit="flux_erg"), bands))
        self.assertFalse(g.is_loadable(pointing(1, 47, band="w"), bands))  # band Teglon doesn't know
        self.assertFalse(g.is_loadable(dict(pointing(1, 47), depth=None), bands))
        self.assertFalse(g.is_loadable(dict(pointing(1, 47), time=None), bands))

    def test_tm_get_error_does_not_leak_token(self):
        import requests
        err = requests.ConnectionError("Max retries exceeded with url: /pointings?api_token=SECRETCANARY")
        with mock.patch.object(g.requests, "get", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                g.tm_get("https://example.invalid/api/v1", "pointings", {"api_token": "SECRETCANARY"})
        self.assertNotIn("SECRETCANARY", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_treasuremap_settings_prefers_env(self):
        with mock.patch.dict("os.environ", {"TM_API_TOKEN": "tok", "TM_ENDPOINT": "https://tm.example/api/v1/"}):
            self.assertEqual(g.treasuremap_settings(), ("tok", "https://tm.example/api/v1"))


# --- get_TM_pointings: sync / planned / clear -------------------------------------------------------
class FakeTeglonDB:
    """Just enough of the ObservedTile table for get_TM_pointings' queries."""

    # (TM_id, Detector.id, Name); two TM instruments map to detectors that share the Name MASTER
    DETECTORS = [(47, 1, "ZTF"), (94, 2, "WINTER"), (11, 3, "Sinistro"), (102, 4, "MASTER"), (103, 5, "MASTER")]

    def __init__(self):
        self.tiles = []
        self.next_id = 1
        self.link_deletes = []

    def query_db(self, qs, commit=False, raise_on_error=False):
        q = qs[0]
        if "FROM Detector WHERE TM_id IS NOT NULL" in q:
            return [list(self.DETECTORS)]
        if "SELECT `Name` FROM Band" in q:
            return [[("SDSS r",), ("SDSS g",), ("UKIRT J",)]]
        if "FROM HealpixMap WHERE GWID" in q:
            return [[(9, "bayestar.fits.gz")]]
        if q.startswith("SELECT Detector_id, FieldName FROM ObservedTile"):
            src = re.search(r"Source = '([^']+)'", q).group(1)
            return [[(t["det"], t["fn"]) for t in self.tiles if t["src"] == src]]
        if q.startswith("SELECT id FROM ObservedTile WHERE Source"):
            m = re.search(r"FieldName IN \(([^)]*)\)", q)
            names = set(re.findall(r"'([^']+)'", m.group(1))) if m else None
            return [[(t["id"],) for t in self.tiles
                     if t["src"] == g.TM_SOURCE_PLANNED and (names is None or t["fn"] in names)]]
        raise AssertionError("unexpected SQL: %s" % q)

    def delete_rows(self, q, id_tuples):
        ids = {i[0] for i in id_tuples}
        if "ObservedTile_HealpixPixel" in q:
            self.link_deletes.append(ids)
        else:
            assert self.link_deletes and self.link_deletes[-1] == ids, "tiles deleted before their pixel links"
            self.tiles = [t for t in self.tiles if t["id"] not in ids]

    # fake loader
    def pointings_to_table(self, pts, source):
        return [{"fn": "TM_%s" % p["id"], "src": source} for p in pts], []

    def load_tables(self, gw_id, healpix_file, tile_dir, tables):
        # sync passes Detector.id keys; record loads by name for readable assertions
        name = {i: n for _, i, n in self.DETECTORS}
        self.loads.append({name[d]: len(t) for d, t in tables.items()})
        self.load_keys.append(sorted(tables))
        for d, rows in tables.items():
            for r in rows:
                self.tiles.append({"id": self.next_id, "det": d, "fn": r["fn"], "src": r["src"]})
                self.next_id += 1
        return 0, {d: len(t) for d, t in tables.items()}

    def fns(self, source):
        return sorted(t["fn"] for t in self.tiles if t["src"] == source)


class TestSyncPointings(unittest.TestCase):
    def setUp(self):
        self.db = FakeTeglonDB()
        self.db.loads = []
        self.db.load_keys = []
        self.tm = {"completed": [], "planned": []}
        self.tm_calls = []

        def tm_get(endpoint, path, params):
            self.tm_calls.append((path, params["status"]))
            return self.tm[params["status"]]

        loader = types.ModuleType("web.src.ingestion.load_observed_tiles")
        loader.load_tables = self.db.load_tables
        loader.pointings_to_table = self.db.pointings_to_table
        for p in (mock.patch.object(g, "query_db", self.db.query_db),
                  mock.patch.object(g, "delete_rows", self.db.delete_rows),
                  mock.patch.object(g, "tm_get", tm_get),
                  mock.patch.object(g, "treasuremap_settings", lambda: ("tok", "https://tm.example/api/v1")),
                  mock.patch.dict(sys.modules, {"web.src.ingestion.load_observed_tiles": loader})):
            p.start()
            self.addCleanup(p.stop)

    def sync(self, status="completed"):
        updated = set()
        rc = g.sync_pointings(["S250206dm"], status=status, updated=updated)
        return rc, updated

    def test_one_query_and_one_load_for_all_detectors(self):
        self.tm["completed"] = [pointing(1, 47), pointing(2, 47, "g"), pointing(3, 94, "J"), pointing(4, 11),
                                pointing(5, 11, "w"),  # band Teglon doesn't know -> not loaded
                                pointing(6, 999),      # TM instrument with no Teglon detector
                                pointing(7, 13)]       # Swift/XRT, always skipped
        rc, updated = self.sync()
        self.assertEqual(rc, 0)
        self.assertEqual(self.tm_calls, [("pointings", "completed")])
        self.assertEqual(self.db.loads, [{"ZTF": 2, "WINTER": 1, "Sinistro": 1}])
        self.assertEqual(updated, {"S250206dm"})

    def test_detectors_sharing_a_name_load_separately(self):
        self.tm["completed"] = [pointing(1, 102), pointing(2, 103), pointing(3, 103)]
        self.sync()
        self.assertEqual(self.db.load_keys, [[4, 5]])  # by Detector.id, not merged/overwritten by Name
        self.assertEqual(sorted((t["det"], t["fn"]) for t in self.db.tiles), [(4, "TM_1"), (5, "TM_2"), (5, "TM_3")])

    def test_unloadable_pointings_are_explained(self):
        self.tm["completed"] = [pointing(1, 102, band="w"), pointing(2, 102, unit="flux_erg"), pointing(3, 102)]
        with self.assertLogs(g.logger, "INFO") as logs:
            self.sync()
        text = "\n".join(logs.output)
        self.assertIn("MASTER (TM 102): 2 pointing(s) can't be loaded: 1 band w, 1 depth unit flux_erg", text)
        self.assertEqual(self.db.loads, [{"MASTER": 1}])

    def test_rerun_loads_nothing(self):
        self.tm["completed"] = [pointing(1, 47), pointing(5, 11, "w")]
        self.sync()
        self.db.loads.clear()
        rc, updated = self.sync()
        self.assertEqual((rc, self.db.loads, updated), (0, [], set()))

    def test_planned_tagged_and_replaced_wholesale(self):
        self.tm["planned"] = [pointing(10, 47), pointing(11, 94, "J")]
        self.sync("planned")
        self.assertEqual(self.db.fns(g.TM_SOURCE_PLANNED), ["TM_10", "TM_11"])
        self.assertEqual(self.db.fns(g.TM_SOURCE), [])

        self.tm["planned"] = [pointing(11, 94, "J")]  # TM_10 cancelled
        rc, updated = self.sync("planned")
        self.assertEqual(self.db.fns(g.TM_SOURCE_PLANNED), ["TM_11"])
        self.assertEqual(updated, {"S250206dm"})

    def test_completed_supersedes_planned(self):
        self.tm["planned"] = [pointing(11, 94, "J"), pointing(12, 47)]
        self.sync("planned")
        self.tm["completed"] = [pointing(11, 94, "J")]
        rc, updated = self.sync()
        self.assertEqual(self.db.fns(g.TM_SOURCE), ["TM_11"])
        self.assertEqual(self.db.fns(g.TM_SOURCE_PLANNED), ["TM_12"])
        self.assertEqual(updated, {"S250206dm"})

    def test_clear_planned_keeps_completed(self):
        self.tm["completed"] = [pointing(1, 47)]
        self.tm["planned"] = [pointing(10, 47), pointing(11, 94, "J")]
        self.sync()
        self.sync("planned")
        self.assertEqual(g.clear_planned("S250206dm"), 2)
        self.assertEqual(self.db.fns(g.TM_SOURCE_PLANNED), [])
        self.assertEqual(self.db.fns(g.TM_SOURCE), ["TM_1"])

    def test_unsupported_status_rejected(self):
        rc, _ = self.sync("cancelled")
        self.assertEqual(rc, 1)
        self.assertEqual(self.tm_calls, [])

    def test_tm_failure_counts_as_failure(self):
        with mock.patch.object(g, "tm_get", side_effect=RuntimeError("Treasure Map `pointings` request failed")):
            rc, updated = self.sync()
        self.assertEqual((rc, updated), (1, set()))

    def test_delete_planned_ignores_unexpected_field_names(self):
        self.assertEqual(g.delete_planned([9], {"x'); DROP TABLE ObservedTile;--"}), 0)

    def test_clear_planned_rejects_unsafe_gw_id(self):
        with self.assertRaises(ValueError):
            g.clear_planned("S1' OR '1'='1")


# --- GetTreasureMapData -----------------------------------------------------------------------------
class TestGetTreasureMapData(unittest.TestCase):
    def test_latest_alert_per_event(self):
        alerts = [{"graceid": "S1", "timesent": "2025-01-01T00:00:00", "alert_type": "Preliminary"},
                  {"graceid": "S1", "timesent": "2025-01-01T01:00:00", "alert_type": "Initial"},
                  {"graceid": "S2", "timesent": "2025-01-02T00:00:00", "alert_type": "Preliminary"},
                  {"timesent": "2025-01-03T00:00:00"}]  # no graceid -> ignored
        with mock.patch.object(gtm, "tm_get", return_value=alerts):
            latest = gtm.latest_alerts("https://tm.example", "tok")
        self.assertEqual(sorted(latest), ["S1", "S2"])
        self.assertEqual(latest["S1"]["alert_type"], "Initial")

    def test_alert_role_filter(self):
        for role, expected in (("observation", "observation"), ("test", "test"), ("all", None)):
            with mock.patch.object(gtm, "tm_get", return_value=[]) as tm_get:
                gtm.latest_alerts("https://tm.example", "tok", lookback_days=1, role=role)
            params = tm_get.call_args[0][2]
            self.assertEqual(params.get("role"), expected)
            self.assertEqual(params["far"], "Significant")
        with mock.patch.object(gtm, "tm_get", return_value=[]) as tm_get:  # an explicit id: any role
            gtm.latest_alerts("https://tm.example", "tok", gw_id="MS250101a", role="observation")
        self.assertEqual(tm_get.call_args[0][2], {"api_token": "tok", "graceid": "MS250101a"})

    def test_cron_order_events_then_pointings_then_plan(self):
        calls = []
        alerts = {"S2NEW": {"skymap_fits_url": "https://gracedb/files/bayestar.fits.gz,1"},
                  "S1OLD": {}, "S3RET": {"alert_type": "Retraction"}}

        def fake_sync(grace_ids, lookback_days, status="completed", updated=None):
            calls.append(("sync", status))
            updated.add("S1OLD")  # an old event got new pointings
            return 0

        def fake_run(argv):
            stage = "load_map" if "load_map.py" in " ".join(argv) else argv[argv.index("web.src.services.teglon_cli") + 1]
            calls.append((stage, argv[argv.index("--gw_id") + 1] if "--gw_id" in argv else argv[4],
                          "--exclude-planned" in argv))
            return 0

        with mock.patch.object(gtm, "treasuremap_settings", return_value=("tok", "https://tm.example")), \
                mock.patch.object(gtm, "latest_alerts", return_value=alerts), \
                mock.patch.object(gtm, "query_db", return_value=[[("S1OLD",)]]), \
                mock.patch.object(gtm, "latest_map", return_value=(9, "bayestar.fits.gz")), \
                mock.patch.object(gtm, "sync_pointings", side_effect=fake_sync), \
                mock.patch.object(gtm, "_run", side_effect=fake_run), \
                mock.patch.object(gtm.logging, "basicConfig"):  # main() would turn on INFO logging globally
            rc = gtm.main(["--planned"])

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [
            ("load_map", "S2NEW", False),                    # 1. new event only (old skipped, retraction skipped)
            ("sync", "completed"), ("sync", "planned"),      # 2. pointings
            ("extract", "S1OLD", False), ("plot", "S1OLD", False), ("extract", "S1OLD", True),  # 3. plan
            ("extract", "S2NEW", False), ("plot", "S2NEW", False), ("extract", "S2NEW", True),
        ])

    def test_load_map_gets_filename_not_url_option(self):
        argvs = []
        with mock.patch.object(gtm, "_run", side_effect=lambda a: argvs.append(a) or 0), \
                mock.patch.object(gtm, "latest_map", return_value=(9, "bayestar.fits.gz,1")):
            self.assertTrue(gtm.ingest_event("S1", {"skymap_fits_url": "https://g/files/bayestar.fits.gz,1"}))
        self.assertEqual(argvs[0][-2:], ["--healpix_file", "bayestar.fits.gz,1"])
        self.assertNotIn("--skymap_fits_url", argvs[0])


# --- load_observed_tiles conversions (need healpy to import) ----------------------------------------
@unittest.skipUnless(HAS_HEALPY, "healpy not installed (run inside the Teglon Docker image)")
class TestPointingConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lot = _import_without_db("web.src.ingestion.load_observed_tiles")

    def test_parse_point_wraps_ra(self):
        ra, dec = self.lot._parse_point("POINT(-112.04 -69.5)")
        self.assertAlmostEqual(ra, 247.96)  # -112.04 % 360 isn't exactly 247.96 in floating point
        self.assertEqual(dec, -69.5)
        with self.assertRaises(ValueError):
            self.lot._parse_point("nonsense")

    def test_depth_to_ab_mag(self):
        self.assertEqual(self.lot._depth_to_ab_mag(21.0, "ab_mag"), 21.0)
        self.assertAlmostEqual(self.lot._depth_to_ab_mag(3631.0, "flux_jy"), 0.0)
        self.assertAlmostEqual(self.lot._depth_to_ab_mag(18.0, "vega_mag", "Clear"), 18.21)
        self.assertIsNone(self.lot._depth_to_ab_mag(21.0, "vega_mag", "w"))  # no offset for this band
        self.assertIsNone(self.lot._depth_to_ab_mag(21.0, "vega_mag"))
        self.assertIsNone(self.lot._depth_to_ab_mag(1e-17, "flux_erg"))
        self.assertIsNone(self.lot._depth_to_ab_mag(0.0, "flux_jy"))

    def test_pointings_to_table(self):
        table, skipped = self.lot.pointings_to_table(
            [pointing(1, 47), pointing(2, 47, unit="flux_erg"), dict(pointing(3, 47), time=None)],
            source=self.lot.TM_SOURCE_PLANNED)
        self.assertEqual(len(table), 1)
        self.assertEqual([s[0] for s in skipped], [2, 3])
        row = table[0]
        self.assertEqual((row["field_name"], row["filter"], row["source"]), ("TM_1", "SDSS r", "TreasureMap-planned"))
        self.assertAlmostEqual(row["mjd"], 60712.416667, places=4)

    def test_sources_match_sync(self):
        self.assertEqual((self.lot.TM_SOURCE, self.lot.TM_SOURCE_PLANNED), (g.TM_SOURCE, g.TM_SOURCE_PLANNED))


# --- teglon_cli wiring ------------------------------------------------------------------------------
class TestPointingCLI(unittest.TestCase):
    def setUp(self):
        self.parser = tc.build_parser()

    def test_load_obs_tm_mode_passes_options_but_never_a_token(self):
        a = self.parser.parse_args(["load-obs", "S1", "--tm-instrument", "47", "--detector-name", "ZTF",
                                    "--tm-status", "planned"])
        with mock.patch.object(tc, "_run_script", return_value=0) as run:
            self.assertEqual(tc.cmd_load_obs(a), 0)
        argv = run.call_args[0][1]
        for opt, val in (("--tm_instrument", "47"), ("--detector_name", "ZTF"), ("--tm_status", "planned")):
            self.assertEqual(argv[argv.index(opt) + 1], val)
        self.assertNotIn("--tile_file", argv)
        self.assertFalse(any("token" in x for x in argv))

    def test_load_obs_needs_a_source(self):
        a = self.parser.parse_args(["load-obs", "S1"])
        with mock.patch.object(tc, "_run_script") as run:
            self.assertEqual(tc.cmd_load_obs(a), 2)
        run.assert_not_called()

    def test_load_obs_rejects_cancelled(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["load-obs", "S1", "--tm-instrument", "47", "--tm-status", "cancelled"])

    def test_extract_exclusion_defaults(self):
        a = self.parser.parse_args(["extract", "S1"])
        self.assertTrue(a.exclude_observed)
        self.assertFalse(a.exclude_planned)
        a = self.parser.parse_args(["extract", "S1", "--include-observed", "--exclude-planned"])
        self.assertFalse(a.exclude_observed)
        self.assertTrue(a.exclude_planned)

    def test_clear_planned_wiring(self):
        a = self.parser.parse_args(["clear-planned", "S1"])
        self.assertIs(a.func, tc.cmd_clear_planned)
        self.assertIsNone(a.healpix_file)

    def test_add_telescope_builds_grid_by_default(self):
        a = self.parser.parse_args(["add-telescope", "--tm-detector-id", "47"])
        self.assertFalse(a.no_grid)
        self.assertIsNone(a.prefix)
        self.assertTrue(self.parser.parse_args(["add-telescope", "--tm-detector-id", "47", "--no-grid"]).no_grid)

    def test_sync_pointings_command(self):
        a = self.parser.parse_args(["sync-pointings", "S1", "S2", "--status", "planned"])
        self.assertEqual((a.gw_ids, a.status, a.lookback_days), (["S1", "S2"], "planned", 30))
        with mock.patch.dict(sys.modules, {"web.src.utilities.get_TM_pointings": g}), \
                mock.patch.object(g, "sync_pointings", return_value=0) as sync:
            self.assertEqual(tc.cmd_sync_pointings(a), 0)
            sync.assert_called_once_with(["S1", "S2"], 30, "planned")

            a = self.parser.parse_args(["sync-pointings", "--lookback-days", "0"])
            tc.cmd_sync_pointings(a)
            sync.assert_called_with(None, 0, "completed")  # no ids -> every event in the DB

    def test_tm_ingest_command_forwards_options(self):
        a = self.parser.parse_args(["tm-ingest", "--gw-id", "S250206dm", "--planned", "--lookback-days", "7"])
        with mock.patch.dict(sys.modules, {"web.src.integrations.GetTreasureMapData": gtm}), \
                mock.patch.object(gtm, "main", return_value=0) as tm_main:
            self.assertEqual(tc.cmd_tm_ingest(a), 0)
        tm_main.assert_called_once_with(["--lookback_days", "7", "--gw_id", "S250206dm", "--planned"])
        # every option tm-ingest forwards must exist on the script's own parser
        a = self.parser.parse_args(["tm-ingest", "--role", "test", "--lookback-days", "1"])
        with mock.patch.dict(sys.modules, {"web.src.integrations.GetTreasureMapData": gtm}), \
                mock.patch.object(gtm, "main", return_value=0) as tm_main_test:
            tc.cmd_tm_ingest(a)
        self.assertEqual(tm_main_test.call_args[0][0], ["--lookback_days", "1", "--role", "test"])
        argv = tm_main.call_args[0][0] + ["--only_with_pointings", "--skip_pointings", "--role", "all"]
        with mock.patch.object(gtm, "treasuremap_settings", return_value=("", "")), \
                mock.patch.object(gtm.logging, "basicConfig"):
            self.assertEqual(gtm.main(argv), 1)  # parses, then stops at the missing token

    def test_list_telescopes_output(self):
        rows = [[(1, "SWOPE", None, -90.0, 30.0, 2061), (12, "ZTF", 47, -30.0, 90.0, 0)]]
        dh = types.ModuleType("web.src.utilities.Database_Helpers")
        dh.query_db = lambda *a, **k: rows
        with mock.patch.dict(sys.modules, {"web.src.utilities.Database_Helpers": dh}):
            out = io.StringIO()
            with redirect_stdout(out):
                tc.cmd_list_telescopes(self.parser.parse_args(["list-telescopes"]))
            lines = out.getvalue().splitlines()
            self.assertIn("re-run add-telescope --tm-detector-id 47", lines[2])
            self.assertNotIn("re-run", lines[1])  # no TM id -> no add-telescope hint

            out = io.StringIO()
            with redirect_stdout(out):
                tc.cmd_list_telescopes(self.parser.parse_args(["list-telescopes", "--json"]))
            data = json.loads(out.getvalue())
            self.assertEqual(data["detectors"][1], {"id": 12, "name": "ZTF", "tm_id": 47, "min_dec": -30.0,
                                                    "max_dec": 90.0, "static_tiles": 0})


if __name__ == "__main__":
    unittest.main()
