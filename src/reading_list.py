"""Chapter progress and reading labels for the local library."""

import base64
import json
import os
import posixpath
import re
import threading
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from src.helper import write_json_atomic

BOOKMARK_FILENAME = "calibre_bookmarks.txt"
LABELS_FILENAME = "reading_labels.json"
BUILTIN_LABELS = ("Reading", "Plan to read", "On hold", "Dropped", "Completed")
# A slash cannot be a folder name, so the saved name list can share the folder map.
# labels/custom is an older extras-only list. labels/names is the full list.
CUSTOM_LABELS_KEY = "labels/custom"
LABEL_NAMES_KEY = "labels/names"
MANAGE_LABELS_COMMAND = "Manage labels..."
RESERVED_LABELS = frozenset({
    "All", "Unlabeled", "Move to", "Clear label",
    MANAGE_LABELS_COMMAND,
})
LABEL_NAME_LIMIT = 40
EMBEDDED_BOOKMARK = "META-INF/calibre_bookmarks.txt"
EMBEDDED_CHAPTERS = "META-INF/novelpia-chapters.json"

_CFI_STEP = re.compile(r"/(\d+)")
_EMBED_CACHE: Dict[tuple, Optional[Tuple[int, int, Optional[float]]]] = {}
_DISK_MISS = object()
_UNRESOLVED = object()
_progress_lock = threading.RLock()
_label_lock = threading.RLock()
_disk_cache = {"path": None, "entries": {}, "dirty": False}
_embed_cache_lock = threading.RLock()


class _EmbeddedUnreadable(Exception):
    pass


class LabelsUnreadable(ValueError):
    """reading_labels.json exists and is not a map of folder name to label."""


class LabelNameError(ValueError):
    """A custom label name is empty, reserved, built in, or already used."""


def _labels_path(root: str) -> str:
    return os.path.join(os.fspath(root), LABELS_FILENAME)


def _bookmark_path(folder: str) -> str:
    return os.path.join(os.fspath(folder), BOOKMARK_FILENAME)


def _label_file_error() -> LabelsUnreadable:
    return LabelsUnreadable(f"{LABELS_FILENAME} could not be read.")


def normalize_label_name(name: str) -> str:
    text = " ".join(name.split()) if isinstance(name, str) else ""
    if not text:
        raise LabelNameError("Enter a label name.")
    if len(text) > LABEL_NAME_LIMIT:
        raise LabelNameError("Use 40 characters or fewer.")
    if matching_label(text, RESERVED_LABELS):
        raise LabelNameError("That name is reserved.")
    return text


def same_label(saved, chosen) -> bool:
    if not isinstance(saved, str) or not isinstance(chosen, str):
        return False
    return " ".join(saved.split()).casefold() == " ".join(chosen.split()).casefold()


def matching_label(name: str, names) -> Optional[str]:
    folded = name.casefold() if isinstance(name, str) else ""
    for item in names:
        if isinstance(item, str) and item.casefold() == folded:
            return item
    return None


def _clean_name_list(raw) -> List[str]:
    if not isinstance(raw, list):
        raise _label_file_error()
    names = []
    seen = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise _label_file_error()
        text = " ".join(item.split())
        folded = text.casefold()
        if not text or folded in seen or matching_label(text, RESERVED_LABELS):
            continue
        seen.add(folded)
        names.append(text)
    return names


def _append_missing(names: List[str], values) -> List[str]:
    saved = list(names)
    seen = {name.casefold() for name in saved}
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        folded = text.casefold()
        if not text or folded in seen or matching_label(text, RESERVED_LABELS):
            continue
        seen.add(folded)
        saved.append(text)
    return saved


def _implicit_names(folders: Dict[str, str]) -> List[str]:
    return _append_missing(list(BUILTIN_LABELS), folders.values())


def _read_label_file(root: str):
    path = _labels_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}, list(BUILTIN_LABELS)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _label_file_error() from exc
    if not isinstance(payload, dict):
        raise _label_file_error()
    folders: Dict[str, str] = {}
    for folder, label in payload.items():
        if folder in (CUSTOM_LABELS_KEY, LABEL_NAMES_KEY):
            continue
        if not isinstance(folder, str) or not isinstance(label, str) or not label.strip():
            raise _label_file_error()
        folders[folder] = label
    if LABEL_NAMES_KEY in payload:
        names = _clean_name_list(payload[LABEL_NAMES_KEY])
    elif CUSTOM_LABELS_KEY in payload:
        names = _append_missing(list(BUILTIN_LABELS), _clean_name_list(payload[CUSTOM_LABELS_KEY]))
    else:
        names = list(BUILTIN_LABELS)
    return folders, _append_missing(names, folders.values())


def _write_saved_labels(root: str, folders: Dict[str, str], names: List[str]) -> None:
    payload = dict(folders)
    if list(names) != _implicit_names(folders):
        payload[LABEL_NAMES_KEY] = list(names)
    write_json_atomic(_labels_path(root), payload)


def read_labels(root: str) -> Dict[str, str]:
    with _label_lock:
        folders, _custom = _read_label_file(root)
        return folders


def custom_labels(root: str) -> List[str]:
    with _label_lock:
        _folders, custom = _read_label_file(root)
        return custom


def write_labels(root: str, mapping: Dict[str, str], custom: Optional[List[str]] = None) -> None:
    with _label_lock:
        folders = {
            folder: label
            for folder, label in mapping.items()
            if folder not in (CUSTOM_LABELS_KEY, LABEL_NAMES_KEY)
        }
        if custom is None:
            _existing, custom = _read_label_file(root)
        _write_saved_labels(root, folders, custom)


def set_labels(root: str, folders, label: str) -> Dict[str, str]:
    with _label_lock:
        mapping, custom = _read_label_file(root)
        text = " ".join(label.split()) if isinstance(label, str) else ""
        for folder in folders:
            if text:
                mapping[folder] = text
            else:
                mapping.pop(folder, None)
        if text and matching_label(text, RESERVED_LABELS) is None and matching_label(text, custom) is None:
            custom = [*custom, text]
        write_labels(root, mapping, custom)
        return mapping


def set_label(root: str, folder: str, label: str) -> Dict[str, str]:
    return set_labels(root, [folder], label)


def add_custom_label(root: str, name: str) -> List[str]:
    with _label_lock:
        mapping, custom = _read_label_file(root)
        text = normalize_label_name(name)
        if matching_label(text, custom):
            raise LabelNameError("That label already exists.")
        write_labels(root, mapping, [*custom, text])
        _folders, saved = _read_label_file(root)
        return saved


def rename_custom_label(root: str, old: str, new: str) -> Dict[str, str]:
    with _label_lock:
        mapping, custom = _read_label_file(root)
        current = matching_label(old, custom)
        if current is None:
            raise LabelNameError("That label does not exist.")
        text = normalize_label_name(new)
        others = [name for name in custom if name.casefold() != current.casefold()]
        if matching_label(text, others):
            raise LabelNameError("That label already exists.")
        mapping = {
            folder: text if same_label(value, current) else value
            for folder, value in mapping.items()
        }
        renamed = [text if name.casefold() == current.casefold() else name for name in custom]
        write_labels(root, mapping, renamed)
        return mapping


def delete_custom_label(root: str, name: str) -> Dict[str, str]:
    with _label_lock:
        mapping, custom = _read_label_file(root)
        current = matching_label(name, custom)
        if current is None:
            raise LabelNameError("That label does not exist.")
        mapping = {
            folder: value
            for folder, value in mapping.items()
            if not same_label(value, current)
        }
        remaining = [item for item in custom if item.casefold() != current.casefold()]
        write_labels(root, mapping, remaining)
        return mapping


def progress_transaction():
    return _progress_lock


def folders_for_label(mapping: Dict[str, str], label: str) -> List[str]:
    return [folder for folder, value in mapping.items() if value == label]


def read_bookmark(folder: str) -> Optional[Dict[str, int]]:
    path = _bookmark_path(folder)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        idx = int(payload["idx"])
        episode_no = int(payload["episode_no"])
    except (KeyError, TypeError, ValueError):
        return None
    if idx < 1:
        return None
    return {"idx": idx, "episode_no": episode_no}


def write_bookmark(folder: str, idx: int, episode_no: int) -> None:
    with progress_transaction():
        write_json_atomic(
            _bookmark_path(folder),
            {"idx": int(idx), "episode_no": int(episode_no)},
        )


def clear_bookmark(folder: str) -> None:
    with progress_transaction():
        try:
            os.remove(_bookmark_path(folder))
        except FileNotFoundError:
            pass


def surviving_episode_index(old, indices, index):
    candidates = [old[index]] + old[index + 1:] + list(reversed(old[:index]))
    return next((indices[episode] for episode in candidates if episode in indices), None)


def bookmark_snapshot(folder: str):
    path = _bookmark_path(folder)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
            stat = os.fstat(handle.fileno())
        return data, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def publish_bookmark_if_unchanged(folder: str, snapshot, bookmark) -> bool:
    with progress_transaction():
        if bookmark_snapshot(folder) != snapshot:
            return False
        if bookmark is not None and read_bookmark(folder) != bookmark:
            write_bookmark(folder, bookmark["idx"], bookmark["episode_no"])
        return True


def bookmark_for_rebuild(folder: str, old, new):
    current = read_bookmark(folder)
    try:
        located = _locate_embedded_progress(folder)
    except (OSError, UnicodeError, ValueError):
        located = None
    if located is not None:
        idx, episode_no, timestamp = located
        embedded = {"idx": idx, "episode_no": episode_no}
        if current is None or (current != embedded and _embedded_is_newer(folder, timestamp)):
            current = embedded
    if current is None:
        return None
    try:
        index = old.index(current["episode_no"])
    except ValueError:
        return current
    indices = {episode: i for i, episode in enumerate(new) if episode is not None}
    target = surviving_episode_index(old, indices, index)
    if target is None:
        return current
    return {"idx": target + 1, "episode_no": new[target]}


def read_chapter_line(folder: str, idx: int) -> Optional[dict]:
    target = int(idx)
    if target < 1:
        return None
    path = os.path.join(os.fspath(folder), "chapters.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if line_no < target:
                    continue
                return _chapter_record(line, target)
    except (OSError, UnicodeError):
        return None
    return None


def resolve_bookmark(folder: str, bookmark: Optional[Dict[str, int]]):
    if not isinstance(bookmark, dict):
        return None
    try:
        hinted_idx = int(bookmark["idx"])
        episode_no = int(bookmark["episode_no"])
    except (KeyError, TypeError, ValueError):
        return None

    hinted = read_chapter_line(folder, hinted_idx)
    if hinted is not None and _episode_no(hinted) == episode_no:
        return hinted_idx, episode_no, str(hinted.get("title") or "")

    found = _find_episode(folder, episode_no)
    if found is None:
        if hinted is None:
            return None
        return hinted_idx, episode_no, str(hinted.get("title") or "")

    found_idx = _idx(found)
    if found_idx is None:
        if hinted is None:
            return None
        return hinted_idx, episode_no, str(hinted.get("title") or "")
    title = str(found.get("title") or "")
    if found_idx != hinted_idx:
        # episode_no is the marked viewer chapter when an earlier insert renumbers idx.
        with progress_transaction():
            if read_bookmark(folder) == bookmark:
                write_bookmark(folder, found_idx, episode_no)
    return found_idx, episode_no, title


def _chapter_record(line: str, target: int) -> Optional[dict]:
    record = _parse_chapter_line(line)
    if record is None or _idx(record) != target:
        return None
    return record


def _parse_chapter_line(line: str) -> Optional[dict]:
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or _idx(record) is None:
        return None
    return record


def read_last_chapter(folder: str) -> Optional[dict]:
    path = os.path.join(os.fspath(folder), "chapters.jsonl")
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return None
            offset = max(0, size - 65536)
            handle.seek(offset)
            blob = handle.read()
    except OSError:
        return None
    record = _last_record(blob, drop_first=offset > 0)
    if record is not None or offset == 0:
        return record
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    return _last_record(blob, drop_first=False)


def flush_embedded_cache() -> None:
    """Write checked in-book positions so a later launch can skip unchanged EPUBs."""
    with _embed_cache_lock:
        path = _disk_cache["path"]
        if not _disk_cache["dirty"] or not path:
            return
        write_json_atomic(path, {"version": 2, "entries": _disk_cache["entries"]})
        _disk_cache["dirty"] = False


def sync_embedded_progress(folder: str) -> Optional[Dict[str, int]]:
    """Copy a newer in-book Calibre last-read position into the sidecar.

    The bookmark is read from the EPUB. The book does not have to be in a Calibre library.
    """
    try:
        located = _locate_embedded_progress(folder)
    except (OSError, UnicodeError, ValueError):
        return None
    if located is None:
        return None
    idx, episode_no, timestamp = located
    with progress_transaction():
        current = read_bookmark(folder)
        if current == {"idx": idx, "episode_no": episode_no}:
            return current
        if current is not None and not _embedded_is_newer(folder, timestamp):
            return None
        write_bookmark(folder, idx, episode_no)
        return {"idx": idx, "episode_no": episode_no}


def _embedded_is_newer(folder: str, timestamp: Optional[float]) -> bool:
    if timestamp is None:
        return False
    try:
        mtime = os.path.getmtime(_bookmark_path(folder))
    except OSError:
        return True
    return timestamp > mtime


def _embed_cache_file(folder: str) -> str:
    parent = os.path.dirname(os.path.abspath(os.fspath(folder)))
    return os.path.join(parent, ".embed_progress.json")


def _load_embed_cache(folder: str) -> None:
    path = _embed_cache_file(folder)
    if _disk_cache["path"] == path:
        return
    if _disk_cache["dirty"]:
        try:
            flush_embedded_cache()
        except OSError:
            _disk_cache["dirty"] = False
    _disk_cache["path"] = path
    _disk_cache["entries"] = {}
    _disk_cache["dirty"] = False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("version") != 2:
        return
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return
    _disk_cache["entries"] = {
        name: entry for name, entry in entries.items() if isinstance(entry, dict)
    }


def _disk_progress(folder: str, key):
    _load_embed_cache(folder)
    epub_path, mtime_ns, size, chapters_mtime = key
    entry = _disk_cache["entries"].get(epub_path)
    if not isinstance(entry, dict):
        return _DISK_MISS
    if (
        entry.get("mtime_ns") != mtime_ns
        or entry.get("size") != size
        or entry.get("chapters_mtime_ns") != chapters_mtime
        or "progress" not in entry
    ):
        return _DISK_MISS
    progress = entry.get("progress")
    if progress is None:
        return None
    if not isinstance(progress, dict):
        return _DISK_MISS
    try:
        idx = int(progress["idx"])
        episode_no = int(progress["episode_no"])
    except (KeyError, TypeError, ValueError):
        return _DISK_MISS
    timestamp = progress.get("timestamp")
    if timestamp is not None:
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            return _DISK_MISS
    return idx, episode_no, timestamp


def _remember_embed(folder: str, key, located) -> None:
    _load_embed_cache(folder)
    epub_path, mtime_ns, size, chapters_mtime = key
    if located is None:
        progress = None
    else:
        idx, episode_no, timestamp = located
        progress = {"idx": idx, "episode_no": episode_no, "timestamp": timestamp}
    _disk_cache["entries"][epub_path] = {
        "mtime_ns": mtime_ns,
        "size": size,
        "chapters_mtime_ns": chapters_mtime,
        "progress": progress,
    }
    _disk_cache["dirty"] = True


def _locate_embedded_progress(folder: str):
    with _embed_cache_lock:
        return _locate_embedded_progress_locked(folder)


def _locate_embedded_progress_locked(folder: str):
    epub_path = _epub_path(folder)
    if not epub_path:
        return None
    try:
        epub_stat = os.stat(epub_path)
    except OSError:
        return None
    chapters_path = os.path.join(os.fspath(folder), "chapters.jsonl")
    try:
        chapters_mtime = os.stat(chapters_path).st_mtime_ns
    except OSError:
        chapters_mtime = 0
    key = (os.path.abspath(epub_path), epub_stat.st_mtime_ns, epub_stat.st_size, chapters_mtime)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    cached = _disk_progress(folder, key)
    if cached is not _DISK_MISS:
        _EMBED_CACHE[key] = cached
        return cached
    try:
        located = _read_embedded_progress(folder, epub_path)
    except _EmbeddedUnreadable:
        # A locked or unreadable EPUB stays a miss for this process only.
        _EMBED_CACHE[key] = None
        return None
    if located is _UNRESOLVED:
        _EMBED_CACHE[key] = None
        return None
    _EMBED_CACHE[key] = located
    _remember_embed(folder, key, located)
    return located


def _read_embedded_progress(folder: str, epub_path: str):
    try:
        with zipfile.ZipFile(epub_path) as archive:
            names = archive.namelist()
            item = _embedded_last_read(archive, names)
            if item is None:
                return None
            opf_name = _pick_member(names, suffix=".opf", prefer="content.opf")
            if not opf_name:
                return _UNRESOLVED
            hrefs = _spine_hrefs(archive.read(opf_name), opf_name.replace("\\", "/"))
            index = _spine_index(item.get("pos"))
            if index is None or index < 0 or index >= len(hrefs):
                return _UNRESOLVED
            manifest_name = _zip_name(names, EMBEDDED_CHAPTERS)
            rows = _load_chapters(folder)
            if manifest_name:
                try:
                    episodes = json.loads(archive.read(manifest_name))
                except (ValueError, UnicodeError):
                    return _UNRESOLVED
                if not isinstance(episodes, list) or len(episodes) != len(hrefs):
                    return _UNRESOLVED
                ids = [episode for episode in episodes if episode is not None]
                if (any(type(episode) is not int or episode < 1 for episode in ids)
                        or len(set(ids)) != len(ids)):
                    return _UNRESOLVED
                episode = episodes[index]
                matches = [row for row in rows if episode is not None and _episode_no(row) == episode]
                row = matches[0] if len(matches) == 1 else None
            else:
                href = hrefs[index]
                nav_name = _pick_member(names, suffix="nav.xhtml")
                titles = _toc_titles(archive.read(nav_name), nav_name.replace("\\", "/")) if nav_name else {}
                row = _match_chapter(rows, _toc_title(titles, href))
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError) as exc:
        raise _EmbeddedUnreadable from exc
    if row is None:
        return _UNRESOLVED
    idx = _idx(row)
    episode_no = _episode_no(row)
    if idx is None or idx < 1 or episode_no is None:
        return _UNRESOLVED
    return idx, episode_no, _timestamp(item.get("timestamp"))


def _epub_path(folder: str) -> Optional[str]:
    folder = os.fspath(folder)
    base = os.path.basename(os.path.normpath(folder))
    direct = os.path.join(folder, base + ".epub")
    if os.path.isfile(direct):
        return direct
    try:
        names = [
            name for name in os.listdir(folder)
            if name.lower().endswith(".epub") and not name.lower().endswith(".tmp")
        ]
    except OSError:
        return None
    if len(names) == 1:
        return os.path.join(folder, names[0])
    return None


def _embedded_last_read(archive: zipfile.ZipFile, names: List[str]) -> Optional[dict]:
    original = _zip_name(names, EMBEDDED_BOOKMARK)
    if not original:
        return None
    return _latest_read(_annotation_list(archive.read(original)))


def _annotation_list(raw: bytes) -> List[dict]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return []
    if text.startswith("encoding=json+base64:"):
        encoded = "".join("".join(text.splitlines()[1:]).split())
        try:
            text = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeError):
            return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _latest_read(items: List[dict]) -> Optional[dict]:
    chosen = None
    chosen_at = None
    for item in items:
        if item.get("type") != "last-read":
            continue
        stamp = _timestamp(item.get("timestamp"))
        if chosen is None or (stamp is not None and (chosen_at is None or stamp >= chosen_at)):
            chosen = item
            chosen_at = stamp
    return chosen


def _timestamp(value) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _spine_index(pos) -> Optional[int]:
    # Calibre's in-book last-read CFI has no '!'; the first step is the spine item.
    # A standard CFI is /6/<item>!/<local>, and the step after /6 is the item.
    if not isinstance(pos, str):
        return None
    raw = pos.strip()
    if raw.startswith("epubcfi(") and raw.endswith(")"):
        raw = raw[len("epubcfi("):-1]
    head = raw.split("!", 1)[0].split(",", 1)[0]
    steps = [int(number) for number in _CFI_STEP.findall(head)]
    if not steps:
        return None
    if "!" in raw and len(steps) >= 2 and steps[0] == 6:
        step = steps[1]
    else:
        step = steps[0]
    if step < 2 or step % 2:
        return None
    return step // 2 - 1


def _spine_hrefs(opf_bytes: bytes, opf_name: str) -> List[str]:
    try:
        root = ElementTree.fromstring(opf_bytes)
    except ElementTree.ParseError:
        return []
    href_by_id = {}
    for elem in root.iter():
        if _local(elem.tag) != "item":
            continue
        item_id = elem.get("id")
        href = elem.get("href")
        if item_id and href:
            href_by_id[item_id] = href
    base = opf_name.rsplit("/", 1)[0] if "/" in opf_name else ""
    hrefs = []
    for elem in root.iter():
        if _local(elem.tag) != "itemref":
            continue
        href = href_by_id.get(elem.get("idref") or "")
        hrefs.append(_resolve_href(base, href) if href else "")
    return hrefs


class _AnchorText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._href = None
        self._parts: List[str] = []
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._parts).strip()))
            self._href = None


def _toc_titles(nav_bytes: bytes, nav_name: str) -> Dict[str, str]:
    parser = _AnchorText()
    try:
        parser.feed(nav_bytes.decode("utf-8"))
    except UnicodeError:
        return {}
    base = nav_name.rsplit("/", 1)[0] if "/" in nav_name else ""
    titles: Dict[str, str] = {}
    name_counts: Dict[str, int] = {}
    names: Dict[str, str] = {}
    for href, title in parser.anchors:
        if not title:
            continue
        path = _resolve_href(base, href)
        if not path:
            continue
        titles.setdefault(path, title)
        name = path.rsplit("/", 1)[-1]
        name_counts[name] = name_counts.get(name, 0) + 1
        names[name] = title
    for name, title in names.items():
        if name_counts[name] == 1:
            titles.setdefault(name, title)
    return titles


def _toc_title(titles: Dict[str, str], href: str) -> str:
    if href in titles:
        return titles[href]
    return titles.get(href.rsplit("/", 1)[-1], "")


def _load_chapters(folder: str) -> List[dict]:
    path = os.path.join(os.fspath(folder), "chapters.jsonl")
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                record = _parse_chapter_line(line)
                if record is not None:
                    rows.append(record)
    except (OSError, UnicodeError):
        return []
    return rows


def _match_chapter(rows: List[dict], toc_title: str) -> Optional[dict]:
    title = (toc_title or "").strip()
    hits = [row for row in rows if title and _same_title(row.get("title"), title)]
    return hits[0] if len(hits) == 1 else None


def _same_title(left, right) -> bool:
    return " ".join(str(left or "").split()).casefold() == " ".join(str(right or "").split()).casefold()


def _resolve_href(base_dir: str, href: str) -> str:
    if not isinstance(href, str):
        return ""
    href = href.split("#", 1)[0].strip()
    if not href or ":" in href.split("/", 1)[0]:
        return ""
    joined = posixpath.normpath(posixpath.join(base_dir, href))
    if joined.startswith("../") or joined == "..":
        return ""
    return joined


def _pick_member(names: List[str], suffix: str, prefer: str = "") -> Optional[str]:
    matches = [name for name in names if name.replace("\\", "/").lower().endswith(suffix.lower())]
    if prefer:
        for name in matches:
            if name.replace("\\", "/").lower().endswith(prefer.lower()):
                return name
    return matches[0] if matches else None


def _zip_name(names: List[str], wanted: str) -> Optional[str]:
    folded = wanted.replace("\\", "/").casefold()
    for name in names:
        if name.replace("\\", "/").casefold() == folded:
            return name
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _last_record(blob: bytes, drop_first: bool) -> Optional[dict]:
    lines = blob.decode("utf-8", errors="replace").splitlines()
    if drop_first:
        lines = lines[1:]
    for line in reversed(lines):
        record = _parse_chapter_line(line)
        if record is not None:
            return record
    return None


def _find_episode(folder: str, episode_no: int) -> Optional[dict]:
    path = os.path.join(os.fspath(folder), "chapters.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and _episode_no(record) == episode_no:
                    return record
    except (OSError, UnicodeError):
        return None
    return None


def _episode_no(record: dict) -> Optional[int]:
    try:
        return int(record.get("episode_no"))
    except (TypeError, ValueError):
        return None


def _idx(record: dict) -> Optional[int]:
    try:
        return int(record.get("idx"))
    except (TypeError, ValueError):
        return None
