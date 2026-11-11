"""Unit tests for the new Teglon CLI functionality.

Covers the four features added on top of v1.0:
  1. delete-event  (clean a GW event from DB + files)
  2. add-telescope (register a new detector, e.g. LSST/Vera Rubin)
  3. setup         (one-time complete initialization)
  4. trigger       (ingest + reweight a skymap and export the updated 4D map)

These tests exercise the *pure helpers* and the argparse wiring only. They do
NOT touch the database, the network, or the filesystem (other than a temp dir),
and they assert that destructive commands default to a safe dry-run.
"""
import os
import shutil
import tempfile
import unittest

import web.src.services.teglon_cli as tc


class TestEventHelpers(unittest.TestCase):
    def test_event_dir_substitutes_gwid(self):
        self.assertEqual(tc.event_dir("S230529ay", "./web/events/{GWID}"),
                         "./web/events/S230529ay")

    def test_build_delete_map_sql_structure(self):
        stmts = tc.build_delete_map_sql(7)
        self.assertEqual(len(stmts), 1)
        self.assertEqual(stmts[0], "CALL DeleteMap(7);")

    def test_build_delete_map_sql_coerces_int(self):
        # A string id must be coerced to int (guards against SQL injection of ids).
        stmts = tc.build_delete_map_sql("42")
        self.assertEqual(stmts[0], "CALL DeleteMap(42);")
        with self.assertRaises(ValueError):
            tc.build_delete_map_sql("42; DROP TABLE HealpixMap;")

    def test_plan_event_file_deletion_lists_files_and_dir(self):
        d = tempfile.mkdtemp()
        try:
            open(os.path.join(d, "a.txt"), "w").close()
            sub = os.path.join(d, "sub")
            os.makedirs(sub)
            open(os.path.join(sub, "b.txt"), "w").close()
            paths = tc.plan_event_file_deletion(d)
            self.assertIn(os.path.join(d, "a.txt"), paths)
            self.assertIn(os.path.join(sub, "b.txt"), paths)
            self.assertIn(d, paths)  # the directory itself is included
        finally:
            shutil.rmtree(d)

    def test_plan_event_file_deletion_missing_dir_is_empty(self):
        self.assertEqual(tc.plan_event_file_deletion("/no/such/dir/xyz"), [])


class TestTelescopeGeometry(unittest.TestCase):
    def test_rectangle_ok(self):
        g = tc.normalize_geometry_args("rectangle", width=1.0, height=2.0)
        self.assertEqual(g["detector_geometry"], "rectangle")
        self.assertEqual(g["detector_width"], 1.0)
        self.assertEqual(g["detector_height"], 2.0)
        self.assertIsNone(g["detector_radius"])

    def test_rectangle_missing_dims_raises(self):
        with self.assertRaises(ValueError):
            tc.normalize_geometry_args("rectangle", width=1.0, height=None)

    def test_circle_ok(self):
        g = tc.normalize_geometry_args("circle", radius=1.75)
        self.assertEqual(g["detector_geometry"], "circle")
        self.assertEqual(g["detector_radius"], 1.75)

    def test_circle_missing_radius_raises(self):
        with self.assertRaises(ValueError):
            tc.normalize_geometry_args("circle")

    def test_polygon_returns_none_geometry(self):
        g = tc.normalize_geometry_args("polygon")
        self.assertIsNone(g["detector_geometry"])

    def test_unknown_geometry_raises(self):
        with self.assertRaises(ValueError):
            tc.normalize_geometry_args("triangle")


class TestGridPrefix(unittest.TestCase):
    def test_first_letter_when_free(self):
        self.assertEqual(tc.choose_grid_prefix("ZTF", set()), "Z")

    def test_first_two_letters_when_first_taken(self):
        self.assertEqual(tc.choose_grid_prefix("WINTER", {"W"}), "WI")

    def test_case_insensitive_and_punctuation_ignored(self):
        self.assertEqual(tc.choose_grid_prefix("Sinistro", {"s", "SI"}), "SIN")
        self.assertEqual(tc.choose_grid_prefix("T80S_T80S-Cam", {"T"}), "T80S")

    def test_skips_prefixes_ending_in_a_digit(self):
        self.assertEqual(tc.choose_grid_prefix("T80S", {"T"}), "T80S")

    def test_all_taken_appends_number(self):
        self.assertEqual(tc.choose_grid_prefix("AB", {"A", "AB"}), "AB2X")

    def test_prefix_from_field_name(self):
        self.assertEqual(tc.grid_prefix_from_field_name("T80S000123"), "T80S")
        self.assertEqual(tc.grid_prefix_from_field_name("Z000000"), "Z")


class TestSetupSteps(unittest.TestCase):
    def test_default_three_steps_in_order(self):
        steps = tc.build_setup_steps()
        labels = [s[0] for s in steps]
        self.assertEqual(len(steps), 3)
        self.assertIn("GLADE", labels[0])
        self.assertIn("Initialize", labels[1])
        self.assertIn("pickle", labels[2].lower())

    def test_init_step_has_all_documented_flags(self):
        init_argv = tc.build_setup_steps()[1][1]
        for flag in ["--is_debug", "--build_skydistances", "--build_skypixels",
                     "--build_detectors", "--build_TM_detectors", "--build_bands",
                     "--build_MWE", "--build_galaxy_skypixel_associations",
                     "--build_completeness", "--compose_completeness",
                     "--build_static_grids"]:
            self.assertIn(flag, init_argv)

    def test_no_debug_omits_is_debug(self):
        init_argv = tc.build_setup_steps(is_debug=False)[1][1]
        self.assertNotIn("--is_debug", init_argv)

    def test_skip_flags_reduce_steps(self):
        steps = tc.build_setup_steps(skip_glade=True, skip_pickles=True)
        self.assertEqual(len(steps), 1)
        self.assertIn("Initialize", steps[0][0])


class TestReweightedMap(unittest.TestCase):
    def test_output_path(self):
        p = tc.reweighted_output_path("S230529ay", "./web/events/{GWID}", "bayestar.fits.gz")
        self.assertEqual(
            p,
            os.path.join("./web/events/S230529ay", "S230529ay_4D_reweighted_bayestar.fits.gz"),
        )

    def test_reconstruct_places_values_at_pixel_index(self):
        nside = 4  # npix = 12*16 = 192
        # rows: (id, HealpixMap_id, Pixel_Index, Prob, NetPixelProb)
        rows = [
            (1, 9, 0, 0.1, 0.5),
            (2, 9, 5, 0.2, 0.25),
            (3, 9, 191, 0.3, 0.05),
        ]
        arr = tc.reconstruct_reweighted_map(rows, nside)
        self.assertEqual(len(arr), 12 * nside * nside)
        self.assertAlmostEqual(arr[0], 0.5)
        self.assertAlmostEqual(arr[5], 0.25)
        self.assertAlmostEqual(arr[191], 0.05)
        self.assertAlmostEqual(arr[1], 0.0)  # untouched pixels stay zero

    def test_reconstruct_ignores_out_of_range_index(self):
        nside = 2  # npix = 48
        rows = [(1, 9, 999, 0.1, 0.9)]  # index out of range -> ignored, no crash
        arr = tc.reconstruct_reweighted_map(rows, nside)
        self.assertEqual(len(arr), 48)
        self.assertAlmostEqual(float(arr.sum()), 0.0)


class TestParserSafetyDefaults(unittest.TestCase):
    def setUp(self):
        self.parser = tc.build_parser()

    def test_delete_event_defaults_to_dry_run(self):
        a = self.parser.parse_args(["delete-event", "S230529ay"])
        self.assertFalse(a.yes, "delete-event must default to dry-run (yes=False)")
        self.assertFalse(a.db_only)
        self.assertFalse(a.files_only)
        self.assertIs(a.func, tc.cmd_delete_event)

    def test_setup_defaults_to_dry_run(self):
        a = self.parser.parse_args(["setup"])
        self.assertFalse(a.run, "setup must default to dry-run (run=False)")
        self.assertIs(a.func, tc.cmd_setup)

    def test_add_telescope_requires_tm_id(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["add-telescope"])  # missing required --tm-detector-id

    def test_add_telescope_wires_func(self):
        a = self.parser.parse_args(["add-telescope", "--tm-detector-id", "22"])
        self.assertEqual(a.tm_detector_id, 22)
        self.assertIs(a.func, tc.cmd_add_telescope)

    def test_trigger_wires_func(self):
        a = self.parser.parse_args(["trigger", "S230529ay"])
        self.assertFalse(a.no_ingest)
        self.assertIs(a.func, tc.cmd_trigger)

    def test_t0_override_parses_for_local_skymaps(self):
        # Non-superevents (e.g. GW170817) need a local skymap + explicit GPS t_0.
        for cmd in ("load-map", "run", "trigger"):
            a = self.parser.parse_args([cmd, "GW170817", "--t0", "1187008882.4"])
            self.assertAlmostEqual(a.t0, 1187008882.4)
        # Default is None (normal GraceDB path).
        self.assertIsNone(self.parser.parse_args(["load-map", "S190425z"]).t0)

    def test_skymap_info_parser(self):
        a = self.parser.parse_args(["skymap-info", "/tmp/x.fits"])
        self.assertEqual(a.skymap_fits_file, "/tmp/x.fits")
        self.assertIs(a.func, tc.cmd_skymap_info)

    def test_skymap_info_gwid_and_json(self):
        a = self.parser.parse_args(["skymap-info", "S240413p", "--json"])
        self.assertEqual(a.skymap_fits_file, "S240413p")
        self.assertTrue(a.json)
        self.assertEqual(a.healpix_file, "bayestar.fits.gz")

    def test_doctor_parses(self):
        self.assertIs(self.parser.parse_args(["doctor"]).func, tc.cmd_doctor)

    def test_setup_force_flag(self):
        self.assertTrue(self.parser.parse_args(["setup", "--run", "--force"]).force)
        self.assertFalse(self.parser.parse_args(["setup", "--run"]).force)

    def test_json_flag_on_extract_compare_efficiency(self):
        for cmd in (["extract", "S240413p", "--json"],
                    ["compare", "S240413p", "--json"],
                    ["efficiency", "S240413p", "--json"]):
            self.assertTrue(self.parser.parse_args(cmd).json)

    def test_global_quiet_verbose(self):
        self.assertTrue(self.parser.parse_args(["-q", "doctor"]).quiet)
        self.assertTrue(self.parser.parse_args(["--verbose", "doctor"]).verbose)
        # mutually exclusive
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["-q", "-v", "doctor"])


class TestCredibleAreas(unittest.TestCase):
    """check_info_skymap's area math (numpy-only; no healpy/FITS needed)."""

    def _ten_pixel_map(self):
        import numpy as np
        prob = np.zeros(192)   # nside = 4
        prob[:10] = 1.0        # 10 equal pixels -> 0.1 each once normalized
        return prob

    def test_areas_count_the_right_pixels(self):
        # Levels chosen mid-step (between cumulative 0.4/0.5/0.9/1.0) to avoid
        # floating-point boundary ambiguity: 0.45 -> 4 px, 0.55 -> 5 px, 0.95 -> 9 px.
        pix = tc._FULL_SKY_DEG2 / 192
        a = tc.credible_areas(self._ten_pixel_map(), levels=(0.45, 0.55, 0.95))
        self.assertAlmostEqual(a[0.45], 4 * pix, places=6)
        self.assertAlmostEqual(a[0.55], 5 * pix, places=6)
        self.assertAlmostEqual(a[0.95], 9 * pix, places=6)

    def test_areas_monotonic_50_90_99(self):
        a = tc.credible_areas(self._ten_pixel_map(), levels=(0.5, 0.9, 0.99))
        self.assertLessEqual(a[0.5], a[0.9])
        self.assertLessEqual(a[0.9], a[0.99])

    def test_normalization_invariant(self):
        # A galaxy-reweighted map sums to < 1; areas must not depend on the total.
        a1 = tc.credible_areas(self._ten_pixel_map(), levels=(0.45, 0.95))
        a2 = tc.credible_areas(self._ten_pixel_map() * 0.37, levels=(0.45, 0.95))
        for lev in (0.45, 0.95):
            self.assertAlmostEqual(a1[lev], a2[lev], places=6)

    def test_pixel_area_matches_healpy_formula(self):
        # _FULL_SKY_DEG2 / npix == healpy.nside2pixarea(nside, degrees=True).
        self.assertAlmostEqual(tc._FULL_SKY_DEG2, 41252.96124941927, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
