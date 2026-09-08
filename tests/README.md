# Test execution lanes

The default `python -m pytest` command remains the complete deterministic,
offline correctness suite. It excludes `tests/benchmarks/` and the Jest API
tree through `pytest.ini`, and every test receives a disposable runtime base
directory from the root fixture.

CI also splits that suite into independently reportable lanes:

- Fast offline correctness: `python -m pytest -m "not integration and not process"`
- Connected integration: `python -m pytest -m "integration and not process"`
- Process, restart, and signal behavior: `python -m pytest -m process`
- Node API contracts: `npm run test:coverage -- --runInBand`
- Installed wheel: build once, then install and test that exact artifact outside
  the checkout

Directory ownership supplies `unit` and `integration` markers during
collection. Process-heavy files are explicitly classified in the root
collection hook. `slow`, `benchmark`, and `network` remain opt-in categories;
live compatibility checks and performance measurements must never enter the
default lane merely because local data or credentials happen to exist.

Python CI publishes JUnit and XML/HTML coverage artifacts. The reviewed fast
offline baseline is 64.69% branch-and-statement coverage (14,559 of 21,412
statements and 3,615 of 6,682 branches); CI starts at a conservative 60%
threshold so ordinary variance cannot silently erase the baseline. Process
coverage is reported separately because child-process tracing is platform- and
start-method-sensitive; the process lane tests the production start method
directly rather than inflating the ordinary coverage number.
