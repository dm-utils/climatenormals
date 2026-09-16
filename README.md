# Climate Normals

Long-term weather forecasting based on historical climate normals: for a given
location and date, blends three lookback windows (e.g. ~100 years, 30 years, 10
years) into one weighted forecast — instead of the usual 10-14 day forecast horizon.

Data sources:

- **Long window**: [NOAA GHCN-Daily](https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily)
  station records (≥80 years, identified per region).
- **Recent windows (30y/10y)**: [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)
  (ERA5 reanalysis, uniform global coverage from 1940).

See [DESIGN.md](DESIGN.md) for the full architecture (region boundaries, cache
format, API, and the website/Home Assistant integrations that will consume it).

Status: early development — validating the pipeline against a Netherlands +
Germany pilot before scaling to the full country list.

## License

MIT — see [LICENSE](LICENSE).
