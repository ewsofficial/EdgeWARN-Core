# AGENTS Guide for `src/EWMRS/`

## Scope
EWMRS rendering service package.

## Major areas
- `api/`: Express routes for renders, tiles, and WPC.
- `render/`: raster rendering, reprojection, tiling, and tools.
- `pipeline.py` / `scheduler.py`: orchestration helpers.

## Agent guidance
- Preserve GUI output structure and tile/index compatibility.
- Be careful with filesystem cleanup so it stays constrained to the configured runtime base directory
- ALWAYS keep the dicts in the "threshold" key one line per dict. 
