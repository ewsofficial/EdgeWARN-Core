# Sanitized weather fixtures

These fixtures contain synthetic values on realistic product boundaries. They
are deterministic, offline, and deliberately tiny.

- `mrms_reflectivity.json` and `mrms_precipitation_type.json` describe regular
  MRMS-like grids; tests materialize them as NetCDF without changing their
  coordinates or values.
- `probsevere.json` is a GeoJSON feature with representative identifier,
  probability, thermodynamic, reflectivity, lightning, and wind fields.
- `glm.json` describes point flashes materialized as a minimal NetCDF product.
- `rap.grib2.b64` is a base64 transport form of a 2×2, north-to-south GRIB2
  fixture. It contains U/V wind aliases at 850, 700, 500, and 250 hPa plus 10 m
  wind, 2 m temperature/dewpoint, and freezing-level height. Tests decode it to
  bytes before exercising ecCodes.

None of the values are observations or suitable for meteorological decisions.
