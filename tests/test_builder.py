import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src import builder
from src.api import DownloadCancelled
from src.const import BASE_URL
from src.epub import EpubBuilder
from src.novel import html_from_episode_text, parse_novel_metadata


def _metadata(novel_data, novel_id=42, title=None):
    metadata = parse_novel_metadata(novel_data, novel_id)
    if title is None:
        return metadata
    return replace(metadata, title=title)


class DummyFetchClient:
    def __init__(self, results=None):
        self.results = results
        self.fetched = []
        self.cancel_event = None

    def fetch_episodes_parallel(
        self, episode_list, max_workers=1, progress_cb=None, on_complete_cb=None
    ):
        self.fetched.extend(episode_list)
        if self.results is not None:
            results = list(self.results)
        else:
            results = []
            for episode in episode_list:
                results.append({
                    "html": f"<p>body {episode['episode_no']}</p>",
                    "epi_no": episode["episode_no"],
                    "epi_title": episode["epi_title"],
                })
        for index, result in enumerate(results, 1):
            if on_complete_cb:
                on_complete_cb(result)
            if progress_cb:
                progress_cb(index, len(results), result.get("epi_title") or "")
        return results

    def fetch_image(self, url, referer_url, episode_cookies=None, episode_no=None):
        return None


def recording_epub_builder(book_dir, *, builds=None, fail=None):
    class RecordingEpubBuilder:
        def __init__(self, out_dir):
            pass

        def build(self, **kwargs):
            if fail is not None:
                raise AssertionError(fail)
            if builds is not None:
                builds.append(kwargs)
            chapters = kwargs.get("chapters") or []
            return str(book_dir / "book.epub"), "Book", len(chapters)

    return RecordingEpubBuilder


def test_build_metadata_writes_deduplicated_tags_and_chapters(tmp_path, novel_data, episodes):
    builder.build_metadata(str(tmp_path), novel_data, 42, episodes)

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    chapters = [json.loads(line) for line in (tmp_path / "chapters.jsonl").read_text(encoding="utf-8").splitlines()]

    assert metadata == {
        "url": f"{BASE_URL}/novel/42",
        "novel_id": 42,
        "title": "A Test / Novel",
        "author": "Test Author",
        "tags": ["fantasy", "adventure"],
        "chapter": 2,
        "status": "Completed",
        "description": "A short description.",
    }
    assert chapters[0] == {
        "idx": 1,
        "episode_no": 101,
        "title": "First / Chapter",
        "url": f"{BASE_URL}/viewer/101",
        "flag_detail_trans": 2,
    }


def test_build_txt_fetches_then_writes_all_files(monkeypatch, tmp_path, novel_data, episodes):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data)),
    )

    class Client:
        def fetch_episodes_parallel(
            self, episode_list, max_workers, progress_cb, on_complete_cb=None
        ):
            results = []
            for i, episode in enumerate(episode_list, 1):
                result = {"html": f"<p>body {i}</p>", "epi_title": episode["epi_title"]}
                results.append(result)
                if on_complete_cb:
                    on_complete_cb(result)
                progress_cb(i, len(episode_list), episode["epi_title"])
            return results

    progress = []
    output, title, count = builder.build_txt(
        Client(), 42, str(tmp_path), threads=2,
        progress_cb=lambda *args: progress.append(args),
    )

    output_path = tmp_path / "a-test-novel"
    assert output == str(output_path)
    assert (output_path / "1_First _ Chapter.txt").read_text(encoding="utf-8") == "body 1"
    assert title == "A Test / Novel"
    assert count == 2
    assert progress[-1][:2] == (2, 2)


@pytest.mark.parametrize("update_mode", [False, True])
def test_build_txt_is_all_or_nothing_on_fetch_failure(
    monkeypatch, tmp_path, novel_data, episodes, update_mode
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data, title="Book")),
    )
    client = SimpleNamespace(fetch_episodes_parallel=lambda *args, **kwargs: [
        {"html": "ok", "epi_title": "one", "epi_no": 101}, {"error": "denied"}
    ])

    with pytest.raises(RuntimeError, match="No TXT files were written"):
        builder.build_txt(
            client, 42, str(tmp_path), update_mode=update_mode, progress_cb=lambda *args: None
        )
    book_dir = tmp_path / "book"
    if update_mode:
        assert (book_dir / ".novel_id").read_text(encoding="ascii") == "42"
        assert not list(book_dir.glob("*.txt"))
    else:
        assert not book_dir.exists()


def test_build_epub_update_skips_consistent_existing_book(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data, title="Book")),
    )
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "metadata.json").write_text(json.dumps({
        "chapter": 2,
        "novel_id": 42,
    }), encoding="utf-8")
    builder.build_metadata(str(book_dir), novel_data, 42, episodes)
    with zipfile.ZipFile(book_dir / "book.epub", "w") as archive:
        archive.writestr("chap_0001.xhtml", "one")
        archive.writestr("chap_0002.xhtml", "two")

    statuses = []
    result = builder.build_epub(
        object(), 42, str(tmp_path), update_mode=True, status_cb=statuses.append
    )

    assert result == (None, "Book", 2)
    assert statuses == ["Fetching novel metadata and episode list..."]


def test_build_epub_update_does_not_shrink_complete_book_when_max_chapters_is_lower(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (
            novel_data,
            episodes[:1],
            _metadata(novel_data, title="Book"),
        ),
    )
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    builder.build_metadata(str(book_dir), novel_data, 42, episodes)
    with zipfile.ZipFile(book_dir / "book.epub", "w") as archive:
        archive.writestr("chap_0001.xhtml", "one")
        archive.writestr("chap_0002.xhtml", "two")

    monkeypatch.setattr(
        builder,
        "EpubBuilder",
        recording_epub_builder(
            book_dir, fail="update must not shrink a complete local book"
        ),
    )

    result = builder.build_epub(
        object(), 42, str(tmp_path), max_chapters=1, update_mode=True
    )

    assert result == (None, "Book", 2)


def test_build_epub_update_refetches_full_list_instead_of_shrinking_on_revision_mismatch(
    monkeypatch, tmp_path, novel_data, episodes
):
    calls = []

    def fake_fetch(client, novel_id, max_chapters=None):
        calls.append(max_chapters)
        meta = _metadata(novel_data, title="Book")
        if max_chapters:
            return novel_data, episodes[:max_chapters], meta
        return novel_data, episodes, meta

    monkeypatch.setattr(builder, "fetch_novel_and_episodes", fake_fetch)
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    stale_first = dict(episodes[0], flag_detail_trans=99)
    builder.build_metadata(str(book_dir), novel_data, 42, [stale_first, episodes[1]])
    with zipfile.ZipFile(book_dir / "book.epub", "w") as archive:
        archive.writestr("chap_0001.xhtml", "one")
        archive.writestr("chap_0002.xhtml", "two")

    received = []
    monkeypatch.setattr(
        builder, "EpubBuilder", recording_epub_builder(book_dir, builds=received)
    )

    result = builder.build_epub(
        DummyFetchClient(), 42, str(tmp_path), max_chapters=1, update_mode=True
    )

    assert calls == [1, None]
    assert [chapter["epi_no"] for chapter in received[0]["chapters"]] == [101, 102]
    assert result == (str(book_dir / "book.epub"), "Book", 2)


def test_build_epub_update_skips_even_with_legacy_image_cache(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data, title="Book")),
    )
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / ".raw_cache" / "images").mkdir(parents=True)
    builder.build_metadata(str(book_dir), novel_data, 42, episodes)
    with zipfile.ZipFile(book_dir / "book.epub", "w") as archive:
        archive.writestr("chap_0001.xhtml", "one")
        archive.writestr("chap_0002.xhtml", "two")

    monkeypatch.setattr(
        builder,
        "EpubBuilder",
        recording_epub_builder(
            book_dir,
            fail="current books should skip even with leftover image cache",
        ),
    )

    result = builder.build_epub(object(), 42, str(tmp_path), update_mode=True)

    assert result == (None, "Book", 2)


@pytest.mark.parametrize("stored_revision", [None, 3])
def test_build_epub_rebuilds_when_chapter_revisions_do_not_match(
    monkeypatch, tmp_path, novel_data, episodes, stored_revision
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data, title="Book")),
    )
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "metadata.json").write_text(
        json.dumps({"chapter": 2, "novel_id": 42, "flag_detail_trans": 4}),
        encoding="utf-8",
    )
    chapter_records = [
        {
            "idx": index,
            "episode_no": episode["episode_no"],
            "title": episode["epi_title"],
            "url": f"{BASE_URL}/viewer/{episode['episode_no']}",
            **(
                {"flag_detail_trans": episode["flag_detail_trans"]}
                if stored_revision is not None or index == 2
                else {}
            ),
        }
        for index, episode in enumerate(episodes, 1)
    ]
    if stored_revision is not None:
        chapter_records[0]["flag_detail_trans"] = stored_revision
    (book_dir / "chapters.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in chapter_records),
        encoding="utf-8",
    )
    with zipfile.ZipFile(book_dir / "book.epub", "w") as archive:
        archive.writestr("chap_0001.xhtml", "one")
        archive.writestr("chap_0002.xhtml", "two")

    received = []
    monkeypatch.setattr(
        builder, "EpubBuilder", recording_epub_builder(book_dir, builds=received)
    )

    result = builder.build_epub(DummyFetchClient(), 42, str(tmp_path), update_mode=True)

    assert result == (str(book_dir / "book.epub"), "Book", 2)
    assert len(received) == 1
    saved_metadata = json.loads((book_dir / "metadata.json").read_text(encoding="utf-8"))
    assert "flag_detail_trans" not in saved_metadata


def test_epub_builder_builds_empty_chapter_epub(tmp_path, novel_data):
    output, title, count = EpubBuilder(str(tmp_path)).build(
        _metadata(novel_data), [], fetch_image=lambda *args, **kwargs: None
    )

    assert output == str(tmp_path / "a-test-novel" / "a-test-novel.epub")
    assert title == "A Test / Novel"
    assert count == 0
    assert (tmp_path / "a-test-novel" / "a-test-novel.epub").exists()

    with zipfile.ZipFile(output) as archive:
        opf = archive.read("EPUB/content.opf").decode("utf-8")
    assert "<dc:title>A Test / Novel</dc:title>" in opf
    assert "<dc:creator id=\"creator\">Test Author</dc:creator>" in opf


def test_load_or_fetch_replaces_raw_episode_cache_with_stale_revision(
    tmp_path, episodes
):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "101.json"
    cache_file.write_text(
        json.dumps({
            "html": "<p>stale</p>",
            "epi_no": 101,
            "epi_title": "First",
            "flag_detail_trans": 1,
        }),
        encoding="utf-8",
    )
    client = DummyFetchClient([{
        "html": "<p>fresh</p>",
        "epi_no": 101,
        "epi_title": "First / Chapter",
    }])

    results = builder._load_or_fetch_episodes(
        client, episodes[:1], str(cache_dir), update_mode=True
    )

    assert client.fetched == episodes[:1]
    assert results[0]["html"] == "<p>fresh</p>"
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["html"] == "<p>fresh</p>"
    assert cached["flag_detail_trans"] == 2


def test_load_or_fetch_caches_each_episode_as_it_arrives(tmp_path, episodes):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    cached_during_fetch = []

    class Client(DummyFetchClient):
        def fetch_episodes_parallel(
            self, episode_list, max_workers=1, progress_cb=None, on_complete_cb=None
        ):
            def remember_cache_state(result):
                if on_complete_cb:
                    on_complete_cb(result)
                cached_during_fetch.append(
                    (cache_dir / f"{result['epi_no']}.json").exists()
                )

            return super().fetch_episodes_parallel(
                episode_list,
                max_workers=max_workers,
                progress_cb=progress_cb,
                on_complete_cb=remember_cache_state,
            )

    builder._load_or_fetch_episodes(
        Client(), episodes, str(cache_dir), update_mode=True
    )

    assert cached_during_fetch == [True, True]
    for episode in episodes:
        cached = json.loads(
            (cache_dir / f"{episode['episode_no']}.json").read_text(encoding="utf-8")
        )
        assert cached["html"] == f"<p>body {episode['episode_no']}</p>"
        assert cached["flag_detail_trans"] == episode["flag_detail_trans"]


def test_update_retry_after_cancel_reuses_cached_book_dir(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes, _metadata(novel_data)),
    )

    class CancelAfterFirst(DummyFetchClient):
        def fetch_episodes_parallel(
            self, episode_list, max_workers=1, progress_cb=None, on_complete_cb=None
        ):
            first = episode_list[0]
            result = {
                "html": "<p>one</p>",
                "epi_no": first["episode_no"],
                "epi_title": first["epi_title"],
            }
            self.fetched.append(first)
            if on_complete_cb:
                on_complete_cb(result)
            raise DownloadCancelled("aborted by user")

    with pytest.raises(DownloadCancelled):
        builder.build_epub(CancelAfterFirst(), 42, str(tmp_path), update_mode=True)

    book_dir = tmp_path / "a-test-novel"
    assert (book_dir / ".novel_id").read_text(encoding="ascii") == "42"
    assert (book_dir / ".raw_cache" / "101.json").exists()
    assert not (tmp_path / "a-test-novel-42").exists()

    client = DummyFetchClient([{
        "html": "<p>two</p>",
        "epi_no": 102,
        "epi_title": episodes[1]["epi_title"],
    }])
    output, title, count = builder.build_epub(
        client, 42, str(tmp_path), update_mode=True
    )

    assert client.fetched == episodes[1:]
    assert title == "A Test / Novel"
    assert count == 2
    assert output == str(book_dir / "a-test-novel.epub")
    assert not (tmp_path / "a-test-novel-42").exists()


def test_load_or_fetch_omits_signed_key_from_written_cache(tmp_path, episodes):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    signed_key = {
        "CloudFront-Policy": "fixture-policy",
        "CloudFront-Key-Pair-Id": "fixture-key-id",
        "CloudFront-Signature": "fixture-signature",
    }
    client = DummyFetchClient([{
        "html": "<p>fresh</p>",
        "epi_no": 101,
        "epi_title": "First / Chapter",
        "signed_key": signed_key,
    }])

    results = builder._load_or_fetch_episodes(
        client, episodes[:1], str(cache_dir), update_mode=True
    )

    assert results[0]["signed_key"] == signed_key
    cached = json.loads((cache_dir / "101.json").read_text(encoding="utf-8"))
    assert "signed_key" not in cached
    assert cached["html"] == "<p>fresh</p>"


def test_load_or_fetch_strips_signed_key_from_cache_hits(tmp_path, episodes):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    (cache_dir / "101.json").write_text(
        json.dumps({
            "html": "<p>cached</p>",
            "epi_no": 101,
            "epi_title": "First / Chapter",
            "flag_detail_trans": 2,
            "signed_key": {
                "CloudFront-Policy": "fixture-policy",
                "CloudFront-Key-Pair-Id": "fixture-key-id",
                "CloudFront-Signature": "fixture-signature",
            },
        }),
        encoding="utf-8",
    )
    client = DummyFetchClient([])
    client.fetch_episodes_parallel = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("matching cache should skip the network")
    )

    results = builder._load_or_fetch_episodes(
        client, episodes[:1], str(cache_dir), update_mode=True
    )

    assert results[0]["html"] == "<p>cached</p>"
    assert "signed_key" not in results[0]


def test_load_or_fetch_caches_successful_episodes_when_a_later_fetch_fails(
    tmp_path, episodes
):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    client = DummyFetchClient([
        {
            "html": "<p>ok</p>",
            "epi_no": 101,
            "epi_title": "First / Chapter",
        },
        {"error": "denied", "epi_no": 102},
    ])

    results = builder._load_or_fetch_episodes(
        client, episodes, str(cache_dir), update_mode=True
    )

    assert results[0]["html"] == "<p>ok</p>"
    assert "error" in results[1]
    first_cache = json.loads((cache_dir / "101.json").read_text(encoding="utf-8"))
    assert first_cache["html"] == "<p>ok</p>"
    assert first_cache["flag_detail_trans"] == 2
    assert not (cache_dir / "102.json").exists()


def test_load_or_fetch_reuses_only_raw_cache_with_matching_episode_revision(
    tmp_path, episodes
):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    for episode, revision in zip(episodes, (2, 3)):
        (cache_dir / f"{episode['episode_no']}.json").write_text(
            json.dumps({
                "html": f"<p>cached {episode['episode_no']}</p>",
                "epi_no": episode["episode_no"],
                "epi_title": episode["epi_title"],
                "flag_detail_trans": revision,
            }),
            encoding="utf-8",
        )
    client = DummyFetchClient([{
        "html": "<p>fresh second</p>",
        "epi_no": 102,
        "epi_title": episodes[1]["epi_title"],
    }])

    results = builder._load_or_fetch_episodes(
        client, episodes, str(cache_dir), update_mode=True
    )

    assert client.fetched == episodes[1:]
    assert results[0]["html"] == "<p>cached 101</p>"
    assert results[1]["html"] == "<p>fresh second</p>"
    first_cache = json.loads((cache_dir / "101.json").read_text(encoding="utf-8"))
    second_cache = json.loads((cache_dir / "102.json").read_text(encoding="utf-8"))
    assert first_cache["html"] == "<p>cached 101</p>"
    assert second_cache["html"] == "<p>fresh second</p>"
    assert second_cache["flag_detail_trans"] == 4


def test_failed_chapter_fetch_leaves_legacy_image_cache(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes[:1], _metadata(novel_data)),
    )
    book_dir = tmp_path / "a-test-novel"
    image_dir = book_dir / ".raw_cache" / "images"
    image_dir.mkdir(parents=True)
    leftover = image_dir / "keep.bin"
    leftover.write_bytes(b"keep")

    with pytest.raises(RuntimeError, match="The existing EPUB was left unchanged"):
        builder.build_epub(
            DummyFetchClient([{"error": "denied"}]),
            42,
            str(tmp_path),
            update_mode=True,
        )

    assert leftover.exists()
    assert not (book_dir / ".raw_cache" / "image_index.json").exists()


def test_failed_rebuild_leaves_epub_replaceable(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes[:1], _metadata(novel_data)),
    )
    EpubBuilder(str(tmp_path)).build(
        _metadata(novel_data),
        [{
            "html": "<p>ok</p>",
            "epi_no": 101,
            "epi_title": "First / Chapter",
        }],
        fetch_image=lambda *args, **kwargs: None,
        update_mode=True,
    )

    stale = dict(episodes[0])
    stale["flag_detail_trans"] = 99
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, [stale], _metadata(novel_data)),
    )

    with pytest.raises(RuntimeError, match="The existing EPUB was left unchanged"):
        builder.build_epub(
            DummyFetchClient([{"error": "denied"}]),
            42,
            str(tmp_path),
            update_mode=True,
        )

    epub_path = tmp_path / "a-test-novel" / "a-test-novel.epub"
    with zipfile.ZipFile(epub_path) as archive:
        chapter = archive.read("EPUB/chap_0001.xhtml").decode("utf-8")
    assert "<p>ok</p>" in chapter


def test_build_txt_reuses_raw_episode_cache_in_update_mode(
    monkeypatch, tmp_path, novel_data, episodes
):
    monkeypatch.setattr(
        builder,
        "fetch_novel_and_episodes",
        lambda *args, **kwargs: (novel_data, episodes[:1], _metadata(novel_data)),
    )
    book_dir = tmp_path / "a-test-novel"
    cache_dir = book_dir / ".raw_cache"
    cache_dir.mkdir(parents=True)
    (book_dir / ".novel_id").write_text("42", encoding="utf-8")
    (cache_dir / "101.json").write_text(
        json.dumps({
            "html": "<p>cached body</p>",
            "epi_no": 101,
            "epi_title": "First / Chapter",
            "flag_detail_trans": 2,
        }),
        encoding="utf-8",
    )
    client = DummyFetchClient([])
    client.fetch_episodes_parallel = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("matching cache should skip the network")
    )

    output, title, count = builder.build_txt(
        client, 42, str(tmp_path), update_mode=True
    )

    assert output == str(book_dir)
    assert title == "A Test / Novel"
    assert count == 1
    assert (book_dir / "1_First _ Chapter.txt").read_text(encoding="utf-8") == "cached body"


DIRTY_HTML = (
    '<script>alert(1)</script>'
    '<p onclick="alert(2)">fresh</p>'
    '<img data-src="https://image.novelpia.com/x.png" src="about:blank">'
)


@pytest.mark.parametrize("update_mode", [False, True])
def test_load_or_fetch_sanitizes_fetched_html_once(tmp_path, episodes, update_mode):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    client = DummyFetchClient([{
        "html": DIRTY_HTML,
        "epi_no": 101,
        "epi_title": "First / Chapter",
    }])

    results = builder._load_or_fetch_episodes(
        client, episodes[:1], str(cache_dir), update_mode=update_mode
    )

    expected = html_from_episode_text(DIRTY_HTML)
    assert results[0]["html"] == expected
    assert "<script>" not in results[0]["html"]
    assert "onclick" not in results[0]["html"]
    assert 'src="https://image.novelpia.com/x.png"' in results[0]["html"]
    if update_mode:
        cached = json.loads((cache_dir / "101.json").read_text(encoding="utf-8"))
        assert cached["html"] == expected
    else:
        assert not (cache_dir / "101.json").exists()


def test_load_or_fetch_uses_cached_html_without_resanitizing(tmp_path, episodes):
    cache_dir = tmp_path / ".raw_cache"
    cache_dir.mkdir()
    # Comment would be stripped if html_from_episode_text ran again.
    cached_html = "<p>cached-as-is</p><!-- keep me -->"
    (cache_dir / "101.json").write_text(
        json.dumps({
            "html": cached_html,
            "epi_no": 101,
            "epi_title": "First / Chapter",
            "flag_detail_trans": 2,
        }),
        encoding="utf-8",
    )
    client = DummyFetchClient([])
    client.fetch_episodes_parallel = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("matching cache should skip the network")
    )

    results = builder._load_or_fetch_episodes(
        client, episodes[:1], str(cache_dir), update_mode=True
    )

    assert results[0]["html"] == cached_html
    assert "<!-- keep me -->" in results[0]["html"]
    assert "<!-- keep me -->" not in html_from_episode_text(cached_html)


def test_epub_builder_accepts_novel_metadata_without_reparsing(tmp_path, novel_data):
    metadata = _metadata(novel_data)
    output, title, count = EpubBuilder(str(tmp_path)).build(
        metadata,
        [{
            "html": "<p>already clean</p>",
            "epi_no": 101,
            "epi_title": "First / Chapter",
        }],
        fetch_image=lambda *args, **kwargs: None,
    )

    assert count == 1
    assert title == metadata.title
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("EPUB/chap_0001.xhtml").decode("utf-8")
    assert "<p>already clean</p>" in chapter
