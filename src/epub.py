import html
import hashlib
import os
import json
import logging

from typing import Dict, List, Optional, Tuple, Callable
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from tqdm import tqdm
from src.api import NovelpiaClient
from src.const import BASE_URL, IMAGE_HOST_COOKIE_POLICY, SIGNED_IMAGE_COOKIE_NAMES
from src.novel import html_from_episode_text
from src.helper import (
    book_base,
    ensure_dir,
    image_type,
    is_approved_image_url,
    kebab,
    normalize_url,
    write_json_atomic,
    write_text_atomic,
)

logger = logging.getLogger("pia_scrap")

# ----------------------------
# EPUB Builder
# ----------------------------

class EpubBuilder:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        ensure_dir(out_dir)

    def _fetch_bytes(self, client: NovelpiaClient, url: str, referer_url: str, episode_cookies: dict = None) -> Optional[bytes]:
        url = normalize_url(url)
        if not is_approved_image_url(url):
            logger.warning(f"[warn] Blocked image URL outside approved Novelpia hosts: {url}")
            return None

        last_error = "Unknown Error"
        for attempt in range(1, 4):
            cancel_event = getattr(client, "cancel_event", None)
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Image download cancelled by user.")
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Referer": referer_url,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                host = urlparse(url).hostname.lower()
                cookie_policy = IMAGE_HOST_COOKIE_POLICY[host]
                cookie_dict = {}
                if cookie_policy == "signed" and isinstance(episode_cookies, dict):
                    cookie_dict = {
                        key: value for key, value in episode_cookies.items()
                        if key in SIGNED_IMAGE_COOKIE_NAMES and value
                    }
                elif cookie_policy == "session":
                    for key in ("USERKEY", "TKEY"):
                        try:
                            value = client.s.cookies.get(key)
                        except Exception:
                            value = None
                        if value:
                            cookie_dict[key] = value

                # An explicit header prevents the session's broad .novelpia.com
                # cookie jar from adding authentication cookies to CDN requests.
                headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookie_dict.items())

                resp = client.s.get(
                    url, headers=headers, timeout=client.timeout, allow_redirects=False
                )

                if resp.status_code in (301, 302, 303, 307, 308):
                    redirect_url = urljoin(url, resp.headers.get("Location", ""))
                    if not is_approved_image_url(redirect_url):
                        last_error = f"Blocked redirect to unapproved host: {redirect_url}"
                        break
                    url = redirect_url
                    continue
                
                if resp.status_code == 429:
                    last_error = "HTTP 429 (Too Many Requests)"
                    if attempt < 3:
                        client.sleep_cooperative(2.0 * attempt)
                    continue
                    
                resp.raise_for_status()
                return resp.content
                
            except Exception as e:
                last_error = f"HTTP Error or Timeout: {e}"
                if attempt < 3:
                    client.sleep_cooperative(1.0)
                
        logger.warning(f"[warn] Image error ({last_error}): {url}")
        return None

    def build(self, client: NovelpiaClient, novel: Dict, episodes: List[Dict],
              filename_hint: Optional[str] = None, language: str = "en",
              author_fallback: str = "Unknown", css_text: Optional[str] = None,
              novel_id: Optional[int] = None, update_mode: bool = False, threads: int = 1,
              progress_cb: Optional[Callable[[int, int, str], None]] = None) -> Tuple[str, str, int]:
        nv = novel["result"]["novel"]
        title = nv.get("novel_name") or f"novel_{nv.get('novel_no', '')}"
        writers = novel["result"].get("writer_list") or []
        author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else author_fallback)
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()

        resolved_novel_id = novel_id or nv.get("novel_no")
        if resolved_novel_id is None:
            base = kebab(filename_hint or title)
        else:
            base = book_base(self.out_dir, filename_hint or title, resolved_novel_id)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)
        if resolved_novel_id is not None:
            write_text_atomic(os.path.join(book_dir, ".novel_id"), str(resolved_novel_id))

        # --- Setup Cache Directory ---
        cache_dir = os.path.join(book_dir, ".raw_cache")
        image_cache_dir = os.path.join(cache_dir, "images")
        if update_mode:
            ensure_dir(cache_dir)
            ensure_dir(image_cache_dir)

        def fetch_cached_image(url: str, referer_url: str, episode_cookies=None) -> Optional[bytes]:
            cancel_event = getattr(client, "cancel_event", None)
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Image download cancelled by user.")
            cache_path = None
            if update_mode:
                cache_name = hashlib.sha256(url.encode("utf-8")).hexdigest() + ".bin"
                cache_path = os.path.join(image_cache_dir, cache_name)
                try:
                    with open(cache_path, "rb") as image_file:
                        cached_bytes = image_file.read()
                    if cached_bytes:
                        return cached_bytes
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(f"[warn] Could not read cached image {cache_path}: {exc}")

            image_bytes = self._fetch_bytes(client, url, referer_url, episode_cookies)
            if image_bytes and cache_path:
                temp_path = cache_path + ".tmp"
                try:
                    with open(temp_path, "wb") as image_file:
                        image_file.write(image_bytes)
                    os.replace(temp_path, cache_path)
                except OSError as exc:
                    logger.warning(f"[warn] Could not cache image {url}: {exc}")
                    try:
                        os.remove(temp_path)
                    except FileNotFoundError:
                        pass
            return image_bytes

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        # Cover
        cover_url = normalize_url(nv.get("novel_full_img") or nv.get("novel_img") or "")
        novel_referer = f"{BASE_URL}/novel/{resolved_novel_id}" if resolved_novel_id else f"{BASE_URL}/"
        cover_bytes = fetch_cached_image(cover_url, referer_url=novel_referer) if cover_url else None
        has_cover = False
        cover_filename = "cover.jpg"
        if cover_bytes:
            cover_ext, _ = image_type(cover_bytes, os.path.splitext(urlparse(cover_url).path)[1])
            cover_filename = f"cover{cover_ext}"
            book.set_cover(cover_filename, cover_bytes)
            has_cover = True

        # CSS
        default_css = css_text or (
            """
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial; line-height: 1.6; }
            h1, h2, h3 { page-break-after: avoid; }
            img { max-width: 100%; height: auto; }
            .epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
            """
        )
        style = epub.EpubItem(uid="style", file_name="style/main.css",
                              media_type="text/css", content=default_css.encode("utf-8"))
        book.add_item(style)

        spine: List = ["nav"]
        toc: List = []
        image_cache: Dict[str, str] = {}
        img_index = 1

        def add_images_and_rewrite(html_str: str, epi_no: str, episode_cookies: dict) -> Tuple[str, List[epub.EpubItem]]:
            nonlocal img_index
            soup = BeautifulSoup(html_str, "html.parser")
            added_items: List[epub.EpubItem] = []
            
            # Construct the exact URL a real user would be on when reading this chapter (Fixed structure)
            viewer_url = f"https://global.novelpia.com/viewer/{epi_no}" if epi_no else "https://global.novelpia.com/"

            for img in soup.find_all("img"):
                src = img.get("src")
                if not src:
                    continue
                src = normalize_url(src)
                if src in image_cache:
                    img["src"] = image_cache[src]
                    continue

                path = urlparse(src).path
                fallback_ext = os.path.splitext(path)[1].lower() or ".jpg"
                img_bytes = fetch_cached_image(
                    src, referer_url=viewer_url, episode_cookies=episode_cookies
                )
                if not img_bytes:
                    img.decompose()
                    continue

                ext, media_type = image_type(img_bytes, fallback_ext)

                fname = f"images/img_{img_index:05d}{ext}"
                image_cache[src] = fname
                item = epub.EpubItem(uid=f"img{img_index}", file_name=fname,
                                     media_type=media_type, content=img_bytes)
                img_index += 1
                added_items.append(item)
                img["src"] = fname

            return str(soup), added_items

        # --- Cache Filter ---
        all_results = [None] * len(episodes)
        to_fetch = []
        fetch_indices = []

        for idx_offset, ep in enumerate(episodes):
            epi_no = int(ep["episode_no"])
            cache_file = os.path.join(cache_dir, f"{epi_no}.json") if update_mode else None
            
            cached_data = None
            if update_mode and cache_file and os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(f"[warn] Ignoring invalid episode cache {cache_file}: {exc}")
            
            try:
                cache_is_valid = (
                    isinstance(cached_data, dict)
                    and isinstance(cached_data.get("html"), str)
                    and int(cached_data.get("epi_no")) == epi_no
                )
            except (TypeError, ValueError):
                cache_is_valid = False

            if cache_is_valid:
                all_results[idx_offset] = cached_data
                epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num', idx_offset+1)}"
                logger.info(f"loaded cached episode {ep.get('epi_num', idx_offset+1)} - {epi_title}")
            else:
                to_fetch.append(ep)
                fetch_indices.append(idx_offset)

        # Let the user know the cache worked!
        cached_count = len(episodes) - len(to_fetch)
        if cached_count > 0:
            logger.info(f"Successfully loaded {cached_count} chapters instantly from local cache.")
            if progress_cb:
                progress_cb(cached_count, len(episodes), "Loaded from cache")

        # Callback to write cache instantly when a thread returns
        def cache_result(res):
            if update_mode and res and "error" not in res:
                epi_no = res.get("epi_no")
                if epi_no:
                    c_file = os.path.join(cache_dir, f"{epi_no}.json")
                    try:
                        write_json_atomic(c_file, res)
                    except (OSError, TypeError, ValueError) as exc:
                        logger.warning(f"[warn] Could not cache episode {epi_no}: {exc}")

        # --- Parallel Fetching ---
        if to_fetch:
            pbar = None
            if not progress_cb:
                # Setup a default CLI tqdm progress bar if no GUI callback is registered
                pbar = tqdm(total=len(to_fetch), desc="Fetching chapters", unit="chap")

            completed_count = 0

            def internal_progress_cb(curr, tot, label):
                nonlocal completed_count
                completed_count += 1
                if pbar:
                    pbar.update(1)
                if progress_cb:
                    progress_cb(cached_count + completed_count, len(episodes), label)

            fetched = client.fetch_episodes_parallel(
                to_fetch, max_workers=threads,
                progress_cb=internal_progress_cb,
                on_complete_cb=cache_result
            )
            
            if pbar:
                pbar.close()

            for i, res in enumerate(fetched):
                orig_idx = fetch_indices[i]
                all_results[orig_idx] = res
        failures = []
        for index, result in enumerate(all_results, 1):
            if not result or "error" in result:
                error = result.get("error") if result else "Unknown error"
                failures.append(f"chapter {index}: {error}")
        if failures:
            details = "; ".join(failures[:3])
            if len(failures) > 3:
                details += f"; and {len(failures) - 3} more"
            raise RuntimeError(
                f"Failed to fetch {len(failures)} of {len(episodes)} chapters ({details}). "
                "The existing EPUB was left unchanged."
            )

        # --- Processing Results ---
        for i, res in enumerate(all_results, 1):
            html_text = html_from_episode_text(res["html"])
            epi_title = res["epi_title"]
            signed_key = res.get("signed_key", {})
            if not isinstance(signed_key, dict):
                signed_key = {}
            
            # Fetch the actual episode number out of the result or fallback gracefully
            current_epi_no = str(res.get("epi_no", episodes[i-1].get("episode_no", str(i))))
            
            html_text, new_imgs = add_images_and_rewrite(html_text, epi_no=current_epi_no, episode_cookies=signed_key)

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
        meta_parts.append(f"<p><strong>Chapters:</strong> {len(episodes)}</p>")
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

        out_path = os.path.join(book_dir, f"{base}.epub")
        temp_path = out_path + ".tmp"
        try:
            epub.write_epub(temp_path, book, {})
            os.replace(temp_path, out_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return out_path, title, len(episodes)
