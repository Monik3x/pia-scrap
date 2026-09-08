# Coding standards

Apply every rule below when reviewing.

A note next to code is a comment when deleting that code would make the note meaningless. A rule an agent could break in `gui.py` without opening the implementation file belongs here. Write both when the rule is project-wide and easy to miss at one site.

## Cancel

Cancel by calling `sleep_cooperative` and letting `DownloadCancelled` propagate. Check the event before starting a novel or submitting pool work.

`failed` counts skip and hard-failure statuses. `cancelled` is a separate flag and result status.

## Chapter results

Chapter failures stay error dicts. Cancel raises.

Expected novel skips raise `NovelSkipError` or a subclass. Other exceptions are hard failures with a traceback.

## HTML

Sanitize episode HTML once in `builder._load_or_fetch_episodes` at fetch-complete. Write that sanitized HTML into `.raw_cache` in update mode. Cache hits are used as-is. `api.py` returns raw joined HTML. `EpubBuilder` does not sanitize again.

## Recap

Recap is `logger.info`, not `update_status`. The GUI status label stays one line.

`issues`, `run_issues`, `report_path`, and `cancelled` are omitted when empty or false.

`[warn]` on `update_status` is a GUI tag. `update_status` logs that same tagged string at INFO. Novel skip and 404 recap lines come from the result dict `error` field. `logger.warning` feeds `_IssueCollector` for intra-novel messages.

Tokens and cookies stay out of `last_run_report.txt`.

## Output

TXT always rewrites chapter files. Only EPUB returns `None` as up to date.

Config, metadata, cache records, and the final EPUB go through the atomic writers. A failed build leaves the previous artifact in place and does not mark the book updated. `metadata.json` is the update-mode commit marker, so write it last.

EPUB update mode keeps the stored chapter set when `max_chapters` is lower than the local count. A rebuild refetches the full episode list.

Sanitize remote titles and episode names before they become path components.

## Book identity

A local book is identified by novel ID. Write `.novel_id` before cache or EPUB work so a cancelled run still owns the folder. Title changes keep the existing folder. Reuse a folder only when it already belongs to that novel ID.

## Images

`IMAGE_HOST_COOKIE_POLICY` is the image allowlist. A new host needs a `signed` or `session` policy. Signed hosts get a case in `test_cdn_image_fetch_sends_only_signed_cloudfront_cookies`.

Fetch and embed through `fetch_image`. Re-check redirect targets against the allowlist. Normalize remote URLs before fetch or embed. Detect image type from content.

## Untrusted input

Escape remote and user-provided text before it enters generated HTML. Validate API payload shape before nested indexing.

## Constraints

API stays in `api.py`, queue in `engine.py`, parse in `novel.py`, output in `builder.py` / `epub.py`. `main.py` has no business logic. Preserve the engine callback and cancellation interfaces. Shared options stay equivalent between CLI and GUI. Email and password are both provided or neither. Option changes update `README.md`. Library modules use `logging.getLogger("pia_scrap")`. Tk widgets update on the UI thread; network and file work stay on the worker.

## Tests

Tests assert return values, written files, raised exceptions, cookies actually sent. A test that only snapshots mock kwargs or private helper booleans is not coverage.

No live Novelpia calls. Use `tests/conftest.py` fixtures. `FakeSession` lives in `tests/test_api.py` and `tests/test_epub_security.py`.

After test edits, `python -m pytest --collect-only -q`, then `python -m pytest`.

If a cache or API contract changes, say so and test against a disposable output directory.

## Secrets

Before the first `git add` of previously untracked `docs/` or `tests/`, and before adding a recaptured dump, scan those files for live values. `_t`, CloudFront `signed_key` fields, `LOGINKEY`, `USERKEY`, `login-at`, and `TKEY` use the same placeholders as `tests/fixtures/novelpia_api_samples.json` (`fixture-token`, `fixture-policy`, `fixture-key-id`, `fixture-signature`) and the Korean spec cookie table (`example-loginkey`, `example-userkey`).

Episode cache records omit `signed_key`.

`.api.json` stays untracked even when empty. It is the live token store.

Chapter HTML dumps (`episodecontent*.json`) are gitignored. Do not force-add them.
