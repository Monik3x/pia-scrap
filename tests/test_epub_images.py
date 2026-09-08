import hashlib
import json
import zipfile

from src.epub import (
    EpubBuilder,
    _is_safe_epub_image_name,
    _load_image_index,
    _write_image_index,
    image_digest,
)
from src.novel import parse_novel_metadata


PNG_A = b"\x89PNG\r\n\x1a\n" + b"payload-a" * 8
PNG_B = b"\x89PNG\r\n\x1a\n" + b"payload-b" * 8
IMAGE_A = "https://image.novelpia.com/a.png"
IMAGE_B = "https://image.novelpia.com/b.png"


def _chapters(html_by_episode, episode_list):
    return [
        {
            "html": html_by_episode[episode["episode_no"]],
            "epi_no": episode["episode_no"],
            "epi_title": episode["epi_title"],
        }
        for episode in episode_list
    ]


def _stub_images(payload_by_url):
    fetches = []

    def fetch_image(url, referer_url, episode_cookies=None, episode_no=None):
        fetches.append(url)
        return payload_by_url[url]

    return fetches, fetch_image


def _epub_names(epub_path):
    with zipfile.ZipFile(epub_path) as archive:
        return archive.namelist()


def _image_members(epub_path):
    return [
        name.replace("\\", "/")
        for name in _epub_names(epub_path)
        if "/images/" in name.replace("\\", "/")
    ]


def _build(tmp_path, novel_data, chapters, fetch_image, update_mode=False):
    return EpubBuilder(str(tmp_path)).build(
        parse_novel_metadata(novel_data, 42),
        chapters,
        fetch_image=fetch_image,
        update_mode=update_mode,
    )


def test_image_index_roundtrip_rejects_unsafe_and_invalid_entries(tmp_path):
    path = tmp_path / "image_index.json"
    _write_image_index(
        str(path),
        {
            IMAGE_A: {"sha256": "a" * 64, "file": "images/" + "a" * 64 + ".png"},
            "https://image.novelpia.com/bad.png": {
                "sha256": "b" * 64,
                "file": "images/../secret.png",
            },
        },
    )

    loaded = _load_image_index(str(path))
    assert IMAGE_A in loaded
    assert loaded[IMAGE_A]["file"].startswith("images/")
    assert "https://image.novelpia.com/bad.png" not in loaded
    assert _load_image_index(str(tmp_path / "missing.json")) == {}
    assert _is_safe_epub_image_name("cover.jpg")
    assert _is_safe_epub_image_name("cover.svg")
    assert not _is_safe_epub_image_name("images/../x.png")
    assert not _is_safe_epub_image_name("EPUB/images/x.png")


def test_update_reuses_images_from_epub_prefixed_zip_members(
    tmp_path, novel_data, episodes
):
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_B})
    digest = image_digest(PNG_A)
    book_dir = tmp_path / "a-test-novel"
    cache_dir = book_dir / ".raw_cache"
    cache_dir.mkdir(parents=True)
    (book_dir / ".novel_id").write_text("42", encoding="utf-8")
    file_name = f"images/{digest}.png"
    with zipfile.ZipFile(book_dir / "a-test-novel.epub", "w") as archive:
        archive.writestr(f"EPUB/{file_name}", PNG_A)
    _write_image_index(
        str(cache_dir / "image_index.json"),
        {IMAGE_A: {"sha256": digest, "file": file_name}},
    )

    output, _, _ = _build(
        tmp_path,
        novel_data,
        _chapters({101: f'<p><img src="{IMAGE_A}"/></p>'}, episodes[:1]),
        fetch_image,
        update_mode=True,
    )

    assert fetches == []
    members = _image_members(output)
    assert len(members) == 1
    with zipfile.ZipFile(output) as archive:
        assert archive.read(members[0]) == PNG_A


def test_epub_dedups_identical_images_by_content_hash(
    tmp_path, novel_data, episodes
):
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_A, IMAGE_B: PNG_A})
    html = f'<p><img src="{IMAGE_A}"/><img src="{IMAGE_B}"/></p>'
    output, _, count = _build(
        tmp_path,
        novel_data,
        _chapters({101: html}, episodes[:1]),
        fetch_image,
    )

    digest = image_digest(PNG_A)
    members = _image_members(output)
    assert count == 1
    assert fetches == [IMAGE_A, IMAGE_B]
    assert len(members) == 1
    assert members[0].endswith(f"/images/{digest}.png")
    with zipfile.ZipFile(output) as archive:
        chapter = [
            name for name in archive.namelist() if name.endswith("chap_0001.xhtml")
        ][0]
        xhtml = archive.read(chapter).decode("utf-8")
    assert xhtml.count(f"images/{digest}.png") == 2


def test_non_update_build_does_not_write_image_index(
    tmp_path, novel_data, episodes
):
    _, fetch_image = _stub_images({IMAGE_A: PNG_A})
    html = f'<p><img src="{IMAGE_A}"/></p>'
    _build(
        tmp_path,
        novel_data,
        _chapters({101: html}, episodes[:1]),
        fetch_image,
    )

    book_dir = tmp_path / "a-test-novel"
    assert not (book_dir / ".raw_cache" / "image_index.json").exists()
    assert not (book_dir / ".raw_cache" / "images").exists()


def test_update_stores_index_and_reuses_epub_images_without_refetch(
    tmp_path, novel_data, episodes
):
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_A})
    html = f'<p><img src="{IMAGE_A}"/></p>'
    chapters = _chapters({101: html}, episodes[:1])

    output, _, _ = _build(
        tmp_path, novel_data, chapters, fetch_image, update_mode=True
    )
    assert fetches == [IMAGE_A]

    book_dir = tmp_path / "a-test-novel"
    index_path = book_dir / ".raw_cache" / "image_index.json"
    assert index_path.exists()
    assert not (book_dir / ".raw_cache" / "images").exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["images"][IMAGE_A]["sha256"] == image_digest(PNG_A)
    assert index["images"][IMAGE_A]["file"] == f"images/{image_digest(PNG_A)}.png"

    fetches.clear()
    _build(tmp_path, novel_data, chapters, fetch_image, update_mode=True)
    assert fetches == []
    assert _image_members(output)[0].endswith(f"/images/{image_digest(PNG_A)}.png")


def test_update_migrates_legacy_url_cache_then_deletes_it(
    tmp_path, novel_data, episodes
):
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_B})
    book_dir = tmp_path / "a-test-novel"
    cache_dir = book_dir / ".raw_cache"
    image_dir = cache_dir / "images"
    image_dir.mkdir(parents=True)
    (book_dir / ".novel_id").write_text("42", encoding="utf-8")
    legacy_name = hashlib.sha256(IMAGE_A.encode("utf-8")).hexdigest() + ".bin"
    (image_dir / legacy_name).write_bytes(PNG_A)

    output, _, _ = _build(
        tmp_path,
        novel_data,
        _chapters({101: f'<p><img src="{IMAGE_A}"/></p>'}, episodes[:1]),
        fetch_image,
        update_mode=True,
    )

    assert fetches == []
    assert not image_dir.exists()
    index = json.loads((cache_dir / "image_index.json").read_text(encoding="utf-8"))
    assert index["images"][IMAGE_A]["sha256"] == image_digest(PNG_A)
    members = _image_members(output)
    assert len(members) == 1
    with zipfile.ZipFile(output) as archive:
        assert archive.read(members[0]) == PNG_A


def test_missing_or_non_numeric_epi_no_does_not_ticket_chapter_index(
    tmp_path, novel_data
):
    image_c = "https://image.novelpia.com/c.png"
    payloads = {IMAGE_A: PNG_A, IMAGE_B: PNG_B, image_c: PNG_A}
    calls = []

    def fetch_image(url, referer_url, episode_cookies=None, episode_no=None):
        calls.append((episode_no, referer_url))
        return payloads[url]

    _build(
        tmp_path,
        novel_data,
        [
            {
                "html": f'<p><img src="{IMAGE_A}"/></p>',
                "epi_title": "Missing id",
            },
            {
                "html": f'<p><img src="{IMAGE_B}"/></p>',
                "epi_no": "abc",
                "epi_title": "Bad id",
            },
            {
                "html": f'<p><img src="{image_c}"/></p>',
                "epi_no": 101,
                "epi_title": "Good id",
            },
        ],
        fetch_image,
    )

    assert calls == [
        (None, "https://global.novelpia.com/"),
        (None, "https://global.novelpia.com/"),
        (101, "https://global.novelpia.com/viewer/101"),
    ]


def test_successful_rebuild_deletes_legacy_images_even_if_some_imgs_dropped(
    tmp_path, novel_data, episodes
):
    image_c = "https://image.novelpia.com/c.png"
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_A, image_c: None})
    book_dir = tmp_path / "a-test-novel"
    cache_dir = book_dir / ".raw_cache"
    image_dir = cache_dir / "images"
    image_dir.mkdir(parents=True)
    (book_dir / ".novel_id").write_text("42", encoding="utf-8")
    a_name = hashlib.sha256(IMAGE_A.encode("utf-8")).hexdigest() + ".bin"
    c_name = hashlib.sha256(image_c.encode("utf-8")).hexdigest() + ".bin"
    (image_dir / a_name).write_bytes(PNG_A)
    (image_dir / c_name).write_bytes(b"")

    _build(
        tmp_path,
        novel_data,
        _chapters({101: f'<p><img src="{IMAGE_A}"/><img src="{image_c}"/></p>'}, episodes[:1]),
        fetch_image,
        update_mode=True,
    )

    assert fetches == [image_c]
    assert not image_dir.exists()
    index = json.loads((cache_dir / "image_index.json").read_text(encoding="utf-8"))
    assert IMAGE_A in index["images"]
    assert image_c not in index["images"]
