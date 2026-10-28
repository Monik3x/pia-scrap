"""Chapter progress and reading labels for the local library."""

import base64
import json
import os
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from src.helper import write_json_atomic

BOOKMARK_FILENAME = "calibre_bookmarks.txt"
LABELS_FILENAME = "reading_labels.json"
BUILTIN_LABELS = ("Reading", "Plan to read", "On hold", "Dropped", "Completed")
EMBEDDED_BOOKMARK = "META-INF/calibre_bookmarks.txt"

_CHAP_FILE = re.compile(r"chap_(\d+)\.xhtml$", re.IGNORECASE)
_CHAPTER_NUMBER = re.compile(
    r"^(?:chapter|ch|ep|episode)\.?\s*(\d+)\b|^c(\d+)\b",
    re.IGNORECASE,
)
_CFI_STEP = re.compile(r"/(\d+)")
_EMBED_CACHE: Dict[tuple, Optional[Tuple[int, int, Optional[float]]]] = {}
_DISK_MISS = object()
_UNRESOLVED = object()
_disk_cache = {"path": None, "entries": {}, "dirty": False}


class _EmbeddedUnreadable(Exception):
    pass


class LabelsUnreadable(ValueError):
    """reading_labels.json exists and is not a map of folder name to label."""


def _labels_path(root: str) -> str:
    return os.path.join(os.fspath(root), LABELS_FILENAME)


def _bookmark_path(folder: str) -> str:
    return os.path.join(os.fspath(folder), BOOKMARK_FILENAME)


def read_labels(root: str) -> Dict[str, str]:
    path = _labels_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LabelsUnreadable(f"{LABELS_FILENAME} could not be read.") from exc
    if not isinstance(payload, dict):
        raise LabelsUnreadable(f"{LABELS_FILENAME} could not be read.")
    labels: Dict[str, str] = {}
    for folder, label in payload.items():
        if not isinstance(folder, str) or not isinstance(label, str) or not label.strip():
            raise LabelsUnreadable(f"{LABELS_FILENAME} could not be read.")
        labels[folder] = label
    return labels


def write_labels(root: str, mapping: Dict[str, str]) -> None:
    write_json_atomic(_labels_path(root), mapping)


def set_label(root: str, folder: str, label: str) -> Dict[str, str]:
    mapping = read_labels(root)
    text = label.strip() if isinstance(label, str) else ""
    if text:
        mapping[folder] = text
    else:
        mapping.pop(folder, None)
    write_labels(root, mapping)
    return mapping


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
    write_json_atomic(
        _bookmark_path(folder),
        {"idx": int(idx), "episode_no": int(episode_no)},
    )


def clear_bookmark(folder: str) -> None:
    try:
        os.remove(_bookmark_path(folder))
    except FileNotFoundError:
        pass


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
    path = _disk_cache["path"]
    if not _disk_cache["dirty"] or not path:
        return
    write_json_atomic(path, {"version": 1, "entries": _disk_cache["entries"]})
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
    if not isinstance(payload, dict) or payload.get("version") != 1:
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
            href = hrefs[index]
            nav_name = _pick_member(names, suffix="nav.xhtml")
            titles = {}
            if nav_name:
                titles = _toc_titles(archive.read(nav_name), nav_name.replace("\\", "/"))
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError) as exc:
        raise _EmbeddedUnreadable from exc
    row = _match_chapter(_load_chapters(folder), href, _toc_title(titles, href))
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
        if not href:
            continue
        resolved = _resolve_href(base, href)
        if resolved:
            hrefs.append(resolved)
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


def _match_chapter(rows: List[dict], href: str, toc_title: str) -> Optional[dict]:
    name = href.rsplit("/", 1)[-1]
    file_match = _CHAP_FILE.search(name)
    file_idx = int(file_match.group(1)) if file_match else None
    title = (toc_title or "").strip()
    title_hits = [row for row in rows if title and _same_title(row.get("title"), title)]
    file_row = next((row for row in rows if file_idx is not None and _idx(row) == file_idx), None)
    if len(title_hits) == 1:
        return title_hits[0]
    if file_row is not None and (not title_hits or file_row in title_hits):
        return file_row
    number = _chapter_number(title)
    if number is None:
        return None
    return next((row for row in rows if _idx(row) == number), None)


def _chapter_number(title: str) -> Optional[int]:
    if not title:
        return None
    match = _CHAPTER_NUMBER.match(title.strip())
    if not match:
        return None
    raw = next(group for group in match.groups() if group)
    try:
        number = int(raw)
    except ValueError:
        return None
    if number < 1:
        return None
    return number


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
