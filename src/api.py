import json
import os
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
from src.helper import attach_auth_cookies, merge_login_at
from src.helper import extract_t_token
from src.novel import html_from_episode_text

logger = logging.getLogger("pia_scrap")

# ----------------------------
# API Client
# ----------------------------

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
        self.throttle = max(0.0, float(throttle or 1.5))
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
        """Sleeps in small intervals to keep the client responsive to cancellation events."""
        if not seconds:
            return
        steps = int(seconds / 0.1)
        for _ in range(steps):
            if self.cancel_event and self.cancel_event.is_set():
                break
            time.sleep(0.1)
        rem = seconds % 0.1
        if rem > 0 and not (self.cancel_event and self.cancel_event.is_set()):
            time.sleep(rem)

    def login(self):
        with self._auth_lock:
            url = f"{const.API_BASE}/v1/member/login"
        url = f"{const.API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s, "POST", url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout, max_retries=2,
            cancel_event=self.cancel_event
        )
        r.raise_for_status()
        data = r.json()
        self.tokens.login_at = data["result"]["LOGINAT"]
        # Capture cookies after successful login
        try:
            self.tokens.tkey = self.s.cookies.get("TKEY")
            self.tokens.userkey = self.s.cookies.get("USERKEY")
        except Exception:
            pass

    def refresh(self) -> Optional[str]:
        with self._auth_lock:
            url = f"{const.API_BASE}/v1/login/refresh"
        url = f"{const.API_BASE}/v1/login/refresh"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, max_retries=2,
            cancel_event=self.cancel_event
        )
        r.raise_for_status()
        self.tokens.login_at = r.json()["result"]["LOGINAT"]
        # Persist refreshed token to config
        try:
            cfg: Dict[str, Any] = {}
            if os.path.exists(const.CONFIG_PATH):
                try:
                    with open(const.CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception as e:
                    logger.error(f"Error loading config: {e}")
                    cfg = {}
            cfg["login_at"] = self.tokens.login_at
            with open(const.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            pass
        return self.tokens.login_at

    def _on_rate_limit(self):
        old = self.throttle
        self.throttle = min(5.0, self.throttle + 0.5) # Reduced harsh penalty
        if const.HTTP_LOG:
            logger.warning(f"[api] Increased throttle from {old}s to {self.throttle}s due to rate limit.")

    def me(self) -> Dict:
        url = f"{const.API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit,
            cancel_event=self.cancel_event
        )
        r.raise_for_status()
        return r.json()

    def novel(self, novel_id: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id},
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit,
            max_retries=1,  # Most likely error here is 500 from accessing an empty/invalid novel_id, limit time lost backing off
            cancel_event=self.cancel_event
        )
        r.raise_for_status()
        return r.json()

    def episode_list(self, novel_id: int, rows: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/list"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit,
            max_retries=1,
            cancel_event=self.cancel_event
        )
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
            
            r = request_with_retries(
                self.s, "GET", url,
                params=params,
                headers=merge_login_at({}, self.tokens.login_at),
                timeout=self.timeout, allow_refresh=True, 
                refresh_fn=self.refresh, login_fn=self.login,
                on_rate_limit=self._on_rate_limit,
                cancel_event=self.cancel_event
            )
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
            
        # Deduplicate while preserving order
        seen = set()
        return [x for x in novel_ids if not (x in seen or seen.add(x))]

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))
        
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Request cancelled by user request.")

        r = request_with_retries(
            self.s, "GET", url,
            headers=headers, params=params,
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit, max_retries=4,
            cancel_event=self.cancel_event
        )
        r.raise_for_status()
        return r.json()

    def episode_content(self, token_t: str) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/content"
        if self.throttle:
            self.sleep_cooperative(random.uniform(self.throttle * 0.5, self.throttle))
            
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Request cancelled by user request.")

        r = request_with_retries(
            self.s, "GET", url,
            params={"_t": token_t},
            timeout=self.timeout, max_retries=3,
            allow_refresh=True, refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit,
            cancel_event=self.cancel_event
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

        if not token_t and not direct_url:
            return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        if self.cancel_event and self.cancel_event.is_set():
            return {"error": "Cancelled by user", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 2) Content
        try:
            if token_t:
                cdata = self.episode_content(token_t)
            else:
                assert direct_url is not None, "direct_url unavailable"
                r = self.s.get(direct_url, timeout=self.timeout)
                r.raise_for_status()
                cdata = r.json()
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 3) Extract HTML
        result_block = cdata.get("result", {})
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        try:
            def _key(k: str):
                m = _re.search(r"(\d+)$", k)
                return (0 if k == "epi_content" else 1, int(m.group(1)) if m else 0)
            for k in sorted([kk for kk in data_block.keys() if str(kk).startswith("epi_content")], key=_key):
                v = data_block.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
        except Exception:
            pass

        html_text = "".join(parts).strip()
        if not html_text:
            html_text = (
                result_block.get("content")
                or result_block.get("html")
                or result_block.get("text")
                or cdata.get("content")
                or ""
            )

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
        results: List[Dict[str, Any]] = [{} for _ in range(len(ep_list))]
        completed = 0
        total = len(ep_list)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.fetch_episode, ep, i+1): i 
                for i, ep in enumerate(ep_list)
            }
            try:
                for future in concurrent.futures.as_completed(future_to_idx):
                    if self.cancel_event and self.cancel_event.is_set():
                        for fut in future_to_idx:
                            fut.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise RuntimeError("Download pool stopped by cancellation request.")

                    idx = future_to_idx[future]
                    try:
                        res = future.result()
                        results[idx] = res
                    except Exception as e:
                        results[idx] = {"error": str(e), "idx": idx+1}
                    
                    completed += 1
                    epi_title = results[idx].get("epi_title") or ep_list[idx].get("epi_title") or f"Episode {idx+1}"
                    
                    if on_complete_cb:
                        on_complete_cb(results[idx])
                    if progress_cb:
                        progress_cb(completed, total, epi_title)
            except (KeyboardInterrupt, RuntimeError) as e:
                logger.warning(f"[warn] Fetch process interrupted gracefully: {e}")
                for fut in future_to_idx:
                    fut.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
        return results

def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None,
                          login_fn=None, on_rate_limit=None, cancel_event=None):
    """Generic request wrapper: retries on 5xx, 429, and network issues.
    If allow_refresh is True and the response indicates an expired token, invoke
    refresh_fn() followed by login_fn() if needed, then retry.
    """
    attempt = 0
    last_exc = None
    did_refresh = False
    did_login = False
    while attempt < max_retries:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Request cancelled by request.")

        attempt += 1
        try:
            # Inject Cookie header (except for login endpoint) using session cookies
            try:
                if "/v1/member/login" not in url:
                    headers = attach_auth_cookies(session, headers)
            except Exception as e:
                logger.error(f"Error occurred while attaching auth cookies: {e}")
                pass

            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
            
            if r.status_code == 429:
                if on_rate_limit:
                    on_rate_limit()
                wait = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                steps = int(wait / 0.1)
                for _ in range(steps):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Request execution cancelled during throttle wait.")
                    time.sleep(0.1)
                continue

            if r.status_code >= 500:
                if on_rate_limit:
                    on_rate_limit()
                wait = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                steps = int(wait / 0.1)
                for _ in range(steps):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Request execution cancelled during rate limit wait.")
                    time.sleep(0.1)
                continue

            # Handle auth refresh-and-retry for all endpoints except login/refresh
            if allow_refresh and (refresh_fn or login_fn) and not did_login:
                trigger_refresh = False
                if r.status_code in (401, 403):
                    trigger_refresh = True
                else:
                    msg = ""
                    try:
                        body = r.json()
                        msg = (body.get("errmsg") or body.get("message") or "").lower()
                    except Exception:
                        pass
                    if "token" in msg and "expire" in msg:
                        trigger_refresh = True

                if trigger_refresh:
                    try:
                        success = False
                        # Try refresh first
                        if refresh_fn and not did_refresh:
                            try:
                                refresh_fn()
                                did_refresh = True
                                success = True
                            except Exception:
                                if const.HTTP_LOG: logger.warning("[api] Refresh failed.")
                                pass
                        
                        if not success and login_fn and not did_login:
                            try:
                                login_fn()
                                did_login = True
                                success = True
                            except Exception as e:
                                if const.HTTP_LOG: logger.warning(f"[api] Re-login failed: {e}")
                                pass

                        if success:
                            # Retry original request once
                            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
                    except Exception as e:
                        if const.HTTP_LOG: logger.error(f"[api] Auth recovery failed: {e}")
                        pass

            if r.status_code >= 500 and attempt < max_retries:
                wait = backoff ** attempt
                steps = int(wait / 0.1)
                for _ in range(steps):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Request execution cancelled during backoff sleep.")
                    time.sleep(0.1)
                continue
            return r
        except requests.RequestException as e:
            if const.HTTP_LOG:
                logger.error(f"[api] !! {method} {url} failed on attempt {attempt}: {e}")
            last_exc = e
            if attempt < max_retries:
                wait = backoff ** attempt
                steps = int(wait / 0.1)
                for _ in range(steps):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Request execution cancelled during attempt sleep.")
                    time.sleep(0.1)
                continue
            raise
    if last_exc:
        raise last_exc
    return r
