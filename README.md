# Teglon-O4

**Pixel-based gravitational-wave electromagnetic follow-up planner**, optimized for
the LIGO/Virgo/KAGRA O4 run.

📖 **[Documentation](docs/user_guide_v2.md)** · **[First-time install](docs/first_time_install.md)**

Given a gravitational-wave sky-localization (a LIGO HEALPix probability map), Teglon
re-weights the 2D sky probability by a galaxy catalog (GLADE) to produce a physically informed
re-weighted skymap, then tiles it with each telescope's field of view to output **ranked
telescope pointings** — answering *where should we look first to find the
electromagnetic counterpart?* 

![](teglon_pipeline_version2.png)



> This is an updated version with a unified `teglon` command-line interface, a Docker
> wrapper, environment-based configuration. **All original commands still work** — the
> new commands are shorter equivalents.

---

## Main functionalities

Everything is exposed through one command, `teglon` (via `./teglon` with Docker, or
the `teglon` console script when pip-installed):

| Command | What it does |
| --- | --- |
| `teglon run <GWID>` | Full pipeline in one shot: load map → extract tiles → plot |
| `teglon trigger <GWID>` | **GW ID → galaxy-reweighted skymap** (ingest + reweight + export FITS) |
| `teglon load-map <GWID>` | Download + ingest a sky map (GraceDB, with **GWOSC fallback**) |
| `teglon extract <GWID>` | Rank observing tiles per telescope |
| `teglon plot <GWID>` | All-sky observation-plan plot |
| `teglon compare <GWID>` | Side-by-side *original vs galaxy-reweighted* skymap PDF + credible areas |
| `teglon load-obs <GWID> --tile-file F` | Ingest executed pointings |
| `teglon efficiency <GWID> --model-type kne` | Transient detection-efficiency modeling |
| `teglon add-telescope --tm-detector-id N` | Register a new telescope (Treasure Map) |
| `teglon delete-event <GWID>` | Clean an event from the DB + files (dry-run by default) |
| `teglon setup --run` | One-time database build (GLADE + initialize + pickles) |
| `teglon up` / `down` / `dbshell` | Manage the bundled database |

Highlights of this version:

- **Unified CLI + Docker wrapper** — no more `docker compose run … python ./web/src/…`
  per stage (the old form still works).
- **No port-forwarding** — the DB connection resolves from environment variables, so the
  CLI talks to the database container directly (or any host with `DATABASE_HOST/PORT`).
- **GWOSC fallback** — events GraceDB doesn't serve (e.g. **GW170817**) are looked up on
  the GWOSC event API for the GPS time (and a skymap, where the catalog publishes one).
- **Physically-correct comparison** — original vs Teglon-updated maps are normalized over
  the same pixel set for an apples-to-apples credible-region comparison.
- **Pip-installable** (`pyproject.toml`) and Docker, with a 34-test unit suite.

---

## Quickstart (Docker)

```bash
git clone https://github.com/phelipedarc/teglon_O4_updated.git
cd teglon_O4_updated
git checkout darcTeglon                 # the updated branch (don't skip this)

cp docker/.env.example docker/.env      # set VOL_APP, VOL_DB, VOL_DUSTMAPS, DB_PWD
cp Settings.example.ini Settings.ini    # set your Treasure Map API token

./teglon up                             # start the bundled MySQL database

# First-time DB build has two prerequisites BEFORE `setup --run` (see Installation):
#   1. place the GLADE catalog at web/src/utilities/galaxy_catalog_files/GLADE_2.4.dat
#      (download from http://glade.elte.hu/)
#   2. fetch the dust maps:
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  run --rm --no-deps --entrypoint python teglon_cli web/src/utilities/initialize_dust.py
./teglon setup --run                    # then build the DB: GLADE + initialize + pickles (~45–63 min)

./teglon trigger S240413p               # GW ID → re-weighted skymap (~2–4 min)
./teglon extract S240413p               # ranked tiles per telescope
./teglon compare S240413p               # side-by-side 2D-vs-4D PDF
```

Outputs land in `web/events/S240413p/`. `./teglon --help` lists everything. Full
step-by-step first-time install: [docs/first_time_install.md](docs/first_time_install.md).

## Installation

Two supported paths (full detail in [docs/first_time_install.md](docs/first_time_install.md)):

- **Docker (recommended).** Bundles MySQL + the scientific Python stack
  (`healpy`, `ligo.skymap`, `dustmaps`, …). The CLI runs inside the Docker network.
- **pip.** `pip install -e .` gives the `teglon` command on the host; point it at a
  database with `DATABASE_HOST`/`DATABASE_PORT` env vars. Extras: `.[web]` (Flask/Celery),
  `.[legacy]` (basemap).

**First-time database build (once, ~45–63 min depending on host I/O):**

```bash
# after configuring docker/.env + Settings.ini:
# 1. place the GLADE catalog at web/src/utilities/galaxy_catalog_files/GLADE_2.4.dat
# 2. fetch dust maps:
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  run --rm --no-deps --entrypoint python teglon_cli web/src/utilities/initialize_dust.py
# 3. build the DB (GLADE upload → initialize_teglon → pickles):
./teglon up
./teglon setup --run
```

## Documentation

Full docs (mkdocs): start with the **[User Guide v2](docs/user_guide_v2.md)**.

- [First-time install (from scratch)](docs/first_time_install.md)
- [CLI reference](docs/cli.md)
- [Configuration](docs/configuration.md) — env vars, no port-forwarding
- [Database](docs/database.md)
- [Testing & validation](docs/testing.md)
- [Benchmark report](docs/benchmark_report.md)

## Performance (measured)

Full from-scratch install (clone the fork → ready-to-run) ≈ **45–63 min** one-time
(I/O-load dependent). Then, per event:

| Event | GW ID → reweighted skymap (`trigger`) | full plan (trigger+extract+plot+compare) |
| --- | --- | --- |
| GW190425 (S190425z) | 178 s | ~234 s |
| GW190814 (S190814bv) | 139 s | ~191 s |
| S240413p | 120 s | ~163 s |
| S231206cc | 257 s | ~305 s |
| GW170817 (MCMC map + GWOSC) | 142 s | ~187 s |

Benchmarked on a fresh from-scratch build of the fork. GW170817 used a local
`MCMC_TF2_LowSpin_AllSky.fits` with the GWOSC fallback supplying the event time —
its galaxy-reweighting shrinks the 90% region **44×** (16.2 → 0.37 deg², where
16.2 deg² is the published full-resolution area). See
[the benchmark report](docs/benchmark_report.md) for the full breakdown.

## License

See [LICENSE](LICENSE).
