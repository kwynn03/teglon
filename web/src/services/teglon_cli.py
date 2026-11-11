"""Unified command-line interface for Teglon-O4.

A single front door for the whole pipeline so you no longer have to run
`load_map.py` / `extract_tiles.py` / `plot_teglon.py` by hand:

    teglon run S230529ay            # load map -> extract tiles -> plot, in one shot
    teglon load-map S230529ay       # individual stages
    teglon extract S230529ay
    teglon plot S230529ay
    teglon load-obs S230529ay --tile-file my_obs.ecsv
    teglon sync-pointings S230529ay # load Treasure Map pointings (completed by default)
    teglon tm-ingest --gw-id S230529ay  # Treasure Map: new event -> pointings -> extract -> plot
    teglon efficiency S230529ay --model-type kne
    teglon bootstrap                # build a fresh DB (dust + GLADE + detectors + grids)

The database connection is resolved from environment variables first (see
`web/src/utilities/Database_Helpers.py`), so the same command works inside the
Docker network (DATABASE_HOST=gw_db) or from the host (DATABASE_HOST=127.0.0.1,
DATABASE_PORT=53306) with no port-forwarding.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

# Logs go through this logger (stderr), controlled by --quiet/--verbose. Structured
# RESULTS (KEY=value blocks, --json) go to stdout via _emit() so a scheduler can parse
# them regardless of the log level.
logger = logging.getLogger("teglon")


def _emit(line=""):
    """Write a machine-readable result line to stdout (always, ignoring log level)."""
    sys.stdout.write(str(line) + "\n")


# --- repo-root awareness -----------------------------------------------------
# Stage logic and the existing scripts use paths relative to the repo root
# (e.g. './web/events/{GWID}', './Settings.ini'). Make sure we run from there
# regardless of where `teglon` was invoked.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _ensure_repo_root():
    if os.path.isdir(os.path.join(REPO_ROOT, "web")):
        os.chdir(REPO_ROOT)


def _get_version():
    """Teglon version, from installed package metadata or pyproject.toml."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("teglon-o4")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    try:
        with open(os.path.join(REPO_ROOT, "pyproject.toml")) as fh:
            for line in fh:
                if line.strip().startswith("version") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _configure_logging(quiet=False, verbose=False):
    """Route log messages (stderr) at the requested level. Default INFO keeps the
    usual progress messages; --quiet shows warnings/errors only (library/scheduler
    use); --verbose adds the per-query debug detail. Format is the bare message, so
    existing message text is unchanged."""
    level = logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()  # stderr
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(level)


def _banner(msg):
    logger.info("\n" + "=" * 70)
    logger.info(msg)
    logger.info("=" * 70 + "\n")


def _run_script(rel_path, extra_args, stdout_to_stderr=False):
    """Run one of the standalone (non-method) scripts as a subprocess from the
    repo root, inheriting the current environment (incl. DATABASE_* overrides).
    With stdout_to_stderr, the child's stdout is redirected to stderr so the
    caller can keep its own stdout clean for a --json result."""
    cmd = [sys.executable, rel_path] + list(extra_args)
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    logger.info("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=REPO_ROOT, env=env,
                           stdout=sys.stderr if stdout_to_stderr else None)


# --- pure helpers (no DB / network / side effects; unit-tested) ---------------
def event_dir(gw_id, healpix_dir="./web/events/{GWID}"):
    """Resolve an event's working directory from the {GWID} template."""
    return healpix_dir.replace("{GWID}", gw_id)


def build_delete_map_sql(map_id):
    """Return the SQL statement(s) that remove a single HealpixMap by id.

    DeleteMap performs a targeted, transactional delete of only this map's rows
    (docker/db_init/delete_map.sql); the old BackupTables/LOCK/restore dance is no
    longer needed. Pure: builds the string only, runs nothing. map_id is coerced to
    int, which also guards against SQL injection via the id."""
    map_id = int(map_id)
    return ["CALL DeleteMap(%d);" % map_id]


def plan_event_file_deletion(directory):
    """List existing paths under `directory` (all files plus the directory itself),
    sorted. Inspects only the filesystem -- safe to call on anything."""
    paths = []
    if os.path.isdir(directory):
        for root, _dirs, files in os.walk(directory):
            for f in files:
                paths.append(os.path.join(root, f))
        paths.append(directory)
    return sorted(paths)


def normalize_geometry_args(geometry, width=None, height=None, radius=None):
    """Validate detector-geometry arguments and return the kwargs add_detector wants.
    Raises ValueError on an invalid combination."""
    if geometry == "rectangle":
        if width is None or height is None:
            raise ValueError("rectangle geometry requires --width and --height")
        return {"detector_geometry": "rectangle", "detector_width": width,
                "detector_height": height, "detector_radius": None}
    if geometry == "circle":
        if radius is None:
            raise ValueError("circle geometry requires --radius")
        return {"detector_geometry": "circle", "detector_width": None,
                "detector_height": None, "detector_radius": radius}
    if geometry in (None, "polygon", "footprint"):
        # Use the Treasure Map footprint polygon as-is.
        return {"detector_geometry": None, "detector_width": None,
                "detector_height": None, "detector_radius": None}
    raise ValueError("Unknown geometry: %r (use rectangle, circle, or polygon)" % geometry)


def grid_prefix_from_field_name(field_name):
    """The static-grid prefix of a StaticTile FieldName (`<prefix><6-digit index>`), e.g. `T80S000123` -> `T80S`."""
    return str(field_name).rstrip("0123456789")


def choose_grid_prefix(detector_name, existing_prefixes):
    """Pick a static-grid FieldName prefix for `detector_name` that no other detector uses.

    Tries the name's first letter, then its first two letters, and so on (alphanumerics only, upper-cased).
    Candidates ending in a digit are skipped so the prefix can't run into the 6-digit field index
    (`T80S_T80S-Cam` -> `T`, then `T80S`). If every candidate is taken, a number is appended to the
    full name. Comparison is case-insensitive. Pure."""
    letters = "".join(ch for ch in str(detector_name).upper() if ch.isalnum()) or "T"
    taken = {str(p).upper() for p in existing_prefixes}
    for n in range(1, len(letters) + 1):
        candidate = letters[:n]
        if candidate[-1].isalpha() and candidate not in taken:
            return candidate
    k = 2
    while "%s%dX" % (letters, k) in taken:
        k += 1
    # Trailing letter keeps the prefix from merging with the field index.
    return "%s%dX" % (letters, k)


def build_setup_steps(skip_glade=False, skip_init=False, skip_pickles=False, is_debug=True):
    """Ordered (label, argv) list for the one-time install, matching the documented
    sequence: GLADE upload -> initialize_teglon (all flags) -> build_init_pickles. Pure."""
    steps = []
    if not skip_glade:
        steps.append((
            "Upload GLADE galaxy catalog",
            [os.path.join("web", "src", "utilities", "bulk_upload_glade.py")],
        ))
    if not skip_init:
        init = [os.path.join("web", "src", "utilities", "initialize_teglon.py")]
        if is_debug:
            init.append("--is_debug")
        init += [
            "--build_skydistances", "--build_skypixels", "--build_detectors",
            "--build_TM_detectors", "--build_bands", "--build_MWE",
            "--build_galaxy_skypixel_associations", "--build_completeness",
            "--compose_completeness", "--build_static_grids",
        ]
        steps.append(("Initialize Teglon core tables", init))
    if not skip_pickles:
        steps.append((
            "Build pickle caches",
            [os.path.join("web", "src", "utilities", "build_init_pickles.py"),
             "--build_skypixels_pickle", "--build_ebv_pickle",
             "--build_composed_completeness_pickle"],
        ))
    return steps


def reweighted_output_path(gw_id, healpix_dir, healpix_file):
    """Path of the exported galaxy-reweighted (4D) skymap for an event."""
    return os.path.join(event_dir(gw_id, healpix_dir),
                        "%s_4D_reweighted_%s" % (gw_id, healpix_file))


def reconstruct_reweighted_map(pixel_rows, nside, prob_index=4, pixindex_index=2):
    """Build a full-sky probability array from HealpixPixel/Completeness DB rows.

    Each row places `row[prob_index]` (NetPixelProb) at HEALPix index
    `row[pixindex_index]` (Pixel_Index). Mirrors plot_teglon's map_4d_pix
    reconstruction. Uses numpy only (npix = 12*nside^2), so it is unit-testable
    without healpy or a database."""
    import numpy as np

    nside = int(nside)
    npix = 12 * nside * nside
    arr = np.zeros(npix)
    for r in pixel_rows:
        idx = int(r[pixindex_index])
        if 0 <= idx < npix:
            arr[idx] = float(r[prob_index])
    return arr


# Area of the full sky in square degrees ( = 4*pi sr ). One HEALPix pixel is this
# divided by npix, which equals healpy.nside2pixarea(nside, degrees=True) exactly
# but needs no healpy import (keeps `credible_areas` unit-testable on the host).
_FULL_SKY_DEG2 = 4.0 * 180.0 * 180.0 / 3.141592653589793  # 41252.961249419...


def credible_areas(prob, levels=(0.5, 0.9, 0.99)):
    """Credible-region areas (sq deg) of a HEALPix probability map.

    For each level L, returns the area of the smallest set of pixels whose summed
    probability reaches L. The map is normalized to unit total first, so the result
    is a proper credible region regardless of the map's absolute normalization
    (e.g. a galaxy-reweighted map whose pixels sum to < 1). numpy-only, so it is
    unit-testable without healpy. Uses the same method (and the same pixel area) as
    `compare`, so the values agree with the reported credible areas.
    """
    import numpy as np

    p = np.nan_to_num(np.asarray(prob, dtype=float), nan=0.0)
    total = float(p.sum())
    if total > 0:
        p = p / total
    pix_area_deg2 = _FULL_SKY_DEG2 / p.size
    cumulative = np.cumsum(np.sort(p)[::-1])  # descending cumulative probability
    return {lev: float(np.sum(cumulative <= lev)) * pix_area_deg2 for lev in levels}


def check_info_skymap(skymap_fits_file, levels=(0.5, 0.9, 0.99)):
    """Read a HEALPix skymap FITS and return its credible-region areas (sq deg).

    Reads the PROB column (field 0) with healpy and computes the areas for `levels`
    (default 50%, 90%, 99%). Works on both an original LIGO localization map and the
    Teglon galaxy-reweighted map exported by `trigger`. Returns
    {file, nside, npix, areas={level: area_sqdeg}}.
    """
    import healpy as hp

    prob = hp.read_map(skymap_fits_file, field=0)
    npix = len(prob)
    return {
        "file": skymap_fits_file,
        "nside": int(hp.npix2nside(npix)),
        "npix": int(npix),
        "areas": credible_areas(prob, levels=levels),
    }


# --- pipeline stages (call Teglon methods directly) --------------------------
def _teglon():
    # Imported lazily so `teglon --help` is fast and does not require the DB.
    from web.src.services.teglon import Teglon

    return Teglon()


def cmd_load_map(args):
    _teglon().load_map(
        gw_id=args.gw_id,
        healpix_dir=args.healpix_dir,
        healpix_file=args.healpix_file,
        analysis_mode=args.analysis_mode,
        clobber=args.clobber,
        skip_swope=args.skip_swope,
        skip_thacher=args.skip_thacher,
        skip_t80=args.skip_t80,
        skip_newfirm=args.skip_newfirm,
        t_0_override=getattr(args, "t0", None),
    )
    return 0


def cmd_extract(args):
    import contextlib
    # With --json, keep stdout clean (only the JSON): send any library chatter
    # (e.g. dustmaps' config message) to stderr during the run.
    ctx = contextlib.redirect_stdout(sys.stderr) if getattr(args, "json", False) \
        else contextlib.nullcontext()
    with ctx:
        _teglon().extract_tiles(
            gw_id=args.gw_id,
            healpix_dir=args.healpix_dir,
            healpix_file=args.healpix_file,
            tele=args.tele,
            band=args.band,
            extinct=args.extinct,
            prob_type=args.prob_type,
            cum_prob=args.cum_prob,
            num_tiles=args.num_tiles,
            min_ra=args.min_ra,
            max_ra=args.max_ra,
            min_dec=args.min_dec,
            max_dec=args.max_dec,
            exclude_observed=args.exclude_observed,
            exclude_planned=args.exclude_planned,
        )
    if getattr(args, "json", False):
        import glob
        d = event_dir(args.gw_id, args.healpix_dir)
        box = "_box" if all(v != -1 for v in (args.min_ra, args.max_ra, args.min_dec, args.max_dec)) else ""
        if args.exclude_observed and args.exclude_planned:
            box += "_exclplanned"
        pattern = os.path.join(d, "%s_*_%s_%s_%s%s.txt" % (
            args.gw_id, args.prob_type, args.cum_prob, args.healpix_file, box))
        out = {"gw_id": args.gw_id, "prob_type": args.prob_type,
               "cum_prob": args.cum_prob, "tile_files": []}
        for f in sorted(glob.glob(pattern)):
            with open(f) as fh:
                n_rows = max(0, sum(1 for ln in fh if not ln.startswith("#")) - 1)
            out["tile_files"].append({"file": os.path.basename(f), "num_tiles": n_rows})
        _emit(json.dumps(out))
    return 0


def cmd_plot(args):
    _teglon().plot_teglon(
        gw_id=args.gw_id,
        healpix_dir=args.healpix_dir,
        healpix_file=args.healpix_file,
        tele=args.tele,
        band=args.band,
        extinct=args.extinct,
        tile_file=args.tile_file,
        num_tiles=args.num_tiles,
        cum_prob_outer=args.cum_prob_outer,
        cum_prob_inner=args.cum_prob_inner,
    )
    return 0


def cmd_run(args):
    """Full planning pipeline for one GW event: load -> extract -> plot.

    Synchronous equivalent of teglon_worker_tasks.load_map_batch_process (no Celery)."""
    t = _teglon()

    _banner("[1/3] Loading map for %s ..." % args.gw_id)
    t.load_map(
        gw_id=args.gw_id,
        healpix_dir=args.healpix_dir,
        healpix_file=args.healpix_file,
        analysis_mode=args.analysis_mode,
        clobber=args.clobber,
        skip_swope=args.skip_swope,
        skip_thacher=args.skip_thacher,
        skip_t80=args.skip_t80,
        skip_newfirm=args.skip_newfirm,
        t_0_override=getattr(args, "t0", None),
    )

    _banner("[2/3] Extracting tiles for %s ..." % args.gw_id)
    t.extract_tiles(
        gw_id=args.gw_id,
        healpix_dir=args.healpix_dir,
        healpix_file=args.healpix_file,
        tele=args.tele,
        extinct=args.extinct,
        prob_type=args.prob_type,
        cum_prob=args.cum_prob,
        num_tiles=args.num_tiles,
    )

    if not args.no_plot:
        _banner("[3/3] Plotting %s ..." % args.gw_id)
        t.plot_teglon(
            gw_id=args.gw_id,
            healpix_dir=args.healpix_dir,
            healpix_file=args.healpix_file,
        )
    else:
        logger.info("[3/3] Skipping plot (--no-plot).")

    _banner("Done. Output is in %s" % args.healpix_dir.replace("{GWID}", args.gw_id))
    return 0


# --- wrappers around standalone scripts --------------------------------------
def cmd_load_obs(args):
    if not args.tile_file and not args.tm_instrument:
        logger.error("load-obs needs either --tile-file or --tm-instrument.")
        return 2
    extra = [
        "--gw_id", args.gw_id,
        "--healpix_file", args.healpix_file,
        "--healpix_dir", args.healpix_dir,
        "--tile_dir", args.tile_dir,
    ]
    if args.tile_file:
        extra += ["--tile_file", args.tile_file]
    # Treasure Map mode. The API token is read by the loader from TM_API_TOKEN, never passed on argv.
    if args.tm_instrument:
        extra += ["--tm_instrument", args.tm_instrument, "--tm_status", args.tm_status]
    if args.detector_name:
        extra += ["--detector_name", args.detector_name]
    return _run_script(os.path.join("web", "src", "ingestion", "load_observed_tiles.py"), extra)


def cmd_efficiency(args):
    extra = [
        "--gw_id", args.gw_id,
        "--healpix_file", args.healpix_file,
        "--healpix_dir", args.healpix_dir,
        "--model_type", args.model_type,
        "--num_cpu", str(args.num_cpu),
    ]
    if args.clobber:
        extra.append("--clobber")
    rc = _run_script(
        os.path.join("web", "src", "analysis", "model_detection_efficiency.py"), extra,
        stdout_to_stderr=getattr(args, "json", False),
    )
    if getattr(args, "json", False):
        import glob
        d = event_dir(args.gw_id, args.healpix_dir)
        mdir = os.path.join(d, "model_detection", args.model_type)
        files = [f for f in glob.glob(os.path.join(mdir, "**", "*"), recursive=True)
                 if os.path.isfile(f)]
        _emit(json.dumps({"gw_id": args.gw_id, "model_type": args.model_type,
                          "output_dir": mdir, "num_files": len(files),
                          "files": sorted(os.path.relpath(f, d) for f in files)[:100]}))
    return rc


def cmd_compare(args):
    extra = [
        "--gw_id", args.gw_id,
        "--healpix_file", args.healpix_file,
        "--healpix_dir", args.healpix_dir,
    ]
    if args.out:
        extra += ["--out", args.out]
    if getattr(args, "json", False):
        extra.append("--json")
    return _run_script(os.path.join("web", "src", "analysis", "compare_skymaps.py"), extra)


def _emit_skymap_info(info, as_json):
    """Write one skymap's credible areas to stdout (KEY=value or JSON)."""
    levels = (0.5, 0.9, 0.99)
    if as_json:
        _emit(json.dumps({
            "file": info["file"], "nside": info["nside"], "npix": info["npix"],
            "areas_sqdeg": {("%d" % int(round(l * 100))): round(info["areas"][l], 4) for l in levels},
        }))
        return
    _emit("SKYMAP_INFO_BEGIN")
    _emit("file=%s" % info["file"])
    _emit("nside=%d" % info["nside"])
    _emit("npix=%d" % info["npix"])
    for lev in levels:
        _emit("area_%d_sqdeg=%.2f" % (int(round(lev * 100)), info["areas"][lev]))
    _emit("SKYMAP_INFO_END")


def cmd_skymap_info(args):
    """50/90/99% credible-region areas of a skymap FITS (healpy), to stdout.

    The positional argument is either a FITS path, or a GW id -- in which case
    the original and the Teglon-reweighted maps in the event directory are both
    reported together with the shrink factor.
    """
    target = args.skymap_fits_file
    levels = (0.5, 0.9, 0.99)

    # File mode: the argument is an existing file.
    if os.path.isfile(target):
        _emit_skymap_info(check_info_skymap(target, levels=levels), args.json)
        return 0

    # GW-id mode: locate the original + reweighted maps in the event directory.
    d = event_dir(target, args.healpix_dir)
    orig = os.path.join(d, args.healpix_file)
    rewt = reweighted_output_path(target, args.healpix_dir, args.healpix_file)
    if not os.path.isfile(orig):
        logger.error("Not a file and no original map for GW id `%s` at %s" % (target, orig))
        return 1
    oinfo = check_info_skymap(orig, levels=levels)
    rinfo = check_info_skymap(rewt, levels=levels) if os.path.isfile(rewt) else None

    if args.json:
        out = {"gw_id": target, "original": {
            "file": oinfo["file"], "nside": oinfo["nside"],
            "areas_sqdeg": {("%d" % int(round(l * 100))): round(oinfo["areas"][l], 4) for l in levels}}}
        if rinfo:
            out["reweighted"] = {
                "file": rinfo["file"], "nside": rinfo["nside"],
                "areas_sqdeg": {("%d" % int(round(l * 100))): round(rinfo["areas"][l], 4) for l in levels}}
            out["shrink_factor_90"] = round(oinfo["areas"][0.9] / rinfo["areas"][0.9], 3) \
                if rinfo["areas"][0.9] > 0 else None
        _emit(json.dumps(out))
        return 0

    _emit_skymap_info(oinfo, False)
    if rinfo:
        _emit_skymap_info(rinfo, False)
        if rinfo["areas"][0.9] > 0:
            _emit("shrink_factor_90=%.2f" % (oinfo["areas"][0.9] / rinfo["areas"][0.9]))
    else:
        logger.info("(no reweighted map yet at %s -- run `teglon trigger %s` first)" % (rewt, target))
    return 0


# --- fresh-build bootstrap ---------------------------------------------------
def cmd_bootstrap(args):
    """Build a fresh DB from scratch: dust -> GLADE -> detectors -> static grids.

    The schema, stored procedures and users are loaded automatically by the
    `db_init/*.sql` mounts when the MySQL container first starts, so this only
    has to populate the science data.
    """
    if not args.skip_dust:
        _banner("[bootstrap] Initializing SFD dust map (ebv.pkl) ...")
        rc = _run_script(os.path.join("web", "src", "utilities", "initialize_dust.py"), [])
        if rc != 0:
            logger.info("Dust initialization failed (rc=%s). Aborting." % rc)
            return rc

    if not args.skip_galaxies:
        _banner("[bootstrap] Uploading GLADE galaxy catalog ...")
        rc = _run_script(os.path.join("web", "src", "utilities", "bulk_upload_glade.py"), [])
        if rc != 0:
            logger.info("GLADE upload failed (rc=%s). Aborting." % rc)
            return rc

    if args.instruments_config:
        if not os.path.exists(args.instruments_config):
            logger.info("Instruments config not found: %s" % args.instruments_config)
            return 1
        with open(args.instruments_config) as fh:
            instruments = json.load(fh)

        t = _teglon()
        for inst in instruments:
            _banner("[bootstrap] Adding detector + static grid: %s" % inst.get("name", "?"))
            t.add_detector(
                tm_detector_id=inst["tm_detector_id"],
                min_dec=inst.get("min_dec", -90.0),
                max_dec=inst.get("max_dec", 90.0),
                detector_geometry=inst.get("detector_geometry"),
                detector_width=inst.get("detector_width"),
                detector_height=inst.get("detector_height"),
                detector_radius=inst.get("detector_radius"),
            )
            # add_static_grid needs the Teglon detector id; it is looked up by the
            # script normally. We pass the prefix and the TM id resolves on the
            # detector just inserted.
            t.add_static_grid(
                teglon_detector_id=inst["teglon_detector_id"],
                detector_prefix=inst["detector_prefix"],
            )
    else:
        logger.info(
            "\nNo --instruments-config provided: dust + GLADE are loaded, but no\n"
            "detectors/static grids were created. Provide a JSON file (see\n"
            "docs/bootstrap.md and Settings.example.ini) describing each instrument,\n"
            "or add them individually with the existing add_detector.py /\n"
            "add_static_grid.py scripts."
        )
    _banner("[bootstrap] Done.")
    return 0


# --- new feature commands ----------------------------------------------------
def cmd_sync_pointings(args):
    """Load Treasure Map pointings for events into ObservedTile (get_TM_pointings.sync_pointings)."""
    from web.src.utilities.get_TM_pointings import sync_pointings
    return sync_pointings(args.gw_ids or None, args.lookback_days, args.status)


def cmd_tm_ingest(args):
    """Treasure Map cron flow (GetTreasureMapData.main): new events -> pointings -> extract -> plot."""
    from web.src.integrations.GetTreasureMapData import main as tm_ingest_main
    argv = ["--lookback_days", str(args.lookback_days)]
    if args.gw_id:
        argv += ["--gw_id", args.gw_id]
    if args.only_with_pointings:
        argv.append("--only_with_pointings")
    if args.skip_pointings:
        argv.append("--skip_pointings")
    if args.planned:
        argv.append("--planned")
    if args.role != "observation":
        argv += ["--role", args.role]
    return tm_ingest_main(argv)


def cmd_clear_planned(args):
    from web.src.utilities.get_TM_pointings import clear_planned
    n = clear_planned(args.gw_id, args.healpix_file)
    _banner("Cleared %d planned pointing(s) for %s." % (n, args.gw_id))
    return 0


def cmd_delete_event(args):
    """Clean a GW event from the DB and/or the filesystem. DRY-RUN by default:
    nothing is removed unless --yes is given."""
    from web.src.utilities.Database_Helpers import query_db

    map_ids = []
    if not args.files_only:
        if args.healpix_file:
            sel = ("SELECT id, Filename FROM HealpixMap WHERE GWID = '%s' AND Filename = '%s'"
                   % (args.gw_id, args.healpix_file))
        else:
            sel = "SELECT id, Filename FROM HealpixMap WHERE GWID = '%s'" % args.gw_id
        rows = query_db([sel])[0]
        map_ids = [(int(r[0]), r[1]) for r in rows]

    directory = event_dir(args.gw_id, args.healpix_dir)
    files = [] if args.db_only else plan_event_file_deletion(directory)

    _banner("Delete plan for event %s" % args.gw_id)
    logger.info("Database maps to remove (%d):" % len(map_ids))
    for mid, fn in map_ids:
        logger.info("  - HealpixMap id=%d  (%s)" % (mid, fn))
    if not args.db_only:
        logger.info("\nFiles/dirs to remove under %s (%d):" % (directory, len(files)))
        for p in files[:20]:
            logger.info("  - %s" % p)
        if len(files) > 20:
            logger.info("  ... and %d more" % (len(files) - 20))

    if not args.yes:
        logger.info("\nDRY RUN -- nothing was deleted. Re-run with --yes to execute.")
        return 0

    if not args.files_only:
        for mid, fn in map_ids:
            logger.info("Deleting HealpixMap id=%d ..." % mid)
            try:
                for q in build_delete_map_sql(mid):
                    query_db([q], commit=True, raise_on_error=True)
            except Exception as e:
                logger.error("Failed to delete HealpixMap id=%d: %s" % (mid, e))
                return 1
    if not args.db_only and os.path.isdir(directory):
        import shutil
        logger.info("Removing directory %s ..." % directory)
        shutil.rmtree(directory)

    _banner("Deleted event %s." % args.gw_id)
    return 0


def cmd_add_telescope(args):
    """Register a new telescope/detector (e.g. LSST / Vera Rubin) from its Treasure
    Map id, and optionally build its all-sky static tile grid."""
    geo = normalize_geometry_args(args.geometry, args.width, args.height, args.radius)
    t = _teglon()
    _banner("Adding detector (TM id=%s) ..." % args.tm_detector_id)
    t.add_detector(tm_detector_id=args.tm_detector_id, min_dec=args.min_dec,
                   max_dec=args.max_dec, **geo)

    from web.src.utilities.Database_Helpers import query_db
    if args.teglon_detector_id is not None:
        rows = query_db(["SELECT id, Name FROM Detector WHERE id = %d;" % args.teglon_detector_id])[0]
    else:
        # add_detector doesn't return the new id; find it by its Treasure Map id (also covers a re-run
        # where the detector already existed).
        rows = query_db(["SELECT id, Name FROM Detector WHERE TM_id = %d ORDER BY id;" % args.tm_detector_id])[0]
    if len(rows) == 0:
        logger.error("Detector not found (TM_id=%s, id=%s); can't build its static grid."
                     % (args.tm_detector_id, args.teglon_detector_id))
        return 1
    detector_id, detector_name = int(rows[0][0]), rows[0][1]

    n_tiles = int(query_db(["SELECT COUNT(*) FROM StaticTile WHERE Detector_id = %d;" % detector_id])[0][0][0])

    if args.no_grid:
        logger.info("\nDetector added without a static grid (--no-grid); `extract` won't produce tiles for it "
                    "until you re-run without --no-grid.")
        return 0

    if n_tiles > 0:
        logger.info("Detector id=%s already has a static grid (%d tiles); not building another." %
                    (detector_id, n_tiles))
        return 0

    # Prefixes already used by other detectors' grids (one sample FieldName per detector is enough).
    existing = {grid_prefix_from_field_name(r[1]): r[0] for r in query_db(
        ["SELECT Detector_id, MIN(FieldName) FROM StaticTile GROUP BY Detector_id;"])[0]}
    prefix = args.prefix
    if prefix is None:
        prefix = choose_grid_prefix(detector_name, existing)
        logger.info("Using static-grid prefix `%s` for %s (taken: %s)." %
                    (prefix, detector_name, ", ".join(sorted(existing)) or "none"))
    elif prefix.upper() in {p.upper() for p in existing}:
        logger.error("Prefix `%s` is already used by another detector's grid; pick another or omit --prefix "
                     "to choose one automatically." % prefix)
        return 1

    _banner("Building static grid for %s (Teglon detector id=%s, prefix `%s`) ..." % (detector_name, detector_id, prefix))
    t.add_static_grid(teglon_detector_id=detector_id, detector_prefix=prefix)
    logger.info("\nStatic grid built. Maps loaded before now have no tiles for this detector; reload them "
                "with `teglon load-map <GWID> --healpix-file <file>` (clobbers by default).")
    return 0


def cmd_list_telescopes(args):
    """List registered telescopes/detectors: Treasure Map id, dec limits, and static-grid size
    (a detector with 0 static tiles gets no `extract` output)."""
    from web.src.utilities.Database_Helpers import query_db
    res = query_db(["""
        SELECT d.id, d.Name, d.TM_id, d.MinDec, d.MaxDec, COUNT(st.id) AS static_tiles
        FROM Detector d
        LEFT JOIN StaticTile st ON st.Detector_id = d.id
        GROUP BY d.id, d.Name, d.TM_id, d.MinDec, d.MaxDec
        ORDER BY d.id;
    """])
    if not res:
        logger.error("Could not query the Detector table (is the database up? `./teglon up`).")
        return 1

    detectors = [{"id": int(r[0]), "name": r[1], "tm_id": None if r[2] is None else int(r[2]),
                  "min_dec": float(r[3]), "max_dec": float(r[4]), "static_tiles": int(r[5])} for r in res[0]]
    if args.json:
        _emit(json.dumps({"detectors": detectors}))
        return 0

    _emit("%-5s %-32s %-7s %16s %13s" % ("ID", "NAME", "TM_ID", "DEC RANGE", "STATIC_TILES"))
    for d in detectors:
        _emit("%-5s %-32s %-7s %16s %13s%s" % (
            d["id"], d["name"], "-" if d["tm_id"] is None else d["tm_id"],
            "%.1f..%.1f" % (d["min_dec"], d["max_dec"]), d["static_tiles"],
            "" if d["static_tiles"] or d["tm_id"] is None
            else "  (no grid: re-run add-telescope --tm-detector-id %s)" % d["tm_id"]))
    return 0


def _setup_stage_done(script_path):
    """Best-effort check whether a setup stage's output already exists, so a re-run
    after a partial failure doesn't duplicate rows. Returns (done, detail)."""
    name = os.path.basename(script_path)
    try:
        if name in ("bulk_upload_glade.py", "initialize_teglon.py"):
            from web.src.utilities.Database_Helpers import query_db
            table = "Galaxy" if name == "bulk_upload_glade.py" else "StaticTile"
            n = int(query_db(["SELECT COUNT(*) FROM %s" % table])[0][0][0])
            return (n > 0, "%s rows=%d" % (table, n))
        if name == "build_init_pickles.py":
            pkl = os.path.join(REPO_ROOT, "web", "src", "utilities", "pickles",
                               "composed_completeness_dict.pkl")
            return (os.path.exists(pkl), pkl)
    except Exception as e:
        logger.debug("idempotency check failed for %s: %s" % (name, e))
    return (False, "")


def cmd_setup(args):
    """One-time complete Teglon initialization. DRY-RUN by default: prints the plan;
    add --run to execute (~45-63 min). Stages already populated are skipped unless
    --force is given."""
    steps = build_setup_steps(skip_glade=args.skip_glade, skip_init=args.skip_init,
                              skip_pickles=args.skip_pickles, is_debug=not args.no_debug)
    _banner("Teglon one-time setup plan (%d steps)" % len(steps))
    for i, (label, argv) in enumerate(steps, 1):
        logger.info("  [%d] %s" % (i, label))
        logger.info("        python %s" % " ".join(argv))

    if not args.run:
        logger.info("\nDRY RUN -- nothing was executed. Re-run with --run to perform setup.")
        return 0

    for i, (label, argv) in enumerate(steps, 1):
        if not args.force:
            done, detail = _setup_stage_done(argv[0])
            if done:
                _banner("[setup %d/%d] %s -- already populated (%s); skipping (use --force to redo)."
                        % (i, len(steps), label, detail))
                continue
        _banner("[setup %d/%d] %s" % (i, len(steps), label))
        rc = _run_script(argv[0], argv[1:])
        if rc != 0:
            logger.info("Step failed (rc=%s): %s. Aborting." % (rc, label))
            return rc
    _banner("Setup complete.")
    return 0


def cmd_trigger(args):
    """Triggering pipeline: ingest + galaxy-reweight a GW skymap and export the
    updated (4D) HEALPix probability map to a FITS file."""
    t = _teglon()
    if not args.no_ingest:
        _banner("[trigger] Ingesting + reweighting %s ..." % args.gw_id)
        t.load_map(gw_id=args.gw_id, healpix_dir=args.healpix_dir,
                   healpix_file=args.healpix_file, clobber=not args.no_clobber,
                   analysis_mode=args.analysis_mode, t_0_override=getattr(args, "t0", None))

    _banner("[trigger] Exporting reweighted skymap ...")
    from web.src.utilities.Database_Helpers import query_db
    import healpy as hp

    sel_map = ("SELECT id, RescaledNSIDE FROM HealpixMap WHERE GWID = '%s' AND Filename = '%s'"
               % (args.gw_id, args.healpix_file))
    mrow = query_db([sel_map])[0][0]
    map_id = int(mrow[0])
    nside = int(mrow[1])

    sel_pix = (
        "SELECT hp.id, hp.HealpixMap_id, hp.Pixel_Index, hp.Prob, hpc.NetPixelProb "
        "FROM HealpixPixel hp JOIN HealpixPixel_Completeness hpc ON hpc.HealpixPixel_id = hp.id "
        "WHERE hp.HealpixMap_id = %d ORDER BY hp.Pixel_Index;" % map_id
    )
    rows = query_db([sel_pix])[0]
    arr = reconstruct_reweighted_map(rows, nside, prob_index=4, pixindex_index=2)

    out_path = reweighted_output_path(args.gw_id, args.healpix_dir, args.healpix_file)
    hp.write_map(out_path, arr, overwrite=True)
    _banner("[trigger] Reweighted skymap written: %s" % out_path)
    return 0


def cmd_doctor(args):
    """Preflight checks: database connectivity + content, pickle caches, dust maps,
    GLADE catalog, and Treasure Map token validity. Exits non-zero on a hard failure."""
    checks = []  # (name, status, detail) with status in OK / WARN / FAIL

    # 1. Database connectivity + content.
    try:
        from web.src.utilities.Database_Helpers import query_db, db_host, db_port, db_name
        nmaps = int(query_db(["SELECT COUNT(*) FROM HealpixMap"])[0][0][0])
        ngal = int(query_db(["SELECT COUNT(*) FROM Galaxy"])[0][0][0])
        ntile = int(query_db(["SELECT COUNT(*) FROM StaticTile"])[0][0][0])
        checks.append(("database", "OK", "%s:%s/%s" % (db_host, db_port, db_name)))
        status = "OK" if (ngal > 0 and ntile > 0) else "WARN"
        checks.append(("database content", status,
                       "maps=%d galaxies=%d statictiles=%d%s"
                       % (nmaps, ngal, ntile, "" if status == "OK" else "  (run `teglon setup --run`)")))
    except Exception as e:
        checks.append(("database", "FAIL", str(e)))

    # 2. Pickle caches.
    pdir = os.path.join(REPO_ROOT, "web", "src", "utilities", "pickles")
    for pk in ("composed_completeness_dict.pkl", "ebv.pkl", "sky_pixels.pkl"):
        path = os.path.join(pdir, pk)
        checks.append(("pickle " + pk, "OK" if os.path.exists(path) else "WARN", path))

    # 3. Dust maps.
    dust_cfg = os.environ.get("DUSTMAPS_CONFIG_FNAME", "")
    dust_base = os.path.dirname(dust_cfg) if dust_cfg else "/dustmaps"
    sfd = os.path.join(dust_base, "sfd", "SFD_dust_4096_ngp.fits")
    checks.append(("dust map (SFD)", "OK" if os.path.exists(sfd) else "WARN", sfd))

    # 4. GLADE catalog file.
    glade = os.path.join(REPO_ROOT, "web", "src", "utilities", "galaxy_catalog_files", "GLADE_2.4.dat")
    checks.append(("GLADE catalog", "OK" if os.path.exists(glade) else "WARN", glade))

    # 5. Treasure Map token validity (the type=1 instruments probe).
    try:
        from configparser import RawConfigParser
        import requests
        cfg = RawConfigParser()
        cfg.read([os.environ.get("TEGLON_SETTINGS", ""),
                  os.path.join(REPO_ROOT, "Settings.ini"), "Settings.ini"])
        token = cfg.get("treasuremap", "TM_API_TOKEN")
        endpoint = cfg.get("treasuremap", "TM_ENDPOINT").rstrip("/")
        resp = requests.get(endpoint + "/instruments",
                            params={"api_token": token, "type": 1}, timeout=20)
        if resp.status_code == 200 and isinstance(resp.json(), list):
            checks.append(("Treasure Map token", "OK", "%d instruments" % len(resp.json())))
        else:
            checks.append(("Treasure Map token", "WARN", "HTTP %s" % resp.status_code))
    except Exception as e:
        checks.append(("Treasure Map token", "WARN", str(e)))

    if getattr(args, "json", False):
        _emit(json.dumps([{"check": n, "status": s, "detail": d} for n, s, d in checks]))
    else:
        for n, s, d in checks:
            _emit("[%-4s] %-26s %s" % (s, n, d))
    return 1 if any(s == "FAIL" for _, s, _ in checks) else 0


# --- argument wiring ---------------------------------------------------------
def _add_common_event_args(p):
    p.add_argument("gw_id", help="LIGO superevent name, e.g. S230529ay")
    p.add_argument("--healpix-dir", dest="healpix_dir", default="./web/events/{GWID}",
                   help="Directory holding the healpix file. Default: ./web/events/{GWID}")
    p.add_argument("--healpix-file", dest="healpix_file", default="bayestar.fits.gz",
                   help="Healpix filename. Default: bayestar.fits.gz")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="teglon",
        description="Teglon-O4: gravitational-wave follow-up observation planner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="teglon " + _get_version())
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true",
                           help="Only log warnings/errors (results still print to stdout).")
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           help="Log per-query debug detail too.")
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p = sub.add_parser("run", help="Full pipeline for a GW event: load -> extract -> plot")
    _add_common_event_args(p)
    p.add_argument("--tele", default="a",
                   help="Telescope: s,t,n,t80,nf,a(ll). Default: a")
    p.add_argument("--extinct", type=float, default=0.5, help="Max extinction (mag). Default: 0.5")
    p.add_argument("--prob-type", dest="prob_type", default="4D", choices=["4D", "2D"],
                   help="Probability type. Default: 4D")
    p.add_argument("--cum-prob", dest="cum_prob", type=float, default=0.9,
                   help="Cumulative probability to cover (0.2-0.95). Default: 0.9")
    p.add_argument("--num-tiles", dest="num_tiles", type=int, default=1000,
                   help="Top N tiles per telescope. Default: 1000")
    p.add_argument("--analysis-mode", dest="analysis_mode", action="store_true",
                   help="Load map at native resolution (no rescale).")
    p.add_argument("--no-clobber", dest="clobber", action="store_false", default=True,
                   help="Do not replace an existing (gw_id, healpix_file) in the DB.")
    p.add_argument("--no-plot", dest="no_plot", action="store_true", help="Skip the plot stage.")
    p.add_argument("--t0", dest="t0", type=float, default=None,
                   help="Event GPS time. Use with a pre-placed local skymap to skip GraceDB "
                        "(required for non-superevents like GW170817).")
    p.add_argument("--skip-swope", dest="skip_swope", action="store_true")
    p.add_argument("--skip-thacher", dest="skip_thacher", action="store_true")
    p.add_argument("--skip-t80", dest="skip_t80", action="store_true")
    p.add_argument("--skip-newfirm", dest="skip_newfirm", action="store_true")
    p.set_defaults(func=cmd_run)

    # load-map
    p = sub.add_parser("load-map", help="Download + ingest a healpix map into the DB")
    _add_common_event_args(p)
    p.add_argument("--analysis-mode", dest="analysis_mode", action="store_true")
    p.add_argument("--no-clobber", dest="clobber", action="store_false", default=True)
    p.add_argument("--t0", dest="t0", type=float, default=None,
                   help="Event GPS time. Use with a pre-placed local skymap to skip GraceDB "
                        "(required for non-superevents like GW170817).")
    p.add_argument("--skip-swope", dest="skip_swope", action="store_true")
    p.add_argument("--skip-thacher", dest="skip_thacher", action="store_true")
    p.add_argument("--skip-t80", dest="skip_t80", action="store_true")
    p.add_argument("--skip-newfirm", dest="skip_newfirm", action="store_true")
    p.set_defaults(func=cmd_load_map)

    # extract
    p = sub.add_parser("extract", help="Extract ranked tile lists from an ingested map")
    _add_common_event_args(p)
    p.add_argument("--tele", default="a")
    p.add_argument("--band", default="r", help="Band (g,r,i,z,I,J). Default: r")
    p.add_argument("--extinct", type=float, default=0.5)
    p.add_argument("--prob-type", dest="prob_type", default="4D", choices=["4D", "2D"])
    p.add_argument("--cum-prob", dest="cum_prob", type=float, default=0.9)
    p.add_argument("--num-tiles", dest="num_tiles", type=int, default=1000)
    p.add_argument("--min-ra", dest="min_ra", type=float, default=-1.0)
    p.add_argument("--max-ra", dest="max_ra", type=float, default=-1.0)
    p.add_argument("--min-dec", dest="min_dec", type=float, default=-1.0)
    p.add_argument("--max-dec", dest="max_dec", type=float, default=-1.0)
    p.add_argument("--include-observed", dest="exclude_observed", action="store_false", default=True,
                   help="Keep map pixels already covered by ingested pointings (excluded by default).")
    p.add_argument("--exclude-planned", dest="exclude_planned", action="store_true",
                   help="Also skip pixels covered by planned Treasure Map pointings (other groups' plans). "
                        "Writes *_exclplanned.txt so the completed-only tiles are kept.")
    p.add_argument("--json", action="store_true", help="Emit a JSON summary of the tile files.")
    p.set_defaults(func=cmd_extract)

    # plot
    p = sub.add_parser("plot", help="Plot probability map + tiles for a GW event")
    _add_common_event_args(p)
    p.add_argument("--tele", default="a")
    p.add_argument("--band", default="r")
    p.add_argument("--extinct", type=float, default=0.5)
    p.add_argument("--tile-file", dest="tile_file", default="{FILENAME}")
    p.add_argument("--num-tiles", dest="num_tiles", type=int, default=-1)
    p.add_argument("--cum-prob-outer", dest="cum_prob_outer", type=float, default=0.9)
    p.add_argument("--cum-prob-inner", dest="cum_prob_inner", type=float, default=0.5)
    p.set_defaults(func=cmd_plot)

    # load-obs
    p = sub.add_parser("load-obs", help="Ingest community/observed tiles into the DB")
    _add_common_event_args(p)
    p.add_argument("--tile-dir", dest="tile_dir", default="./web/events/{GWID}/observed_tiles")
    p.add_argument("--tile-file", dest="tile_file", default="",
                   help="File of observed tiles to import (or use --tm-instrument).")
    p.add_argument("--tm-instrument", dest="tm_instrument", default="",
                   help="Treasure Map instrument id/name: pull pointings from Treasure Map instead of a file. "
                        "Reads the token from the TM_API_TOKEN env var.")
    p.add_argument("--tm-status", dest="tm_status", default="completed", choices=["completed", "planned"],
                   help="Planned pointings are tagged so they can be cleared with `clear-planned`.")
    p.add_argument("--detector-name", dest="detector_name", default="",
                   help="Teglon Detector.Name to load the pointings under.")
    p.set_defaults(func=cmd_load_obs)

    # efficiency
    p = sub.add_parser("efficiency", help="Model transient detection efficiency for an event")
    _add_common_event_args(p)
    p.add_argument("--model-type", dest="model_type", default="kne", help="e.g. kne, grb. Default: kne")
    p.add_argument("--num-cpu", dest="num_cpu", type=int, default=5)
    p.add_argument("--clobber", action="store_true")
    p.add_argument("--json", action="store_true", help="Emit a JSON summary of the output files.")
    p.set_defaults(func=cmd_efficiency)

    # compare
    p = sub.add_parser("compare",
                       help="Side-by-side 2D vs 4D (galaxy-reweighted) skymap PDF + area metrics")
    _add_common_event_args(p)
    p.add_argument("--out", default=None, help="Output PDF path (default: in the event dir).")
    p.add_argument("--json", action="store_true", help="Emit the area metrics as JSON.")
    p.set_defaults(func=cmd_compare)

    # skymap-info
    p = sub.add_parser("skymap-info",
                       help="50/90/99%% credible-region areas of a skymap FITS (healpy)")
    p.add_argument("skymap_fits_file",
                   help="A FITS path, OR a GW id (then the original + reweighted maps "
                        "in the event dir are both reported with the shrink factor).")
    p.add_argument("--healpix-file", dest="healpix_file", default="bayestar.fits.gz",
                   help="Original map filename, GW-id mode only. Default: bayestar.fits.gz")
    p.add_argument("--healpix-dir", dest="healpix_dir", default="./web/events/{GWID}",
                   help="Event directory, GW-id mode only.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of KEY=value.")
    p.set_defaults(func=cmd_skymap_info)

    # doctor
    p = sub.add_parser("doctor",
                       help="Preflight: check DB, pickles, dust maps, GLADE, Treasure Map token")
    p.add_argument("--json", action="store_true", help="Emit the checks as JSON.")
    p.set_defaults(func=cmd_doctor)

    # bootstrap
    p = sub.add_parser("bootstrap", help="Build a fresh DB: dust + GLADE (+ optional detectors)")
    p.add_argument("--instruments-config", dest="instruments_config", default=None,
                   help="JSON file describing instruments (detectors + static grids) to create.")
    p.add_argument("--skip-dust", dest="skip_dust", action="store_true")
    p.add_argument("--skip-galaxies", dest="skip_galaxies", action="store_true")
    p.set_defaults(func=cmd_bootstrap)

    # sync-pointings
    p = sub.add_parser("sync-pointings",
                       help="Load Treasure Map pointings for events (only new ones; per event's latest map)")
    p.add_argument("gw_ids", nargs="*", metavar="gw_id",
                   help="Event(s) to sync. Default: every event in the DB within --lookback-days.")
    p.add_argument("--lookback-days", dest="lookback_days", type=int, default=30,
                   help="With no gw_id: only events from the last N days (0 = all). Default: 30")
    p.add_argument("--status", default="completed", choices=["completed", "planned"],
                   help="Planned pointings are tagged so `extract --exclude-planned` can use them and "
                        "`clear-planned` can remove them. Default: completed")
    p.set_defaults(func=cmd_sync_pointings)

    # tm-ingest
    p = sub.add_parser("tm-ingest",
                       help="Treasure Map flow: load new events, sync pointings, then extract + plot changed events")
    p.add_argument("--gw-id", dest="gw_id", default="",
                   help="Only this event. Default: all recent significant events.")
    p.add_argument("--lookback-days", dest="lookback_days", type=int, default=30,
                   help="Alerts/events from the last N days (0 = all). Default: 30")
    p.add_argument("--only-with-pointings", dest="only_with_pointings", action="store_true",
                   help="Only ingest events that already have Treasure Map pointings.")
    p.add_argument("--skip-pointings", dest="skip_pointings", action="store_true", help="Ingest events only.")
    p.add_argument("--role", default="observation", choices=["observation", "test", "all"],
                   help="Alert role: real events (observation), LIGO mock MS... events (test), or both. Mock "
                        "alerts are frequent, so pair `test` with a short --lookback-days. Ignored with --gw-id.")
    p.add_argument("--planned", action="store_true",
                   help="Also load planned pointings and extract tiles that avoid them (*_exclplanned.txt).")
    p.set_defaults(func=cmd_tm_ingest)

    # clear-planned
    p = sub.add_parser("clear-planned", help="Remove an event's planned Treasure Map pointings from the DB")
    p.add_argument("gw_id", help="LIGO superevent name, e.g. S230529ay")
    p.add_argument("--healpix-file", dest="healpix_file", default=None,
                   help="Only this map's planned pointings. Default: all of the event's maps.")
    p.set_defaults(func=cmd_clear_planned)

    # delete-event
    p = sub.add_parser("delete-event",
                       help="Clean a GW event from the DB and files (dry-run unless --yes)")
    _add_common_event_args(p)
    p.add_argument("--yes", action="store_true", help="Actually perform the deletion (otherwise dry-run).")
    p.add_argument("--db-only", dest="db_only", action="store_true", help="Only remove DB records, keep files.")
    p.add_argument("--files-only", dest="files_only", action="store_true", help="Only remove files, keep DB records.")
    p.set_defaults(func=cmd_delete_event, healpix_file=None)

    # add-telescope
    p = sub.add_parser("add-telescope", help="Register a new telescope (e.g. LSST/Vera Rubin)")
    p.add_argument("--tm-detector-id", dest="tm_detector_id", type=int, required=True,
                   help="Treasure Map instrument id for the telescope.")
    p.add_argument("--geometry", default="polygon", choices=["rectangle", "circle", "polygon"],
                   help="FOV geometry. Default: polygon (use the Treasure Map footprint).")
    p.add_argument("--width", type=float, default=None, help="FOV width (deg); rectangle only.")
    p.add_argument("--height", type=float, default=None, help="FOV height (deg); rectangle only.")
    p.add_argument("--radius", type=float, default=None, help="FOV radius (deg); circle only.")
    p.add_argument("--min-dec", dest="min_dec", type=float, default=-90.0)
    p.add_argument("--max-dec", dest="max_dec", type=float, default=90.0)
    p.add_argument("--teglon-detector-id", dest="teglon_detector_id", type=int, default=None,
                   help="Override: DB Detector.id to build the grid for. Default: looked up by --tm-detector-id.")
    p.add_argument("--prefix", default=None,
                   help="Field-name prefix for the static grid (e.g. 'L'). Default: chosen automatically from "
                        "the detector name (first letter, then first two, ...) so it's unique.")
    p.add_argument("--no-grid", dest="no_grid", action="store_true",
                   help="Only register the detector; don't build its static grid (extract needs the grid).")
    p.set_defaults(func=cmd_add_telescope)

    # list-telescopes
    p = sub.add_parser("list-telescopes", help="List registered telescopes, their Treasure Map ids and grid sizes")
    p.add_argument("--json", action="store_true", help="Emit the detectors as JSON.")
    p.set_defaults(func=cmd_list_telescopes)

    # setup
    p = sub.add_parser("setup", help="One-time complete initialization (dry-run unless --run)")
    p.add_argument("--run", action="store_true", help="Actually execute setup (otherwise print the plan).")
    p.add_argument("--force", action="store_true",
                   help="Re-run stages even if their tables/pickles already exist.")
    p.add_argument("--skip-glade", dest="skip_glade", action="store_true")
    p.add_argument("--skip-init", dest="skip_init", action="store_true")
    p.add_argument("--skip-pickles", dest="skip_pickles", action="store_true")
    p.add_argument("--no-debug", dest="no_debug", action="store_true",
                   help="Omit the --is_debug flag from initialize_teglon.")
    p.set_defaults(func=cmd_setup)

    # trigger
    p = sub.add_parser("trigger",
                       help="Ingest + reweight a skymap and export the updated 4D HEALPix map")
    _add_common_event_args(p)
    p.add_argument("--no-ingest", dest="no_ingest", action="store_true",
                   help="Skip load-map; just export from an already-ingested event.")
    p.add_argument("--no-clobber", dest="no_clobber", action="store_true",
                   help="Do not replace an existing (gw_id, healpix_file) when ingesting.")
    p.add_argument("--analysis-mode", dest="analysis_mode", action="store_true")
    p.add_argument("--t0", dest="t0", type=float, default=None,
                   help="Event GPS time. Use with a pre-placed local skymap to skip GraceDB "
                        "(required for non-superevents like GW170817).")
    p.set_defaults(func=cmd_trigger)

    return parser


def main(argv=None):
    _ensure_repo_root()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(quiet=getattr(args, "quiet", False), verbose=getattr(args, "verbose", False))
    start = time.time()
    rc = args.func(args)
    logger.info("\n[teglon %s] total execution time: %.1fs" % (args.command, time.time() - start))
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
