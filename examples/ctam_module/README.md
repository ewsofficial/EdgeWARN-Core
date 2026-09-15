# Example CTAM module

Copy this directory to the configured external module root as
`ctam_modules/example_stats`; do not place it under `src/`. Validate it with
`--check-ctam-modules` before enabling a service cycle.

The module reads the cycle snapshot through `CTAM_API_URL`, stages one owned
`modules.ExampleStats` patch per cell, then explicitly commits. It receives no
runtime artifact paths and performs no direct writes to EdgeWARN data folders.
It also declares and stages a `summary` public route; that JSON joins the same
transaction and cannot become publishable before the commit.
