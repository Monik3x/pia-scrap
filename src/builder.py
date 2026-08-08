import json
import os
import logging
import zipfile
from typing import List, Optional, Callable

from bs4 import BeautifulSoup
from tqdm import tqdm
from src.epub import EpubBuilder
from src.helper import book_base, ensure_dir, sanitize_filename, write_json_atomic, write_text_atomic
from src.novel import fetch_novel_and_episodes

logger = logging.getLogger("pia_scrap")

# ----------------------------
# Main Build Function
# ----------------------------

def _epub_chapter_count(epub_path: str) -> int:
    """Return the number of generated chapter documents, or -1 if invalid."""
    try:
        with zipfile.ZipFile(epub_path) as book:
            return sum(
                1
                for name in book.namelist()
                if os.path.basename(name).startswith("chap_") and name.endswith(".xhtml")
            )
    except (OSError, zipfile.BadZipFile):
        return -1

def build_epub(client, novel_id, out_dir, max_chapters=None, language="en", update_mode=False, threads=1,
               progress_cb: Optional[Callable[[int, int, str], None]] = None,
               status_cb: Optional[Callable[[str], None]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters=max_chapters)
    base = book_base(out_dir, title, novel_id)
    book_dir = os.path.join(out_dir, base)

    if update_mode:
        meta_path = os.path.join(book_dir, "metadata.json")
        epub_path = os.path.join(book_dir, f"{base}.epub")
        
        if os.path.exists(meta_path) and os.path.exists(epub_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                existing_chapters = int(meta.get("chapter", 0))
                target_chapters = len(ep_list)
                packaged_chapters = _epub_chapter_count(epub_path)
                if (
                    existing_chapters >= target_chapters
                    and packaged_chapters >= target_chapters
                    and existing_chapters == packaged_chapters
                    and target_chapters > 0
                ):
                    msg = f"'{title}' is already up to date ({existing_chapters} chapters). Skipping chapter downloads."
                    logger.info(msg)
                    if status_cb:
                        status_cb(msg)
                    return None, title, existing_chapters
                if packaged_chapters != existing_chapters:
                    logger.warning(
                        f"[warn] Rebuilding '{title}': metadata lists {existing_chapters} "
                        f"chapters but the EPUB contains {max(packaged_chapters, 0)}."
                    )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(f"[warn] Ignoring invalid update metadata {meta_path}: {exc}")

    if status_cb:
        status_cb("Downloading chapters and building EPUB...")
    builder = EpubBuilder(out_dir)
    out_file, title, count = builder.build(
        client=client,
        threads=threads,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        update_mode=update_mode,
        progress_cb=progress_cb
    )

    if status_cb:
        status_cb("Finalizing metadata...")
    ensure_dir(book_dir)
    build_metadata(book_dir, data_novel, novel_id, ep_list)

    return out_file, title, count

def build_txt(client, novel_id, out_dir, max_chapters=None, threads=1,
              progress_cb: Optional[Callable[[int, int, str], None]] = None,
              status_cb: Optional[Callable[[str], None]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    base = book_base(out_dir, title, novel_id)
    book_dir = os.path.join(out_dir, base)

    total = len(ep_list)
    pbar = None
    if not progress_cb:
        pbar = tqdm(total=total, desc="Exporting TXT", unit="chap")

    completed = 0
    def internal_progress_cb(curr, tot, label):
        nonlocal completed
        completed += 1
        if pbar:
            pbar.update(1)
        if progress_cb:
            progress_cb(completed, total, label)

    if status_cb:
        status_cb("Downloading chapters...")
    fetched_results = client.fetch_episodes_parallel(
        ep_list, max_workers=threads, progress_cb=internal_progress_cb
    )
    if pbar:
        pbar.close()

    failures = []
    for i, result in enumerate(fetched_results, 1):
        if not result or "error" in result:
            error = result.get("error") if result else "Unknown error"
            failures.append(f"chapter {i}: {error}")
    if failures:
        details = "; ".join(failures[:3])
        if len(failures) > 3:
            details += f"; and {len(failures) - 3} more"
        raise RuntimeError(
            f"Failed to fetch {len(failures)} of {total} chapters ({details}). "
            "No TXT files were written."
        )

    ensure_dir(book_dir)
    if status_cb:
        status_cb("Saving TXT files to disk...")
    for i, res in enumerate(fetched_results, 1):

        html_text = res["html"]
        epi_title = res["epi_title"]

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        write_text_atomic(os.path.join(book_dir, fname), text)

    if status_cb:
        status_cb("Writing metadata files...")
    build_metadata(book_dir, data_novel, novel_id, ep_list)

    return book_dir, title, len(fetched_results)

def build_metadata(book_dir, data_novel, novel_id, ep_list):
    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name") or f"novel_{nv.get('novel_no', '')}"

    writers = data_novel["result"].get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    description = (nv.get("novel_story") or "").strip()
    
    # Tags can be in result.tag_list or novel.tag_list, accept str or dict with name fields
    tag_items = (data_novel.get("result", {}).get("tag_list")
                 or nv.get("tag_list")
                 or [])
    tags: List[str] = []
    for t in tag_items:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            val = t.get("tag_name") or t.get("name") or t.get("title")
            if isinstance(val, str):
                tags.append(val)

    seen = set()
    uniq_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq_tags.append(t)

    meta = {
        "url": f"https://global.novelpia.com/novel/{novel_id}",
        "novel_id": int(novel_id),
        "title": nv.get("novel_name") or title,
        "author": author,
        "tags": uniq_tags,
        "chapter": len(ep_list),
        "status": status,
        "description": description,
    }

    chapters_path = os.path.join(book_dir, "chapters.jsonl")
    chapter_lines = []
    for idx, ep in enumerate(ep_list, 1):
        epi_no = int(ep.get("episode_no"))
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        rec = {"idx": idx, "title": epi_title, "url": f"https://global.novelpia.com/viewer/{epi_no}"}
        chapter_lines.append(json.dumps(rec, ensure_ascii=False))
    write_text_atomic(chapters_path, "\n".join(chapter_lines) + ("\n" if chapter_lines else ""))

    # Metadata is the update-mode commit marker, so write it last.
    meta_path = os.path.join(book_dir, "metadata.json")
    write_json_atomic(meta_path, meta, indent=2)
