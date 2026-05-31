import html
import os
import json
import time
import logging
from curl_cffi import requests

from typing import Dict, List, Optional, Tuple, Callable
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from tqdm import tqdm
from src.api import NovelpiaClient
from src.const import BASE_URL
from src.helper import ensure_dir, kebab, media_type_from_ext, normalize_url

logger = logging.getLogger("pia_scrap")

# ----------------------------
# EPUB Builder
# ----------------------------

class EpubBuilder:
    def __init__(self, out_dir: str, debug_dump: bool = False):
        self.out_dir = out_dir
        self.debug_dump = debug_dump
        self._pinged_referers = set()
        ensure_dir(out_dir)

    def _fetch_bytes(self, client: NovelpiaClient, url: str, referer_url: str, episode_cookies: dict = None) -> Optional[bytes]:
        last_error = "Unknown Error"
        for attempt in range(1, 4):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Referer": referer_url,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                # Combine our base cookies with the specific chapter's CloudFront keys!
                cookie_dict = {k: v for k, v in client.s.cookies.items()}
                if episode_cookies:
                    cookie_dict.update(episode_cookies)
                    
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                if cookie_str:
                    headers["Cookie"] = cookie_str
                    
                resp = client.s.get(url, headers=headers, timeout=client.timeout)
                
                if resp.status_code == 429:
                    last_error = "HTTP 429 (Too Many Requests)"
                    time.sleep(2.0 * attempt)
                    continue
                    
                resp.raise_for_status()
                return resp.content
                
            except Exception as e:
                last_error = f"HTTP Error or Timeout: {e}"
                if attempt < 3: time.sleep(1.0)
                
        logger.warning(f"[warn] Image error ({last_error}): {url}")
        return None

    def build(self, client: NovelpiaClient, novel: Dict, episodes: List[Dict],
              filename_hint: Optional[str] = None, language: str = "en",
              author_fallback: str = "Unknown", css_text: Optional[str] = None,
              novel_id: Optional[int] = None, update_mode: bool = False, threads: int = 1,
              progress_cb: Optional[Callable[[int, int, str], None]] = None) -> Tuple[str, str, int]:
        nv = novel["result"]["novel"]
        title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
        writers = novel["result"].get("writer_list") or[]
        author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else author_fallback)
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()

        base = kebab(filename_hint or title)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)

        # --- Setup Cache Directory ---
        cache_dir = os.path.join(book_dir, ".raw_cache")
        if update_mode:
            ensure_dir(cache_dir)

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        # Cover
        cover_url = normalize_url(nv.get("novel_full_img") or nv.get("novel_img") or "")
        novel_referer = f"https://global.novelpia.com/novel/{novel_id}" if novel_id else "https://global.novelpia.com/"
        cover_bytes = self._fetch_bytes(client, cover_url, referer_url=novel_referer) if cover_url else None
        has_cover = False
        if cover_bytes:
            book.set_cover("cover.jpg", cover_bytes)
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
            added_items: List[epub.EpubItem] =[]
            
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
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    ext = ".jpg"

                img_bytes = self._fetch_bytes(client, src, referer_url=viewer_url, episode_cookies=episode_cookies)
                if not img_bytes:
                    continue

                fname = f"images/img_{img_index:05d}{ext}"
                image_cache[src] = fname
                img_index += 1

                item = epub.EpubItem(uid=f"img{img_index}", file_name=fname,
                                     media_type=media_type_from_ext(ext), content=img_bytes)
                added_items.append(item)
                img["src"] = fname

            return str(soup), added_items

        # --- Cache Filter ---
        all_results =[None] * len(episodes)
        to_fetch = []
        fetch_indices =[]

        for idx_offset, ep in enumerate(episodes):
            epi_no = int(ep["episode_no"])
            cache_file = os.path.join(cache_dir, f"{epi_no}.json") if update_mode else None
            
            cached_data = None
            if update_mode and cache_file and os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                except Exception:
                    pass
            
            if cached_data:
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

        # Callback to write cache instantly when a thread returns
        def cache_result(res):
            if update_mode and res and "error" not in res:
                epi_no = res.get("epi_no")
                if epi_no:
                    c_file = os.path.join(cache_dir, f"{epi_no}.json")
                    try:
                        with open(c_file, "w", encoding="utf-8") as f:
                            json.dump(res, f, ensure_ascii=False)
                    except Exception:
                        pass

        # --- Parallel Fetching ---
        if to_fetch:
            pbar = None
            if not progress_cb:
                # Setup a default CLI tqdm progress bar if no GUI callback is registered
                pbar = tqdm(total=len(to_fetch), desc="Fetching chapters", unit="chap")

            completed_count = 0
            total_count = len(to_fetch)

            def internal_progress_cb(curr, tot, label):
                nonlocal completed_count
                completed_count += 1
                if pbar:
                    pbar.update(1)
                if progress_cb:
                    progress_cb(completed_count, total_count, label)

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
        elif progress_cb:
            # If everything was cached, let's trigger one finish update
            progress_cb(len(episodes), len(episodes), "Completed (cached)")

        # --- Processing Results ---
        for i, res in enumerate(all_results, 1):
            if not res or "error" in res:
                err = res.get("error") if res else "Unknown error"
                logger.warning(f"[warn] Failed to fetch chapter {i}: {err}")
                continue

            html_text = res["html"]
            epi_title = res["epi_title"]
            signed_key = res.get("signed_key", {}) # ---> NEW: Get keys <---
            
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
        src_url = f"{BASE_URL}/novel/{novel_id}" if novel_id else ""
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append("<p><img src='cover.jpg' alt='Cover' style='width:230px;max-width:90%;height:auto;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>")
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
        epub.write_epub(out_path, book, {})
        return out_path, title, len(episodes)
