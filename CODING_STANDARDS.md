# Coding standards

Apply every rule below when reviewing.

A note next to code is a comment when deleting that code would make the note meaningless. A rule an agent could break in `gui.py` without opening the implementation file belongs here. Write both when the rule is project-wide and easy to miss at one site.

## Cancel

Cancel by calling `sleep_cooperative` and letting `DownloadCancelled` propagate. Check the event before starting a novel or submitting pool work. Check the event before each sequential episode. GUI cancel sets `cancel_event` only; the worker honors it through `sleep_cooperative`. A completed pool future is stored before cancel is honored.

`failed` counts skip and hard-failure statuses. `cancelled` is a separate flag and result status.

## Chapter results

Chapter failures stay error dicts. Cancel raises.
Ad-gated (ticket 0008) and premium (ticket 0009) denials are chapter error dicts, not NovelSkipError. A content 403 remints `_t` via episode_ticket; do not refresh login_at for that status.
`/v1/novel` omits `login-at` so plus sessions keep real free/ad/premium counts. Auth recovery refreshes cookies but adds `login-at` only when the original request sent that header.

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
After a successful TXT write, remove leftover `N_*.txt` files that were not just written.

Config, metadata, cache records, and the final EPUB go through the atomic writers. `EpubBuilder` writes a sibling `.tmp` then `os.replace`, because ebooklib emits a binary zip. A failed build leaves the previous artifact in place and does not mark the book updated. `metadata.json` is the update-mode commit marker, so write it last.

EPUB update mode keeps the stored chapter set when `max_chapters` is lower than the local count. A rebuild refetches the full episode list.

Sanitize remote titles and episode names before they become path components.

## Reading list

Reading progress is `calibre_bookmarks.txt` in the book folder. `idx` is the chapter number on screen and the line hint in `chapters.jsonl`. `episode_no` is the marked viewer chapter, so an earlier insert can move `idx` without losing the place. The reading label is one string per folder name in `reading_labels.json` at the library root. `metadata.json` `status` stays the publication status. `build_metadata` leaves the sidecar and the label file alone. The library folder scan stays a directory listing. The library list is one table. Changing page refills that table and does not build a widget per book. Cached rows draw immediately. EPUB and chapter reads for the visible page run on the library worker. An empty chapter box does not remove the sidecar. Calibre's in-book bookmark, `META-INF/calibre_bookmarks.txt` inside the EPUB, may update the sidecar. The book does not have to be in a Calibre library. A newer in-book last-read position replaces the sidecar. A newer sidecar is left as it was set. Rewriting the EPUB keeps that bookmark member, and a failed copy still publishes the new EPUB. `.embed_progress.json` in the output directory remembers checked in-book positions so an unchanged EPUB is not opened again. It is not the reading position.

## Book identity

EPUBs store their packaged episode order in `META-INF/novelpia-chapters.json`. Rebuilds use it to remap embedded Calibre last-read positions and named bookmarks. Keep the local CFI for unchanged chapter bytes. Edited chapters restart at the chapter beginning. A removed chapter falls forward to the next surviving old chapter, or backward if none follows. Older EPUBs use `chapters.jsonl` only when its titles match the packaged chapters. Unreadable or unsupported bookmark data is preserved as-is.

A local book is identified by novel ID. Write `.novel_id` before cache or EPUB work so a cancelled run still owns the folder. Title changes keep the existing folder. Reuse a folder only when it already belongs to that novel ID. When the novel endpoint says the novel does not exist (HTTP 404 or 500 with that body) and a local folder already claims that ID, write `.retired`, delete `.novel_id`, and leave the files. `book_directory_novel_id` ignores a retired folder, including a queue index entry that still names it. A later download of that ID starts a new folder. A 404 from another request, a missing episode list, or missing metadata does not release the ID.

## Images

`IMAGE_HOST_COOKIE_POLICY` is the image allowlist. A new host needs a `signed` or `session` policy. Signed hosts get a case in `test_cdn_image_fetch_sends_only_signed_cloudfront_cookies`. A 403 on a signed host refreshes CloudFront cookies and retries the GET. Cover that success path when changing `fetch_image`.

`IMAGE_HOST_ALIASES` rewrites dead or CloudFront-wrong hosts onto a working host in `normalize_url` before the allowlist and cookie policy. The target host's policy applies. Cover a fetch GET to the canonical URL when changing aliases.

Image GETs use a dedicated session that does not hold USERKEY/TKEY. Signed hosts receive only CloudFront cookies; session hosts receive only USERKEY/TKEY. Tests must fail if the request session's cookie jar would attach API auth cookies.

Fetch and embed through `fetch_image`. Re-check redirect targets against the allowlist. Normalize remote URLs before fetch or embed. Detect image type from content.

Image reuse is `.raw_cache/image_index.json` plus bytes in the EPUB.

## Untrusted input

Escape remote and user-provided text before it enters generated HTML. Validate API payload shape before nested indexing. A bad row in a list endpoint is skipped. A bad auth body raises. `my_library` returns liked IDs of any locale. `recent_novels` is Korean K-Premium only.

## Constraints

API stays in `api.py`, queue in `engine.py`, parse in `novel.py`, output in `builder.py` / `epub.py`. Engine calls `build_epub` and `build_txt`. Those two own skip and chapter fetch. `main.py` has no business logic. Preserve the engine callback and cancellation interfaces. Shared options stay equivalent between CLI and GUI. CLI `local` is GUI Check & Update All: scan `--out` for novel IDs and enable update mode. Account status is logged once after client auth; `fetch_novel_and_episodes` does not call `/v1/login/me`. Email and password are both provided or neither. Option changes update `README.md`. Library modules use `logging.getLogger("pia_scrap")`. Tk widgets update on the UI thread; network and file work stay on the worker. A successful `login()` or `refresh()` writes `login_at`, `userkey`, and `tkey` to `.api.json`.

## Tests

Tests assert return values, written files, raised exceptions, cookies actually sent. A test that only snapshots mock kwargs, stub call counts, or private helper booleans is not coverage.

No live Novelpia calls. Use `tests/conftest.py` fixtures. `FakeSession` lives in `tests/test_api.py` and `tests/test_epub_security.py`.

Install test deps from `requirements-dev.txt`. After test edits, `python -m pytest --collect-only -q`, then `python -m pytest`.

If a cache or API contract changes, say so and test against a disposable output directory.

## Secrets

Before the first `git add` of previously untracked `docs/` or `tests/`, and before adding a recaptured dump, scan those files for live values. `_t`, CloudFront `signed_key` fields, `LOGINKEY`, `USERKEY`, `login-at`, and `TKEY` use the same placeholders as `tests/fixtures/novelpia_api_samples.json` (`fixture-token`, `fixture-policy`, `fixture-key-id`, `fixture-signature`) and the Korean spec cookie table (`example-loginkey`, `example-userkey`).

Episode cache records omit `signed_key`.

`.api.json` stays untracked even when empty. It is the live token store.

Chapter HTML dumps (`episodecontent*.json`) are gitignored. Do not force-add them.
