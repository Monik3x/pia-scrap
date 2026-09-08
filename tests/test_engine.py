import logging
import threading
from types import SimpleNamespace

import pytest

from src import engine
from src.api import DownloadCancelled, NovelUnavailableError
from src.novel import NoEpisodesError, NoMetadataError


def _core_queue_result(result):
    return {
        "success": result["success"],
        "skipped": result["skipped"],
        "failed": result["failed"],
        "results": result["results"],
    }


@pytest.mark.parametrize(
    "kwargs",
    [{"throttle": -1}, {"max_chapters": -1}, {"threads": 0}],
)
def test_engine_validates_numeric_settings(kwargs):
    with pytest.raises(ValueError):
        engine.ScraperEngine(**kwargs)


def test_initialize_client_reuses_stored_auth(monkeypatch):
    created = []
    me_calls = []

    class Client:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.tokens = SimpleNamespace(login_at=None)

        def me(self):
            me_calls.append(True)
            return {"result": {"ok": True}}

    monkeypatch.setattr(engine, "load_config", lambda: {"login_at": "login", "userkey": "user", "tkey": "t"})
    monkeypatch.setattr(engine, "NovelpiaClient", Client)
    scraper = engine.ScraperEngine()

    assert scraper.initialize_client()
    assert scraper.client.tokens.login_at == "login"
    assert created[0]["userkey"] == "user"
    assert len(me_calls) == 1


def test_initialize_client_requires_login_when_stored_auth_fails(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.tokens = SimpleNamespace(login_at=None)

        def me(self):
            raise RuntimeError("token rejected")

    monkeypatch.setattr(engine, "load_config", lambda: {"login_at": "login", "userkey": "user", "tkey": "t"})
    monkeypatch.setattr(engine, "NovelpiaClient", Client)
    scraper = engine.ScraperEngine()

    with pytest.raises(RuntimeError, match="log in again"):
        scraper.initialize_client()


def test_initialize_client_logs_in_and_saves_tokens(monkeypatch):
    saved = []

    class Cookies(dict):
        pass

    class Client:
        def __init__(self, **kwargs):
            self.tokens = SimpleNamespace(login_at=None, tkey="token-fallback")
            self.s = SimpleNamespace(cookies=Cookies(USERKEY="new-user", TKEY="new-token"))

        def login(self):
            self.tokens.login_at = "new-login"

    monkeypatch.setattr(engine, "load_config", lambda: {})
    def save(tokens):
        saved.append(tokens)
        return True

    monkeypatch.setattr(engine, "save_config", save)
    monkeypatch.setattr(engine, "NovelpiaClient", Client)

    scraper = engine.ScraperEngine(email="me@test", password="secret")
    assert scraper.initialize_client()
    assert saved == [{"login_at": "new-login", "userkey": "new-user", "tkey": "new-token"}]


def test_initialize_client_reports_token_persistence_failure(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.tokens = SimpleNamespace(login_at=None, tkey="token", userkey="user")
            self.s = SimpleNamespace(cookies={"USERKEY": "user", "TKEY": "token"})

        def login(self):
            self.tokens.login_at = "new-login"

    statuses = []
    monkeypatch.setattr(engine, "load_config", lambda: {})
    monkeypatch.setattr(engine, "save_config", lambda config: False)
    monkeypatch.setattr(engine, "NovelpiaClient", Client)

    scraper = engine.ScraperEngine(
        email="me@test", password="secret", status_callback=statuses.append
    )

    assert scraper.initialize_client()
    assert statuses[-1] == "Login successful, but tokens could not be stored."


def test_initialize_client_returns_false_without_auth(monkeypatch):
    monkeypatch.setattr(engine, "load_config", lambda: {})
    monkeypatch.delenv("NOVELPIA_EMAIL", raising=False)
    monkeypatch.delenv("NOVELPIA_PASSWORD", raising=False)
    assert not engine.ScraperEngine().initialize_client()


def test_resolve_ids_delegates_named_lists_or_parses_range():
    scraper = engine.ScraperEngine()
    scraper.client = SimpleNamespace(
        my_library=lambda: [9, 10],
        recent_novels=lambda rows: [20, 21] if rows == 30 else [],
    )
    assert scraper.resolve_novel_ids("library") == [9, 10]
    assert scraper.resolve_novel_ids("recent") == [20, 21]
    assert scraper.resolve_novel_ids("LATEST") == [20, 21]
    assert scraper.resolve_novel_ids("1,3-4") == [1, 3, 4]


def test_download_queue_aggregates_success_skip_and_failure(monkeypatch, tmp_path):
    def fake_build(client, novel_id, **kwargs):
        if novel_id == 1:
            return "one.epub", "One", 3
        if novel_id == 2:
            return None, "Two", 4
        raise NoMetadataError(f"Novel {novel_id} returned no metadata.")

    monkeypatch.setattr(engine, "build_epub", fake_build)
    slept = []
    statuses = []
    scraper = engine.ScraperEngine(
        out_dir=str(tmp_path), status_callback=statuses.append
    )
    scraper.client = SimpleNamespace(sleep_cooperative=slept.append)

    result = scraper.run_download_queue([1, 2, 3])

    assert result["success"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert [item["status"] for item in result["results"]] == ["success", "skipped", "not_exist"]
    assert result["results"][2]["error"] == "Novel 3 returned no metadata. Skipping."
    assert slept == [1.0]
    assert statuses.count("[skipped] Novel 'Two' is already up to date (4 chapters). Skipping.") == 1
    assert "[warn] Novel 3 returned no metadata. Skipping." in statuses
    assert any("Finished queue" in status for status in statuses)
    assert "Novel 3 returned no metadata. Skipping." in engine.format_run_recap(result)


@pytest.mark.parametrize(
    ("error", "novel_id", "status"),
    [
        (NovelUnavailableError("Novel 7 is unassigned or unavailable."), 7, "not_exist"),
        (NoEpisodesError("Novel 647 has no downloadable episodes."), 647, "no_data"),
    ],
)
def test_download_queue_logs_novel_skip_without_traceback(
    monkeypatch, tmp_path, caplog, error, novel_id, status
):
    monkeypatch.setattr(
        engine,
        "build_epub",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    statuses = []
    scraper = engine.ScraperEngine(
        out_dir=str(tmp_path), status_callback=statuses.append
    )
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    with caplog.at_level("INFO", logger="pia_scrap"):
        result = scraper.run_download_queue([novel_id])

    skip_text = f"{error} Skipping."
    assert _core_queue_result(result) == {
        "success": 0,
        "skipped": 0,
        "failed": 1,
        "results": [{"novel_id": novel_id, "status": status, "error": skip_text}],
    }
    assert skip_text in engine.format_run_recap(result)
    assert result.get("report_path")
    assert f"[warn] {skip_text}" in statuses
    assert any("Failed/No Data: 1" in item for item in statuses)
    assert any(
        rec.levelno == logging.INFO and rec.getMessage() == f"[warn] {skip_text}"
        for rec in caplog.records
    )
    assert "Traceback" not in caplog.text
    assert "=== Run recap ===" in caplog.text


def test_plain_value_error_is_hard_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        engine,
        "build_epub",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("Novel 3 returned no metadata.")
        ),
    )
    statuses = []
    scraper = engine.ScraperEngine(
        out_dir=str(tmp_path), status_callback=statuses.append
    )
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    with caplog.at_level("ERROR", logger="pia_scrap"):
        result = scraper.run_download_queue([3])

    assert result["results"] == [
        {"novel_id": 3, "status": "failed", "error": "Novel 3 returned no metadata."}
    ]
    assert any("Failed processing 3" in status for status in statuses)
    assert "Traceback" in caplog.text


def test_download_queue_passes_update_mode_to_txt(monkeypatch, tmp_path):
    received = []
    book_index = {1: "existing-book"}

    def fake_build_txt(client, novel_id, **kwargs):
        received.append(kwargs)
        return "out", "Book", 1

    monkeypatch.setattr(engine, "build_txt", fake_build_txt)
    monkeypatch.setattr(
        engine, "build_epub", lambda *args, **kwargs: pytest.fail("epub builder called")
    )
    monkeypatch.setattr(
        engine, "build_book_directory_index", lambda out_dir: book_index
    )
    scraper = engine.ScraperEngine(
        out_dir=str(tmp_path), txt_mode=True, update_mode=True
    )
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    result = scraper.run_download_queue([1])

    assert result["success"] == 1
    assert scraper.update_mode is True
    assert received[0]["update_mode"] is True
    assert received[0]["book_index"] is book_index
    assert received[0]["progress_cb"] == scraper.update_progress


def test_download_queue_stops_before_work_when_cancelled(monkeypatch, tmp_path):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(engine, "build_epub", lambda *args, **kwargs: pytest.fail("builder called"))
    scraper = engine.ScraperEngine(out_dir=str(tmp_path), cancel_event=event)
    scraper.client = SimpleNamespace()

    result = scraper.run_download_queue([1])

    assert _core_queue_result(result) == {
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "results": [],
    }
    assert result.get("cancelled") is True
    assert result.get("report_path")
    recap = engine.format_run_recap(result)
    assert "Download queue was interrupted." in recap
    assert "No failures or warnings." not in recap


def test_download_queue_keeps_summary_when_cancelled_during_failure_backoff(
    monkeypatch, tmp_path
):
    built = []

    def fake_build(client, novel_id, **kwargs):
        built.append(novel_id)
        raise RuntimeError("first novel failed")

    monkeypatch.setattr(engine, "build_epub", fake_build)
    statuses = []
    scraper = engine.ScraperEngine(
        out_dir=str(tmp_path), status_callback=statuses.append
    )
    scraper.client = SimpleNamespace(
        sleep_cooperative=lambda seconds: (_ for _ in ()).throw(
            DownloadCancelled("Request cancelled during wait.")
        )
    )

    result = scraper.run_download_queue([1, 2])

    assert built == [1]
    assert _core_queue_result(result) == {
        "success": 0,
        "skipped": 0,
        "failed": 1,
        "results": [{"novel_id": 1, "status": "failed", "error": "first novel failed"}],
    }
    assert result.get("cancelled") is True
    assert "[cancelled] Download queue cancelled by user." in statuses
    assert any("Finished queue" in status for status in statuses)


def test_status_and_progress_callbacks_are_isolated_from_callback_errors(caplog):
    scraper = engine.ScraperEngine(
        status_callback=lambda message: (_ for _ in ()).throw(RuntimeError("boom")),
        progress_callback=lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with caplog.at_level("ERROR", logger="pia_scrap"):
        scraper.update_status("still safe")
        scraper.update_progress(1, 2, "safe")
    assert "Error in status callback" in caplog.text
    assert "Error in progress callback" in caplog.text


def test_format_run_recap_lists_failures_and_intra_novel_warnings():
    summary = {
        "success": 1,
        "skipped": 1,
        "failed": 2,
        "run_issues": ["Multiple local book directories claim novel ID 9; using 'a'."],
        "results": [
            {
                "novel_id": 1,
                "status": "success",
                "title": "One",
                "issues": ["Image error (404): https://example/img.png"],
            },
            {"novel_id": 2, "status": "skipped", "title": "Two"},
            {"novel_id": 3, "status": "not_exist"},
            {
                "novel_id": 4,
                "status": "failed",
                "error": "boom",
                "issues": ["Ignoring invalid episode cache x"],
            },
        ],
        "report_path": "output/last_run_report.txt",
    }

    recap = engine.format_run_recap(summary)

    assert "Succeeded: 1 | Up to date: 1 | Failed/No Data: 2 | Warnings: 3" in recap
    assert "Run-level warnings:" in recap
    assert "Multiple local book directories claim novel ID 9" in recap
    assert "Novels that did not complete:" in recap
    assert "ID 3: not_exist" in recap
    assert "ID 4: boom" in recap
    assert "Intra-novel warnings:" in recap
    assert "ID 1 (One): Image error (404)" in recap
    assert "ID 4: Ignoring invalid episode cache x" in recap
    assert "ID 2 (" not in recap
    assert "Report saved to: output/last_run_report.txt" in recap


def test_format_run_recap_clean_run():
    recap = engine.format_run_recap(
        {
            "success": 2,
            "skipped": 0,
            "failed": 0,
            "results": [
                {"novel_id": 1, "status": "success"},
                {"novel_id": 2, "status": "success"},
            ],
        }
    )
    assert "Failed/No Data: 0 | Warnings: 0" in recap
    assert "No failures or warnings." in recap


def test_format_run_recap_truncates_for_dialogs():
    results = [{"novel_id": i, "status": "failed", "error": f"err-{i}"} for i in range(1, 30)]
    summary = {"success": 0, "skipped": 0, "failed": len(results), "results": results}
    recap = engine.format_run_recap(summary, max_lines=8)
    assert len(recap.splitlines()) == 9
    assert "more lines; see console or last_run_report.txt" in recap


def test_download_queue_attaches_intra_novel_logger_warnings(monkeypatch, tmp_path, caplog):
    def fake_build(client, novel_id, **kwargs):
        logging.getLogger("pia_scrap").warning(
            "Blocked image URL outside approved Novelpia hosts: https://evil/x"
        )
        logging.getLogger("pia_scrap").warning(
            "Image error (timeout): https://cdn/img.png"
        )
        return "book.epub", "Warned Book", 2

    monkeypatch.setattr(engine, "build_epub", fake_build)
    monkeypatch.setattr(engine, "build_book_directory_index", lambda out_dir: {})
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    with caplog.at_level("INFO", logger="pia_scrap"):
        result = scraper.run_download_queue([42])

    entry = result["results"][0]
    assert entry["status"] == "success"
    assert entry["issues"] == [
        "Blocked image URL outside approved Novelpia hosts: https://evil/x",
        "Image error (timeout): https://cdn/img.png",
    ]
    recap = engine.format_run_recap(result)
    assert "ID 42 (Warned Book): Blocked image URL" in recap
    assert "=== Run recap ===" in caplog.text
    report = (tmp_path / "last_run_report.txt").read_text(encoding="utf-8")
    assert "Warned Book" in report


def test_download_queue_captures_run_level_index_warnings(monkeypatch, tmp_path):
    def fake_index(out_dir):
        logging.getLogger("pia_scrap").warning(
            "Multiple local book directories claim novel ID 5; using 'keep'."
        )
        return {5: "keep"}

    monkeypatch.setattr(engine, "build_book_directory_index", fake_index)
    monkeypatch.setattr(
        engine,
        "build_epub",
        lambda *args, **kwargs: ("a.epub", "Keep", 1),
    )
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    result = scraper.run_download_queue([5])

    assert result["run_issues"] == [
        "Multiple local book directories claim novel ID 5; using 'keep'."
    ]
    assert "issues" not in result["results"][0]
    recap = engine.format_run_recap(result)
    assert "Run-level warnings:" in recap
    assert "Warnings: 1" in recap


def test_download_queue_does_not_duplicate_hard_failure_as_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(
        engine,
        "build_epub",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("disk full")),
    )
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)
    result = scraper.run_download_queue([9])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["error"] == "disk full"
    assert "issues" not in result["results"][0]
    recap = engine.format_run_recap(result)
    assert "ID 9: disk full" in recap


def test_download_queue_warnings_scoped_per_novel(monkeypatch, tmp_path):
    def fake_build(client, novel_id, **kwargs):
        if novel_id == 1:
            logging.getLogger("pia_scrap").warning("warn-one")
            return "a.epub", "A", 1
        return "b.epub", "B", 1

    monkeypatch.setattr(engine, "build_epub", fake_build)
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)
    result = scraper.run_download_queue([1, 2])
    assert result["results"][0]["issues"] == ["warn-one"]
    assert "issues" not in result["results"][1]


def test_format_run_recap_cancelled_empty_does_not_claim_clean_run():
    recap = engine.format_run_recap(
        {"success": 0, "skipped": 0, "failed": 0, "results": [], "cancelled": True}
    )
    assert "Download queue was interrupted." in recap
    assert "No failures or warnings." not in recap


def test_download_queue_404_error_text_appears_in_recap(monkeypatch, tmp_path):
    class HttpError(Exception):
        def __init__(self):
            super().__init__("not found")
            self.response = SimpleNamespace(status_code=404)

    monkeypatch.setattr(
        engine,
        "build_epub",
        lambda *args, **kwargs: (_ for _ in ()).throw(HttpError()),
    )
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace(sleep_cooperative=lambda seconds: None)

    result = scraper.run_download_queue([9])
    entry = result["results"][0]
    assert entry["status"] == "404"
    assert entry["error"] == "Novel 9 returned 404. Skipping."
    assert result["failed"] == 1
    recap = engine.format_run_recap(result)
    assert "Novel 9 returned 404. Skipping." in recap


def test_download_queue_mid_novel_cancel_records_interrupted_and_keeps_warnings(
    monkeypatch, tmp_path
):
    def fake_build(client, novel_id, **kwargs):
        logging.getLogger("pia_scrap").warning("Image error (timeout): https://cdn/x")
        raise DownloadCancelled("Download stopped by cancellation request.")

    monkeypatch.setattr(engine, "build_epub", fake_build)
    monkeypatch.setattr(engine, "build_book_directory_index", lambda out_dir: {})
    scraper = engine.ScraperEngine(out_dir=str(tmp_path))
    scraper.client = SimpleNamespace()

    result = scraper.run_download_queue([11, 12])

    assert result["failed"] == 0
    assert result["cancelled"] is True
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["novel_id"] == 11
    assert entry["status"] == "cancelled"
    assert "Stopped processing 11" in entry["error"]
    assert entry["issues"] == ["Image error (timeout): https://cdn/x"]
    recap = engine.format_run_recap(result)
    assert "Download queue was interrupted." in recap
    assert "ID 11" in recap
    assert "Image error (timeout)" in recap
    assert "No failures or warnings." not in recap
