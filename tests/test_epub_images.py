import json
import zipfile

from src.epub import EpubBuilder, image_digest
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


def test_update_reuses_safe_index_entries_and_refetches_unsafe_paths(
    tmp_path, novel_data, episodes
):
    traversal_url = "https://image.novelpia.com/bad.png"
    prefixed_url = "https://image.novelpia.com/prefixed.png"
    good_digest = image_digest(PNG_A)
    stolen_digest = image_digest(PNG_B)
    book_dir = tmp_path / "a-test-novel"
    cache_dir = book_dir / ".raw_cache"
    cache_dir.mkdir(parents=True)
    (book_dir / ".novel_id").write_text("42", encoding="utf-8")
    good_name = f"images/{good_digest}.png"
    with zipfile.ZipFile(book_dir / "a-test-novel.epub", "w") as archive:
        archive.writestr(f"EPUB/{good_name}", PNG_A)
        archive.writestr("EPUB/images/../secret.png", PNG_B)
    (cache_dir / "image_index.json").write_text(
        json.dumps({
            "version": 1,
            "images": {
                IMAGE_A: {"sha256": good_digest, "file": good_name},
                traversal_url: {
                    "sha256": stolen_digest,
                    "file": "images/../secret.png",
                },
                prefixed_url: {
                    "sha256": good_digest,
                    "file": f"EPUB/{good_name}",
                },
            },
        }),
        encoding="utf-8",
    )
    fetches, fetch_image = _stub_images({
        IMAGE_A: PNG_A,
        traversal_url: PNG_A,
        prefixed_url: PNG_B,
    })

    output, _, _ = _build(
        tmp_path,
        novel_data,
        _chapters(
            {101: (
                f'<p><img src="{IMAGE_A}"/>'
                f'<img src="{traversal_url}"/>'
                f'<img src="{prefixed_url}"/></p>'
            )},
            episodes[:1],
        ),
        fetch_image,
        update_mode=True,
    )

    assert fetches == [traversal_url, prefixed_url]
    written = json.loads((cache_dir / "image_index.json").read_text(encoding="utf-8"))
    assert written["images"][IMAGE_A]["file"] == good_name
    for url in (traversal_url, prefixed_url):
        file_name = written["images"][url]["file"]
        assert ".." not in file_name
        assert not file_name.startswith("EPUB/")
    with zipfile.ZipFile(output) as archive:
        assert all(".." not in name for name in archive.namelist())
        stored = [archive.read(name) for name in _image_members(output)]
    assert PNG_A in stored
    assert PNG_B in stored


def test_epub_dedups_identical_images_by_content_hash(
    tmp_path, novel_data, episodes
):
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_A, IMAGE_B: PNG_A})
    html = f'<p><img src="{IMAGE_A}"/><img src="{IMAGE_B}"/></p>'
    output, _, _ = _build(
        tmp_path,
        novel_data,
        _chapters({101: html}, episodes[:1]),
        fetch_image,
    )

    digest = image_digest(PNG_A)
    members = _image_members(output)
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
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["images"][IMAGE_A]["sha256"] == image_digest(PNG_A)
    assert index["images"][IMAGE_A]["file"] == f"images/{image_digest(PNG_A)}.png"

    fetches.clear()
    output, _, _ = _build(
        tmp_path, novel_data, chapters, fetch_image, update_mode=True
    )
    assert fetches == []
    members = _image_members(output)
    assert len(members) == 1
    with zipfile.ZipFile(output) as archive:
        assert archive.read(members[0]) == PNG_A


def test_update_reuses_svg_cover_recorded_in_the_index(tmp_path, novel_data):
    cover_url = "https://image.novelpia.com/cover.svg"
    svg = b"<svg xmlns='x'></svg>"
    payload = json.loads(json.dumps(novel_data))
    payload["result"]["novel"]["novel_img"] = cover_url
    fetches, fetch_image = _stub_images({cover_url: svg})

    _build(tmp_path, payload, [], fetch_image, update_mode=True)
    assert fetches == [cover_url]
    index = json.loads(
        (tmp_path / "a-test-novel" / ".raw_cache" / "image_index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["images"][cover_url]["file"] == "cover.svg"

    fetches.clear()
    output, _, _ = _build(tmp_path, payload, [], fetch_image, update_mode=True)

    assert fetches == []
    with zipfile.ZipFile(output) as archive:
        cover_names = [name for name in archive.namelist() if name.endswith("cover.svg")]
        assert len(cover_names) == 1
        assert archive.read(cover_names[0]) == svg


def test_dropped_images_are_omitted_from_image_index(
    tmp_path, novel_data, episodes
):
    image_c = "https://image.novelpia.com/c.png"
    fetches, fetch_image = _stub_images({IMAGE_A: PNG_A, image_c: None})

    _build(
        tmp_path,
        novel_data,
        _chapters({101: f'<p><img src="{IMAGE_A}"/><img src="{image_c}"/></p>'}, episodes[:1]),
        fetch_image,
        update_mode=True,
    )

    assert fetches == [IMAGE_A, image_c]
    index = json.loads(
        (tmp_path / "a-test-novel" / ".raw_cache" / "image_index.json").read_text(
            encoding="utf-8"
        )
    )
    assert IMAGE_A in index["images"]
    assert image_c not in index["images"]
