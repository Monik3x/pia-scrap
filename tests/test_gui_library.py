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


class Widget:
    def __init__(self, value=""):
        self.value = value
        self.state = None

    def get(self):
        return self.value

    def delete(self, *_):
        self.value = ""

    def insert(self, _, value):
        self.value = str(value)

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)


class Tree:
    def __init__(self):
        self.rows = {}
        self.selected = ()

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *ids):
        for row_id in ids:
            del self.rows[row_id]
        self.selected = ()

    def heading(self, *args, **kwargs):
        pass

    def insert(self, parent, end, iid, values):
        self.rows[iid] = values

    def item(self, iid, values):
        self.rows[iid] = values

    def selection(self):
        return self.selected

    def selection_set(self, ids):
        self.selected = tuple(ids)


def library_view(out_dir):
    view = SimpleNamespace(
        _library_generation=0, _page_rows={}, _library_cache_lock=threading.Lock(),
        _library_row_cache={}, _label_map={}, _library_jobs=queue.Queue(),
        filtered_dirs=["Book", "Other"], current_page=0, items_per_page=10,
        output_entry=Widget(str(out_dir)), chapter_entry=Widget(), library_tree=Tree(),
        pag_label=Widget(), prev_btn=Widget(), next_btn=Widget(),
        library_update_btn=Widget(), library_set_btn=Widget(),
        _hide_library_preview=lambda: None, _show_library_table=lambda visible: None,
    )
    for name in (
        "display_current_page", "_cached_library_row", "_library_cache_id",
        "_page_row_info", "_library_values", "_on_library_select", "_selected_novel_ids",
        "_apply_library_row", "_update_library_actions",
    ):
        setattr(view, name, getattr(PiaScrapGUI, name).__get__(view))
    return view


def test_library_refresh_preserves_selection_and_typed_chapter(tmp_path):
    view = library_view(tmp_path)
    view.display_current_page()
    view.library_tree.selection_set(["row-0"])
    view._on_library_select()
    view.chapter_entry.insert(0, "37")
    row = {"folder": "Book", "novel_id": 42, "chapter_count": 100,
           "has_metadata": True, "has_chapters": True, "progress_idx": 3,
           "title": "A Book", "progress": "c3", "latest": "c100", "status": ""}
    view._apply_library_row(view._library_generation, row)
    assert view.chapter_entry.get() == "37"
    assert view.library_tree.rows["row-0"][0] == "A Book"
    assert view.library_update_btn.state == "normal"
    assert view.library_set_btn.state == "normal"
    view._apply_library_row(view._library_generation - 1, dict(row, title="Old"))
    assert view.library_tree.rows["row-0"][0] == "A Book"

    view.filtered_dirs = ["Other", "Book"]
    view.display_current_page()
    view._on_library_select()
    assert view.library_tree.selection() == ("row-1",)
    assert view.chapter_entry.get() == "37"

    view.library_tree.selection_set(["row-0"])
    view._on_library_select()
    assert view.chapter_entry.get() == ""
    assert view.library_set_btn.state == "disabled"

    view.library_tree.selection_set(["row-0", "row-1"])
    view._on_library_select()
    assert view.library_set_btn.state == "disabled"
    view.filtered_dirs = []
    view.display_current_page()
    assert view.library_tree.selection() == ()
    assert view.library_update_btn.state == "disabled"
    assert view.library_set_btn.state == "disabled"


def _loaded_row(folder, progress_idx):
    return {
        "folder": folder, "novel_id": 42, "chapter_count": 100,
        "has_metadata": True, "has_chapters": True, "progress_idx": progress_idx,
        "title": folder, "progress": f"c{progress_idx}" if progress_idx else "",
        "latest": "c100", "status": "",
    }


def test_arriving_row_fills_empty_chapter_box_only_for_the_sole_selection(tmp_path):
    view = library_view(tmp_path)
    view.display_current_page()
    view.library_tree.selection_set(["row-0"])
    view._on_library_select()
    assert view.chapter_entry.get() == ""
    book = _loaded_row("Book", "4")
    view._apply_library_row(view._library_generation - 1, book)
    assert view.chapter_entry.get() == ""
    view._apply_library_row(view._library_generation, _loaded_row("Book", ""))
    assert view.chapter_entry.get() == ""
    view._apply_library_row(view._library_generation, book)
    assert view.chapter_entry.get() == "4"

    view.chapter_entry.delete(0, "end")
    view.library_tree.selection_set(["row-1"])
    view._on_library_select()
    view._apply_library_row(view._library_generation, book)
    assert view.chapter_entry.get() == ""
    view._apply_library_row(view._library_generation, _loaded_row("Other", "9"))
    assert view.chapter_entry.get() == "9"

    view.chapter_entry.delete(0, "end")
    view.library_tree.selection_set(["row-0", "row-1"])
    view._on_library_select()
    view._apply_library_row(view._library_generation, book)
    view._apply_library_row(view._library_generation, _loaded_row("Other", "9"))
    assert view.chapter_entry.get() == ""


def test_set_disabled_until_chapter_data_is_loaded(tmp_path):
    folder = tmp_path / "Book"
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({"title": "Book", "chapter": 10}), encoding="utf-8")
    loader = library_loader()
    view = library_view(tmp_path)
    view.display_current_page()
    view.library_tree.selection_set(["row-0"])
    view._on_library_select()
    assert view.library_set_btn.state == "disabled"
    view._apply_library_row(view._library_generation, PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book"))
    assert view.library_set_btn.state == "disabled"
    (folder / "chapters.jsonl").write_text(json.dumps({"idx": 1, "episode_no": 100}) + "\n", encoding="utf-8")
    view._apply_library_row(view._library_generation, PiaScrapGUI._load_library_row(loader, str(tmp_path), "Book"))
    assert view.library_set_btn.state == "normal"


def test_edit_jobs_survive_page_refreshes(tmp_path):
    from src import reading_list

    folder = tmp_path / "Book"
    folder.mkdir()
    (folder / "chapters.jsonl").write_text('{"idx": 1, "episode_no": 101}\n', encoding="utf-8")
    view = library_loader()
    view._library_jobs = queue.Queue()
    view.gui_queue = queue.Queue()
    view._library_generation = 3
    view._load_library_row = lambda *_: None
    view._library_edit = PiaScrapGUI._library_edit.__get__(view)
    view.logger = SimpleNamespace(warning=lambda _: None)
    view._library_jobs.put(("page", 1, str(tmp_path), ["Book"]))
    view._library_jobs.put(("chapter", str(tmp_path), ["Book"], 1))
    view._library_jobs.put(("page", 2, str(tmp_path), ["Book"]))
    view._library_jobs.put(("labels", str(tmp_path), ["Book"], "Reading"))
    view._library_jobs.put(("page", 3, str(tmp_path), ["Book"]))
    threading.Thread(target=PiaScrapGUI._library_loader, args=(view,), daemon=True).start()
    assert view.gui_queue.get(timeout=5) == ("library_edit_done", ("chapter", str(tmp_path), None))
    assert view.gui_queue.get(timeout=5) == ("library_edit_done", ("labels", str(tmp_path), {"Book": "Reading"}))
    assert reading_list.read_bookmark(str(folder)) == {"idx": 1, "episode_no": 101}
    assert reading_list.read_labels(str(tmp_path)) == {"Book": "Reading"}


def test_failed_label_batch_keeps_mapping_and_selection(tmp_path, monkeypatch):
    from src import reading_list

    reading_list.write_labels(str(tmp_path), {"A": "Reading", "B": "Reading"})
    selected = ("row-0", "row-1")
    view = SimpleNamespace(
        move_var=SimpleNamespace(set=lambda _: None),
        library_tree=SimpleNamespace(selection=lambda: selected),
        output_entry=SimpleNamespace(get=lambda: str(tmp_path)),
        _page_rows={"row-0": {"folder": "A"}, "row-1": {"folder": "B"}},
        _library_jobs=queue.Queue(), gui_queue=queue.Queue(),
    )
    PiaScrapGUI._move_selected(view, "Completed")
    def fail(*_):
        raise OSError("disk full")
    monkeypatch.setattr(reading_list, "write_labels", fail)
    kind, *args = view._library_jobs.get_nowait()
    PiaScrapGUI._library_edit(view, kind, *args)
    assert view.gui_queue.get_nowait() == ("library_edit_error", ("Label", "disk full"))
    assert view.library_tree.selection() == selected
    assert reading_list.read_labels(str(tmp_path)) == {"A": "Reading", "B": "Reading"}
