import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src import helper


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


def test_image_url_allowlist_is_exact_and_https_only():
    assert helper.is_approved_image_url("https://image.novelpia.com/a.jpg")
    assert helper.is_approved_image_url("https://global.novelpia.com/a.jpg")
    assert helper.is_approved_image_url(
        "https://d.novelpia.com/imagebox/80/804a58cc60faed71c0e370df07d17073_499547.png"
    )
    assert helper.is_approved_image_url("https://gn.novelpia.com/a.jpg")
    assert helper.is_approved_image_url("https://img.novelpia.com/a.jpg")
    assert not helper.is_approved_image_url("http://image.novelpia.com/a.jpg")
    assert not helper.is_approved_image_url("https://image.novelpia.com.evil.test/a.jpg")
    assert not helper.is_approved_image_url("https://evil.test/a.jpg")


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


def test_token_detection_and_extraction():
    jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln"
    assert helper.looks_like_jwt(jwt)
    assert not helper.looks_like_jwt("not-a-jwt")
    assert helper.extract_t_token({"result": {"nested": {"token": jwt}}}) == jwt


def test_extract_token_from_official_content_url():
    url = "https://api-global.novelpia.com/v1/novel/episode/content?_t=simple-token"
    assert helper.extract_t_token({"result": {"url": url}}) == "simple-token"
    assert helper.extract_t_token({"result": {"url": "https://evil.test/content?_t=x"}}) is None


def test_attach_auth_cookies_does_not_replace_explicit_cookie():
    cookies = {"USERKEY": "user", "TKEY": "token"}
    session = SimpleNamespace(cookies=cookies)
    assert helper.attach_auth_cookies(session) == {
        "Cookie": "USERKEY=user; TKEY=token; last_login=basic"
    }
    assert helper.attach_auth_cookies(session, {"Cookie": "custom=1"})["Cookie"] == "custom=1"


def test_atomic_writes_create_parent_and_valid_json(tmp_path):
    text_path = tmp_path / "nested" / "value.txt"
    helper.write_text_atomic(text_path, "hello")
    assert text_path.read_text(encoding="utf-8") == "hello"

    json_path = tmp_path / "nested" / "value.json"
    helper.write_json_atomic(json_path, {"한글": True}, indent=2)
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"한글": True}
    assert not list(json_path.parent.glob(".tmp-*"))


def test_book_base_reuses_matching_novel_and_disambiguates_collision(tmp_path):
    book_dir = tmp_path / "same-title"
    book_dir.mkdir()
    (book_dir / "metadata.json").write_text(json.dumps({"novel_id": 42}), encoding="utf-8")

    assert helper.book_base(str(tmp_path), "Same Title", 42) == "same-title"
    assert helper.book_base(str(tmp_path), "Same Title", 99) == "same-title-99"


def test_book_base_handles_secondary_collision(tmp_path):
    (tmp_path / "same-title").mkdir()
    (tmp_path / "same-title-99").mkdir()

    assert helper.book_base(str(tmp_path), "Same Title", 99) == "same-title-99-2"


def test_ensure_book_identity_writes_marker_and_rejects_conflicts(tmp_path):
    paths = helper.book_output_paths(str(tmp_path), "A Test Novel", 42)
    helper.ensure_book_identity(paths)

    assert (tmp_path / "a-test-novel" / ".novel_id").read_text(encoding="ascii") == "42"

    helper.ensure_book_identity(paths)
    with pytest.raises(ValueError, match="belongs to novel ID 42"):
        helper.ensure_book_identity(replace(paths, novel_id=99))


def test_book_base_finds_existing_novel_after_remote_rename(tmp_path):
    old_book_dir = tmp_path / "old-title"
    old_book_dir.mkdir()
    (old_book_dir / ".novel_id").write_text("42", encoding="ascii")

    assert helper.book_base(str(tmp_path), "New Title", 42) == "old-title"


def test_directory_index_reuses_single_library_scan(tmp_path):
    for name, novel_id in (("first", 1), ("second", 2)):
        book_dir = tmp_path / name
        book_dir.mkdir()
        (book_dir / ".novel_id").write_text(str(novel_id), encoding="ascii")

    index = helper.build_book_directory_index(str(tmp_path))

    assert index == {1: "first", 2: "second"}
    assert helper.book_base(str(tmp_path), "Renamed First", 1, index) == "first"


def test_book_output_paths_returns_one_consistent_layout(tmp_path):
    paths = helper.book_output_paths(str(tmp_path), "A Test / Novel", 42)

    assert paths.base == "a-test-novel"
    assert paths.book_dir == str(tmp_path / "a-test-novel")
    assert paths.epub_path == str(tmp_path / "a-test-novel" / "a-test-novel.epub")
    assert paths.metadata_path == str(tmp_path / "a-test-novel" / "metadata.json")
    assert paths.chapters_path == str(tmp_path / "a-test-novel" / "chapters.jsonl")
    assert paths.novel_id_path == str(tmp_path / "a-test-novel" / ".novel_id")
    assert paths.image_cache_dir == str(tmp_path / "a-test-novel" / ".raw_cache" / "images")
    assert paths.image_index_path == str(
        tmp_path / "a-test-novel" / ".raw_cache" / "image_index.json"
    )


@pytest.mark.parametrize("novel_id", [0, -1, "invalid"])
def test_book_output_paths_rejects_invalid_novel_id(tmp_path, novel_id):
    with pytest.raises(ValueError, match="positive integer"):
        helper.book_output_paths(str(tmp_path), "Book", novel_id)


def test_book_directory_novel_id_prefers_marker_over_metadata(tmp_path):
    book_dir = tmp_path / "renamed"
    book_dir.mkdir()
    (book_dir / ".novel_id").write_text("42", encoding="ascii")
    (book_dir / "metadata.json").write_text(
        json.dumps({"novel_id": 99, "url": "https://global.novelpia.com/novel/7"}),
        encoding="utf-8",
    )

    assert helper.book_directory_novel_id(str(book_dir)) == 42


@pytest.mark.parametrize("value", ["0", "-5", "nope"])
def test_book_directory_novel_id_rejects_non_positive_and_invalid_ids(tmp_path, value):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / ".novel_id").write_text(value, encoding="ascii")

    assert helper.book_directory_novel_id(str(book_dir)) is None


def test_book_directory_novel_id_falls_through_invalid_marker_to_metadata(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / ".novel_id").write_text("0", encoding="ascii")
    (book_dir / "metadata.json").write_text(json.dumps({"novel_id": 42}), encoding="utf-8")

    assert helper.book_directory_novel_id(str(book_dir)) == 42


def test_book_directory_novel_id_falls_back_to_metadata_url(tmp_path):
    book_dir = tmp_path / "from-url"
    book_dir.mkdir()
    (book_dir / "metadata.json").write_text(
        json.dumps({"url": "https://global.novelpia.com/novel/86"}),
        encoding="utf-8",
    )

    assert helper.book_directory_novel_id(str(book_dir)) == 86


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


def test_local_library_novel_ids_skips_non_positive_ids(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".novel_id").write_text("-5", encoding="ascii")
    good = tmp_path / "good"
    good.mkdir()
    (good / ".novel_id").write_text("42", encoding="ascii")

    assert helper.local_library_novel_ids(str(tmp_path)) == [42]


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
    monkeypatch.setattr(helper, "CONFIG_PATH", str(tmp_path / "config.json"))
    assert helper.save_config({"login_at": "token"}) is True

    monkeypatch.setattr(
        helper,
        "write_json_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )
    assert helper.save_config({"login_at": "token"}) is False
