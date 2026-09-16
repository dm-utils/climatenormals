# Climate Normals

Long-term weather forecasting based on historical climate normals: for a given
location and date, blends three lookback windows (long-term/30y/10y, weighted
40/30/30 by default) into one forecast — instead of the usual 10-14 day
forecast horizon.

Live: **https://climatenormals.vercel.app** (API) · website page at
[datamodder.com/utils/climate-forecast](https://datamodder.com/utils/climate-forecast).

Data sources:

- **Long window**: [NOAA GHCN-Daily](https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily)
  station records (≥80 years). 336 regions across 30 countries were identified
  this way — see the region list and per-country granularity notes in the
  project history; `dev/regions.json` has the final per-region station
  assignments (one representative long-record station per admin-1 region,
  found via reverse-geocoding every qualifying station against Nominatim/OSM).
- **Recent windows (30y/10y)**: [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)
  (ERA5 reanalysis), one 30-year `temperature_2m_max` fetch per region.

## Architecture

Precomputed, not on-demand: `dev/build_normals.py` fetches each region's long
station history once (NOAA) and one 30-year Open-Meteo series once, then
computes **all 366 days of the year** from that same in-memory data (the
network fetch is the expensive/rate-limited part; once you have the data,
computing every day instead of just one is nearly free). Results are cached
in `normals.json`, committed to this repo and bundled with the Vercel
deployment — `api/normal.py` just does a nearest-region lookup (haversine
against every known region's coordinates, not only the ones already
computed — see the fix in that file's history) and a dict lookup, no live
external calls on the request path at all.

**Why not fetch live per request?** Considered and rejected: Open-Meteo's
free tier rate-limited us hard (a single 30-year/1-location request appears
to cost far more than "1 call" for their quota) when the whole dataset was
first built in one sitting. Precomputing once and serving from a static
bundle avoids that entirely on the serving path.

**Building the dataset incrementally**: `dev/build_normals.py` is resumable
(skips regions already in `normals.json`) and checkpoints every 5 regions
within a run, so a crash or rate-limit hit never loses more than a few
regions of progress. `--max N` caps how many *new* regions a single run
processes. `dev/run_next_batch.ps1` is a Windows Task Scheduler entry point
that runs one batch (default 8 regions) and commits+pushes the result —
scheduled hourly, spreading the full build over roughly a day and a half
instead of hitting the rate limit in one burst.

Check progress any time: `GET /api/status` (or `dev/batch_log.txt` locally).

## API

- `GET /api/normal?lat=&lon=&date=` or `?address=&date=` — nearest-region
  climate normal for that date. Returns `200` with the blended forecast, or
  `202` with the correctly-identified region if that region hasn't been
  computed yet (never silently substitutes a wrong region).
- `GET /api/status` — build progress, overall and per country.

## License

MIT — see [LICENSE](LICENSE).
