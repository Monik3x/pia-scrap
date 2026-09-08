import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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

class NovelSkipError(ValueError):
    """A novel the download queue should skip instead of failing the run."""

    result_status = "failed"

class NoMetadataError(NovelSkipError):
    """The novel payload had no usable metadata."""

    result_status = "not_exist"

class NoEpisodesError(NovelSkipError):
    """The novel has no downloadable prose episodes."""

    result_status = "no_data"

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


def user_subscription_status(me_response: Any) -> str:
    if not isinstance(me_response, dict):
        return "unknown"
    result = me_response.get("result")
    if not isinstance(result, dict):
        return "unknown"
    login = result.get("login")
    if not isinstance(login, dict):
        return "unknown"
    if result.get("subscription") is not None:
        return "paid"
    plus_type = login.get("mem_plus_type")
    if isinstance(plus_type, int):
        return "paid" if plus_type != 0 else "free"
    if isinstance(plus_type, str) and plus_type.isdecimal():
        return "paid" if int(plus_type) != 0 else "free"
    return "unknown"


def _info_count(info: Any, key: str) -> int:
    raw = info.get(key) if isinstance(info, dict) else None
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _is_webtoon_episode(episode: Any) -> bool:
    """Return whether an episode-list entry is a webtoon without prose content."""
    if not isinstance(episode, dict):
        return False
    return (
        str(episode.get("flag_content")) == "1"
        and str(episode.get("flag_type")) == "0"
    )


def parse_novel_metadata(
    data: Dict[str, Any],
    novel_id: Optional[int] = None,
    author_fallback: str = "Unknown Author",
) -> NovelMetadata:
    """Validate a novel API payload and normalize metadata used by all outputs."""
    if not isinstance(data, dict):
        raise NoMetadataError("Novel response returned no metadata.")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("novel"), dict):
        raise NoMetadataError("Novel response returned no metadata.")

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
    if str(nv.get("flag_complete", 0)) == "1":
        status = "Completed"
    elif str(nv.get("flag_live", 0)) == "2":
        status = "Discontinued"
    else:
        status = "Ongoing"

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

    return "".join(str(tag) for tag in soup.contents)

def fetch_novel_and_episodes(
    client, novel_id, max_chapters=None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], NovelMetadata]:
    logger.info("extracting metadata…")
    data_novel = client.novel(novel_id)

    try:
        metadata = parse_novel_metadata(data_novel, novel_id)
    except NoMetadataError as exc:
        raise NoMetadataError(f"Novel {novel_id} returned no metadata.") from exc

    info = (data_novel.get("result") or {}).get("info") if isinstance(data_novel, dict) else None
    ad_cnt = _info_count(info, "ad_epi_cnt")
    premium_cnt = _info_count(info, "premium_epi_cnt")
    logger.info(
        f"title='{metadata.title}' author='{metadata.author}' "
        f"chapter={metadata.episode_count} status={metadata.status} "
        f"ad={ad_cnt} premium={premium_cnt}"
    )

    try:
        me_payload = client.me()
    except Exception:
        logger.warning("could not read account status")
        status = "unknown"
        nick = None
    else:
        status = user_subscription_status(me_payload)
        login = (
            (me_payload.get("result") or {}).get("login")
            if isinstance(me_payload, dict)
            else None
        )
        raw_nick = login.get("mem_nick") if isinstance(login, dict) else None
        nick = raw_nick.strip() if isinstance(raw_nick, str) and raw_nick.strip() else None

    if nick:
        logger.info(f"account={status} nick='{nick}'")
    else:
        logger.info(f"account={status}")

    if metadata.episode_count == 0:
        raise NoEpisodesError(f"Novel {novel_id} has no downloadable episodes.")

    data_list = client.episode_list(novel_id, rows=metadata.episode_count)
    if not isinstance(data_list, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    list_result = data_list.get("result") or {}
    if not isinstance(list_result, dict):
        raise ValueError(f"Novel {novel_id} returned an invalid episode response.")
    ep_list = list_result.get("list", [])
    if not isinstance(ep_list, list):
        raise ValueError(f"Novel {novel_id} returned an invalid episode list.")

    webtoon_count = sum(1 for episode in ep_list if _is_webtoon_episode(episode))
    if webtoon_count:
        logger.info(
            f"skipping {webtoon_count} webtoon episode"
            f"{'s' if webtoon_count != 1 else ''} without prose content"
        )
        ep_list = [episode for episode in ep_list if not _is_webtoon_episode(episode)]

    if max_chapters:
        ep_list = ep_list[:int(max_chapters)]

    if not ep_list:
        raise NoEpisodesError(f"Novel {novel_id} has no downloadable episodes.")

    return data_novel, ep_list, metadata
