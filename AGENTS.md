# AGENTS.md

`pia-scrap` is a personal Python 3.9+ Novelpia downloader. `main.py` (CLI) and
`gui.py` (CustomTkinter) share `ScraperEngine`. Auth, then ID resolve, then
metadata/episodes, then throttled fetch, then EPUB or TXT.

Sequential and throttled by default. Leave throttle alone unless the change was
tested against rate limits.

`.env`, `.api.json`, and `output/` stay local. `.env.example` holds placeholders
only. Tokens do not appear in source, logs, fixtures, issues, or agent docs.

## Map

- `main.py` / `gui.py`: entry points
- `src/engine.py`: orchestration, callbacks, cancellation, queue recap, `last_run_report.txt`
- `src/api.py`: HTTP, auth, throttle, retries
- `src/novel.py`: HTML and metadata parse
- `src/builder.py` / `src/epub.py`: output and cache
- `src/helper.py` / `src/const.py`: shared IO and constants
- `output/`: generated books and caches
- `docs/`: captured API responses and OpenAPI specs

## Pointers

- `CODING_STANDARDS.md`: review; write comments; change CLI/GUI options; touch
  auth, cache, book identity, cancel, recap, image hosts, or API contracts;
  add tests; first `git add` of `docs/` or `tests/`
- `tests/conftest.py`, `tests/test_api.py`: add tests. `FakeSession` lives in
  `test_api.py` and `test_epub_security.py`, not conftest.
- `docs/novelpia-api.openapi.yaml`: change request or response shapes in
  `src/api.py`
- `docs/sample_api_responses/`: compare against captured payloads
- `docs/krnovelpia_engtranslation_openapi.yaml`: translated Korean-site spec;
  secondary, may differ from this fork
- `tests/fixtures/novelpia_api_samples.json`: pytest captured-shape fixtures

## Tests

`pip install -r requirements-dev.txt`, then `python -m pytest`.
`PermissionError` from `pytest-of-*`, `.pytest-tmp`, or `--basetemp` is a
Windows sandbox failure (`WinError 5` on `tmp_path`), not a project test
failure.
