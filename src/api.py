import random
import time
import uuid
import logging
import threading
from curl_cffi import requests
import concurrent.futures
import re as _re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable
from src import const
from src.helper import (
    attach_auth_cookies,
    extract_t_token,
    load_config,
    merge_login_at,
    save_config,
    unique_in_order,
)
from src.novel import NoEpisodesError, NovelSkipError, html_from_episode_text

logger = logging.getLogger("pia_scrap")

# ----------------------------
# API Client
# ----------------------------

class NovelUnavailableError(NovelSkipError):
    """The requested novel ID is not assigned or is unavailable."""

    result_status = "not_exist"


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
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        self.cancel_event = cancel_event
        # To avoid duplicate refreshes later on possible expiration mid process
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

    def sleep_cooperative(self, seconds: float):
        """Sleep until the delay expires or cancellation is requested."""
        if seconds <= 0:
            return
        if self.cancel_event:
            self.cancel_event.wait(seconds)
        else:
            time.sleep(seconds)

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
            self.tokens.login_at = r.json()["result"]["LOGINAT"]
            try:
                self.tokens.tkey = self.s.cookies.get("TKEY")
                self.tokens.userkey = self.s.cookies.get("USERKEY")
            except Exception:
                pass
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
            self.tokens.login_at = r.json()["result"]["LOGINAT"]
            cfg = load_config()
            cfg["login_at"] = self.tokens.login_at
            if not save_config(cfg):
                logger.warning(
                    "Authentication token refreshed in memory, but could not be stored."
                )
            return self.tokens.login_at

    def _on_rate_limit(self):
        old = self.throttle
        self.throttle = min(5.0, self.throttle + 0.5) # Reduced harsh penalty
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
        """Authenticated JSON API call with shared refresh, login, and rate-limit recovery."""
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
        """Fetches the user's bookmarked novels from their library."""
        url = f"{const.API_BASE}/v1/novel/like/list"
        novel_ids = []
        page = 1
        
        while True:
            if self.cancel_event and self.cancel_event.is_set():
                logger.info("Library fetch interrupted by user cancel command.")
                break

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
            
            res_block = data.get("result") or {}
            items = res_block.get("list", []) if isinstance(res_block, dict) else []
            if not items:
                break
                
            for item in items:
                novel_no = item.get("novel", {}).get("novel_no")
                if novel_no:
                    novel_ids.append(int(novel_no))
                    
            if len(items) < 100:
                # we've hit the last page
                break
                
            page += 1
            self.sleep_cooperative(0.5)
            
        return unique_in_order(novel_ids)

    def recent_novels(self, rows: int = 30) -> List[int]:
        """Fetch recent public K-Premium novel IDs, newest first."""
        if rows < 1:
            raise ValueError("rows must be at least 1")

        url = f"{const.API_BASE}/v1/novel/list"
        r = self._api_request("GET", url, params={"rows": rows})
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, dict):
            return []
        result = data.get("result") or {}
        items = result.get("list", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            return []

        novel_ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            novel = item.get("novel") or {}
            if not isinstance(novel, dict):
                continue
            # The global API identifies Korean K-Premium titles by locale.
            if str(novel.get("novel_locale") or "").casefold() != "ko":
                continue
            novel_no = novel.get("novel_no")
            try:
                novel_id = int(novel_no)
            except (TypeError, ValueError):
                continue
            if novel_id > 0:
                novel_ids.append(novel_id)

        return unique_in_order(novel_ids)

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))
        
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Request cancelled by user request.")

        r = self._api_request(
            "GET", url, headers=headers, params=params, max_retries=4
        )
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

    def episode_content(self, token_t: str) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/content"
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))
            
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Request cancelled by user request.")

        r = self._api_request(
            "GET", url, params={"_t": token_t}, max_retries=3
        )
        r.raise_for_status()
        return r.json()

    def fetch_episode(self, ep: Dict, idx: int = 0) -> Dict:
        if self.cancel_event and self.cancel_event.is_set():
            return {"error": "Cancelled by user", "epi_no": None, "epi_title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}", "idx": idx}

        self.sleep_cooperative(random.uniform(0.1, 0.6))
        
        episode_no = ep.get("episode_no")
        if episode_no is None:
            return {
                "error": "missing episode_no",
                "epi_no": None,
                "epi_title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                "idx": idx,
            }
        epi_no = int(episode_no)
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        
        if self.cancel_event and self.cancel_event.is_set():
            return {"error": "Cancelled by user", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 1) Ticket
        logger.info(f"ticket for episode {ep.get('epi_num', idx)} - {epi_title}")
        try:
            tdata = self.episode_ticket(epi_no)
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        token_t, direct_url = extract_t_token(tdata)
        res_block = tdata.get("result") or {}
        signed_key = res_block.get("signed_key", {}) if isinstance(res_block, dict) else {}
        if not isinstance(signed_key, dict):
            signed_key = {}

        if not token_t and not direct_url:
            return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        if self.cancel_event and self.cancel_event.is_set():
            return {"error": "Cancelled by user", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 2) Content
        try:
            if token_t:
                cdata = self.episode_content(token_t)
            else:
                r = self.s.get(direct_url, timeout=self.timeout)
                r.raise_for_status()
                cdata = r.json()
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 3) Extract HTML
        result_block = cdata.get("result", {})
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        if isinstance(data_block, dict):
            def content_order(k: str):
                match = _re.search(r"(\d+)$", k)
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
                "idx": idx,
            }

        return {
            "html": html_from_episode_text(html_text),
            "epi_title": epi_title,
            "epi_no": epi_no,
            "idx": idx,
            "signed_key": signed_key,
        }

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
                    raise RuntimeError("Download stopped by cancellation request.")
                try:
                    result = self.fetch_episode(episode, idx + 1)
                except Exception as exc:
                    result = {"error": str(exc), "idx": idx + 1}
                store_result(idx, result)
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.fetch_episode, ep, i+1): i 
                for i, ep in enumerate(ep_list)
            }
            try:
                for future in concurrent.futures.as_completed(future_to_idx):
                    if self.cancel_event and self.cancel_event.is_set():
                        raise RuntimeError("Download pool stopped by cancellation request.")

                    idx = future_to_idx[future]
                    try:
                        res = future.result()
                    except Exception as e:
                        res = {"error": str(e), "idx": idx + 1}
                    store_result(idx, res)
            except (KeyboardInterrupt, RuntimeError) as e:
                logger.warning(f"[warn] Fetch process interrupted gracefully: {e}")
                for fut in future_to_idx:
                    fut.cancel()
                raise
        return results

def _wait_for_retry(seconds: float, cancel_event, reason: str) -> None:
    if seconds <= 0:
        return
    if cancel_event:
        if cancel_event.wait(seconds):
            raise RuntimeError(f"Request cancelled during {reason}.")
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
            raise RuntimeError("Request cancelled.")

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
                        if new_login_at:
                            headers = merge_login_at(headers, new_login_at)
                        response = send_request()
                        if not _response_requires_auth(response):
                            break
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

            if response.status_code >= 500 and attempt < max_retries:
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
