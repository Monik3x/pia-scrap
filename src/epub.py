import html
import hashlib
import os
import json
import logging
import shutil
import zipfile

from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from src.const import BASE_URL
from src.helper import (
    BookOutputPaths,
    book_output_paths,
    ensure_dir,
    image_type,
    normalize_url,
    write_json_atomic,
    write_text_atomic,
)
from src.novel import html_from_episode_text, parse_novel_metadata

logger = logging.getLogger("pia_scrap")

IMAGE_INDEX_VERSION = 1
_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
_DEFAULT_EPUB_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial; line-height: 1.6; }
h1, h2, h3 { page-break-after: avoid; }
img { max-width: 100%; height: auto; }
.epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
"""


def image_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_safe_epub_image_name(file_name: str) -> bool:
    if not file_name or ".." in file_name or file_name.startswith("/") or "\\" in file_name:
        return False
    if file_name.startswith("images/"):
        rest = file_name[len("images/"):]
        return bool(rest) and "/" not in rest and "\\" not in rest
    root, ext = os.path.splitext(file_name)
    return root == "cover" and ext.lower() in _COVER_EXTENSIONS


def _load_image_index(path: str) -> Dict[str, Dict[str, str]]:
    """Return URL -> {sha256, file} from a previous update-mode build."""
    try:
        with open(path, "r", encoding="utf-8") as index_file:
            data = json.load(index_file)
    except FileNotFoundError:
        return {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[warn] Ignoring invalid image index {path}: {exc}")
        return {}

    if not isinstance(data, dict) or data.get("version") != IMAGE_INDEX_VERSION:
        return {}
    images = data.get("images")
    if not isinstance(images, dict):
        return {}

    result: Dict[str, Dict[str, str]] = {}
    for url, entry in images.items():
        if not isinstance(url, str) or not url or not isinstance(entry, dict):
            continue
        digest = entry.get("sha256")
        file_name = entry.get("file")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        if not isinstance(file_name, str) or not _is_safe_epub_image_name(file_name):
            continue
        result[normalize_url(url)] = {
            "sha256": digest.lower(),
            "file": file_name,
        }
    return result


def _write_image_index(path: str, images: Dict[str, Dict[str, str]]) -> None:
    write_json_atomic(
        path,
        {"version": IMAGE_INDEX_VERSION, "images": images},
        indent=2,
    )


def _zip_member_bytes(
    archive: zipfile.ZipFile,
    names: set,
    file_name: str,
) -> Optional[bytes]:
    if not _is_safe_epub_image_name(file_name):
        return None
    for candidate in (file_name, f"EPUB/{file_name}"):
        if candidate not in names:
            continue
        try:
            return archive.read(candidate)
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            logger.warning(f"[warn] Could not read {file_name} from EPUB: {exc}")
            return None
    return None


def _legacy_image_cache_path(image_cache_dir: str, url: str) -> str:
    return os.path.join(
        image_cache_dir,
        hashlib.sha256(url.encode("utf-8")).hexdigest() + ".bin",
    )


def _read_legacy_image_cache(image_cache_dir: str, url: str) -> Optional[bytes]:
    cache_path = _legacy_image_cache_path(image_cache_dir, url)
    try:
        with open(cache_path, "rb") as image_file:
            cached_bytes = image_file.read()
        if cached_bytes:
            return cached_bytes
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f"[warn] Could not read cached image {cache_path}: {exc}")
    return None


def _remove_legacy_image_cache(image_cache_dir: str) -> None:
    if not os.path.isdir(image_cache_dir):
        return
    try:
        shutil.rmtree(image_cache_dir)
    except OSError as exc:
        logger.warning(
            f"[warn] Could not remove leftover image cache {image_cache_dir}: {exc}"
        )


class _EpubImageStore:
    """Resolve image bytes from the previous EPUB, leftover URL cache, or a download."""

    def __init__(
        self,
        paths: BookOutputPaths,
        update_mode: bool,
        cancel_event=None,
    ):
        self.paths = paths
        self.update_mode = update_mode
        self.cancel_event = cancel_event
        self.url_to_digest: Dict[str, str] = {}
        self.digest_bytes: Dict[str, bytes] = {}
        self.digest_file: Dict[str, str] = {}
        self.url_file: Dict[str, str] = {}
        self._reused = 0
        if update_mode:
            self._prime_from_index_and_epub()

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Image download cancelled by user.")

    def _prime_from_index_and_epub(self) -> None:
        index = _load_image_index(self.paths.image_index_path)
        for url, entry in index.items():
            self.url_to_digest[url] = entry["sha256"]
        epub_path = self.paths.epub_path
        if not index or not os.path.isfile(epub_path):
            return
        try:
            with zipfile.ZipFile(epub_path) as archive:
                names = set(archive.namelist())
                for entry in index.values():
                    digest = entry["sha256"]
                    if digest in self.digest_bytes:
                        continue
                    data = _zip_member_bytes(archive, names, entry["file"])
                    if data and image_digest(data) == digest:
                        self.digest_bytes[digest] = data
                        self._reused += 1
        except (OSError, zipfile.BadZipFile) as exc:
            logger.warning(f"[warn] Could not open existing EPUB for image reuse: {exc}")
        if self._reused:
            logger.info(f"Reusing {self._reused} unique images from the existing EPUB.")

    def _bytes_for(self, url: str, fetch: Callable[[], Optional[bytes]]) -> Optional[bytes]:
        digest = self.url_to_digest.get(url)
        if digest and digest in self.digest_bytes:
            return self.digest_bytes[digest]

        if self.update_mode:
            legacy = _read_legacy_image_cache(self.paths.image_cache_dir, url)
            if legacy:
                digest = image_digest(legacy)
                self.url_to_digest[url] = digest
                self.digest_bytes.setdefault(digest, legacy)
                return self.digest_bytes[digest]

        image_bytes = fetch()
        if not image_bytes:
            return None
        digest = image_digest(image_bytes)
        self.url_to_digest[url] = digest
        self.digest_bytes.setdefault(digest, image_bytes)
        return self.digest_bytes[digest]

    def _assign_filename(self, url: str, data: bytes, *, as_cover: bool) -> str:
        digest = self.url_to_digest[url]
        existing = self.digest_file.get(digest)
        if existing:
            self.url_file[url] = existing
            return existing
        fallback_ext = os.path.splitext(urlparse(url).path)[1]
        ext, _ = image_type(data, fallback_ext)
        file_name = f"cover{ext}" if as_cover else f"images/{digest}{ext}"
        self.digest_file[digest] = file_name
        self.url_file[url] = file_name
        return file_name

    def resolve(
        self,
        url: str,
        fetch: Callable[[], Optional[bytes]],
        *,
        as_cover: bool = False,
    ) -> Optional[Tuple[bytes, str]]:
        self._raise_if_cancelled()
        url = normalize_url(url)
        data = self._bytes_for(url, fetch)
        if not data:
            return None
        return data, self._assign_filename(url, data, as_cover=as_cover)

    def commit(self) -> None:
        if self.update_mode:
            images: Dict[str, Dict[str, str]] = {}
            for url, file_name in self.url_file.items():
                digest = self.url_to_digest.get(url)
                if not digest or not _is_safe_epub_image_name(file_name):
                    continue
                images[url] = {"sha256": digest, "file": file_name}
            try:
                ensure_dir(self.paths.cache_dir)
                _write_image_index(self.paths.image_index_path, images)
            except OSError as exc:
                logger.warning(
                    f"[warn] Could not write image index {self.paths.image_index_path}: {exc}"
                )
        _remove_legacy_image_cache(self.paths.image_cache_dir)

# ----------------------------
# EPUB Builder
# ----------------------------

class EpubBuilder:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        ensure_dir(out_dir)

    def _embed_chapter_images(
        self,
        html_str: str,
        epi_no: Optional[int],
        episode_cookies: dict,
        image_store: _EpubImageStore,
        fetch_image: Callable[..., Optional[bytes]],
        embedded_files: set,
    ) -> Tuple[str, List[epub.EpubItem]]:
        soup = BeautifulSoup(html_str, "html.parser")
        added_items: List[epub.EpubItem] = []
        viewer_url = f"{BASE_URL}/viewer/{epi_no}" if epi_no else f"{BASE_URL}/"

        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            src = normalize_url(src)
            resolved = image_store.resolve(
                src,
                lambda src=src: fetch_image(
                    src,
                    viewer_url,
                    episode_cookies,
                    episode_no=epi_no,
                ),
            )
            if not resolved:
                img.decompose()
                continue

            img_bytes, fname = resolved
            img["src"] = fname
            if fname in embedded_files:
                continue

            path = urlparse(src).path
            fallback_ext = os.path.splitext(path)[1].lower() or ".jpg"
            _, media_type = image_type(img_bytes, fallback_ext)
            digest = image_digest(img_bytes)
            item = epub.EpubItem(
                uid=f"img-{digest}",
                file_name=fname,
                media_type=media_type,
                content=img_bytes,
            )
            embedded_files.add(fname)
            added_items.append(item)

        return str(soup), added_items

    def build(
        self,
        novel: Dict,
        chapters: List[Dict],
        fetch_image: Callable[..., Optional[bytes]],
        filename_hint: Optional[str] = None,
        language: str = "en",
        novel_id: Optional[int] = None,
        update_mode: bool = False,
        output_paths: Optional[BookOutputPaths] = None,
        cancel_event=None,
    ) -> Tuple[str, str, int]:
        metadata = parse_novel_metadata(novel, novel_id)
        novel_fields = metadata.novel
        title = metadata.title
        author = metadata.author
        status = metadata.status
        description = metadata.description
        resolved_novel_id = metadata.novel_id
        if output_paths is not None:
            if output_paths.novel_id != resolved_novel_id:
                raise ValueError("Resolved output paths do not match the novel ID.")
            expected_root = os.path.normcase(os.path.abspath(self.out_dir))
            actual_root = os.path.normcase(os.path.dirname(os.path.abspath(output_paths.book_dir)))
            if actual_root != expected_root:
                raise ValueError("Resolved output paths do not belong to this output directory.")
            paths = output_paths
        else:
            paths = book_output_paths(self.out_dir, filename_hint or title, resolved_novel_id)
        book_dir = paths.book_dir
        ensure_dir(book_dir)
        if resolved_novel_id is not None:
            write_text_atomic(paths.novel_id_path, str(resolved_novel_id))

        image_store = _EpubImageStore(
            paths,
            update_mode,
            cancel_event=cancel_event,
        )
        embedded_files = set()

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{metadata.novel_id}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        # Cover
        cover_url = normalize_url(
            novel_fields.get("novel_full_img") or novel_fields.get("novel_img") or ""
        )
        novel_referer = f"{BASE_URL}/novel/{resolved_novel_id}" if resolved_novel_id else f"{BASE_URL}/"
        cover_resolved = (
            image_store.resolve(
                cover_url,
                lambda: fetch_image(cover_url, novel_referer),
                as_cover=True,
            )
            if cover_url
            else None
        )
        has_cover = False
        cover_filename = "cover.jpg"
        if cover_resolved:
            cover_bytes, cover_filename = cover_resolved
            book.set_cover(cover_filename, cover_bytes)
            has_cover = True
            embedded_files.add(cover_filename)

        style = epub.EpubItem(
            uid="style",
            file_name="style/main.css",
            media_type="text/css",
            content=_DEFAULT_EPUB_CSS.encode("utf-8"),
        )
        book.add_item(style)

        spine: List = ["nav"]
        toc: List = []

        for i, res in enumerate(chapters, 1):
            html_text = html_from_episode_text(res["html"])
            epi_title = res["epi_title"]
            signed_key = res.get("signed_key", {})
            if not isinstance(signed_key, dict):
                signed_key = {}

            try:
                current_epi_no = int(res.get("epi_no"))
            except (TypeError, ValueError):
                current_epi_no = None

            html_text, new_imgs = self._embed_chapter_images(
                html_text,
                epi_no=current_epi_no,
                episode_cookies=signed_key,
                image_store=image_store,
                fetch_image=fetch_image,
                embedded_files=embedded_files,
            )

            html_content = f'''<html xmlns="http://www.w3.org/1999/xhtml">
            <head>
                <title>{html.escape(epi_title)}</title>
                <link rel="stylesheet" href="style/main.css"/>
            </head>
            <body>
                <h2 class="epi-title">{html.escape(epi_title)}</h2>
                {html_text}
            </body>
            </html>'''

            chapter = epub.EpubHtml(
                title=epi_title,
                file_name=f"chap_{i:04d}.xhtml",
                lang=language,
                content=html_content,
            )

            book.add_item(chapter)
            spine.append(chapter)
            toc.append(chapter)

            for item in new_imgs:
                book.add_item(item)

        # About / metadata page
        src_url = f"{BASE_URL}/novel/{resolved_novel_id}" if resolved_novel_id else ""
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append(f"<p><img src='{cover_filename}' alt='Cover' style='width:230px;max-width:90%;height:auto;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>")
        meta_parts.append(f"<p><strong>Author:</strong> {html.escape(author)}</p>")
        meta_parts.append(f"<p><strong>Chapters:</strong> {len(chapters)}</p>")
        meta_parts.append(f"<p><strong>Status:</strong> {html.escape(status)}</p>")
        if src_url:
            meta_parts.append(f"<p><strong>Source:</strong> <a href='{src_url}'>{src_url}</a></p>")
        if description:
            meta_parts.append(f"<p>{html.escape(description)}</p>")
        meta_html = (
            "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>"
             + "".join(meta_parts) + "</body></html>"
        )
        about = epub.EpubHtml(title="About", file_name="about.xhtml", lang=language, content=meta_html)
        book.add_item(about)
        spine.insert(1, about)
        toc.insert(0, about)

        # TOC, NCX, Nav
        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Spine & CSS
        book.spine = spine

        out_path = paths.epub_path
        temp_path = out_path + ".tmp"
        try:
            epub.write_epub(temp_path, book, {})
            os.replace(temp_path, out_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        image_store.commit()
        return out_path, title, len(chapters)
