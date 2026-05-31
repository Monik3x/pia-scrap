import json
import os
import logging
from typing import List, Optional, Callable

from bs4 import BeautifulSoup
from tqdm import tqdm
from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_novel_and_episodes

logger = logging.getLogger("pia_scrap")

# ----------------------------
# Main Build Function
# ----------------------------

def build_epub(client, novel_id, out_dir, max_chapters=None, language="en", debug_dump=False, update_mode=False, threads=1,
               progress_cb: Optional[Callable[[int, int, str], None]] = None,
               status_cb: Optional[Callable[[str], None]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters=max_chapters)

    if update_mode:
        base = kebab(title)
        book_dir = os.path.join(out_dir, base)
        meta_path = os.path.join(book_dir, "metadata.json")
        
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    existing_chapters = meta.get("chapter", 0)
                    target_chapters = len(ep_list)
                        
                    if existing_chapters >= target_chapters and target_chapters > 0:
                        msg = f"'{title}' is already up to date ({existing_chapters} chapters). Skipping API fetch."
                        logger.info(f"{msg}")
                        if status_cb:
                            status_cb(msg)
                        return None, title, existing_chapters
            except Exception:
                pass

    if status_cb:
        status_cb("Downloading chapters and building EPUB...")
    builder = EpubBuilder(out_dir, debug_dump=debug_dump)
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
    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)
    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)   

    return out_file, title, count

def build_txt(client, novel_id, out_dir, max_chapters=None, language="en", debug_dump=False, threads=1,
              progress_cb: Optional[Callable[[int, int, str], None]] = None,
              status_cb: Optional[Callable[[str], None]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

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
        status_cb("Downloading chapters in parallel...")
    fetched_results = client.fetch_episodes_parallel(
        ep_list, max_workers=threads, progress_cb=internal_progress_cb
    )
    if pbar:
        pbar.close()

    if status_cb:
        status_cb("Saving TXT files to disk...")
    success_count = 0
    for i, res in enumerate(fetched_results, 1):
        if not res or "error" in res:
            err = res.get("error") if res else "Unknown error"
            logger.warning(f"[warn] Failed to fetch chapter {i}: {err}")
            continue

        html_text = res["html"]
        epi_title = res["epi_title"]

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)

        success_count += 1
    
    if status_cb:
        status_cb("Writing metadata files...")
    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)

    return book_dir, title, success_count

def build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters=None):
    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")

    result = data_novel.get("result") or {}
    info = result.get("info") if isinstance(result, dict) else {}
    epi_cnt = info.get("epi_cnt") or nv.get("count_epi") or 0

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
        "title": nv.get("novel_name") or title,
        "author": author,
        "tags": uniq_tags,
        "chapter": len(ep_list) if (max_chapters and max_chapters > 0) else (int(epi_cnt) if epi_cnt else len(ep_list)),
        "status": status,
        "description": description,
    }

    meta_path = os.path.join(book_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    chapters_path = os.path.join(book_dir, "chapters.jsonl")
    with open(chapters_path, "w", encoding="utf-8") as f:
        for idx, ep in enumerate(ep_list, 1):
            epi_no = int(ep.get("episode_no"))
            epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
            rec = {"idx": idx, "title": epi_title, "url": f"https://global.novelpia.com/viewer/{epi_no}"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
