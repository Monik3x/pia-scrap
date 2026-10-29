import base64
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

import pytest

from src import reading_list


def _write_chapters(folder, records):
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    (folder / "chapters.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chapter(idx, episode_no, title):
    return {"idx": idx, "episode_no": episode_no, "title": title}


def test_write_bookmark_records_chapter_and_leaves_book_files(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    epub = folder / "succubus-system.epub"
    metadata = folder / "metadata.json"
    epub.write_bytes(b"PK\x03\x04fake-epub")
    metadata.write_text(
        json.dumps({"status": "Ongoing", "chapter": 1972}),
        encoding="utf-8",
    )
    epub_before = epub.read_bytes()
    metadata_before = metadata.read_bytes()

    reading_list.write_bookmark(str(folder), 40, 100040)

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 40, "episode_no": 100040}
    assert reading_list.read_bookmark(str(folder)) == {"idx": 40, "episode_no": 100040}
    assert epub.read_bytes() == epub_before
    assert metadata.read_bytes() == metadata_before


def test_resolve_uses_episode_no_when_idx_line_moved(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    records = [_chapter(i, 100000 + i, f"Chapter {i}") for i in range(1, 40)]
    records.append(_chapter(40, 1, "Wrong heading"))
    records.append(
        _chapter(41, 100040, "Dating My Celebrity Sister at the Karaoke (8)")
    )
    _write_chapters(folder, records)
    reading_list.write_bookmark(str(folder), 40, 100040)

    assert reading_list.resolve_bookmark(
        str(folder), {"idx": 40, "episode_no": 100040}
    ) == (41, 100040, "Dating My Celebrity Sister at the Karaoke (8)")
    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 41, "episode_no": 100040}


def test_chapter_heading_is_the_jsonl_title(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    records = [_chapter(i, 100000 + i, f"Chapter {i}") for i in range(1, 40)]
    records.append(
        _chapter(40, 100040, "Dating My Celebrity Sister at the Karaoke (8)")
    )
    _write_chapters(folder, records)

    assert reading_list.resolve_bookmark(
        str(folder), {"idx": 40, "episode_no": 100040}
    ) == (40, 100040, "Dating My Celebrity Sister at the Karaoke (8)")


def test_clear_bookmark_removes_the_sidecar(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    reading_list.write_bookmark(str(folder), 40, 100040)
    reading_list.clear_bookmark(str(folder))
    assert not (folder / "calibre_bookmarks.txt").exists()
    reading_list.clear_bookmark(str(folder))


def test_folders_for_label_uses_the_saved_map():
    mapping = {"succubus-system": "Reading", "other-book": "Dropped"}
    assert reading_list.folders_for_label(mapping, "Reading") == ["succubus-system"]


def test_reading_label_is_not_metadata_status(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    metadata = folder / "metadata.json"
    metadata.write_text(
        json.dumps({"status": "Ongoing", "chapter": 1972}),
        encoding="utf-8",
    )
    before = metadata.read_bytes()

    reading_list.set_label(str(tmp_path), "succubus-system", "Reading")

    saved = json.loads((tmp_path / "reading_labels.json").read_text(encoding="utf-8"))
    assert saved == {"succubus-system": "Reading"}
    assert metadata.read_bytes() == before


def _bookmark_bytes(pos, timestamp="2026-10-14T22:58:24+00:00"):
    payload = [{
        "pos": pos,
        "pos_type": "epubcfi",
        "timestamp": timestamp,
        "type": "last-read",
    }]
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"encoding=json+base64:\n{encoded}\n".encode("utf-8")


def _write_marked_epub(folder, member, pos, toc_title, href, timestamp="2026-10-14T22:58:24+00:00"):
    folder.mkdir(exist_ok=True)
    spine = [
        ("nav", "nav.xhtml"),
        ("about", "about.xhtml"),
        ("c1", "chap_0001.xhtml"),
        ("c2", "chap_0002.xhtml"),
        ("note", "notes.xhtml"),
    ]
    manifest = "\n".join(
        f'    <item id="{item_id}" href="{name}" media-type="application/xhtml+xml"/>'
        for item_id, name in spine
    )
    itemrefs = "\n".join(f'    <itemref idref="{item_id}"/>' for item_id, _name in spine)
    opf = (
        '<?xml version="1.0"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">\n'
        "  <manifest>\n"
        f"{manifest}\n"
        "  </manifest>\n"
        "  <spine>\n"
        f"{itemrefs}\n"
        "  </spine>\n"
        "</package>\n"
    )
    nav = (
        '<?xml version="1.0"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc">'
        f'<a href="{href}">{toc_title}</a>'
        "</nav></body></html>\n"
    )
    epub_path = folder / f"{folder.name}.epub"
    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("EPUB/content.opf", opf)
        archive.writestr("EPUB/nav.xhtml", nav)
        archive.writestr(member, _bookmark_bytes(pos, timestamp))
    return epub_path


@pytest.mark.parametrize("member,pos", [
    ("META-INF/calibre_bookmarks.txt", "epubcfi(/8/2/4/1:0)"),
    ("META-INF/calibre_bookmarks.txt", "epubcfi(/6/8!/4/2/1:0)"),
])
def test_epub_bookmark_updates_progress_without_a_calibre_library(tmp_path, member, pos):
    folder = tmp_path / "succubus-system"
    epub_path = _write_marked_epub(folder, member, pos, "Chapter 9", "chap_0002.xhtml")
    before = epub_path.read_bytes()
    _write_chapters(folder, [
        _chapter(2, 200, "Warm"),
        _chapter(9, 900, "Nine"),
    ])

    reading_list.sync_embedded_progress(str(folder))

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 2, "episode_no": 200}
    assert epub_path.read_bytes() == before


def test_toc_title_matches_the_chapter_after_an_insert(tmp_path):
    folder = tmp_path / "succubus-system"
    _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/8/2/4/1:0)",
        "Warm",
        "chap_0002.xhtml",
    )
    _write_chapters(folder, [
        _chapter(2, 200, "Other"),
        _chapter(5, 500, "Warm"),
    ])

    reading_list.sync_embedded_progress(str(folder))

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 5, "episode_no": 500}


def test_toc_heading_chapter_number_selects_the_idx(tmp_path):
    folder = tmp_path / "succubus-system"
    _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/10/2/4/1:0)",
        "Chapter 4",
        "notes.xhtml",
    )
    _write_chapters(folder, [
        _chapter(2, 200, "Warm"),
        _chapter(4, 40, "Something Else"),
    ])

    reading_list.sync_embedded_progress(str(folder))

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 4, "episode_no": 40}


def test_newer_sidecar_beats_an_older_epub_bookmark(tmp_path):
    folder = tmp_path / "succubus-system"
    _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/8/2/4/1:0)",
        "Warm",
        "chap_0002.xhtml",
        timestamp="2020-01-01T00:00:00+00:00",
    )
    _write_chapters(folder, [_chapter(2, 200, "Warm")])
    reading_list.write_bookmark(str(folder), 1, 111)
    later = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(folder / "calibre_bookmarks.txt", (later, later))

    reading_list.sync_embedded_progress(str(folder))

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 1, "episode_no": 111}


def test_ambiguous_toc_heading_does_not_write_progress(tmp_path):
    folder = tmp_path / "succubus-system"
    epub_path = _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/10/2/4/1:0)",
        "Notes",
        "notes.xhtml",
    )
    before = epub_path.read_bytes()
    _write_chapters(folder, [
        _chapter(2, 200, "Notes"),
        _chapter(4, 40, "Notes"),
    ])

    reading_list.sync_embedded_progress(str(folder))

    assert not (folder / "calibre_bookmarks.txt").exists()
    assert epub_path.read_bytes() == before


def test_last_chapter_is_the_final_jsonl_record(tmp_path):
    folder = tmp_path / "succubus-system"
    folder.mkdir()
    _write_chapters(folder, [
        _chapter(1, 101, "First"),
        _chapter(2, 102, "Epilogue"),
    ])

    assert reading_list.read_last_chapter(str(folder))["title"] == "Epilogue"


def test_set_label_leaves_unreadable_file_unchanged(tmp_path):
    path = tmp_path / "reading_labels.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(reading_list.LabelsUnreadable):
        reading_list.set_label(str(tmp_path), "succubus-system", "Reading")
    assert path.read_text(encoding="utf-8") == "{"


def _restore_stat(path, stat):
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _sync_in_fresh_process(folder):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = "from src import reading_list\nreading_list.sync_embedded_progress(%r)\n" % str(folder)
    env = os.environ.copy()
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_flushed_check_restores_progress_when_the_epub_bytes_change(tmp_path):
    folder = tmp_path / "succubus-system"
    epub = _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/8/2/4/1:0)",
        "Warm",
        "chap_0002.xhtml",
    )
    _write_chapters(folder, [_chapter(2, 200, "Warm")])
    reading_list.sync_embedded_progress(str(folder))
    reading_list.flush_embedded_cache()
    sidecar = folder / "calibre_bookmarks.txt"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {"idx": 2, "episode_no": 200}
    sidecar.unlink()
    stat = epub.stat()
    epub.write_bytes(b"\x00" * stat.st_size)
    _restore_stat(epub, stat)

    _sync_in_fresh_process(folder)

    assert json.loads(sidecar.read_text(encoding="utf-8")) == {"idx": 2, "episode_no": 200}


def test_unreadable_epub_is_tried_again_on_the_next_launch(tmp_path):
    folder = tmp_path / "succubus-system"
    epub = _write_marked_epub(
        folder,
        "META-INF/calibre_bookmarks.txt",
        "epubcfi(/8/2/4/1:0)",
        "Warm",
        "chap_0002.xhtml",
    )
    _write_chapters(folder, [_chapter(2, 200, "Warm")])
    original = epub.read_bytes()
    stat = epub.stat()
    epub.write_bytes(b"\x00" * len(original))
    _restore_stat(epub, stat)
    reading_list.sync_embedded_progress(str(folder))
    reading_list.flush_embedded_cache()
    epub.write_bytes(original)
    _restore_stat(epub, stat)

    _sync_in_fresh_process(folder)

    saved = json.loads((folder / "calibre_bookmarks.txt").read_text(encoding="utf-8"))
    assert saved == {"idx": 2, "episode_no": 200}


def test_sync_does_not_overwrite_a_concurrent_manual_bookmark(tmp_path, monkeypatch):
    import threading

    reading_list.write_bookmark(str(tmp_path), 1, 101)
    monkeypatch.setattr(reading_list, "_locate_embedded_progress", lambda _: (2, 102, 9999999999))
    entered, release, manual_started = threading.Event(), threading.Event(), threading.Event()
    original = reading_list.write_bookmark

    def paused_write(folder, idx, episode_no):
        if idx == 2:
            entered.set()
            assert release.wait(5)
        original(folder, idx, episode_no)

    monkeypatch.setattr(reading_list, "write_bookmark", paused_write)
    sync = threading.Thread(target=reading_list.sync_embedded_progress, args=(str(tmp_path),))
    sync.start()
    assert entered.wait(5)

    def manual():
        manual_started.set()
        reading_list.write_bookmark(str(tmp_path), 3, 103)

    edit = threading.Thread(target=manual)
    edit.start()
    assert manual_started.wait(5)
    edit.join(0.2)
    release.set()
    sync.join(5)
    edit.join(5)
    assert reading_list.read_bookmark(str(tmp_path)) == {"idx": 3, "episode_no": 103}


def test_batch_labels_write_once_and_leave_existing_map_on_failure(tmp_path, monkeypatch):
    reading_list.write_labels(str(tmp_path), {"A": "Reading", "B": "Reading"})
    before = (tmp_path / "reading_labels.json").read_bytes()
    def fail(root, mapping):
        assert mapping == {"A": "Completed", "B": "Completed"}
        raise OSError("disk full")
    monkeypatch.setattr(reading_list, "write_labels", fail)
    with pytest.raises(OSError):
        reading_list.set_labels(str(tmp_path), ["A", "B"], "Completed")
    assert (tmp_path / "reading_labels.json").read_bytes() == before


def test_batch_labels_updates_every_folder_in_one_write(tmp_path, monkeypatch):
    reading_list.write_labels(str(tmp_path), {"A": "Reading", "B": "Reading", "C": "Dropped"})
    original = reading_list.write_labels
    written = []
    def record(root, mapping):
        written.append(dict(mapping))
        original(root, mapping)
    monkeypatch.setattr(reading_list, "write_labels", record)
    reading_list.set_labels(str(tmp_path), ["A", "B"], "Completed")
    expected = {"A": "Completed", "B": "Completed", "C": "Dropped"}
    assert written == [expected]
    assert reading_list.read_labels(str(tmp_path)) == expected
