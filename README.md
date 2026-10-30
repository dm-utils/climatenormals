# Climate Normals

Long-term weather forecasting based on historical climate normals: for a given
location and date, blends three lookback windows (long-term/30y/10y, weighted
40/30/30 by default) into one forecast — instead of the usual 10-14 day
forecast horizon.

The data behind [typicalweather.com](https://typicalweather.com).

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
deployment — `api/normal.py` finds the nearest reference stations (haversine
against every known region's coordinates, not only the ones already
computed — see the fix in that file's history) and averages up to three of
them within 300 km, nearer ones counting more (1/distance²); no live
external calls on the request path for the normals themselves.

Why average: tested leave-one-out on the stations themselves, the weighted
average of the three nearest is off by 1.5 °C on average against 1.8 °C for
the nearest alone, and for a place between two stations by 1.0 °C against
1.8 °C.

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

## Day details

`details/<region_id>.json`, built by `dev/build_details.py` from the station
records alone (no Open-Meteo requests; run it again at any time): for every
day of the year and each period the average daytime high and night low and
the range of highs on 8 days in 10, the warmest day and coldest night on
record, the warming trend since 1950 (°C per decade) and averages per decade.

## API

- `GET /api/normal?lat=&lon=&date=` or `?address=&date=` — the climate normal
  for that place and date, averaged over the nearest stations (listed in
  `stations`, with their weights). Returns `200`, or `202` with the
  correctly-identified nearest region if that region hasn't been computed
  yet (never silently substitutes a wrong region).
- `GET /api/status` — build progress, overall and per country.

The API is not public: with `API_KEYS` set (comma-separated), calls need one
of them in an `X-Api-Key` header. A site calling from its server passes its
visitor's address as `X-Client-IP`, so the rate limit stays per visitor.

## License

MIT — see [LICENSE](LICENSE).
