# Test data

The test suite never reads `data/`: `tests/conftest.py` points every `data/...` path resolved through
`Settings.path` at a per-run temporary copy of this folder, so importing your own data never changes a test result.

- `events.csv`, `holidays.csv`, `fii_dii.csv`, `nifty50.csv`: frozen copies of the committed files of 18-Nov-2026.
- `external/*.csv`: headers only. The external repo's rows are personal-use only and are never committed.
- `option_download_status.json`: an empty download progress record.
