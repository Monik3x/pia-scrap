import json
import os
import logging
import zipfile
from typing import Callable, Dict, List, Optional

from bs4 import BeautifulSoup
from tqdm import tqdm
from src.const import BASE_URL, EPISODE_REVISION_FIELD
from src.epub import EpubBuilder
from src.helper import book_output_paths, ensure_dir, sanitize_filename, write_json_atomic, write_text_atomic
from src.novel import fetch_novel_and_episodes, parse_novel_metadata

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


def _chapter_revisions_match(chapters_path: str, episodes: List[Dict]) -> bool:
    """Return whether stored chapter revisions match the current episode list."""
    try:
        with open(chapters_path, "r", encoding="utf-8") as chapter_file:
            stored_chapters = [
                json.loads(line) for line in chapter_file if line.strip()
            ]
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[warn] Ignoring invalid chapter metadata {chapters_path}: {exc}")
        return False

    if len(stored_chapters) < len(episodes):
        return False

    for stored, episode in zip(stored_chapters, episodes):
        if (
            not isinstance(stored, dict)
            or EPISODE_REVISION_FIELD not in stored
            or EPISODE_REVISION_FIELD not in episode
            or episode[EPISODE_REVISION_FIELD] is None
        ):
            return False
        try:
            stored_episode_no = int(stored.get("episode_no"))
            current_episode_no = int(episode.get("episode_no"))
        except (TypeError, ValueError):
            return False
        if stored_episode_no != current_episode_no:
            return False
        if stored[EPISODE_REVISION_FIELD] != episode[EPISODE_REVISION_FIELD]:
            return False

    return True


def should_skip_epub_update(
    *,
    revisions_match: bool,
    epub_exists: bool,
    existing_chapters: int,
    packaged_chapters: int,
    target_chapters: int,
) -> bool:
    """Skip rebuild when the local EPUB already covers the requested chapters."""
    return (
        revisions_match
        and epub_exists
        and target_chapters > 0
        and existing_chapters == packaged_chapters
        and existing_chapters >= target_chapters
    )


def _cached_episode_result(cache_file: str, epi_no: int, episode: Dict) -> Optional[Dict]:
    try:
        with open(cache_file, "r", encoding="utf-8") as cache_handle:
            cached_data = json.load(cache_handle)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[warn] Ignoring invalid episode cache {cache_file}: {exc}")
        return None

    try:
        cache_is_valid = (
            isinstance(cached_data, dict)
            and isinstance(cached_data.get("html"), str)
            and int(cached_data.get("epi_no")) == epi_no
            and EPISODE_REVISION_FIELD in cached_data
            and EPISODE_REVISION_FIELD in episode
            and episode[EPISODE_REVISION_FIELD] is not None
            and cached_data[EPISODE_REVISION_FIELD] == episode[EPISODE_REVISION_FIELD]
        )
    except (TypeError, ValueError):
        return None

    if not cache_is_valid:
        return None
    cached_result = dict(cached_data)
    cached_result.pop("signed_key", None)
    return cached_result


def _write_episode_cache(cache_file: str, result: Dict, revision) -> None:
    epi_no = result.get("epi_no")
    try:
        cache_record = dict(result)
        cache_record.pop("signed_key", None)
        cache_record[EPISODE_REVISION_FIELD] = revision
        write_json_atomic(cache_file, cache_record)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(f"[warn] Could not cache episode {epi_no}: {exc}")

def _raise_chapter_fetch_failures(results: List[Optional[Dict]], unchanged_message: str) -> None:
    failures = []
    for index, result in enumerate(results, 1):
        if not result or "error" in result:
            error = result.get("error") if result else "Unknown error"
            failures.append(f"chapter {index}: {error}")
    if failures:
        details = "; ".join(failures[:3])
        if len(failures) > 3:
            details += f"; and {len(failures) - 3} more"
        raise RuntimeError(
            f"Failed to fetch {len(failures)} of {len(results)} chapters ({details}). "
            f"{unchanged_message}"
        )


def _load_or_fetch_episodes(
    client,
    episodes: List[Dict],
    cache_dir: str,
    update_mode: bool,
    threads: int = 1,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict]:
    """Return chapter payloads from .raw_cache and/or the network, in list order."""
    all_results: List[Optional[Dict]] = [None] * len(episodes)
    to_fetch = []
    fetch_indices = []

    for idx_offset, ep in enumerate(episodes):
        epi_no = int(ep["episode_no"])
        cached_result = None
        if update_mode:
            cached_result = _cached_episode_result(
                os.path.join(cache_dir, f"{epi_no}.json"),
                epi_no,
                ep,
            )
        if cached_result is not None:
            all_results[idx_offset] = cached_result
            epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num', idx_offset+1)}"
            logger.info(f"loaded cached episode {ep.get('epi_num', idx_offset+1)} - {epi_title}")
        else:
            to_fetch.append(ep)
            fetch_indices.append(idx_offset)

    cached_count = len(episodes) - len(to_fetch)
    if cached_count > 0:
        logger.info(f"Successfully loaded {cached_count} chapters instantly from local cache.")
        if progress_cb:
            progress_cb(cached_count, len(episodes), "Loaded from cache")

    episode_revisions = {
        int(ep["episode_no"]): ep.get(EPISODE_REVISION_FIELD) for ep in episodes
    }

    if to_fetch:
        pbar = None
        if not progress_cb:
            pbar = tqdm(total=len(to_fetch), desc="Fetching chapters", unit="chap")

        completed_count = 0

        def internal_progress_cb(curr, tot, label):
            nonlocal completed_count
            completed_count += 1
            if pbar:
                pbar.update(1)
            if progress_cb:
                progress_cb(cached_count + completed_count, len(episodes), label)

        def cache_fetched_episode(result):
            if not result or "error" in result:
                return
            epi_no = result.get("epi_no")
            if not epi_no:
                return
            _write_episode_cache(
                os.path.join(cache_dir, f"{epi_no}.json"),
                result,
                episode_revisions[int(epi_no)],
            )

        fetched = client.fetch_episodes_parallel(
            to_fetch, max_workers=threads,
            progress_cb=internal_progress_cb,
            on_complete_cb=cache_fetched_episode if update_mode else None,
        )

        if pbar:
            pbar.close()

        for i, res in enumerate(fetched):
            orig_idx = fetch_indices[i]
            all_results[orig_idx] = res

    return all_results


def build_epub(client, novel_id, out_dir, max_chapters=None, language="en", update_mode=False, threads=1,
               progress_cb: Optional[Callable[[int, int, str], None]] = None,
               status_cb: Optional[Callable[[str], None]] = None,
               book_index: Optional[Dict[int, str]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters=max_chapters)
    paths = book_output_paths(out_dir, title, novel_id, book_index=book_index)
    book_dir = paths.book_dir

    if update_mode:
        meta_path = paths.metadata_path
        epub_path = paths.epub_path
        existing_chapters = None

        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if not isinstance(meta, dict):
                    raise ValueError("metadata root must be an object")
                existing_chapters = int(meta.get("chapter", 0))
                target_chapters = len(ep_list)
                revisions_match = _chapter_revisions_match(paths.chapters_path, ep_list)
                packaged_chapters = _epub_chapter_count(epub_path) if os.path.exists(epub_path) else -1
                if should_skip_epub_update(
                    revisions_match=revisions_match,
                    epub_exists=os.path.exists(epub_path),
                    existing_chapters=existing_chapters,
                    packaged_chapters=packaged_chapters,
                    target_chapters=target_chapters,
                ):
                    return None, title, existing_chapters
                if not revisions_match:
                    logger.info(
                        f"Rebuilding '{title}'. One or more chapter revisions changed or are missing."
                    )
                if packaged_chapters != existing_chapters:
                    logger.warning(
                        f"[warn] Rebuilding '{title}': metadata lists {existing_chapters} "
                        f"chapters but the EPUB contains {max(packaged_chapters, 0)}."
                    )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                existing_chapters = None
                logger.warning(f"[warn] Ignoring invalid update metadata {meta_path}: {exc}")

        if (
            existing_chapters is not None
            and max_chapters
            and existing_chapters > len(ep_list)
        ):
            logger.info(
                f"Rebuilding '{title}' without reducing {existing_chapters} local "
                f"chapters to {len(ep_list)}."
            )
            if status_cb:
                status_cb(
                    "Fetching full episode list so update does not shrink the local book..."
                )
            data_novel, ep_list, title = fetch_novel_and_episodes(
                client, novel_id, max_chapters=None
            )

    if status_cb:
        status_cb("Downloading chapters and building EPUB...")
    chapters = _load_or_fetch_episodes(
        client,
        ep_list,
        paths.cache_dir,
        update_mode,
        threads=threads,
        progress_cb=progress_cb,
    )
    _raise_chapter_fetch_failures(chapters, "The existing EPUB was left unchanged.")

    builder = EpubBuilder(out_dir)
    out_file, title, count = builder.build(
        novel=data_novel,
        chapters=chapters,
        fetch_image=client.fetch_image,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        update_mode=update_mode,
        output_paths=paths,
        cancel_event=client.cancel_event,
    )

    if status_cb:
        status_cb("Finalizing metadata...")
    ensure_dir(book_dir)
    build_metadata(book_dir, data_novel, novel_id, ep_list)

    return out_file, title, count

def build_txt(client, novel_id, out_dir, max_chapters=None, threads=1,
              update_mode: bool = False,
              progress_cb: Optional[Callable[[int, int, str], None]] = None,
              status_cb: Optional[Callable[[str], None]] = None,
              book_index: Optional[Dict[int, str]] = None):
    if status_cb:
        status_cb("Fetching novel metadata and episode list...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    paths = book_output_paths(out_dir, title, novel_id, book_index=book_index)
    book_dir = paths.book_dir

    if status_cb:
        status_cb("Downloading chapters...")
    fetched_results = _load_or_fetch_episodes(
        client,
        ep_list,
        paths.cache_dir,
        update_mode,
        threads=threads,
        progress_cb=progress_cb,
    )
    _raise_chapter_fetch_failures(fetched_results, "No TXT files were written.")

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
    metadata = parse_novel_metadata(data_novel, novel_id)

    meta = {
        "url": f"{BASE_URL}/novel/{novel_id}",
        "novel_id": metadata.novel_id,
        "title": metadata.title,
        "author": metadata.author,
        "tags": metadata.tags,
        "chapter": len(ep_list),
        "status": metadata.status,
        "description": metadata.description,
    }

    chapters_path = os.path.join(book_dir, "chapters.jsonl")
    chapter_lines = []
    for idx, ep in enumerate(ep_list, 1):
        epi_no = int(ep.get("episode_no"))
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        rec = {
            "idx": idx,
            "episode_no": epi_no,
            "title": epi_title,
            "url": f"{BASE_URL}/viewer/{epi_no}",
            EPISODE_REVISION_FIELD: ep.get(EPISODE_REVISION_FIELD),
        }
        chapter_lines.append(json.dumps(rec, ensure_ascii=False))
    write_text_atomic(chapters_path, "\n".join(chapter_lines) + ("\n" if chapter_lines else ""))

    # Metadata is the update-mode commit marker, so write it last.
    meta_path = os.path.join(book_dir, "metadata.json")
    write_json_atomic(meta_path, meta, indent=2)
