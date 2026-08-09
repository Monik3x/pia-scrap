import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Comment
from src.helper import normalize_url

logger = logging.getLogger("pia_scrap")

EPUB_ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "caption", "code", "col", "colgroup",
    "dd", "del", "div", "dl", "dt", "em", "figcaption", "figure", "h1",
    "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "li", "ol",
    "p", "pre", "q", "ruby", "rp", "rt", "s", "small", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "u", "ul",
}
EPUB_DROP_CONTENT_TAGS = {
    "applet", "audio", "button", "canvas", "embed", "form", "iframe", "input",
    "link", "meta", "noscript", "object", "script", "select", "source", "style",
    "svg", "template", "textarea", "video",
}
EPUB_GLOBAL_ATTRIBUTES = {"class", "dir", "id", "lang", "title"}
EPUB_TAG_ATTRIBUTES = {
    "a": {"href"},
    "col": {"span"},
    "img": {"alt", "height", "src", "width"},
    "li": {"value"},
    "ol": {"reversed", "start", "type"},
    "q": {"cite"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}
SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


@dataclass(frozen=True)
class NovelMetadata:
    novel: Dict[str, Any]
    novel_id: Optional[int]
    title: str
    author: str
    status: str
    description: str
    episode_count: int
    tags: List[str]


def parse_novel_metadata(
    data: Dict[str, Any],
    novel_id: Optional[int] = None,
    author_fallback: str = "Unknown Author",
) -> NovelMetadata:
    """Validate a novel API payload and normalize metadata used by all outputs."""
    if not isinstance(data, dict):
        raise ValueError("Novel response returned no metadata.")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("novel"), dict):
        raise ValueError("Novel response returned no metadata.")

    nv = result["novel"]
    raw_novel_id = novel_id if novel_id is not None else nv.get("novel_no")
    resolved_novel_id = None
    if raw_novel_id is not None:
        try:
            resolved_novel_id = int(raw_novel_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Novel metadata contains an invalid novel ID.") from exc
        if resolved_novel_id <= 0:
            raise ValueError("Novel metadata contains an invalid novel ID.")

    raw_title = nv.get("novel_name")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    title = title or f"novel_{resolved_novel_id or ''}"

    author = author_fallback
    writers = result.get("writer_list")
    if isinstance(writers, list) and writers and isinstance(writers[0], dict):
        writer_name = writers[0].get("writer_name")
        if isinstance(writer_name, str) and writer_name.strip():
            author = writer_name.strip()

    info = result.get("info")
    raw_episode_count = info.get("epi_cnt") if isinstance(info, dict) else None
    if raw_episode_count in (None, ""):
        raw_episode_count = nv.get("count_epi", 0)
    try:
        episode_count = max(0, int(raw_episode_count or 0))
    except (TypeError, ValueError):
        episode_count = 0

    raw_description = nv.get("novel_story")
    description = raw_description.strip() if isinstance(raw_description, str) else ""
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"

    tag_items = result.get("tag_list") or nv.get("tag_list") or []
    if not isinstance(tag_items, list):
        tag_items = []
    tags = []
    seen = set()
    for item in tag_items:
        value = item if isinstance(item, str) else None
        if isinstance(item, dict):
            value = item.get("tag_name") or item.get("name") or item.get("title")
        if isinstance(value, str) and value not in seen:
            seen.add(value)
            tags.append(value)

    return NovelMetadata(
        novel=nv,
        novel_id=resolved_novel_id,
        title=title,
        author=author,
        status=status,
        description=description,
        episode_count=episode_count,
        tags=tags,
    )

# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------

def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")

    for img in soup.find_all("img"):
        lazy_src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")
        if lazy_src:
            img["src"] = lazy_src

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        if not tag.name:
            continue
        name = tag.name.lower()
        if name in EPUB_DROP_CONTENT_TAGS:
            tag.decompose()
        elif name not in EPUB_ALLOWED_TAGS:
            tag.unwrap()

    for tag in soup.find_all(True):
        allowed_attributes = EPUB_GLOBAL_ATTRIBUTES | EPUB_TAG_ATTRIBUTES.get(tag.name, set())
        for attribute in list(tag.attrs):
            if attribute.lower() not in allowed_attributes:
                del tag.attrs[attribute]

        if tag.name == "a" and tag.get("href"):
            href = tag["href"].strip()
            scheme = href.split(":", 1)[0].lower() if ":" in href else ""
            if not (href.startswith(("#", "/")) or scheme in SAFE_LINK_SCHEMES):
                del tag["href"]

    for img in soup.find_all("img"):
        if img.get("src"):
            img["src"] = normalize_url(img["src"])

    # Return just the clean inner HTML, without forcing an <html> wrapper
    return "".join(str(tag) for tag in soup.contents)

def fetch_novel_and_episodes(client, novel_id, max_chapters=None):
    logger.info("extracting metadata…")
    data_novel = client.novel(novel_id)

    try:
        metadata = parse_novel_metadata(data_novel, novel_id)
    except ValueError as exc:
        raise ValueError(f"Novel {novel_id} returned no metadata.") from exc

    logger.info(
        f"title='{metadata.title}' author='{metadata.author}' "
        f"chapter={metadata.episode_count} status={metadata.status}"
    )

    rows = metadata.episode_count or 1000
    data_list = client.episode_list(novel_id, rows=rows)
    if not isinstance(data_list, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    list_result = data_list.get("result") or {}
    if not isinstance(list_result, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    ep_list = list_result.get("list", [])
    if not isinstance(ep_list, list):
        raise ValueError(f"Novel {novel_id} returned an invalid episode list.")

    if max_chapters:
        ep_list = ep_list[:int(max_chapters)]

    return data_novel, ep_list, metadata.title
