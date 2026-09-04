import base64
import json
import os
import re
import logging
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse
from src.const import APPROVED_IMAGE_HOSTS, BASE_URL, CONFIG_PATH, IMG_BASE_HTTPS

logger = logging.getLogger("pia_scrap")

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
DEFAULT_COMPONENT_LENGTH = 120
BOOK_SLUG_LENGTH = 96


@dataclass(frozen=True)
class BookOutputPaths:
    """All filesystem locations belonging to one novel export."""

    novel_id: Optional[int]
    base: str
    book_dir: str
    epub_path: str
    metadata_path: str
    chapters_path: str
    novel_id_path: str
    cache_dir: str
    image_cache_dir: str
    image_index_path: str


@dataclass(frozen=True)
class LocalBookInfo:
    """Identity and listing fields for one local book directory."""

    novel_id: Optional[int]
    title: str
    author: str
    status: str
    chapter_count: Optional[int]
    has_metadata: bool


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize_path_component(
    name: str, fallback: str = "book", max_length: int = DEFAULT_COMPONENT_LENGTH
) -> str:
    """Return a bounded path component that is valid on Windows and POSIX."""
    if max_length < 1:
        raise ValueError("max_length must be positive")

    value = unicodedata.normalize("NFKC", str(name or ""))
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", value).strip(" .")
    if not value:
        value = fallback
    value = value[:max_length].rstrip(" .") or fallback[:max_length].rstrip(" .") or "_"

    # Windows reserves these names even when an extension is present.
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
        value = value[:max_length].rstrip(" .") or "_"
    return value


def sanitize_filename(name: str) -> str:
    return sanitize_path_component(name)

def normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    if u.startswith("//"):
        return IMG_BASE_HTTPS + u
    if not urlparse(u).scheme:
        return urljoin(f"{BASE_URL}/", u)
    return u

def is_approved_image_url(url: str) -> bool:
    """Accept only HTTPS image URLs on Novelpia's known first-party hosts."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in APPROVED_IMAGE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False

def media_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    if ext == ".svg":
        return "image/svg+xml"
    return "image/jpeg"

def image_type(data: bytes, fallback_ext: str = ".jpg") -> Tuple[str, str]:
    """Return a safe EPUB extension and media type based on image contents."""
    if data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        ext = ".gif"
    elif len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        ext = ".webp"
    elif b"<svg" in data[:512].lower():
        ext = ".svg"
    else:
        ext = fallback_ext.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
            ext = ".jpg"
    return ext, media_type_from_ext(ext)

def looks_like_jwt(token: Optional[str]) -> bool:
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    for p in parts:
        try:
            base64.urlsafe_b64decode(p + "===")
        except Exception:
            return False
    return True

def kebab(s: str, fallback: str = "book") -> str:
    """Create a readable, Unicode-safe directory name."""
    normalized = unicodedata.normalize("NFKC", s or "").casefold()
    slug = re.sub(r"[\W_]+", "-", normalized, flags=re.UNICODE).strip("-")
    return sanitize_path_component(
        slug, fallback=fallback, max_length=BOOK_SLUG_LENGTH
    )

def _with_component_suffix(base: str, suffix: str, max_length: int = BOOK_SLUG_LENGTH) -> str:
    suffix = sanitize_path_component(suffix, fallback="", max_length=max_length)
    prefix_length = max(1, max_length - len(suffix))
    prefix = base[:prefix_length].rstrip(" .-") or "book"
    return sanitize_path_component(prefix + suffix, max_length=max_length)


def book_directory_novel_id(book_dir: str) -> Optional[int]:
    """Return the stored novel ID for a local book folder, if it is a positive integer."""

    def parse_positive_id(value: Any) -> Optional[int]:
        try:
            novel_id = int(value)
        except (TypeError, ValueError):
            return None
        return novel_id if novel_id > 0 else None

    marker_path = os.path.join(book_dir, ".novel_id")
    try:
        with open(marker_path, "r", encoding="ascii") as marker_file:
            novel_id = parse_positive_id(marker_file.read().strip() or None)
        if novel_id is not None:
            return novel_id
    except OSError:
        pass

    meta_path = os.path.join(book_dir, "metadata.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as meta_file:
            metadata = json.load(meta_file)
        novel_id = parse_positive_id(metadata.get("novel_id"))
        if novel_id is not None:
            return novel_id
        match = re.search(r"/novel/(\d+)", str(metadata.get("url") or ""))
        return parse_positive_id(match.group(1) if match else None)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _iter_book_dir_entries(out_dir: str) -> Iterator[os.DirEntry]:
    # Does not skip '.' names or catch scandir errors.
    with os.scandir(out_dir) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name.casefold())
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        yield entry


def _find_existing_book_base(out_dir: str, novel_id: int, preferred_base: str) -> Optional[str]:
    """Locate an existing book by its stable ID, even after a remote rename."""
    matches = []
    try:
        entries = list(_iter_book_dir_entries(out_dir))
    except OSError:
        return None

    for entry in entries:
        if book_directory_novel_id(entry.path) == novel_id:
            if entry.name == preferred_base:
                return entry.name
            matches.append(entry.name)

    if len(matches) > 1:
        logger.warning(
            f"Multiple local book directories claim novel ID {novel_id}; using '{matches[0]}'."
        )
    return matches[0] if matches else None


def build_book_directory_index(out_dir: str) -> Dict[int, str]:
    """Scan an output root once and map each known novel ID to a directory name."""
    index: Dict[int, str] = {}
    try:
        entries = list(_iter_book_dir_entries(out_dir))
    except OSError:
        return index

    for entry in entries:
        novel_id = book_directory_novel_id(entry.path)
        if novel_id is None:
            continue
        if novel_id in index:
            logger.warning(
                f"Multiple local book directories claim novel ID {novel_id}; "
                f"using '{index[novel_id]}'."
            )
            continue
        index[novel_id] = entry.name
    return index


def list_local_book_directories(out_dir: str) -> List[str]:
    names: List[str] = []
    try:
        entries = list(_iter_book_dir_entries(out_dir))
    except FileNotFoundError:
        return names
    except OSError as exc:
        logger.warning(f"Could not scan library directory {out_dir}: {exc}")
        return names

    for entry in entries:
        if entry.name.startswith("."):
            continue
        names.append(entry.name)
    return names


def load_local_book_info(book_dir: str) -> LocalBookInfo:
    book_dir = os.fspath(book_dir)
    directory_name = os.path.basename(os.path.normpath(book_dir)) or book_dir
    novel_id = book_directory_novel_id(book_dir)

    title = directory_name
    author = "Unknown Author"
    status = "Unknown"
    chapter_count: Optional[int] = None
    has_metadata = False

    meta_path = os.path.join(book_dir, "metadata.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as meta_file:
            metadata = json.load(meta_file)
        if not isinstance(metadata, dict):
            raise ValueError("metadata root must be an object")
        has_metadata = True
        title = metadata.get("title") or title
        author = metadata.get("author") or author
        status = str(metadata.get("status") or status)
        try:
            chapter_count = int(metadata.get("chapter", 0))
        except (TypeError, ValueError):
            chapter_count = 0
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"Ignoring invalid local book metadata {meta_path}: {exc}")

    return LocalBookInfo(
        novel_id=novel_id,
        title=title,
        author=author,
        status=status,
        chapter_count=chapter_count,
        has_metadata=has_metadata,
    )


def local_library_novel_ids(out_dir: str) -> List[int]:
    ids: List[int] = []
    for name in list_local_book_directories(out_dir):
        novel_id = book_directory_novel_id(os.path.join(out_dir, name))
        if novel_id is not None:
            ids.append(novel_id)
    return unique_in_order(ids)

def book_base(
    out_dir: str,
    title: str,
    novel_id: int,
    book_index: Optional[Dict[int, str]] = None,
) -> str:
    """Choose a readable book directory without overwriting a different novel."""
    novel_id = int(novel_id)
    base = kebab(title, fallback=f"novel-{novel_id}")
    preferred_dir = os.path.join(out_dir, base)

    # Prefer one directory check over scanning the whole library.
    if os.path.isdir(preferred_dir) and book_directory_novel_id(preferred_dir) == novel_id:
        return base

    if book_index is None:
        existing_base = _find_existing_book_base(out_dir, novel_id, base)
    else:
        existing_base = book_index.get(novel_id)
        if existing_base and not os.path.isdir(os.path.join(out_dir, existing_base)):
            existing_base = None
    if existing_base:
        return existing_base

    candidates = [base, _with_component_suffix(base, f"-{novel_id}")]
    counter = 2
    while True:
        if candidates:
            candidate = candidates.pop(0)
        else:
            candidate = _with_component_suffix(base, f"-{novel_id}-{counter}")
            counter += 1
        book_dir = os.path.join(out_dir, candidate)
        if not os.path.isdir(book_dir):
            return candidate
        if book_directory_novel_id(book_dir) == novel_id:
            return candidate


def book_output_paths(
    out_dir: str,
    title: str,
    novel_id: Optional[int] = None,
    book_index: Optional[Dict[int, str]] = None,
) -> BookOutputPaths:
    """Resolve and validate the complete output layout for a novel."""
    out_dir = os.fspath(out_dir)
    if not out_dir.strip():
        raise ValueError("Output directory must not be empty.")

    if novel_id is None:
        base = kebab(title)
    else:
        try:
            novel_id = int(novel_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Novel ID must be a positive integer.") from exc
        if novel_id <= 0:
            raise ValueError("Novel ID must be a positive integer.")
        base = book_base(out_dir, title, novel_id, book_index=book_index)

    book_dir = os.path.join(out_dir, base)
    cache_dir = os.path.join(book_dir, ".raw_cache")
    return BookOutputPaths(
        novel_id=novel_id,
        base=base,
        book_dir=book_dir,
        epub_path=os.path.join(book_dir, f"{base}.epub"),
        metadata_path=os.path.join(book_dir, "metadata.json"),
        chapters_path=os.path.join(book_dir, "chapters.jsonl"),
        novel_id_path=os.path.join(book_dir, ".novel_id"),
        cache_dir=cache_dir,
        image_cache_dir=os.path.join(cache_dir, "images"),
        image_index_path=os.path.join(cache_dir, "image_index.json"),
    )

def unique_in_order(values: List[int]) -> List[int]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique

def write_text_atomic(path, value: str) -> None:
    """Write text without exposing readers to a partially written file."""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(value)
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

def write_json_atomic(path, value: Any, *, indent: Optional[int] = None) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=indent))


def load_config() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            if isinstance(config, dict):
                return config
            logger.warning(f"Ignoring invalid config structure in {CONFIG_PATH}.")
    except Exception as e:
        logger.error(f"Error occurred while loading config: {e}")
        return {}
    return {}

def save_config(cfg: Dict[str, Any]) -> bool:
    try:
        write_json_atomic(CONFIG_PATH, cfg, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error occurred while saving config: {e}")
        return False


def merge_login_at(headers: dict, login_at: Optional[str]) -> dict:
    h = dict(headers or {})
    if login_at:
        h["login-at"] = login_at
    return h

def attach_auth_cookies(session, headers=None):
    ck = getattr(session, "cookies", None)
    if ck is None:
        return headers

    uval = None
    tval = None

    try:
        uval = ck.get("USERKEY")
        tval = ck.get("TKEY")
    except Exception as e:
        logger.error(f"Error occurred while fetching cookies: {e}")

    cookie_parts = []
    if uval:
        cookie_parts.append(f"USERKEY={uval}")
    if tval:
        cookie_parts.append(f"TKEY={tval}")

    cookie_parts.append("last_login=basic")

    if cookie_parts:
        headers = dict(headers or {})
        headers.setdefault("Cookie", "; ".join(cookie_parts))

    return headers


def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)

def extract_t_token(tdata: dict) -> Optional[str]:
    """Return a content token from ticket data, preferring JWT-like values.
    Accepts nested `_t`/`t`/`token` fields and `_t` on the official content URL.
    """
    res = tdata.get("result") or {}
    if not isinstance(res, dict):
        res = {}
    fallback_token: Optional[str] = None

    for k in ("_t", "t", "token"):
        v = res.get(k)
        if isinstance(v, str) and v:
            if looks_like_jwt(v):
                return v
            fallback_token = fallback_token or v

    if isinstance(res, dict):
        for _, v in res.items():
            if isinstance(v, dict):
                for k in ("_t", "t", "token"):
                    vv = v.get(k)
                    if isinstance(vv, str) and vv:
                        if looks_like_jwt(vv):
                            return vv
                        fallback_token = fallback_token or vv

    for s in iter_strings(tdata):
        if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
            try:
                p = urlparse(s)
                if p.netloc.endswith("api-global.novelpia.com") and p.path.endswith("/v1/novel/episode/content"):
                    q = parse_qs(p.query)
                    cand = (q.get("_t") or [None])[0]
                    if isinstance(cand, str) and cand:
                        if looks_like_jwt(cand):
                            return cand
                        fallback_token = fallback_token or cand
            except Exception as e:
                logger.error(f"Error occurred while parsing URL: {e}")
                pass
    return fallback_token


def parse_range(range_str: str) -> List[int]:
    """Parses mixed strings like '100', '100-105', or '47,50,51-55' into a list of integers."""
    result = []
    parts = str(range_str).strip().split(',')
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start = int(start_s.strip())
                end = int(end_s.strip())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid range format: {part}. Use 'start-end' (e.g., 100-105)."
                ) from exc
            if start <= 0 or end <= 0:
                raise ValueError("Novel IDs must be positive integers.")
            if start > end:
                raise ValueError(f"Range start must not exceed range end: {part}")
            result.extend(range(start, end + 1))
        else:
            try:
                novel_id = int(part)
            except ValueError as exc:
                raise ValueError(f"Invalid ID: {part}") from exc
            if novel_id <= 0:
                raise ValueError("Novel IDs must be positive integers.")
            result.append(novel_id)

    return unique_in_order(result)
