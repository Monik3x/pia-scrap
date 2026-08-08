import logging
from bs4 import BeautifulSoup
from src.helper import normalize_url

logger = logging.getLogger("pia_scrap")

# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------

def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")

    for img in soup.find_all("img"):
        lazy_src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")
        if lazy_src:
            img["src"] = lazy_src
        if "style" in img.attrs:
            del img["style"]
        for attribute in ("srcset", "data-srcset"):
            if attribute in img.attrs:
                del img[attribute]
        if img.get("src"):
            img["src"] = normalize_url(img["src"])

    # Return just the clean inner HTML, without forcing an <html> wrapper
    return "".join(str(tag) for tag in soup.contents)

def fetch_novel_and_episodes(client, novel_id, max_chapters=None):
    logger.info("extracting metadata…")
    data_novel = client.novel(novel_id)

    result = data_novel.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("novel"), dict):
        raise ValueError(f"Novel {novel_id} returned no metadata.")

    nv = result["novel"]
    title = nv.get("novel_name") or f"novel_{novel_id}"
    info = result.get("info") or {}
    if not isinstance(info, dict):
        info = {}
    epi_cnt = info.get("epi_cnt") or nv.get("count_epi") or 0
    writers = result.get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"

    logger.info(f"title='{title}' author='{author}' chapter={epi_cnt} status={status}")

    rows = int(epi_cnt) if epi_cnt else 1000
    data_list = client.episode_list(novel_id, rows=rows)
    if not isinstance(data_list, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    list_result = data_list.get("result") or {}
    if not isinstance(list_result, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    ep_list = list_result.get("list") or []
    if not isinstance(ep_list, list):
        raise ValueError(f"Novel {novel_id} returned an invalid episode list.")

    if max_chapters:
        ep_list = ep_list[:int(max_chapters)]

    return data_novel, ep_list, title
