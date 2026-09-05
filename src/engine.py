import logging
import os
import threading
from datetime import datetime, timezone
from typing import List, Optional, Callable, Dict, Any
from src.api import DownloadCancelled, NovelpiaClient
from src.builder import build_epub, build_txt
from src.helper import (
    build_book_directory_index,
    load_config,
    parse_range,
    save_config,
    write_text_atomic,
)
from src.novel import NovelSkipError

logger = logging.getLogger("pia_scrap")

# Outcome statuses that are not clean success / up-to-date skip.
_PROBLEM_STATUSES = frozenset({"failed", "404", "not_exist", "no_data"})


class _IssueCollector(logging.Handler):
    """Capture WARNING+ from pia_scrap during a download queue."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.current_novel_id: Optional[int] = None
        self._lock = threading.Lock()
        self._by_novel: Dict[Optional[int], List[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if not msg:
                return
            with self._lock:
                bucket = self._by_novel.setdefault(self.current_novel_id, [])
                if msg not in bucket:
                    bucket.append(msg)
        except Exception:
            self.handleError(record)

    def take(self, novel_id: Optional[int]) -> List[str]:
        with self._lock:
            return list(self._by_novel.pop(novel_id, []))

    def peek_run_issues(self) -> List[str]:
        with self._lock:
            return list(self._by_novel.get(None, []))


def _attach_issues(entry: Dict[str, Any], issues: List[str]) -> None:
    if not issues:
        return
    error_text = entry.get("error") or ""
    filtered = [msg for msg in issues if not error_text or error_text not in msg]
    if filtered:
        entry["issues"] = filtered


def _novel_recap_label(item: Dict[str, Any]) -> str:
    novel_id = item.get("novel_id")
    title = item.get("title")
    if title:
        return f"ID {novel_id} ({title})"
    return f"ID {novel_id}"


def format_run_recap(summary: Dict[str, Any], *, max_lines: Optional[int] = None) -> str:
    """Build a readable end-of-run recap from run_download_queue results."""
    results = list(summary.get("results") or [])
    run_issues = list(summary.get("run_issues") or [])
    problem_lines: List[str] = []
    issue_lines: List[str] = []
    issue_count = 0

    for item in results:
        label = _novel_recap_label(item)
        status = item.get("status")
        # cancelled is display-only; failed totals stay on real failures.
        if status in _PROBLEM_STATUSES or status == "cancelled":
            extra = item.get("error") or status
            problem_lines.append(f"  {label}: {extra}")
        for msg in item.get("issues") or []:
            issue_count += 1
            issue_lines.append(f"  {label}: {msg}")

    warning_count = len(run_issues) + issue_count
    lines = [
        f"Succeeded: {summary.get('success', 0)} | "
        f"Up to date: {summary.get('skipped', 0)} | "
        f"Failed/No Data: {summary.get('failed', 0)} | "
        f"Warnings: {warning_count}"
    ]

    cancelled = bool(summary.get("cancelled"))
    if cancelled:
        lines.append("Download queue was interrupted.")

    if not problem_lines and not issue_lines and not run_issues:
        if not cancelled:
            lines.append("No failures or warnings.")
    else:
        if run_issues:
            lines.append("")
            lines.append("Run-level warnings:")
            for msg in run_issues:
                lines.append(f"  - {msg}")
        if problem_lines:
            lines.append("")
            lines.append("Novels that did not complete:")
            lines.extend(problem_lines)
        if issue_lines:
            lines.append("")
            lines.append("Intra-novel warnings:")
            lines.extend(issue_lines)

    report_path = summary.get("report_path")
    if report_path:
        lines.append("")
        lines.append(f"Report saved to: {report_path}")

    if max_lines is not None and max_lines > 0 and len(lines) > max_lines:
        kept = lines[:max_lines]
        kept.append(
            f"... ({len(lines) - max_lines} more lines; see console or last_run_report.txt)"
        )
        return "\n".join(kept)
    return "\n".join(lines)


def write_run_report(out_dir: str, recap: str) -> Optional[str]:
    """Persist the recap under out_dir for review after the process exits."""
    if not out_dir:
        return None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        body = f"pia-scrap run report ({stamp})\n\n{recap.rstrip()}\n"
        path = os.path.join(out_dir, "last_run_report.txt")
        write_text_atomic(path, body)
        return path
    except OSError as exc:
        logger.warning(f"Could not write run report under {out_dir}: {exc}")
        return None


def _skip_warning(exc: NovelSkipError) -> str:
    text = str(exc).rstrip()
    if not text.endswith("."):
        text += "."
    return f"{text} Skipping."


class ScraperEngine:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, throttle: float = 1.5,
                 out_dir: str = "output", language: str = "en",
                 max_chapters: int = 0, threads: int = 1, txt_mode: bool = False,
                 update_mode: bool = False, debug_mode: bool = False,
                 status_callback: Optional[Callable[[str], None]] = None,
                 progress_callback: Optional[Callable[[int, int, str], None]] = None,
                 cancel_event: Optional[threading.Event] = None):
        self.email = email
        self.password = password
        self.proxy = proxy
        self.throttle = float(throttle)
        self.out_dir = out_dir
        self.language = language
        self.max_chapters = int(max_chapters)
        self.threads = int(threads)
        self.txt_mode = txt_mode
        self.update_mode = update_mode
        self.debug_mode = debug_mode

        if self.throttle < 0:
            raise ValueError("Throttle must be zero or greater.")
        if self.max_chapters < 0:
            raise ValueError("Max chapters must be zero or greater.")
        if self.threads < 1:
            raise ValueError("Threads must be at least 1.")

        # Skip if an entry point already set the level.
        pia_logger = logging.getLogger("pia_scrap")
        if pia_logger.level == logging.NOTSET:
            pia_logger.setLevel(logging.DEBUG if self.debug_mode else logging.INFO)
        
        self.status_callback = status_callback
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event or threading.Event()
        
        self.client: Optional[NovelpiaClient] = None

    def update_status(self, message: str):
        logger.info(message)
        if self.status_callback:
            try:
                self.status_callback(message)
            except Exception as e:
                logger.error(f"Error in status callback: {e}")

    def update_progress(self, current: int, total: int, label: str = ""):
        if self.progress_callback:
            try:
                self.progress_callback(current, total, label)
            except Exception as e:
                logger.error(f"Error in progress callback: {e}")

    def initialize_client(self) -> bool:
        self.update_status("Initializing client...")
        cfg = load_config()
        cfg_login_at = (cfg.get("login_at") or "").strip() or None
        cfg_userkey = (cfg.get("userkey") or "").strip() or None
        cfg_tkey = (cfg.get("tkey") or "").strip() or None

        email = self.email or os.getenv("NOVELPIA_EMAIL")
        password = self.password or os.getenv("NOVELPIA_PASSWORD")

        if email and password:
            self.update_status("Logging in with provided credentials...")
            self.client = NovelpiaClient(
                email=email, password=password, proxy=self.proxy,
                throttle=self.throttle, userkey=cfg_userkey, tkey=cfg_tkey,
                cancel_event=self.cancel_event
            )
            try:
                self.client.login()
            except Exception as e:
                self.update_status(f"Login failed: {e}")
                raise

            userkey_val = None
            tkey_val = None
            try:
                userkey_val = self.client.s.cookies.get("USERKEY")
                tkey_val = self.client.s.cookies.get("TKEY")
            except Exception as e:
                logger.debug(f"Error reading cookies after login: {e}")

            tokens_stored = save_config({
                "login_at": self.client.tokens.login_at,
                "userkey": userkey_val or cfg_userkey or "",
                "tkey": tkey_val or self.client.tokens.tkey or cfg_tkey or "",
            })
            if tokens_stored:
                self.update_status("Login successful. Stored tokens updated.")
            else:
                self.update_status("Login successful, but tokens could not be stored.")
            return True
        elif cfg_login_at and cfg_userkey:
            self.update_status("Validating stored authentication tokens...")
            self.client = NovelpiaClient(
                email=None, password=None, proxy=self.proxy,
                throttle=self.throttle, userkey=cfg_userkey, tkey=cfg_tkey,
                cancel_event=self.cancel_event
            )
            self.client.tokens.login_at = cfg_login_at
            try:
                self.client.me()
            except Exception as e:
                raise RuntimeError(
                    "Stored authentication could not be validated. "
                    "Enter your email and password to log in again."
                ) from e
            self.update_status("Stored authentication is valid.")
            return True
        else:
            self.update_status("No credentials or stored tokens found.")
            return False

    def resolve_novel_ids(self, novel_ids_input: str) -> List[int]:
        if not self.client:
            raise RuntimeError("Client not initialized. Call initialize_client first.")

        if novel_ids_input.lower() in ("mybook", "library"):
            self.update_status("Fetching target novel IDs from library...")
            return self.client.my_library()
        elif novel_ids_input.lower() in ("recent", "latest"):
            self.update_status("Fetching the 30 most recent public K-Premium listings...")
            return self.client.recent_novels(rows=30)
        else:
            return parse_range(novel_ids_input)

    def run_download_queue(self, target_ids: List[int]) -> Dict[str, Any]:
        if not self.client:
            raise RuntimeError("Client not initialized. Call initialize_client first.")

        success_count = 0
        fail_count = 0
        skipped_count = 0
        cancelled = False
        results_summary = []
        collector = _IssueCollector()
        pia_logger = logging.getLogger("pia_scrap")
        pia_logger.addHandler(collector)

        try:
            total_novels = len(target_ids)
            self.update_status(f"Starting download queue of {total_novels} novels...")
            book_index = build_book_directory_index(self.out_dir)

            for idx, novel_id in enumerate(target_ids):
                if self.cancel_event and self.cancel_event.is_set():
                    self.update_status("[cancelled] Download queue cancelled by user.")
                    cancelled = True
                    break

                msg = f"Processing ID {novel_id} ({idx+1}/{total_novels})"
                self.update_status(f"--- {msg} ---")
                collector.current_novel_id = novel_id
                self.update_progress(0, 1, "Starting...")
                entry: Optional[Dict[str, Any]] = None

                try:
                    if self.txt_mode:
                        # TXT always rewrites chapter files. Only EPUB returns None when up to date.
                        out_dir_final, title, count = build_txt(
                            client=self.client,
                            novel_id=novel_id,
                            out_dir=self.out_dir,
                            max_chapters=(self.max_chapters if self.max_chapters > 0 else None),
                            threads=self.threads,
                            update_mode=self.update_mode,
                            progress_cb=self.update_progress,
                            status_cb=self.update_status,
                            book_index=book_index,
                        )
                        success_msg = f"Wrote TXT files under: {out_dir_final} | Title: {title} | Chapters: {count}"
                        self.update_status(f"[success] {success_msg}")
                        entry = {
                            "novel_id": novel_id,
                            "status": "success",
                            "title": title,
                            "count": count,
                            "type": "txt",
                        }
                        success_count += 1
                    else:
                        out_file, title, count = build_epub(
                            client=self.client,
                            novel_id=novel_id,
                            out_dir=self.out_dir,
                            max_chapters=(self.max_chapters if self.max_chapters > 0 else None),
                            language=self.language,
                            update_mode=self.update_mode,
                            threads=self.threads,
                            progress_cb=self.update_progress,
                            status_cb=self.update_status,
                            book_index=book_index,
                        )

                        if out_file is None:
                            skipped_msg = (
                                f"Novel '{title}' is already up to date "
                                f"({count} chapters). Skipping."
                            )
                            self.update_status(f"[skipped] {skipped_msg}")
                            entry = {"novel_id": novel_id, "status": "skipped", "title": title}
                            skipped_count += 1
                        else:
                            success_msg = f"Wrote EPUB: {out_file} | Title: {title} | Chapters: {count}\n"
                            self.update_status(f"[success] {success_msg}")
                            entry = {
                                "novel_id": novel_id,
                                "status": "success",
                                "title": title,
                                "count": count,
                                "type": "epub",
                            }
                            success_count += 1

                except DownloadCancelled:
                    cancel_msg = (
                        f"Stopped processing {novel_id} due to user cancellation."
                    )
                    self.update_status(f"[cancelled] {cancel_msg}")
                    entry = {
                        "novel_id": novel_id,
                        "status": "cancelled",
                        "error": cancel_msg,
                    }
                    _attach_issues(entry, collector.take(novel_id))
                    results_summary.append(entry)
                    entry = None
                    collector.current_novel_id = None
                    cancelled = True
                    break
                except Exception as e:
                    err_str = str(e)
                    response = getattr(e, "response", None)
                    status_code = getattr(response, "status_code", None)
                    if isinstance(e, NovelSkipError):
                        warn_msg = _skip_warning(e)
                        self.update_status(f"[warn] {warn_msg}")
                        entry = {
                            "novel_id": novel_id,
                            "status": e.result_status,
                            "error": warn_msg,
                        }
                    elif status_code == 404:
                        warn_msg = f"Novel {novel_id} returned 404. Skipping."
                        self.update_status(f"[warn] {warn_msg}")
                        entry = {
                            "novel_id": novel_id,
                            "status": "404",
                            "error": warn_msg,
                        }
                    else:
                        err_msg = f"Failed processing {novel_id}: {e}"
                        logger.error(err_msg, exc_info=True)
                        self.update_status(f"[error] {err_msg}")
                        entry = {"novel_id": novel_id, "status": "failed", "error": err_str}

                    fail_count += 1
                    try:
                        self.client.sleep_cooperative(1.0)
                    except DownloadCancelled:
                        self.update_status("[cancelled] Download queue cancelled by user.")
                        if entry is not None:
                            _attach_issues(entry, collector.take(novel_id))
                            results_summary.append(entry)
                            entry = None
                        collector.current_novel_id = None
                        cancelled = True
                        break

                if entry is not None:
                    _attach_issues(entry, collector.take(novel_id))
                    results_summary.append(entry)
                collector.current_novel_id = None

            summary_msg = (
                f"Finished queue. Success: {success_count}, "
                f"Skipped (Up to date): {skipped_count}, Failed/No Data: {fail_count}"
            )
            self.update_status(f"[done] {summary_msg}")

            summary: Dict[str, Any] = {
                "success": success_count,
                "skipped": skipped_count,
                "failed": fail_count,
                "results": results_summary,
            }
            if cancelled:
                summary["cancelled"] = True
            run_issues = collector.peek_run_issues()
            if run_issues:
                summary["run_issues"] = run_issues

            recap = format_run_recap(summary)
            report_path = write_run_report(self.out_dir, recap)
            if report_path:
                summary["report_path"] = report_path
                recap = format_run_recap(summary)
            # Recap is logger-only so the GUI status label stays one line.
            logger.info("=== Run recap ===\n%s", recap)
            return summary
        finally:
            pia_logger.removeHandler(collector)
