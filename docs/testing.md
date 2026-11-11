# Testing & validation

Teglon ships unit tests for the CLI and the Treasure Map pointing pipeline, a
reproducible 3-event integration test, and a pointings integration test.

## Unit tests

Stdlib `unittest`; no database or network needed (`query_db`, the Treasure Map API
and the observed-tile loader are replaced with in-memory fakes where needed).

| File | Covers |
|------|--------|
| `tests/test_new_functions.py` | CLI helpers + wiring: `delete-event`/`setup` default to a dry-run, detector geometry validation, 4D map reconstruction, credible areas, automatic static-grid prefixes |
| `tests/test_tm_pipeline.py` | Treasure Map pointings: band mapping, `get_TM_pointings` sync (one query/load per event, re-runs load nothing, planned pointings replaced/superseded/cleared, token never in errors), `GetTreasureMapData` step order, TM pointing → tile conversion, `load-obs`/`extract`/`clear-planned`/`list-telescopes` wiring |

```bash
# inside the Docker image (production runtime; nothing is skipped)
./teglon test                                   # = unittest discover -s tests -v
./teglon test -k TestSyncPointings              # extra args go to unittest

# host (no MySQL drivers or healpy needed; the 4 TM-conversion tests that need
# healpy are skipped)
PYTHONPATH="$PWD" python -m unittest discover -s tests -v
```

`./teglon test` runs in the `teglon_cli` service, which depends on `gw_db`, so
Compose starts the database if it isn't already running (the tests don't use it).
Without the wrapper, the equivalent is:

```bash
docker compose -p teglon --env-file docker/.env -f docker/docker-compose.yml \
  run --rm -T --entrypoint python teglon_cli -m unittest discover -s tests -v
```

Use the same `-p` as `COMPOSE_PROJECT_NAME` in `docker/.env` (what `./teglon`
uses); a different project name starts a second `gw_db` that collides on the DB port.

New test files are picked up automatically if they're named `tests/test*.py`.

## 3-event integration test (observational mode)

A canonical end-to-end check using three landmark events:

| Event | GraceDB id | Notes |
|-------|-----------|-------|
| GW170817 | `GW170817` (event G298048) | Not a superevent → local skymap; **GWOSC fallback** supplies the event time automatically |
| GW190425 | `S190425z` | Public superevent (downloaded from GraceDB) |
| GW190814 | `S190814bv` | Public superevent (downloaded from GraceDB) |

For each event the test runs the observational pipeline and the comparison:

```bash
# GW170817: place a FITS at web/events/GW170817/bayestar.fits.gz first, then
# (GraceDB 404 -> Teglon falls back to the GWOSC event API for the GPS time):
./teglon trigger GW170817
./teglon extract GW170817
./teglon compare GW170817        # side-by-side 2D vs 4D PDF + area metrics

# GW190425 / GW190814 (downloaded automatically):
./teglon trigger S190425z && ./teglon extract S190425z && ./teglon compare S190425z
./teglon trigger S190814bv && ./teglon extract S190814bv && ./teglon compare S190814bv
```

The `compare` step prints the 50%/90% credible-region areas for the original
LIGO map (2D) and the Teglon galaxy-reweighted map (4D), how much the localization
shrank, and writes `<GWID>_2D_vs_4D_comparison.pdf` to the event directory.

## Pointings integration test (Treasure Map)

Checks that suggested tiles skip sky other groups have already imaged. Use an O4
event with Treasure Map pointings (e.g. `S250206dm`) and at least one telescope
that has both a Treasure Map id and a static grid:

```bash
./teglon list-telescopes                        # TM_ID set + STATIC_TILES > 0 for the telescopes to test
./teglon add-telescope --tm-detector-id <id>    # if not: links/adds it and builds its grid
./teglon load-map S250206dm                     # after any new grids, so the map is tiled for them

./teglon sync-pointings S250206dm               # completed pointings
./teglon sync-pointings S250206dm               # re-run: every telescope "up to date"

./teglon extract S250206dm                      # logs "Excluding N already-imaged map pixels (2D prob: ...)"
./teglon plot S250206dm                         # one panel per telescope with a tile file
```

Check in `./teglon dbshell`:

```sql
-- pointings loaded per telescope for the event
SELECT d.Name, ot.Source, COUNT(*) FROM ObservedTile ot
  JOIN Detector d ON d.id = ot.Detector_id JOIN HealpixMap m ON m.id = ot.HealpixMap_id
  WHERE m.GWID = 'S250206dm' GROUP BY 1, 2;
-- map pixels they cover (0 => extract has nothing to exclude)
SELECT COUNT(DISTINCT HealpixPixel_id) FROM ObservedTile_HealpixPixel ot_hp
  JOIN HealpixMap m ON m.id = ot_hp.HealpixMap_id WHERE m.GWID = 'S250206dm';
```

Pass criteria: the second sync loads nothing; `extract` excludes a non-zero number
of pixels; re-running one telescope with `./teglon extract S250206dm --tele <Name> --include-observed`
ranks the already-imaged fields back in (that overwrites its tile file, so re-run
without the flag afterwards).

Planned pointings (optional):

```bash
./teglon sync-pointings S250206dm --status planned
./teglon extract S250206dm --exclude-planned    # writes *_exclplanned.txt alongside the normal files
./teglon clear-planned S250206dm                # removes only the planned rows
```

The whole cron flow for one event (load new event → sync pointings → extract → plot)
is `./teglon tm-ingest --gw-id S250206dm --planned`.

### Running a second instance side-by-side

The integration test above was validated on a separate install at a different
path, MySQL port, and compose project — proving Teglon can run multiple isolated
instances on one host. The keys are three env values in `docker/.env`:

```bash
LOCAL_DB_PORT=53307            # distinct host port
DB_CONTAINER_NAME=teglon_test_db   # distinct container name
COMPOSE_PROJECT_NAME=teglon_test   # distinct compose project (also export it)
```
