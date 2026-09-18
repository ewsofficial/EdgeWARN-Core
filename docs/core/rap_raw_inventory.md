# RAP Raw Inventory

Source file inspected:

`/home/yuchenwei/EdgeWARN-Operational-Data/data/RAP/RAP.20260429-19z.awp130pgrbf00.grib2`

This inventory was derived from the actual RAP GRIB2 file, not from the repository's configured extraction list.

That file is not in this repository and the path above is not reachable from a
normal checkout, so this page is a point-in-time snapshot that cannot be
regenerated or checked during ordinary development. Treat it as a reference for
what RAP *carried on that cycle*, not as a contract. `config/integration.yaml`
`rap_products.products` is the authoritative statement of what EdgeWARN
actually extracts.

> **Known gap.** The listing below is a point-in-time inventory and is not the
> extraction contract. The current configured catalog contains pressure-level
> `u`/`v`, 10 m `u`/`v`, temperature, dewpoint, and freezing-height products;
> it does not depend on `vstm`/`vvcsh`. Missing GRIB messages are skipped by the
> loader and produce partial integration output. The counts below must not be
> used to infer the current configured field set.

## Counts

- Total GRIB messages: `306`
- Unique layer definitions: `302`

Both counts are as-reported by the tool that generated this page and are
understated — see the known gap above.

Unique layer definitions here are grouped when the same raw variable appears across many levels.

## Repeated Pressure-Level Series

- `gh` | Geopotential height | `isobaricInhPa` | levels `1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100` | `instant` | `gpm`
- `t` | Temperature | `isobaricInhPa` | levels `1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100` | `instant` | `K`
- `r` | Relative humidity | `isobaricInhPa` | levels `1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100` | `instant` | `%`
- `w` | Vertical velocity | `isobaricInhPa` | levels `1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100` | `instant` | `Pa s**-1`
- `u` | U component of wind | `isobaricInhPa` | levels `1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100` | `instant` | `m s**-1`
- `absv` | Absolute vorticity | `isobaricInhPa` | level `500` | `instant` | `s**-1`

## Surface And Near-Surface

- `refc` | Maximum/Composite radar reflectivity | `atmosphere` | `0` | `instant` | `dB`
- `vis` | Visibility | `surface` | `0` | `instant` | `m`
- `gust` | Wind speed (gust) | `surface` | `0` | `instant` | `m s**-1`
- `hindex` | Haines Index | `surface` | `0` | `instant` | `Numeric`
- `mslma` | MSLP (MAPS System Reduction) | `meanSea` | `0` | `instant` | `Pa`
- `sp` | Surface pressure | `surface` | `0` | `instant` | `Pa`
- `orog` | Orography | `surface` | `0` | `instant` | `m`
- `t` | Temperature | `surface` | `0` | `instant` | `K`
- `mstav` | Moisture availability | `depthBelowLand` | `0` | `instant` | `%`
- `sdwe` | Water equivalent of accumulated snow depth (deprecated) | `surface` | `0` | `instant` | `kg m**-2`
- `sde` | Snow depth | `surface` | `0` | `instant` | `m`
- `papt` | Pseudo-adiabatic potential temperature | `surface` | `0` | `instant` | `K`
- `prate` | Precipitation rate | `surface` | `0` | `instant` | `kg m**-2 s**-1`
- `tp` | Total precipitation | `surface` | `0` | `accum` | `kg m**-2`
- `acpcp` | Convective precipitation (water) | `surface` | `0` | `accum` | `kg m**-2`
- `sdwe` | Water equivalent of accumulated snow depth (deprecated) | `surface` | `0` | `accum` | `kg m**-2`
- `frzr` | Freezing rain | `surface` | `0` | `accum` | `kg m**-2`
- `ssrun` | Storm surface runoff | `surface` | `0` | `accum` | `kg m**-2`
- `bgrun` | Baseflow-groundwater runoff | `surface` | `0` | `accum` | `kg m**-2`
- `csnow` | Categorical snow | `surface` | `0` | `instant` | `(Code table 4.222)`
- `cicep` | Categorical ice pellets | `surface` | `0` | `instant` | `(Code table 4.222)`
- `cfrzr` | Categorical freezing rain | `surface` | `0` | `instant` | `(Code table 4.222)`
- `crain` | Categorical rain | `surface` | `0` | `instant` | `(Code table 4.222)`
- `cape` | Convective available potential energy | `surface` | `0` | `instant` | `J kg**-1`
- `cin` | Convective inhibition | `surface` | `0` | `instant` | `J kg**-1`
- `ltng` | Lightning | `surface` | `0` | `instant` | `dimensionless`

## Height Above Ground

- `refd` | Derived radar reflectivity | `heightAboveGround` | `1000` | `instant` | `dB`
- `refd` | Derived radar reflectivity | `heightAboveGround` | `4000` | `instant` | `dB`
- `2t` | 2 metre temperature | `heightAboveGround` | `2` | `instant` | `K`
- `pt` | Potential temperature | `heightAboveGround` | `2` | `instant` | `K`
- `2sh` | 2 metre specific humidity | `heightAboveGround` | `2` | `instant` | `kg kg**-1`
- `2d` | 2 metre dewpoint temperature | `heightAboveGround` | `2` | `instant` | `K`
- `2r` | 2 metre relative humidity | `heightAboveGround` | `2` | `instant` | `%`
- `10u` | 10 metre U wind component | `heightAboveGround` | `10` | `instant` | `m s**-1`
- `t` | Temperature | `heightAboveGround` | `80` | `instant` | `K`
- `q` | Specific humidity | `heightAboveGround` | `80` | `instant` | `kg kg**-1`
- `pres` | Pressure | `heightAboveGround` | `80` | `instant` | `Pa`
- `u` | U component of wind | `heightAboveGround` | `80` | `instant` | `m s**-1`

## Special Vertical Levels

- `gh` | Geopotential height | `planetaryBoundaryLayer` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `lowestLevelWetBulb0` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `isothermZero` | `0` | `instant` | `gpm`
- `r` | Relative humidity | `isothermZero` | `0` | `instant` | `%`
- `pres` | Pressure | `isothermZero` | `0` | `instant` | `Pa`
- `gh` | Geopotential height | `highestTroposphericFreezing` | `0` | `instant` | `gpm`
- `r` | Relative humidity | `highestTroposphericFreezing` | `0` | `instant` | `%`
- `pres` | Pressure | `highestTroposphericFreezing` | `0` | `instant` | `Pa`
- `gh` | Geopotential height | `isothermal` | `263` | `instant` | `gpm`
- `gh` | Geopotential height | `isothermal` | `253` | `instant` | `gpm`
- `trpp` | Tropopause pressure | `tropopause` | `0` | `instant` | `Pa`
- `t` | Temperature | `tropopause` | `0` | `instant` | `K`
- `pt` | Potential temperature | `tropopause` | `0` | `instant` | `K`
- `u` | U component of wind | `tropopause` | `0` | `instant` | `m s**-1`
- `pres` | Pressure | `maxWind` | `0` | `instant` | `Pa`
- `u` | U component of wind | `maxWind` | `0` | `instant` | `m s**-1`

## Layer, Cloud, And Diagnostic Products

- `lftx` | Surface lifted index | `isobaricLayer` | `500` | `instant` | `K`
- `pwat` | Precipitable water | `atmosphereSingleLayer` | `0` | `instant` | `kg m**-2`
- `tcc` | Total cloud cover | `boundaryLayerCloudLayer` | `0` | `instant` | `%`
- `lcc` | Low cloud cover | `lowCloudLayer` | `0` | `instant` | `%`
- `mcc` | Medium cloud cover | `middleCloudLayer` | `0` | `instant` | `%`
- `hcc` | High cloud cover | `highCloudLayer` | `0` | `instant` | `%`
- `tcc` | Total cloud cover | `atmosphere` | `0` | `instant` | `%`
- `gh` | Geopotential height | `convectiveCloudTop` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `cloudCeiling` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `cloudBase` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `cloudTop` | `0` | `instant` | `gpm`
- `hlcy` | Storm relative helicity | `heightAboveGroundLayer` | `3000` | `instant` | `m**2 s**-2`
- `hlcy` | Storm relative helicity | `heightAboveGroundLayer` | `1000` | `instant` | `m**2 s**-2`
- `ustm` | U-component storm motion | `heightAboveGroundLayer` | `0` | `instant` | `m s**-1`
- `vucsh` | Vertical u-component shear | `heightAboveGroundLayer` | `0` | `instant` | `s**-1`
- `gh` | Geopotential height | `equilibrium` | `0` | `instant` | `gpm`
- `gh` | Geopotential height | `freeConvection` | `0` | `instant` | `gpm`
- `layth` | Layer thickness | `unknown` | `261` | `instant` | `m`

## Pressure-From-Ground Layers

- `t` | Temperature | `pressureFromGroundLayer` | levels `3000, 6000, 9000, 12000, 15000, 18000` | `instant` | `K`
- `r` | Relative humidity | `pressureFromGroundLayer` | levels `3000, 6000, 9000, 12000, 15000, 18000` | `instant` | `%`
- `u` | U component of wind | `pressureFromGroundLayer` | levels `3000, 6000, 9000, 12000, 15000, 18000` | `instant` | `m s**-1`
- `w` | Vertical velocity | `pressureFromGroundLayer` | levels `3000, 6000, 9000, 12000, 15000, 18000` | `instant` | `Pa s**-1`
- `4lftx` | Best (4-layer) lifted index | `pressureFromGroundLayer` | `18000` | `instant` | `K`
- `cape` | Convective available potential energy | `pressureFromGroundLayer` | levels `9000, 18000, 25500` | `instant` | `J kg**-1`
- `cin` | Convective inhibition | `pressureFromGroundLayer` | levels `9000, 18000, 25500` | `instant` | `J kg**-1`
- `plpl` | Pressure of level from which parcel was lifted | `pressureFromGroundLayer` | `25500` | `instant` | `Pa`

## Other Layered Convective Product

- `cape` | Convective available potential energy | `heightAboveGroundLayer` | `0` | `instant` | `J kg**-1`

## Simulated Satellite Brightness Temperatures

- `SBT123` | Simulated brightness temperature for GOES 12, Channel 3 | `nominalTop` | `0` | `instant` | `K`
- `SBT124` | Simulated brightness temperature for GOES 12, Channel 4 | `nominalTop` | `0` | `instant` | `K`
- `SBT113` | Simulated brightness temperature for GOES 11, Channel 3 | `nominalTop` | `0` | `instant` | `K`
- `SBT114` | Simulated brightness temperature for GOES 11, Channel 4 | `nominalTop` | `0` | `instant` | `K`

## Unknown Or Undecoded Entries

- `unknown` | `atmosphereSingleLayer` | `0` | `instant` | `unknown` | count `2`
- `unknown` | `surface` | `0` | `accum` | `unknown` | count `2`
- `unknown` | `heightAboveGround` | `2` | `instant` | `unknown`
- `unknown` | `heightAboveGround` | `8` | `instant` | `unknown`
- `unknown` | `surface` | `0` | `instant` | `unknown` | count `2`
- `unknown` | `heightAboveGroundLayer` | `0` | `instant` | `unknown` | count `2`
- `unknown` | `atmosphere` | `0` | `instant` | `unknown`

## Shear Note

The raw RAP file includes:

- `hlcy` at `1000 m` and `3000 m`
- `vucsh` at `heightAboveGroundLayer=0`

It does not include a direct standard `0-6 km bulk shear` field in this inspected file.
