# AGENTS Guide for `tests/`

## Scope
Repository test suites.

## Layout
- `api/`: Jest/Supertest coverage for Node services.
- `core/`: module-level Python tests.
- `integration/`: cross-module pipeline tests.
- `unit/`: focused regression and algorithm tests.
- `util/`: shared helper tests.

## Agent guidance
- Use targeted tests for local changes first, then broader suites as needed.
- Run Python tests in the `EdgeWARN` conda environment.
- Keep test data small and deterministic.
