import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from curl_cffi import requests

from src import helper
from src.api import NovelpiaClient, request_with_retries


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("47", [47]),
        ("47, 50, 51-53, 50", [47, 50, 51, 52, 53]),
        ("1-1", [1]),
        ("", []),
    ],
)
def test_parse_range(value, expected):
    assert helper.parse_range(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "5-2", "x", "1-x"])
def test_parse_range_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        helper.parse_range(value)


def test_filename_and_slug_normalization():
    assert helper.sanitize_filename('  a/b:c*?"<>|  ') == "a_b_c_"
    assert helper.sanitize_filename("") == "book"
    assert helper.kebab("  Héllo, 世界! ") == "héllo-世界"
    assert helper.kebab("CON") == "_con"
    assert helper.sanitize_filename("CON") == "_CON"
    assert helper.sanitize_filename("title. ") == "title"
    assert len(helper.sanitize_filename("x" * 300)) == helper.DEFAULT_COMPONENT_LENGTH
    assert len(helper.kebab("x" * 300)) == helper.BOOK_SLUG_LENGTH


def test_normalize_url():
    assert helper.normalize_url("//images.example/cover.jpg") == "https://images.example/cover.jpg"
    assert helper.normalize_url("/novel/42") == "https://global.novelpia.com/novel/42"
    assert helper.normalize_url("https://example.test/a") == "https://example.test/a"
    assert helper.normalize_url("") == ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://d.novelpia.com/imagebox/5b/5bc9ca9dc34ddcf2d254463768eaf4c7_954868.png",
            "https://image.novelpia.com/imagebox/5b/5bc9ca9dc34ddcf2d254463768eaf4c7_954868.png",
        ),
        (
            "https://img.novelpia.com/imagebox/8b/8b770edd1622d3f72b4f65ec23c80a41_689960.png",
            "https://image.novelpia.com/imagebox/8b/8b770edd1622d3f72b4f65ec23c80a41_689960.png",
        ),
        (
            "//d.novelpia.com/imagebox/c2/c262c4f4ddcfa6efca21e33d83c8d9cc_1723139.png",
            "https://image.novelpia.com/imagebox/c2/c262c4f4ddcfa6efca21e33d83c8d9cc_1723139.png",
        ),
    ],
)
def test_normalize_url_rewrites_dead_image_hosts(source, expected):
    assert helper.normalize_url(source) == expected


@pytest.mark.parametrize("url", [
    "http://image.novelpia.com/a.jpg",
    "https://image.novelpia.com.evil.test/a.jpg",
])
def test_image_fetch_blocks_http_and_lookalike_hosts(url):
    client = object.__new__(NovelpiaClient)
    client.cancel_event = None
    client.timeout = 1
    client.s = SimpleNamespace(cookies={})
    client._image_s = SimpleNamespace(
        cookies={}, get=lambda *args, **kwargs: pytest.fail("unapproved URL was fetched")
    )

    assert client.fetch_image(url, "https://global.novelpia.com/") is None


@pytest.mark.parametrize(
    ("data", "fallback", "expected"),
    [
        (b"\xff\xd8\xffrest", ".png", (".jpg", "image/jpeg")),
        (b"\x89PNG\r\n\x1a\nrest", ".jpg", (".png", "image/png")),
        (b"GIF89arest", ".jpg", (".gif", "image/gif")),
        (b"RIFFxxxxWEBPrest", ".jpg", (".webp", "image/webp")),
        (b"  <svg xmlns='x'>", ".jpg", (".svg", "image/svg+xml")),
        (b"unknown", ".bmp", (".jpg", "image/jpeg")),
    ],
)
def test_image_type_uses_file_signature(data, fallback, expected):
    assert helper.image_type(data, fallback) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"result": {"nested": {"token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln"}}},
            "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln",
        ),
        (
            {
                "result": {
                    "_t": "simple-token",
                    "token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln",
                }
            },
            "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln",
        ),
        (
            {
                "result": {
                    "url": (
                        "https://api-global.novelpia.com/v1/novel/episode/content"
                        "?_t=simple-token"
                    )
                }
            },
            "simple-token",
        ),
        ({"result": {"url": "https://evil.test/content?_t=x"}}, None),
    ],
)
def test_extract_t_token_reads_nested_and_official_content_urls(payload, expected):
    assert helper.extract_t_token(payload) == expected


def test_request_sends_session_and_explicit_cookies_to_server():
    class CookieEcho(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write("; ".join(self.headers.get_all("Cookie", [])).encode("utf-8"))

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), CookieEcho)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            session.cookies.set("USERKEY", "example-userkey")
            session.cookies.set("TKEY", "fixture-token")
            url = f"http://127.0.0.1:{server.server_port}/data"
            default = request_with_retries(session, "GET", url).text
            explicit = request_with_retries(
                session, "GET", url, headers={"Cookie": "custom=1"}
            ).text
        assert "USERKEY=example-userkey" in default
        assert "TKEY=fixture-token" in default
        assert "last_login=basic" in default
        assert "custom=1" in explicit
        assert "last_login=basic" not in explicit
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_atomic_writes_create_parent_and_valid_json(tmp_path):
    text_path = tmp_path / "nested" / "value.txt"
    helper.write_text_atomic(text_path, "hello")
    assert text_path.read_text(encoding="utf-8") == "hello"

    json_path = tmp_path / "nested" / "value.json"
    helper.write_json_atomic(json_path, {"한글": True}, indent=2)
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"한글": True}
    assert not list(json_path.parent.glob(".tmp-*"))


def test_book_base_keeps_owned_folder_and_disambiguates_collisions(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "same-title").mkdir()
    (owned / "same-title" / "metadata.json").write_text(
        json.dumps({"novel_id": 42}), encoding="utf-8"
    )
    assert helper.book_base(str(owned), "Same Title", 42) == "same-title"
    assert helper.book_base(str(owned), "Same Title", 99) == "same-title-99"

    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "same-title").mkdir()
    (taken / "same-title-99").mkdir()
    assert helper.book_base(str(taken), "Same Title", 99) == "same-title-99-2"

    renamed = tmp_path / "renamed"
    renamed.mkdir()
    (renamed / "old-title").mkdir()
    (renamed / "old-title" / ".novel_id").write_text("42", encoding="ascii")
    assert helper.book_base(str(renamed), "New Title", 42) == "old-title"
    index = helper.build_book_directory_index(str(renamed))
    assert index == {42: "old-title"}
    assert helper.book_base(str(renamed), "New Title", 42, book_index=index) == "old-title"


@pytest.mark.parametrize("claim", ["marker", "metadata"])
def test_retire_owned_novel_releases_the_id_and_keeps_the_files(tmp_path, claim):
    kept = tmp_path / "your-generic-tower-climber-novel"
    kept.mkdir()
    metadata = {
        "novel_id": 4786,
        "title": "Your Generic Tower Climber Novel",
        "url": "https://global.novelpia.com/novel/4786",
    }
    (kept / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (kept / "your-generic-tower-climber-novel.epub").write_bytes(b"epub-bytes")
    if claim == "marker":
        (kept / ".novel_id").write_text("4786", encoding="ascii")
    extra = tmp_path / "also-4786"
    extra.mkdir()
    (extra / ".novel_id").write_text("4786", encoding="ascii")
    index = helper.build_book_directory_index(str(tmp_path))

    released = helper.retire_owned_novel(str(tmp_path), 4786)

    assert set(released) == {str(kept), str(extra)}
    assert not (kept / ".novel_id").exists()
    assert not (extra / ".novel_id").exists()
    assert (kept / "your-generic-tower-climber-novel.epub").read_bytes() == b"epub-bytes"
    assert json.loads((kept / "metadata.json").read_text(encoding="utf-8")) == metadata
    retired = json.loads((kept / ".retired").read_text(encoding="utf-8"))
    assert retired["novel_id"] == 4786
    assert retired["title"] == "Your Generic Tower Climber Novel"
    assert retired["retired_at"]
    assert helper.book_directory_novel_id(str(kept)) is None
    assert helper.local_library_novel_ids(str(tmp_path)) == []
    assert helper.book_base(
        str(tmp_path), "Your Generic Tower Climber Novel", 4786, book_index=index
    ) == "your-generic-tower-climber-novel-4786"


def test_ensure_book_identity_writes_marker_and_rejects_conflicts(tmp_path):
    paths = helper.book_output_paths(str(tmp_path), "A Test Novel", 42)
    helper.ensure_book_identity(paths)

    assert (tmp_path / "a-test-novel" / ".novel_id").read_text(encoding="ascii") == "42"

    helper.ensure_book_identity(paths)
    with pytest.raises(ValueError, match="belongs to novel ID 42"):
        helper.ensure_book_identity(replace(paths, novel_id=99))


def test_book_output_paths_returns_one_consistent_layout(tmp_path):
    paths = helper.book_output_paths(str(tmp_path), "A Test / Novel", 42)

    assert paths.base == "a-test-novel"
    assert paths.book_dir == str(tmp_path / "a-test-novel")
    assert paths.epub_path == str(tmp_path / "a-test-novel" / "a-test-novel.epub")
    assert paths.metadata_path == str(tmp_path / "a-test-novel" / "metadata.json")
    assert paths.chapters_path == str(tmp_path / "a-test-novel" / "chapters.jsonl")
    assert paths.novel_id_path == str(tmp_path / "a-test-novel" / ".novel_id")
    assert paths.image_index_path == str(
        tmp_path / "a-test-novel" / ".raw_cache" / "image_index.json"
    )


@pytest.mark.parametrize("novel_id", [0, -1, "invalid"])
def test_book_output_paths_rejects_invalid_novel_id(tmp_path, novel_id):
    with pytest.raises(ValueError, match="positive integer"):
        helper.book_output_paths(str(tmp_path), "Book", novel_id)


@pytest.mark.parametrize(
    ("marker", "metadata", "expected"),
    [
        ("42", {"novel_id": 99, "url": "https://global.novelpia.com/novel/7"}, 42),
        ("0", None, None),
        ("-5", None, None),
        ("nope", None, None),
        ("0", {"novel_id": 42}, 42),
        (None, {"url": "https://global.novelpia.com/novel/86"}, 86),
    ],
)
def test_book_directory_novel_id_prefers_marker_then_metadata(
    tmp_path, marker, metadata, expected
):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    if marker is not None:
        (book_dir / ".novel_id").write_text(marker, encoding="ascii")
    if metadata is not None:
        (book_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    assert helper.book_directory_novel_id(str(book_dir)) == expected


def test_load_local_book_info_resolves_marker_when_metadata_is_missing(tmp_path):
    book_dir = tmp_path / "old-title"
    book_dir.mkdir()
    (book_dir / ".novel_id").write_text("42", encoding="ascii")

    info = helper.load_local_book_info(str(book_dir))

    assert info.novel_id == 42
    assert info.title == "old-title"
    assert info.author == "Unknown Author"
    assert info.has_metadata is False
    assert info.chapter_count is None


def test_load_local_book_info_reads_listing_fields_from_metadata(tmp_path):
    book_dir = tmp_path / "a-test-novel"
    book_dir.mkdir()
    (book_dir / "metadata.json").write_text(
        json.dumps({
            "novel_id": 42,
            "title": "A Test / Novel",
            "author": "Test Author",
            "chapter": 12,
            "status": "Ongoing",
        }),
        encoding="utf-8",
    )

    info = helper.load_local_book_info(str(book_dir))

    assert info.novel_id == 42
    assert info.title == "A Test / Novel"
    assert info.author == "Test Author"
    assert info.chapter_count == 12
    assert info.status == "Ongoing"
    assert info.has_metadata is True


def test_local_library_novel_ids_includes_marker_only_folders(tmp_path):
    marker_only = tmp_path / "old-title"
    marker_only.mkdir()
    (marker_only / ".novel_id").write_text("42", encoding="ascii")
    with_meta = tmp_path / "other"
    with_meta.mkdir()
    (with_meta / "metadata.json").write_text(json.dumps({"novel_id": 7}), encoding="utf-8")
    (tmp_path / ".ignored").mkdir()
    duplicate = tmp_path / "also-42"
    duplicate.mkdir()
    (duplicate / ".novel_id").write_text("42", encoding="ascii")

    assert helper.list_local_book_directories(str(tmp_path)) == [
        "also-42",
        "old-title",
        "other",
    ]
    assert helper.local_library_novel_ids(str(tmp_path)) == [42, 7]


def test_load_local_book_info_logs_corrupt_metadata(tmp_path, caplog):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / ".novel_id").write_text("42", encoding="ascii")
    (book_dir / "metadata.json").write_text("{not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="pia_scrap"):
        info = helper.load_local_book_info(str(book_dir))

    assert info.novel_id == 42
    assert info.has_metadata is False
    assert "invalid local book metadata" in caplog.text


def test_list_local_book_directories_is_silent_when_output_dir_is_missing(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="pia_scrap"):
        names = helper.list_local_book_directories(str(tmp_path / "missing"))

    assert names == []
    assert caplog.text == ""


def test_list_local_book_directories_logs_scan_failure(monkeypatch, tmp_path, caplog):
    def fail_scandir(path):
        raise PermissionError("denied")

    monkeypatch.setattr(helper.os, "scandir", fail_scandir)

    with caplog.at_level("WARNING", logger="pia_scrap"):
        names = helper.list_local_book_directories(str(tmp_path))

    assert names == []
    assert "Could not scan library directory" in caplog.text


def test_book_dir_scans_include_dot_dirs_except_library_listing(tmp_path):
    visible = tmp_path / "visible"
    visible.mkdir()
    (visible / ".novel_id").write_text("7", encoding="ascii")
    hidden = tmp_path / ".hidden-book"
    hidden.mkdir()
    (hidden / ".novel_id").write_text("42", encoding="ascii")

    assert helper.list_local_book_directories(str(tmp_path)) == ["visible"]
    assert helper.build_book_directory_index(str(tmp_path)) == {
        7: "visible",
        42: ".hidden-book",
    }
    assert helper.book_base(str(tmp_path), "Renamed Hidden", 42) == ".hidden-book"


def test_save_config_reports_success_and_failure(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(helper, "CONFIG_PATH", str(config_path))
    assert helper.save_config({"login_at": "token"}) is True
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"login_at": "token"}

    # Path exists as a directory so the atomic replace cannot write the file.
    config_path.unlink()
    config_path.mkdir()
    assert helper.save_config({"login_at": "other"}) is False
    assert config_path.is_dir()
