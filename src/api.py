import random
import time
import uuid
import logging
import threading
import re
from curl_cffi import requests
import concurrent.futures

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from src import const
from src.helper import (
    attach_auth_cookies,
    extract_t_token,
    is_approved_image_url,
    load_config,
    merge_login_at,
    normalize_url,
    save_config,
    unique_in_order,
)
from src.novel import NoEpisodesError, NovelSkipError

logger = logging.getLogger("pia_scrap")


class DownloadCancelled(RuntimeError):
    """The active download was cancelled by the user."""


class NovelUnavailableError(NovelSkipError):
    """The requested novel ID is not assigned or is unavailable."""

    result_status = "not_exist"


class TicketAccessError(RuntimeError):
    """Chapter-scoped ticket denial (0008/0009); do not skip the whole novel."""


CONTENT_FETCH_ATTEMPTS = 3
CONTENT_REMINT_WAIT_SECONDS = 1.0


@dataclass
class Tokens:
    login_at: Optional[str] = None
    tkey: Optional[str] = None
    userkey: Optional[str] = None

class NovelpiaClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: int = 30, throttle: float = 1.5,
                 userkey: Optional[str] = None, tkey: Optional[str] = None,
                 cancel_event: Optional[threading.Event] = None):
        self.s = requests.Session(impersonate="chrome110")
        self.s.headers.update(const.SESSION_HEADERS.copy())
        # Image GETs must not use the API jar; curl_cffi sends jar cookies even with a Cookie header.
        self._image_s = requests.Session(impersonate="chrome110")
        if proxy:
            proxies = {"http": proxy, "https": proxy}
            self.s.proxies.update(proxies)
            self._image_s.proxies.update(proxies)
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        self.cancel_event = cancel_event
        # Serialize refresh and login so concurrent workers cannot renew tokens twice.
        self._auth_lock = threading.RLock() 
        self.throttle = max(0.0, float(1.5 if throttle is None else throttle))
        try:
            if not userkey:
                userkey = uuid.uuid4().hex
            self.s.cookies.set("USERKEY", userkey, domain=".novelpia.com", path="/")
            self.tokens.userkey = userkey
            if tkey:
                self.s.cookies.set("TKEY", tkey, domain=".novelpia.com", path="/")
                self.tokens.tkey = tkey
        except Exception as e:
            logger.error(f"Error setting cookies: {e}")

    def sleep_cooperative(self, seconds: float) -> None:
        """Wait, or raise DownloadCancelled if the cancel event is set."""
        _wait_for_retry(seconds, self.cancel_event, "wait")

    def login(self) -> Optional[str]:
        with self._auth_lock:
            if not self.email or not self.password:
                raise RuntimeError("Email and password are required to log in again.")
            url = f"{const.API_BASE}/v1/member/login"
            r = request_with_retries(
                self.s, "POST", url,
                json={"email": self.email, "passwd": self.password},
                timeout=self.timeout, max_retries=2,
                cancel_event=self.cancel_event
            )
            r.raise_for_status()
            self.tokens.login_at = _login_at_from_payload(r.json())
            try:
                self.tokens.tkey = self.s.cookies.get("TKEY")
                self.tokens.userkey = self.s.cookies.get("USERKEY")
            except Exception:
                pass
            self._persist_tokens()
            return self.tokens.login_at

    def refresh(self) -> Optional[str]:
        with self._auth_lock:
            url = f"{const.API_BASE}/v1/login/refresh"
            r = request_with_retries(
                self.s, "GET", url,
                headers=merge_login_at({}, self.tokens.login_at),
                timeout=self.timeout, max_retries=2,
                cancel_event=self.cancel_event
            )
            r.raise_for_status()
            self.tokens.login_at = _login_at_from_payload(r.json())
            self._persist_tokens()
            return self.tokens.login_at

    def _persist_tokens(self) -> None:
        cfg = load_config()
        cfg["login_at"] = self.tokens.login_at
        if self.tokens.userkey:
            cfg["userkey"] = self.tokens.userkey
        if self.tokens.tkey:
            cfg["tkey"] = self.tokens.tkey
        if not save_config(cfg):
            logger.warning(
                "Authentication token refreshed in memory, but could not be stored."
            )

    def _on_rate_limit(self):
        old = self.throttle
        self.throttle = min(5.0, self.throttle + 0.5)
        if const.HTTP_LOG:
            logger.warning(f"[api] Increased throttle from {old}s to {self.throttle}s due to rate limit.")

    def _api_request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        data: Any = None,
        max_retries: int = 3,
        allow_refresh: bool = True,
    ):
        request_headers = headers
        if request_headers is None:
            request_headers = merge_login_at({}, self.tokens.login_at)
        return request_with_retries(
            self.s,
            method,
            url,
            headers=request_headers,
            params=params,
            json=json,
            data=data,
            timeout=self.timeout,
            max_retries=max_retries,
            allow_refresh=allow_refresh,
            refresh_fn=self.refresh,
            login_fn=self.login,
            on_rate_limit=self._on_rate_limit,
            cancel_event=self.cancel_event,
        )

    def me(self) -> Dict:
        url = f"{const.API_BASE}/v1/login/me"
        r = self._api_request("GET", url)
        r.raise_for_status()
        return r.json()

    def novel(self, novel_id: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel"
        r = self._api_request(
            "GET",
            url,
            # Login-At makes plus sessions report every chapter as free in info.
            headers={},
            params={"novel_no": novel_id},
            max_retries=1,  # Empty/invalid IDs return 500; skip extra backoff
        )
        if _response_indicates_missing_novel(r):
            raise NovelUnavailableError(
                f"Novel {novel_id} is unassigned or unavailable."
            )
        r.raise_for_status()
        return r.json()

    def episode_list(self, novel_id: int, rows: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/list"
        r = self._api_request(
            "GET",
            url,
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            max_retries=1,
        )
        if _response_indicates_missing_episodes(r):
            raise NoEpisodesError(f"Novel {novel_id} has no downloadable episodes.")
        r.raise_for_status()
        return r.json()
    
    def my_library(self) -> List[int]:
        url = f"{const.API_BASE}/v1/novel/like/list"
        novel_ids = []
        page = 1
        
        while True:
            if self.cancel_event and self.cancel_event.is_set():
                raise DownloadCancelled("Library fetch cancelled by user.")

            params = {
                "sort": "desc",
                "sort_col": "lnl.reg_dt",
                "page": page,
                "rows": 100,
                "like_filter": 0
            }
            
            r = self._api_request("GET", url, params=params)
            r.raise_for_status()
            data = r.json()

            items = _result_list(data)
            if not items:
                break
            novel_ids.extend(_novel_ids_from_list_items(items))
            if len(items) < 100:
                break
                
            page += 1
            self.sleep_cooperative(0.5)
            
        return unique_in_order(novel_ids)

    def recent_novels(self, rows: int = 30) -> List[int]:
        if rows < 1:
            raise ValueError("rows must be at least 1")

        url = f"{const.API_BASE}/v1/novel/list"
        r = self._api_request("GET", url, params={"rows": rows})
        r.raise_for_status()
        data = r.json()

        # The global API identifies Korean K-Premium titles by locale.
        return unique_in_order(
            _novel_ids_from_list_items(
                _result_list(data),
                novel_ok=lambda novel: str(novel.get("novel_locale") or "").casefold() == "ko",
            )
        )

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))

        r = self._api_request(
            "GET", url, headers=headers, params=params, max_retries=4
        )
        if _ad_episode_block(r):
            raise TicketAccessError("ad-gated episode")
        if _premium_episode_block(r):
            raise TicketAccessError("premium episode blocked")
        r.raise_for_status()
        return r.json()

    def episode_signed_key(self, episode_no: int) -> Dict[str, Any]:
        """Request fresh signed cookies for an episode's CDN images."""
        ticket = self.episode_ticket(episode_no)
        result = ticket.get("result") if isinstance(ticket, dict) else None
        signed_key = result.get("signed_key") if isinstance(result, dict) else None
        if not isinstance(signed_key, dict) or not signed_key:
            raise RuntimeError("Episode ticket did not contain signed image authorization.")
        return signed_key


    def fetch_image(
        self,
        url: str,
        referer_url: str,
        episode_cookies: Optional[Dict] = None,
        episode_no: Optional[int] = None,
    ) -> Optional[bytes]:
        """GET an image with CDN cookie policy, without leaking session auth cookies."""
        if self.cancel_event and self.cancel_event.is_set():
            raise DownloadCancelled("Image download cancelled by user.")
        url = normalize_url(url)
        if not is_approved_image_url(url):
            logger.warning(f"Blocked image URL outside approved Novelpia hosts: {url}")
            return None

        last_error = "Unknown Error"
        refreshed_signed_key = False
        max_attempts = 3
        attempt = 1
        while attempt <= max_attempts:
            if self.cancel_event and self.cancel_event.is_set():
                raise DownloadCancelled("Image download cancelled by user.")
            try:
                headers = dict(const.IMAGE_HEADERS)
                headers["Referer"] = referer_url

                host = urlparse(url).hostname.lower()
                cookie_policy = const.IMAGE_HOST_COOKIE_POLICY[host]
                cookie_dict = {}
                if cookie_policy == "signed" and isinstance(episode_cookies, dict):
                    cookie_dict = {
                        key: value for key, value in episode_cookies.items()
                        if key in const.SIGNED_IMAGE_COOKIE_NAMES and value
                    }
                elif cookie_policy == "session":
                    for key in ("USERKEY", "TKEY"):
                        try:
                            value = self.s.cookies.get(key)
                        except requests.RequestsError:
                            value = None
                        if value:
                            cookie_dict[key] = value

                headers["Cookie"] = "; ".join(
                    f"{key}={value}" for key, value in cookie_dict.items()
                )

                # Drop leftover CDN Set-Cookie values so they cannot ride the next GET.
                image_cookies = getattr(self._image_s, "cookies", None)
                clear_image_cookies = getattr(image_cookies, "clear", None)
                if callable(clear_image_cookies):
                    clear_image_cookies()

                resp = self._image_s.get(
                    url, headers=headers, timeout=self.timeout, allow_redirects=False
                )

                if resp.status_code in (301, 302, 303, 307, 308):
                    redirect_url = urljoin(url, resp.headers.get("Location", ""))
                    if not is_approved_image_url(redirect_url):
                        last_error = f"Blocked redirect to unapproved host: {redirect_url}"
                        break
                    url = redirect_url
                    attempt += 1
                    continue

                if resp.status_code == 429:
                    last_error = "HTTP 429 (Too Many Requests)"
                    if attempt < max_attempts:
                        self.sleep_cooperative(2.0 * attempt)
                    attempt += 1
                    continue

                if (
                    resp.status_code == 403
                    and cookie_policy == "signed"
                    and episode_no is not None
                    and not refreshed_signed_key
                ):
                    refreshed_signed_key = True
                    try:
                        fresh_cookies = self.episode_signed_key(episode_no)
                        if isinstance(episode_cookies, dict):
                            episode_cookies.clear()
                            episode_cookies.update(fresh_cookies)
                        else:
                            episode_cookies = fresh_cookies
                        logger.info(
                            f"renewed image authorization for episode {episode_no}"
                        )
                        max_attempts += 1
                        attempt += 1
                        continue
                    except DownloadCancelled:
                        raise
                    except (requests.RequestsError, RuntimeError) as exc:
                        last_error = (
                            f"HTTP 403; could not renew image authorization: {exc}"
                        )
                        break

                resp.raise_for_status()
                return resp.content

            except requests.RequestsError as e:
                last_error = f"HTTP Error or Timeout: {e}"
                if attempt < max_attempts:
                    self.sleep_cooperative(1.0)
            attempt += 1

        logger.warning(f"Image error ({last_error}): {url}")
        return None


    def episode_content(self, token_t: str) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/content"
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))

        # Content auth is cookies plus _t; a 403 is a dead ticket, not a dead session.
        r = self._api_request(
            "GET", url, headers={}, params={"_t": token_t},
            max_retries=3, allow_refresh=False,
        )
        if r.status_code == 403:
            raise RuntimeError("content ticket rejected")
        r.raise_for_status()
        return r.json()

    def fetch_episode(self, ep: Dict, idx: int = 0) -> Dict:
        """Return a result dict with chapter HTML or an error; cancel raises DownloadCancelled."""
        self.sleep_cooperative(random.uniform(0.1, 0.6))

        episode_no = ep.get("episode_no")
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        if episode_no is None:
            return {
                "error": "missing episode_no",
                "epi_no": None,
                "epi_title": epi_title,
            }

        epi_no = episode_no
        try:
            epi_no = int(episode_no)

            logger.info(f"ticket for episode {ep.get('epi_num', idx)} - {epi_title}")
            cdata = None
            signed_key = {}
            for attempt in range(1, CONTENT_FETCH_ATTEMPTS + 1):
                tdata = self.episode_ticket(epi_no)

                token_t = extract_t_token(tdata)
                res_block = tdata.get("result") or {}
                signed_key = res_block.get("signed_key", {}) if isinstance(res_block, dict) else {}
                if not isinstance(signed_key, dict):
                    signed_key = {}

                if not token_t:
                    return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title}

                try:
                    cdata = self.episode_content(token_t)
                    break
                except (DownloadCancelled, KeyboardInterrupt):
                    raise
                except Exception as e:
                    if str(e) == "content ticket rejected" and attempt < CONTENT_FETCH_ATTEMPTS:
                        logger.warning(
                            f"content 403 for episode {epi_no}; reminting ticket "
                            f"({attempt}/{CONTENT_FETCH_ATTEMPTS})"
                        )
                        self.sleep_cooperative(CONTENT_REMINT_WAIT_SECONDS)
                        continue
                    return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title}

            if cdata is None:
                return {"error": "content ticket rejected", "epi_no": epi_no, "epi_title": epi_title}

            result_block = cdata.get("result", {})
            data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

            parts = []
            if isinstance(data_block, dict):
                def content_order(k: str):
                    match = re.search(r"(\d+)$", k)
                    return (0 if k == "epi_content" else 1, int(match.group(1)) if match else 0)

                content_keys = [key for key in data_block if str(key).startswith("epi_content")]
                for k in sorted(content_keys, key=content_order):
                    v = data_block.get(k)
                    if isinstance(v, str) and v:
                        parts.append(v)

            html_text = "".join(parts).strip()
            if not html_text:
                html_text = (
                    result_block.get("content")
                    or result_block.get("html")
                    or result_block.get("text")
                    or cdata.get("content")
                    or ""
                )

            if not isinstance(html_text, str) or not html_text.strip():
                return {
                    "error": "episode content was empty",
                    "epi_no": epi_no,
                    "epi_title": epi_title,
                }

            # Raw joined HTML; builder sanitizes once at fetch-complete.
            return {
                "html": html_text,
                "epi_title": epi_title,
                "epi_no": epi_no,
                "signed_key": signed_key,
            }
        except (DownloadCancelled, KeyboardInterrupt):
            raise
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title}

    def fetch_episodes_parallel(self, ep_list: List[Dict[str, Any]], max_workers: int = 2,
                                progress_cb: Optional[Callable[[int, int, str], None]] = None,
                                on_complete_cb=None) -> List[Dict[str, Any]]:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")

        results: List[Dict[str, Any]] = [{} for _ in range(len(ep_list))]
        completed = 0
        total = len(ep_list)

        def store_result(idx: int, result: Dict[str, Any]) -> None:
            nonlocal completed
            results[idx] = result
            completed += 1
            epi_title = result.get("epi_title") or ep_list[idx].get("epi_title") or f"Episode {idx+1}"
            if on_complete_cb:
                on_complete_cb(result)
            if progress_cb:
                progress_cb(completed, total, epi_title)

        if max_workers == 1:
            for idx, episode in enumerate(ep_list):
                if self.cancel_event and self.cancel_event.is_set():
                    raise DownloadCancelled("Download stopped by cancellation request.")
                result = self.fetch_episode(episode, idx + 1)
                store_result(idx, result)
            return results

        # Already cancelled: skip fetch_episode and on_complete_cb.
        if self.cancel_event and self.cancel_event.is_set():
            raise DownloadCancelled("Download stopped by cancellation request.")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.fetch_episode, ep, i+1): i 
                for i, ep in enumerate(ep_list)
            }

            def store_finished_results() -> None:
                for fut, done_idx in future_to_idx.items():
                    if not fut.done() or fut.cancelled() or results[done_idx]:
                        continue
                    try:
                        done_res = fut.result()
                    except Exception:
                        continue
                    if isinstance(done_res, dict):
                        store_result(done_idx, done_res)

            try:
                for future in concurrent.futures.as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        res = future.result()
                    except DownloadCancelled:
                        store_finished_results()
                        raise
                    store_result(idx, res)
                    # Keep a finished chapter so on_complete_cb can write the update cache.
                    if self.cancel_event and self.cancel_event.is_set():
                        raise DownloadCancelled("Download pool stopped by cancellation request.")
            except (KeyboardInterrupt, DownloadCancelled) as e:
                logger.warning(f"Fetch process interrupted gracefully: {e}")
                if isinstance(e, DownloadCancelled):
                    store_finished_results()
                for fut in future_to_idx:
                    fut.cancel()
                raise
        return results

def _result_list(payload: Any) -> List[Any]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result") or {}
    items = result.get("list", []) if isinstance(result, dict) else []
    return items if isinstance(items, list) else []


def _novel_ids_from_list_items(
    items: Any,
    *,
    novel_ok: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> List[int]:
    novel_ids: List[int] = []
    if not isinstance(items, list):
        return novel_ids
    for item in items:
        if not isinstance(item, dict):
            continue
        novel = item.get("novel") or {}
        if not isinstance(novel, dict):
            continue
        if novel_ok is not None and not novel_ok(novel):
            continue
        try:
            novel_id = int(novel.get("novel_no"))
        except (TypeError, ValueError):
            continue
        if novel_id > 0:
            novel_ids.append(novel_id)
    return novel_ids


def _login_at_from_payload(payload: Any) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    login_at = result.get("LOGINAT") if isinstance(result, dict) else None
    if not isinstance(login_at, str) or not login_at.strip():
        raise RuntimeError(
            "Authentication response did not contain a login token."
        )
    return login_at


def _wait_for_retry(seconds: float, cancel_event, reason: str) -> None:
    if seconds <= 0:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled(f"Request cancelled during {reason}.")
        return
    if cancel_event:
        if cancel_event.wait(seconds):
            raise DownloadCancelled(f"Request cancelled during {reason}.")
    else:
        time.sleep(seconds)


def _response_requires_auth(response) -> bool:
    if response.status_code == 401:
        return True

    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        return False

    def iter_error_strings(value):
        if isinstance(value, str):
            yield value.casefold()
        elif isinstance(value, dict):
            for key, nested_value in value.items():
                if str(key).casefold() in {
                    "code", "error", "error_code", "errmsg", "message", "reason", "result"
                }:
                    yield from iter_error_strings(nested_value)
        elif isinstance(value, list):
            for item in value:
                yield from iter_error_strings(item)

    error_text = " ".join(iter_error_strings(body))
    if not error_text:
        return False

    auth_subjects = ("auth", "login", "session", "token", "tkey", "userkey")
    auth_failures = ("expire", "invalid", "missing", "required", "revoked", "not valid")
    return (
        any(subject in error_text for subject in auth_subjects)
        and any(failure in error_text for failure in auth_failures)
    )


def _server_error_messages(response) -> Optional[Dict[str, Any]]:
    """Parse a Novelpia 500 body into messages and result fields, if present."""
    if getattr(response, "status_code", None) != 500:
        return None

    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None

    result = body.get("result")
    if not isinstance(result, dict):
        result = {}

    messages = []
    for value in (body.get("errmsg"), result.get("message")):
        if isinstance(value, str):
            messages.append(value)
    errmsgs = body.get("errmsgs")
    if isinstance(errmsgs, list):
        messages.extend(value for value in errmsgs if isinstance(value, str))

    return {"body": body, "result": result, "messages": messages}


def _response_indicates_missing_novel(response) -> bool:
    """Return whether a server error is Novelpia's missing-novel response."""
    parsed = _server_error_messages(response)
    if not parsed:
        return False

    body = parsed["body"]
    result = parsed["result"]
    has_missing_message = any(
        message.strip().casefold().rstrip(".") == "the novel does not exist"
        for message in parsed["messages"]
    )
    has_novel_error_marker = (
        str(result.get("name", "")).casefold() == "novel_error"
        or str(body.get("code", "")) == "0001"
        or str(result.get("code", "")) == "0001"
    )
    return has_missing_message and has_novel_error_marker


def _response_indicates_missing_episodes(response) -> bool:
    """Return whether a server error means the novel has no episodes to list."""
    parsed = _server_error_messages(response)
    if not parsed:
        return False

    body = parsed["body"]
    result = parsed["result"]
    has_missing_message = any(
        message.strip().casefold().rstrip(".") == "the episode does not exist"
        for message in parsed["messages"]
    )
    has_novel_error_marker = (
        str(result.get("name", "")).casefold() == "novel_error"
        or str(body.get("code", "")) == "0002"
        or str(result.get("code", "")) == "0002"
    )
    return has_missing_message and has_novel_error_marker


def _int_or_decimal(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _ticket_block_ids(result: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    episode_data = data.get("data")
    if not isinstance(episode_data, dict):
        return None
    novel_no = _int_or_decimal(data.get("novel_no") or episode_data.get("novel_no"))
    episode_no = _int_or_decimal(episode_data.get("episode_no"))
    if novel_no is None or episode_no is None:
        return None
    return (novel_no, episode_no)


def _ticket_block(response, code: str, errmsg: str) -> Optional[Tuple[int, int]]:
    parsed = _server_error_messages(response)
    if not parsed:
        return None
    body = parsed["body"]
    if str(body.get("code") or "") != code or body.get("errmsg") != errmsg:
        return None
    return _ticket_block_ids(parsed["result"])


def _ad_episode_block(response) -> Optional[Tuple[int, int]]:
    return _ticket_block(response, "0008", "novel.ADVERTISEMENT_EPISODE")


def _premium_episode_block(response) -> Optional[Tuple[int, int]]:
    return _ticket_block(response, "0009", "novel.PREMIUM_EPISODE")


def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None,
                          login_fn=None, on_rate_limit=None, cancel_event=None):
    """Send a request, retrying transient failures and recovering expired auth."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    def send_request():
        request_headers = headers
        if "/v1/member/login" not in url:
            request_headers = attach_auth_cookies(session, request_headers)
        return session.request(
            method, url, headers=request_headers, params=params,
            json=json, data=data, timeout=timeout
        )

    did_refresh = False
    did_login = False

    for attempt in range(1, max_retries + 1):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Request cancelled.")

        try:
            response = send_request()

            if allow_refresh and _response_requires_auth(response):
                recovery_steps = (
                    ("refresh", refresh_fn, did_refresh),
                    ("login", login_fn, did_login),
                )
                for recovery_name, recovery_fn, already_attempted in recovery_steps:
                    if not recovery_fn or already_attempted:
                        continue
                    try:
                        new_login_at = recovery_fn()
                        if recovery_name == "refresh":
                            did_refresh = True
                        else:
                            did_login = True
                        if new_login_at and (headers is None or "login-at" in headers):
                            # /v1/novel omits login-at; keep that header off the retry.
                            headers = merge_login_at(headers, new_login_at)
                        response = send_request()
                        if not _response_requires_auth(response):
                            break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        if recovery_name == "refresh":
                            did_refresh = True
                        else:
                            did_login = True
                        if const.HTTP_LOG:
                            logger.warning(f"[api] Auth {recovery_name} failed: {exc}")

            if response.status_code == 429:
                if on_rate_limit:
                    on_rate_limit()
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    wait = max(5.0, retry_after, backoff ** attempt)
                    _wait_for_retry(wait, cancel_event, "rate-limit wait")
                    continue

            if response.status_code >= 500:
                # 0008/0009 are access denials, not transient server errors.
                if _ad_episode_block(response) or _premium_episode_block(response):
                    return response
                if attempt < max_retries:
                    _wait_for_retry(backoff ** attempt, cancel_event, "server-error backoff")
                    continue

            return response
        except requests.RequestsError as exc:
            if const.HTTP_LOG:
                logger.error(f"[api] {method} {url} failed on attempt {attempt}: {exc}")
            if attempt >= max_retries:
                raise
            _wait_for_retry(backoff ** attempt, cancel_event, "network-error backoff")

    raise RuntimeError("Request retry loop ended without a response.")
