import threading
import io
import json
import queue
import zipfile
from types import SimpleNamespace

import pytest

from gui import PiaScrapGUI
from PIL import Image


def library_loader():
    loader = SimpleNamespace(
        _library_cache_lock=threading.Lock(), _library_row_cache={}
    )
    for name in (
        "_library_file_key", "_library_cache_id", "_progress_line", "_latest_marker",
    ):
        setattr(loader, name, getattr(PiaScrapGUI, name).__get__(loader))
    return loader


def test_refresh_receives_row_cached_by_previous_generation(tmp_path):
    folder = tmp_path / "Book"
    folder.mkdir()
    (folder / ".novel_id").write_text("42", encoding="utf-8")
    loader = library_loader()
    first = PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book")
    refreshed = PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book")
    assert refreshed == first
    assert refreshed["novel_id"] == 42


def test_refresh_reloads_retired_book_identity(tmp_path):
    folder = tmp_path / "Book"
    folder.mkdir()
    (folder / ".novel_id").write_text("42", encoding="utf-8")
    loader = library_loader()
    assert PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book")["novel_id"] == 42
    (folder / ".retired").write_text("retired", encoding="utf-8")
    row = PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book")
    assert row is not None
    assert row["novel_id"] is None


@pytest.mark.parametrize(
    ("query", "label", "expected"),
    [
        ("The Book", "All", ["the-book", "The Book"]),
        ("THE BOOK", "Reading", ["the-book"]),
        ("The: Book", "All", ["the-book"]),
        ("BOOK", "All", ["the-book", "The Book"]),
        ("[", "All", ["[Draft]"]),
    ],
)
def test_library_search_matches_multiword_titles_in_folder_names(query, label, expected):
    view = SimpleNamespace(
        output_entry=SimpleNamespace(get=lambda: "output"),
        search_var=SimpleNamespace(get=lambda: query),
        library_dirs=["the-book", "The Book", "other-story", "[Draft]"],
        label_filter=label,
        current_page=3,
        _labels_for_view=lambda _: {"the-book": "Reading"},
        _sync_label_filter=lambda _: None,
        display_current_page=lambda: None,
    )

    PiaScrapGUI._apply_library_query(view, reset_page=True)

    assert view.filtered_dirs == expected
    assert view.current_page == 0


def test_preview_reads_saved_description_and_epub_cover(tmp_path):
    from src.library_preview import load_book_preview

    (tmp_path / "metadata.json").write_text(json.dumps({
        "title": "A Novel", "author": "Writer", "description": "First line\nSecond line",
    }), encoding="utf-8")
    image = io.BytesIO()
    Image.new("RGB", (300, 450), "red").save(image, format="PNG")
    with zipfile.ZipFile(tmp_path / (tmp_path.name + ".epub"), "w") as archive:
        archive.writestr("EPUB/cover.png", image.getvalue())
        archive.writestr("EPUB/images/other.png", b"not the cover")

    preview = load_book_preview(str(tmp_path))

    assert preview["title"] == "A Novel"
    assert preview["author"] == "Writer"
    assert preview["description"] == "First line\nSecond line"
    assert preview["cover"].size == (140, 210)
    assert preview["cover"].getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("metadata", ["not json", "[]", '{"description": null}'])
def test_preview_survives_missing_or_damaged_local_data(tmp_path, metadata):
    from src.library_preview import load_book_preview

    (tmp_path / "metadata.json").write_text(metadata, encoding="utf-8")
    (tmp_path / "broken.epub").write_bytes(b"broken zip")
    preview = load_book_preview(str(tmp_path))
    assert preview["title"] == tmp_path.name
    assert preview["description"] == "No description saved."
    assert preview["cover"] is None


def test_open_book_reports_missing_epub_to_gui(tmp_path):
    view = SimpleNamespace(gui_queue=queue.Queue())
    PiaScrapGUI._launch_library_epub(view, str(tmp_path))
    task, message = view.gui_queue.get_nowait()
    assert task == "library_open_error"
    assert "No EPUB found" in message
