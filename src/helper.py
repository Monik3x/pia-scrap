import base64
import json
import os
import re
import logging
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
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

# ----------------------------
# Helpers
# ----------------------------

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


def _book_directory_novel_id(book_dir: str) -> Optional[int]:
    existing_id = None
    marker_path = os.path.join(book_dir, ".novel_id")
    try:
        with open(marker_path, "r", encoding="ascii") as marker_file:
            existing_id = marker_file.read().strip() or None
    except OSError:
        pass

    meta_path = os.path.join(book_dir, "metadata.json")
    if existing_id is None:
        try:
            with open(meta_path, "r", encoding="utf-8") as meta_file:
                metadata = json.load(meta_file)
            existing_id = metadata.get("novel_id")
            if not existing_id:
                match = re.search(r"/novel/(\d+)", str(metadata.get("url") or ""))
                existing_id = match.group(1) if match else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    try:
        return int(existing_id) if existing_id is not None else None
    except (TypeError, ValueError):
        return None


def _find_existing_book_base(out_dir: str, novel_id: int, preferred_base: str) -> Optional[str]:
    """Locate an existing book by its stable ID, even after a remote rename."""
    matches = []
    try:
        with os.scandir(out_dir) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
    except OSError:
        return None

    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if _book_directory_novel_id(entry.path) == novel_id:
            if entry.name == preferred_base:
                return entry.name
            matches.append(entry.name)

    if len(matches) > 1:
        logger.warning(
            f"Multiple local book directories claim novel ID {novel_id}; using '{matches[0]}'."
        )
    return matches[0] if matches else None

def book_base(out_dir: str, title: str, novel_id: int) -> str:
    """Choose a readable book directory without overwriting a different novel."""
    novel_id = int(novel_id)
    base = kebab(title, fallback=f"novel-{novel_id}")
    existing_base = _find_existing_book_base(out_dir, novel_id, base)
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
        if _book_directory_novel_id(book_dir) == novel_id:
            return candidate

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
    """Serialize and atomically write a JSON value."""
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=indent))

# ----------------------------
# Config management
# ----------------------------

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

def save_config(cfg: Dict[str, Any]) -> None:
    try:
        write_json_atomic(CONFIG_PATH, cfg, indent=2)
    except Exception as e:
        logger.error(f"Error occurred while saving config: {e}")

# ----------------------------
# Auth token management & header merging
# ----------------------------

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

# ----------------------------
# Token extraction (STRICT)
# ----------------------------

def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)

def extract_t_token(tdata: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, direct_content_url_or_none).
    Prefer JWT-like tokens, but accept any non-empty string if present.
    If using URL, accept any _t value on the official content endpoint.
    """
    res = tdata.get("result") or {}
    if not isinstance(res, dict):
        res = {}
    fallback_token: Optional[str] = None

    # 1) common keys at result
    for k in ("_t", "t", "token"):
        v = res.get(k)
        if isinstance(v, str) and v:
            if looks_like_jwt(v):
                return v, None
            fallback_token = fallback_token or v

    # 2) nested dicts under result
    if isinstance(res, dict):
        for _, v in res.items():
            if isinstance(v, dict):
                for k in ("_t", "t", "token"):
                    vv = v.get(k)
                    if isinstance(vv, str) and vv:
                        if looks_like_jwt(vv):
                            return vv, None
                        fallback_token = fallback_token or vv

    # 3) URL that is the official content endpoint with any _t
    for s in iter_strings(tdata):
        if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
            try:
                p = urlparse(s)
                if p.netloc.endswith("api-global.novelpia.com") and p.path.endswith("/v1/novel/episode/content"):
                    q = parse_qs(p.query)
                    cand = (q.get("_t") or [None])[0]
                    if isinstance(cand, str) and cand:
                        if looks_like_jwt(cand):
                            return cand, s
                        # fallback
                        fallback_token = fallback_token or cand
            except Exception as e:
                logger.error(f"Error occurred while parsing URL: {e}")
                pass
    if fallback_token:
        return fallback_token, None
    return None, None

# ----------------------------
# Advanced Range Parsing
# ----------------------------

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
