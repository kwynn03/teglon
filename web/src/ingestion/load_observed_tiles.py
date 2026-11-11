import os
import re
import sys
import csv
import json
import time
import pickle
from collections import OrderedDict

import numpy as np
import requests
from astropy import units as u
import astropy.coordinates as coord
from astropy.time import Time
import healpy as hp
import pprint
from astropy.table import Table
from astropy.io import ascii

from web.src.objects.Detector import *
from web.src.objects.Tile import *
from web.src.objects.Pixel_Element import *
from web.src.utilities.Database_Helpers import *
from web.src.utilities.tm_bands import teglon_band_name, vega_to_ab_offset


isDEBUG = False

TREASUREMAP_BASE_URL = "https://treasuremap.space/api/v1"

# ObservedTile.Source tags for Treasure Map rows. Planned pointings are tagged separately so they can be
# excluded (or not) at extract time and cleared without touching completed observations.
TM_SOURCE = "TreasureMap"
TM_SOURCE_PLANNED = "TreasureMap-planned"

# Columns the tile-loading loop expects; NaN is used for anything TM doesn't provide.
TILE_COLUMNS = ["source", "field_name", "ra", "dec", "filter", "mjd", "exp_time", "mag_lim", "position_angle",
                "x0", "x0_err", "a", "a_err", "n", "n_err"]

UTILITIES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utilities")


def tm_source(status):
    return TM_SOURCE_PLANNED if status == "planned" else TM_SOURCE


# ----------------------------------------------------------------------------------------------------------------
# Treasure Map helpers
# ----------------------------------------------------------------------------------------------------------------
def _tm_get(endpoint, params):
    base_url = (os.environ.get("TM_ENDPOINT") or TREASUREMAP_BASE_URL).rstrip("/")
    url = "%s/%s" % (base_url, endpoint)
    try:
        r = requests.get(url, params=params, timeout=120)
    except requests.RequestException as e:
        # requests' message includes the full URL, i.e. the api_token query param; don't propagate it.
        raise RuntimeError("Treasure Map `%s` request failed: %s" % (endpoint, type(e).__name__)) from None
    if r.status_code != 200:
        raise RuntimeError("Treasure Map `%s` request failed (%s): %s" % (endpoint, r.status_code, r.text[:500]))

    # Older versions of the API return a JSON list of JSON-encoded strings; handle both shapes.
    data = r.json()
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = [data]
    return [json.loads(d) if isinstance(d, str) else d for d in data]


def _parse_point(position):
    m = re.search(r'POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)', str(position))
    if m is None:
        raise ValueError("Could not parse pointing position `%s`" % position)
    ra = float(m.group(1)) % 360.0
    dec = float(m.group(2))
    return ra, dec


def _depth_to_ab_mag(depth, depth_unit, teglon_band=None):
    if depth is None:
        return None
    unit = str(depth_unit or "").lower()
    depth = float(depth)

    if "ab_mag" in unit:
        return depth
    if "flux_jy" in unit:
        if depth <= 0:
            return None
        return -2.5 * np.log10(depth / 3631.0)

    if "vega_mag" in unit:
        # Band-dependent offset (tm_bands.VEGA_TO_AB, Blanton & Roweis 2007); skip bands without one.
        offset = vega_to_ab_offset(teglon_band)
        return None if offset is None else depth + offset

    # flux_erg needs a bandpass; skip rather than guess.
    return None


def pointings_to_table(pointings, source=TM_SOURCE):
    """Convert Treasure Map pointing dicts to a Teglon tile table. Returns (table, skipped)."""
    rows = []
    skipped = []
    for p in pointings:
        pid = p.get("id")
        try:
            ra, dec = _parse_point(p.get("position"))
        except ValueError as e:
            skipped.append((pid, str(e)))
            continue

        if p.get("time") is None:
            skipped.append((pid, "no observation time"))
            continue
        mjd = Time(str(p["time"]).replace(" ", "T"), format="isot", scale="utc").mjd

        band = teglon_band_name(p.get("band"))
        mag_lim = _depth_to_ab_mag(p.get("depth"), p.get("depth_unit"), band)
        if mag_lim is None:
            skipped.append((pid, "unsupported/missing depth (%s %s)" % (p.get("depth"), p.get("depth_unit"))))
            continue

        pa = p.get("pos_angle")
        pa = 0.0 if pa is None else float(pa)

        rows.append({
            "source": source,
            "field_name": "TM_%s" % pid,
            "ra": ra,
            "dec": dec,
            "filter": band,
            "mjd": mjd,
            "exp_time": np.nan,  # TM pointings don't carry exposure time
            "mag_lim": float(mag_lim),
            "position_angle": pa,
            "x0": np.nan, "x0_err": np.nan,
            "a": np.nan, "a_err": np.nan,
            "n": np.nan, "n_err": np.nan,
        })

    table = Table(rows=[[r[c] for c in TILE_COLUMNS] for r in rows], names=TILE_COLUMNS) if rows \
        else Table(names=TILE_COLUMNS)
    return table, skipped


# ----------------------------------------------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------------------------------------------
def _nan_to_none(val):
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(val) else val


def load_tables(gw_id, healpix_file, tile_dir, tables_by_detector_name):
    """Load tile tables for one or more detectors against the map (gw_id, healpix_file).
    Keys are Detector.Name, or Detector.id (int) to pick one of several detectors sharing a Name.

    The pickles, map pixels and band lookup are loaded once and shared by every detector.
    Returns (rc, {detector_name: number of tiles inserted})."""

    pickle_output_dir = "%s/pickles/" % UTILITIES_DIR
    inserted = {}

    tables_by_detector_name = {d: t for d, t in tables_by_detector_name.items() if len(t) > 0}
    if len(tables_by_detector_name) == 0:
        print("No tiles to load.")
        return 0, inserted

    print("\tLoading NSIDE 128 pixels...")
    with open(pickle_output_dir + 'N128_dict.pkl', 'rb') as handle:
        N128_dict = pickle.load(handle)

    print("\tLoading existing mwe...")
    if not os.path.exists(pickle_output_dir + "ebv.pkl"):
        print("`%sebv.pkl` does not exist; run `teglon bootstrap` first. Exiting..." % pickle_output_dir)
        return 1, inserted
    with open(pickle_output_dir + "ebv.pkl", 'rb') as handle:
        ebv = pickle.load(handle)

    # Get Map ID
    healpix_map_select = "SELECT id, RescaledNSIDE FROM HealpixMap WHERE GWID = '%s' and Filename = '%s'"
    healpix_map_query = query_db([healpix_map_select % (gw_id, healpix_file)])[0]
    if len(healpix_map_query) == 0:
        print("Map (%s, %s) is not loaded in Teglon. Exiting..." % (gw_id, healpix_file))
        return 1, inserted
    healpix_map_id = int(healpix_map_query[0][0])
    healpix_map_nside = int(healpix_map_query[0][1])

    print("Get map pixel")
    map_pixel_select = "SELECT id, HealpixMap_id, Pixel_Index, Prob, Distmu, Distsigma, Distnorm, Mean, Stddev, Norm, N128_SkyPixel_id FROM HealpixPixel WHERE HealpixMap_id = %s;"
    map_pixels = query_db([map_pixel_select % healpix_map_id])[0]
    print("Retrieved %s map pixels..." % len(map_pixels))

    # Initialize map pix dict for later access
    map_pixel_dict = {}
    for p in map_pixels:
        map_pixel_dict[int(p[2])] = p

    filter_select = '''
        SELECT `Name`, id FROM Band;
    '''
    filter_result = query_db([filter_select])[0]
    filter_lookup = {br[0]: int(br[1]) for br in filter_result}

    os.makedirs(tile_dir, exist_ok=True)

    rc = 0
    for detector_key, input_table in tables_by_detector_name.items():
        # One detector failing shouldn't stop the others from loading.
        try:
            detector_rc, n = _load_detector_table(detector_key, input_table, tile_dir, healpix_map_id,
                                                  healpix_map_nside, map_pixel_dict, filter_lookup, N128_dict, ebv)
        except Exception as e:
            print("Loading tiles for detector `%s` failed: %s: %s" % (detector_key, type(e).__name__, e))
            detector_rc, n = 1, 0
        inserted[detector_key] = n
        rc = rc or detector_rc
    return rc, inserted


def _load_detector_table(input_detector_name, input_table, tile_dir, healpix_map_id, healpix_map_nside,
                         map_pixel_dict, filter_lookup, N128_dict, ebv):
    """Insert one detector's tiles + their tile-pixel relation. Returns (rc, number of tiles inserted)."""

    detector_select_by_name = "SELECT id, Name, Deg_width, Deg_height, Deg_radius, Area, MinDec, MaxDec, ST_AsText(Poly) FROM Detector WHERE Name='%s'"

    duplicate_tile_data = []
    unknown_filter_data = []
    obs_tile_insert_data = {}

    if isinstance(input_detector_name, int):  # Detector.id (several TM instruments can share a Name)
        detector_query = query_db([detector_select_by_name.replace("Name='%s'", "id=%d") % input_detector_name])[0]
    else:
        detector_query = query_db([detector_select_by_name % input_detector_name])[0]
    if len(detector_query) == 0:
        print("Detector `%s` not found in Teglon's Detector table. Use --detector_name to map the Treasure "
              "Map instrument to a Teglon detector. Skipping..." % input_detector_name)
        return 1, 0
    detector_result = detector_query[0]

    detector_name = detector_result[1]
    detector_poly = Detector.get_detector_vertices_from_teglon_db(detector_result[8])
    detector = Detector(detector_name, detector_poly, detector_id=int(detector_result[0]))

    print("Processing %s tiles for %s" % (len(input_table), detector.name))

    # Sanity checks:
    #   0. Check for duplication: What defines the tile being the same:
    #       (Detector_id, MJD, Filter, N128_SkyPixel_id, HealpixMap_id)
    #       While exp time, mag lim, etc might be different, there's no legitimate way for the same detector to
    #       take > 1 "r-band" image at the exact same.
    #       Treasure Map rows are instead keyed by their TM pointing id (FieldName `TM_<id>`): dithers reported with
    #       the same nominal time land in the same N128 pixel but are distinct pointings, and it's the id that
    #       get_TM_pointings uses to decide what's already loaded.
    #   1. Check for duplication within the db. This preempts duplication within the payload.
    #   2. Check for duplication within the payload.
    #   3. Only upload tile-pixel relations for uploaded tiles (vs all tiles in the db)

    select_existing = '''
        SELECT
            COUNT(*)
        FROM ObservedTile
        WHERE
            Detector_id = %s AND
            MJD = %s AND
            Band_Id = %s AND
            N128_SkyPixel_id = %s AND
            HealpixMap_id = %s
    '''
    select_existing_tm = '''
        SELECT
            COUNT(*)
        FROM ObservedTile
        WHERE
            Detector_id = %s AND
            Source = '%s' AND
            FieldName = '%s' AND
            HealpixMap_id = %s
    '''

    for row in input_table:

        source = str(row['source'])
        field_name = str(row['field_name'])
        ra = float(row['ra'])
        dec = float(row['dec'])

        n128_pix_index = hp.ang2pix(128, ra, dec, lonlat=True)
        tile_ebv = float(ebv[n128_pix_index])
        n128_id = N128_dict[n128_pix_index]

        filter_name = str(row['filter'])
        if filter_name not in filter_lookup:
            print("\tUnknown filter `%s` for %s; skipping. Add it to TM_TO_TEGLON_BAND or the Band table." %
                  (filter_name, field_name))
            unknown_filter_data.append((field_name, filter_name))
            continue
        filter_id = filter_lookup[filter_name]

        mjd = float(row['mjd'])
        mag_lim = float(row['mag_lim'])
        position_angle = float(row['position_angle'])

        # 1. db check
        error_msg = "\tDuplicate Tile! (Detector: %s, MJD: %s, Filter_id: %s, N128_id: %s, HealpixMap_id: %s)" % \
                    (detector.id, mjd, filter_id, n128_id, healpix_map_id) + \
                    "\nSkipping! ***********\n"
        if source in (TM_SOURCE, TM_SOURCE_PLANNED) and re.match(r"^TM_\d+$", field_name):
            # Per Source, so a completed pointing isn't mistaken for its own planned copy.
            unique_key = (detector.id, source, field_name, healpix_map_id)
            already_exists = query_db([select_existing_tm % unique_key])[0]
        else:
            unique_key = (detector.id, mjd, filter_id, n128_id, healpix_map_id)
            already_exists = query_db([select_existing % unique_key])[0]
        if len(already_exists) > 0 and already_exists[0][0] >= 1:
            print("\n *********** Duplicated in DB: *********** ")
            print(error_msg)
            duplicate_tile_data.append(unique_key)
            continue

        # 2. payload check
        if unique_key not in obs_tile_insert_data:
            obs_tile_insert_data[unique_key] = None
        else:
            print("\n*********** Duplicate in Payload: *********** ")
            print(error_msg)
            duplicate_tile_data.append(unique_key)
            continue

        t = Tile(ra, dec, detector, int(healpix_map_nside), position_angle_deg=position_angle)

        obs_tile_insert_data[unique_key] = (
            source,
            detector.id,
            field_name,
            ra,
            dec,
            "POINT(%s %s)" % (dec, ra - 180.0),  # Dec, RA order due to MySQL convention for lat/lon
            t.query_polygon_string,
            tile_ebv,
            n128_id,
            filter_id,
            mjd,
            _nan_to_none(row['exp_time']),
            mag_lim,
            healpix_map_id,
            position_angle,
            _nan_to_none(row['x0']),
            _nan_to_none(row['x0_err']),
            _nan_to_none(row['a']),
            _nan_to_none(row['a_err']),
            _nan_to_none(row['n']),
            _nan_to_none(row['n_err'])
        )

    if len(unknown_filter_data) > 0:
        print("Tiles skipped due to unknown filters:")
        pprint.pprint(unknown_filter_data)

    # Convert dictionary to list for db insert
    insert_observed_tile_list = [value for key, value in obs_tile_insert_data.items()]

    # 3. Only insert tile-pixel data if there were tiles inserted (previous bug would regen tile-pixel relations
    #       for ALL existing tiles...
    if len(insert_observed_tile_list) <= 0:
        print("No tiles inserted for %s!" % detector.name)
        print("Duplicates removed from input:")
        pprint.pprint(duplicate_tile_data)
        return 0, 0

    insert_observed_tile = '''
        INSERT INTO
            ObservedTile (Source, Detector_id, FieldName, RA, _Dec, Coord, Poly, EBV, N128_SkyPixel_id, Band_id,
            MJD, Exp_Time, Mag_Lim, HealpixMap_id, PositionAngle, x0, x0_err, a, a_err, n, n_err)
        VALUES (%s, %s, %s, %s, %s, ST_PointFromText(%s, 4326), ST_GEOMFROMTEXT(%s, 4326), %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s)
    '''

    print("Inserting %s tiles..." % len(insert_observed_tile_list))
    batch_insert(insert_observed_tile, insert_observed_tile_list)
    print("Done...")

    # Only insert tile-pixel data for things just inserted!!!
    select_inserted = '''
        WITH NewINSERT AS (
            SELECT id FROM ObservedTile ORDER BY id DESC LIMIT %s
        )
        SELECT id FROM NewINSERT ORDER BY id ASC;
    '''
    inserted_ids = query_db([select_inserted % len(insert_observed_tile_list)])[0]
    # generate id list
    id_list = ",".join([str(id[0]) for id in inserted_ids])

    # Link the new tiles to the map's pixels. If that fails, remove the tiles again: a later sync would
    # otherwise treat them as loaded and never build their links (so extract couldn't exclude them).
    linked = False
    try:
        linked = _link_tiles_to_map_pixels(detector, id_list, tile_dir, healpix_map_id, healpix_map_nside,
                                           map_pixel_dict)
    finally:
        if not linked:
            print("Removing the %s tiles just inserted for %s ..." % (len(inserted_ids), detector.name))
            query_db(["DELETE FROM ObservedTile_HealpixPixel WHERE ObservedTile_id IN (%s);" % id_list,
                      "DELETE FROM ObservedTile WHERE id IN (%s);" % id_list], commit=True)
    if not linked:
        return 1, 0

    print("Duplicates removed from input:")
    pprint.pprint(duplicate_tile_data)
    return 0, len(insert_observed_tile_list)


def _link_tiles_to_map_pixels(detector, id_list, tile_dir, healpix_map_id, healpix_map_nside, map_pixel_dict):
    """Build ObservedTile_HealpixPixel rows for the tiles in `id_list`. Returns True on success."""
    print("Tiles inserted: Building observed tile-healpix map pixel relation...")

    obs_tile_select = '''
        SELECT `Source`, id, Detector_id, FieldName, RA, _Dec, Coord, Poly, EBV, N128_SkyPixel_id, Band_id, MJD, Exp_Time, Mag_Lim, HealpixMap_id, PositionAngle
        FROM ObservedTile
        WHERE id IN (%s)
    '''

    # Obtain the tile with id's so they can be used in later INSERTs
    tile_pixel_data = []
    obs_tiles_for_detector = query_db([obs_tile_select % id_list])[0]
    for otfd in obs_tiles_for_detector:

        ot_id = int(otfd[1])
        ra = float(otfd[4])
        dec = float(otfd[5])
        pa = float(otfd[15])

        t = Tile(ra, dec, detector, healpix_map_nside, position_angle_deg=pa)
        t.id = ot_id

        for p in t.enclosed_pixel_indices:
            # In search mode load_map only stores the ~90% credible region, so a pointing can cover pixels
            # the map doesn't have; those carry no probability to exclude (same as load_map's tile links).
            if p in map_pixel_dict:
                tile_pixel_data.append((t.id, map_pixel_dict[p][0], healpix_map_id))

    print("Length of tile_pixel_data: %s" % len(tile_pixel_data))
    if len(tile_pixel_data) == 0:
        print("None of these tiles overlap the map's stored pixels; nothing to link.")
        return True

    tile_pixel_upload_csv = "%s/%s_tile_pixel_upload.csv" % (tile_dir, detector.name.replace("/", "_"))

    # Create CSV
    try:
        t1 = time.time()
        print("Creating `%s`" % tile_pixel_upload_csv)
        with open(tile_pixel_upload_csv, 'w') as csvfile:
            csvwriter = csv.writer(csvfile)
            for data in tile_pixel_data:
                csvwriter.writerow(data)

        t2 = time.time()
        print("\n********* start DEBUG ***********")
        print("Tile-Pixel CSV creation execution time: %s" % (t2 - t1))
        print("********* end DEBUG ***********\n")
    except Exception as e:
        print("Error in creating Tile-Pixel CSV:\n")
        print(e)
        return False

    # clean up
    print("freeing `tile_pixel_data`...")
    del tile_pixel_data

    print("Bulk uploading `tile_pixel_data`...")
    ot_hp_upload_sql = """LOAD DATA LOCAL INFILE '%s'
                INTO TABLE ObservedTile_HealpixPixel
                FIELDS TERMINATED BY ','
                LINES TERMINATED BY '\n'
                (ObservedTile_id, HealpixPixel_id, HealpixMap_id);"""

    success = bulk_upload(ot_hp_upload_sql % tile_pixel_upload_csv)
    if not success:
        print("\nUnsuccessful bulk upload.")
        return False

    try:
        print("Removing `%s`..." % tile_pixel_upload_csv)
        os.remove(tile_pixel_upload_csv)
        print("... Done")
    except Exception as e:
        # The links are in; a leftover CSV isn't worth failing (and removing the tiles) over.
        print("Couldn't remove `%s`: %s" % (tile_pixel_upload_csv, e))
    return True


class Teglon:

    def add_options(self, parser=None, usage=None, config=None):
        import optparse
        if parser == None:
            parser = optparse.OptionParser(usage=usage, conflict_handler="resolve")

        parser.add_option('--gw_id', default="", type="str", help='LIGO superevent name, e.g. `S190425z`.')

        parser.add_option('--healpix_file', default="", type="str",
                          help='Healpix filename. Used with `gw_id` to identify unique map.')

        parser.add_option('--healpix_dir', default='./web/events/{GWID}', type="str",
                          help='Directory for where to look for the healpix file.')

        parser.add_option('--tile_dir', default="./web/events/{GWID}/observed_tiles", type="str",
                          help='Directory for where to look for observed tiles to import. In Treasure Map mode, '
                               'this is where the temporary tile-pixel CSV is written.')

        parser.add_option('--tile_file', default="", type="str",
                          help='File that contains the tile observations. Not needed if using --tm_instrument.')

        # Treasure Map options
        parser.add_option('--tm_api_token', default="", type="str",
                          help='Treasure Map API token. Prefer the TM_API_TOKEN environment variable: a token passed '
                               'here is visible to other users in `ps`.')

        parser.add_option('--tm_graceid', default="", type="str",
                          help='Treasure Map graceid to query. Defaults to `gw_id`.')

        parser.add_option('--tm_instrument', default="", type="str",
                          help='Treasure Map instrument name or id (e.g. `ZTF`). Setting this switches the script '
                               'to pull pointings from the Treasure Map API instead of `tile_file`.')

        parser.add_option('--tm_status', default="completed", type="str",
                          help='Treasure Map pointing status to query (completed, planned, cancelled). '
                               'Default: completed. Planned pointings are tagged Source=`%s`.' % TM_SOURCE_PLANNED)

        parser.add_option('--tm_band', default="", type="str",
                          help='Optional Treasure Map band filter (e.g. `r`).')

        parser.add_option('--detector_name', default="", type="str",
                          help='Teglon Detector.Name to associate the pointings with. In Treasure Map mode this '
                               'defaults to the Treasure Map instrument name.')

        parser.add_option('--tm_save_file', default="", type="str",
                          help='Optional: write the converted Treasure Map pointings to this path in Teglon tile '
                               'file format, so the run can be reproduced from a file later.')

        return (parser)

    def fetch_treasuremap_tiles(self, api_token, graceid, instrument, status, band=""):
        params = {
            "api_token": api_token,
            "graceid": graceid,
            "instrument": instrument,
            "status": status,
        }
        if band != "":
            params["band"] = band

        print("Querying Treasure Map pointings (graceid=%s, instrument=%s, status=%s%s)..." %
              (graceid, instrument, status, ", band=%s" % band if band else ""))
        pointings = _tm_get("pointings", params)
        print("Retrieved %s pointings from Treasure Map." % len(pointings))

        # Resolve the instrument name (used as the default Teglon detector name)
        tm_instrument_name = instrument
        instrument_ids = {p.get("instrumentid") for p in pointings if p.get("instrumentid") is not None}
        if len(instrument_ids) > 1:
            raise RuntimeError("Treasure Map returned pointings for multiple instruments %s; this script loads one "
                               "detector at a time. Use a more specific --tm_instrument." % sorted(instrument_ids))
        if len(instrument_ids) == 1:
            try:
                inst = _tm_get("instruments", {"api_token": api_token, "id": list(instrument_ids)[0]})
                if len(inst) > 0 and inst[0].get("instrument_name"):
                    tm_instrument_name = inst[0]["instrument_name"]
            except Exception as e:
                print("Could not resolve Treasure Map instrument name (%s); using `%s`." % (e, instrument))

        table, skipped = pointings_to_table(pointings, source=tm_source(status))
        if len(skipped) > 0:
            print("Skipped %s Treasure Map pointings:" % len(skipped))
            pprint.pprint(skipped)

        return table, tm_instrument_name

    def main(self):

        is_error = False

        use_treasuremap = self.options.tm_instrument != ""
        tm_api_token = self.options.tm_api_token or os.environ.get("TM_API_TOKEN", "")
        if use_treasuremap and tm_api_token == "":
            # Same fallback as the cron sync: [treasuremap] in Settings.ini (the ./teglon Docker wrapper
            # doesn't pass TM_API_TOKEN into the container).
            from web.src.utilities.get_TM_pointings import treasuremap_settings
            tm_api_token, tm_endpoint = treasuremap_settings()
            os.environ.setdefault("TM_ENDPOINT", tm_endpoint)
        tm_graceid = self.options.tm_graceid or self.options.gw_id

        # Parameter checks
        if self.options.gw_id == "":
            is_error = True
            print("GWID is required.")

        if self.options.healpix_file == "":
            is_error = True
            print("Healpix file is required.")

        if use_treasuremap:
            if tm_api_token == "":
                is_error = True
                print("A Treasure Map API token is required (--tm_api_token or TM_API_TOKEN).")
        elif self.options.tile_file == "":
            is_error = True
            print("Either --tile_file or --tm_instrument is required.")

        if is_error:
            print("Exiting...")
            return 1

        formatted_healpix_dir = self.options.healpix_dir
        if "{GWID}" in formatted_healpix_dir:
            formatted_healpix_dir = formatted_healpix_dir.replace("{GWID}", self.options.gw_id)

        formatted_tile_dir = self.options.tile_dir
        if "{GWID}" in formatted_tile_dir:
            formatted_tile_dir = formatted_tile_dir.replace("{GWID}", self.options.gw_id)

        hpx_path = "%s/%s" % (formatted_healpix_dir, self.options.healpix_file)
        tile_path = "%s/%s" % (formatted_tile_dir, self.options.tile_file)

        # Check if the above files exist...
        if not os.path.exists(hpx_path):
            is_error = True
            print("Healpix file `%s` does not exist." % hpx_path)

        if not use_treasuremap and not os.path.exists(tile_path):
            is_error = True
            print("Tile file `%s` does not exist." % tile_path)

        if is_error:
            print("Exiting...")
            return 1

        # Load the input tiles, either from Treasure Map or from a file
        if use_treasuremap:
            try:
                input_table, tm_instrument_name = self.fetch_treasuremap_tiles(tm_api_token, tm_graceid,
                                                                               self.options.tm_instrument,
                                                                               self.options.tm_status,
                                                                               self.options.tm_band)
            except Exception as e:
                print("Error retrieving Treasure Map pointings:\n%s\nExiting..." % e)
                return 1

            input_detector_name = self.options.detector_name or tm_instrument_name

            if self.options.tm_save_file != "":
                input_table.meta["comments"] = OrderedDict([("detector_name", input_detector_name)])
                ascii.write(input_table, self.options.tm_save_file, format="ecsv", overwrite=True)
                print("Wrote converted Treasure Map pointings to `%s`" % self.options.tm_save_file)

            if len(input_table) == 0:
                print("No usable Treasure Map pointings. Exiting...")
                return 0
        else:
            input_table = ascii.read(tile_path, delimiter=' ')
            input_detector_name = self.options.detector_name or input_table.meta["comments"]["detector_name"]

        rc, _ = load_tables(self.options.gw_id, self.options.healpix_file, formatted_tile_dir,
                            {input_detector_name: input_table})
        return rc


if __name__ == "__main__":
    useagestring = """python load_observed_tiles.py [options]

From a tile file:
python load_observed_tiles.py --gw_id <gwid> --healpix_file <filename> --tile_file <tile file>

From the Treasure Map API (token from the TM_API_TOKEN env var):
python load_observed_tiles.py --gw_id <gwid> --healpix_file <filename> --tm_instrument ZTF \\
    [--detector_name <Teglon detector name>] [--tm_band r] [--tm_status completed]
"""

    start = time.time()

    teglon = Teglon()
    parser = teglon.add_options(usage=useagestring)
    options, args = parser.parse_args()
    teglon.options = options

    rc = teglon.main()

    end = time.time()
    duration = (end - start)
    print("\n********* start DEBUG ***********")
    print("Teglon `LoadObservedTiles` execution time: %s" % duration)
    print("********* end DEBUG ***********\n")
    sys.exit(rc or 0)
