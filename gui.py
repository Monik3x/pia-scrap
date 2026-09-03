import os
import queue
import logging
import threading
import traceback
from typing import Dict, Any

from dotenv import load_dotenv
import customtkinter as ctk
from tkinter import filedialog, messagebox

from src.engine import ScraperEngine
from src.helper import list_local_book_directories, load_config, load_local_book_info, local_library_novel_ids
from src import const

ctk.set_appearance_mode("System")  # Options: "System", "Dark", "Light"
ctk.set_default_color_theme("blue") # Themes: "blue", "green", "dark-blue"

class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue
        
    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_queue.put(("log", msg))
        except Exception:
            self.handleError(record)

# ----------------------------
# Main GUI Window
# ----------------------------
class PiaScrapGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Pia-Scrap")
        self.geometry("1020x740")
        self.minsize(900, 640)
        
        load_dotenv()
        
        self.gui_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread = None
        
        self.logger = logging.getLogger("pia_scrap")
        self.logger.setLevel(logging.INFO)
        
        self.queue_handler = QueueLogHandler(self.gui_queue)
        self.queue_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
        self.queue_handler.setFormatter(formatter)
        self.logger.addHandler(self.queue_handler)
        
        self.nav_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.nav_frame.pack(fill="x", padx=15, pady=(15, 0))
        
        self.nav_var = ctk.StringVar(value="Downloader")
        self.nav_selector = ctk.CTkSegmentedButton(
            self.nav_frame, 
            values=["Downloader", "Local Library"], 
            variable=self.nav_var, 
            command=self.on_nav_changed
        )
        self.nav_selector.pack(side="left")
        
        self.view_container = ctk.CTkFrame(self, fg_color="transparent")
        self.view_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.tab_download = ctk.CTkFrame(self.view_container, fg_color="transparent")
        self.tab_library = ctk.CTkFrame(self.view_container, fg_color="transparent")
        
        self.tab_download.pack(fill="both", expand=True)
        
        self.tab_download.grid_columnconfigure(0, weight=1, minsize=380)
        self.tab_download.grid_columnconfigure(1, weight=2, minsize=420)
        self.tab_download.grid_rowconfigure(0, weight=1)
        
        self._create_left_panel()
        self._create_right_panel()
        self._create_library_panel()

        self.current_page = 0
        self.items_per_page = 50
        self.library_dirs = []       # Master sorted folder list
        self.filtered_dirs = []      # Subset after search query is applied
        
        self._load_saved_configurations()
        
        self.after(50, self.process_queue)

    def _create_left_panel(self):
        left_frame = ctk.CTkFrame(self.tab_download, corner_radius=10)
        left_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)
        
        title_lbl = ctk.CTkLabel(left_frame, text="Options", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")
        
        # Authentication
        auth_frame = ctk.CTkFrame(left_frame, corner_radius=8)
        auth_frame.grid(row=1, column=0, padx=15, pady=5, sticky="ew")
        auth_frame.grid_columnconfigure(0, weight=1)
        
        auth_title = ctk.CTkLabel(auth_frame, text="Authentication Credentials", font=ctk.CTkFont(weight="bold"))
        auth_title.grid(row=0, column=0, padx=10, pady=(5, 2), sticky="w")
        
        self.email_entry = ctk.CTkEntry(auth_frame, placeholder_text="Email (Leave blank if using stored token)")
        self.email_entry.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        
        self.pass_entry = ctk.CTkEntry(auth_frame, placeholder_text="Password", show="*")
        self.pass_entry.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.load_env_btn = ctk.CTkButton(auth_frame, text="Import from .env", fg_color="#4A5568", hover_color="#2D3748", command=self.import_from_env)
        self.load_env_btn.grid(row=3, column=0, padx=10, pady=(5, 10), sticky="ew")
        
        # Main Download Options 
        opts_frame = ctk.CTkFrame(left_frame, corner_radius=8)
        opts_frame.grid(row=2, column=0, padx=15, pady=5, sticky="ew")
        opts_frame.grid_columnconfigure(0, weight=1)
        opts_frame.grid_columnconfigure(1, weight=0)
        
        opts_title = ctk.CTkLabel(opts_frame, text="Download Target & Output", font=ctk.CTkFont(weight="bold"))
        opts_title.grid(row=0, column=0, columnspan=2, padx=10, pady=(5, 2), sticky="w")
        
        self.ids_entry = ctk.CTkEntry(opts_frame, placeholder_text="Novel IDs, 'library', or 'recent'")
        self.ids_entry.grid(row=1, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        
        self.output_entry = ctk.CTkEntry(opts_frame, placeholder_text="Output Directory")
        self.output_entry.insert(0, "output")
        self.output_entry.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        
        browse_btn = ctk.CTkButton(opts_frame, text="Browse", width=60, command=self.browse_output_dir)
        browse_btn.grid(row=2, column=1, padx=(0, 10), pady=5, sticky="ew")
        
        self.format_var = ctk.StringVar(value="EPUB")
        format_switch = ctk.CTkSegmentedButton(
            opts_frame,
            values=["EPUB", "TXT"],
            variable=self.format_var,
        )
        format_switch.grid(row=3, column=0, columnspan=2, padx=10, pady=(5, 10), sticky="ew")
        
        # Advanced Scraper Config
        adv_frame = ctk.CTkFrame(left_frame, corner_radius=8)
        adv_frame.grid(row=3, column=0, padx=15, pady=5, sticky="ew")
        adv_frame.grid_columnconfigure((0, 1), weight=1)
        
        adv_title = ctk.CTkLabel(adv_frame, text="Advanced Settings", font=ctk.CTkFont(weight="bold"))
        adv_title.grid(row=0, column=0, columnspan=2, padx=10, pady=(5, 2), sticky="w")
        
        ctk.CTkLabel(adv_frame, text="Max Chapters (0=All):").grid(row=1, column=0, padx=10, pady=2, sticky="w")
        self.max_chap_entry = ctk.CTkEntry(adv_frame, placeholder_text="0", width=80)
        self.max_chap_entry.insert(0, "0")
        self.max_chap_entry.grid(row=1, column=1, padx=10, pady=2, sticky="e")
        
        ctk.CTkLabel(adv_frame, text="Delay Throttle (s):").grid(row=2, column=0, padx=10, pady=2, sticky="w")
        self.throttle_entry = ctk.CTkEntry(adv_frame, placeholder_text="1.5", width=80)
        self.throttle_entry.insert(0, "1.5")
        self.throttle_entry.grid(row=2, column=1, padx=10, pady=2, sticky="e")
        
        ctk.CTkLabel(adv_frame, text="Threads (1 rec.):").grid(row=3, column=0, padx=10, pady=2, sticky="w")
        self.threads_box = ctk.CTkComboBox(adv_frame, values=["1", "2", "3", "4"], width=80, command=self.on_threads_warning)
        self.threads_box.set("1")
        self.threads_box.grid(row=3, column=1, padx=10, pady=2, sticky="e")
        
        ctk.CTkLabel(adv_frame, text="Language code:").grid(row=4, column=0, padx=10, pady=2, sticky="w")
        self.lang_entry = ctk.CTkEntry(adv_frame, placeholder_text="en", width=80)
        self.lang_entry.insert(0, "en")
        self.lang_entry.grid(row=4, column=1, padx=10, pady=2, sticky="e")

        ctk.CTkLabel(adv_frame, text="Proxy Server:").grid(row=5, column=0, padx=10, pady=2, sticky="w")
        self.proxy_entry = ctk.CTkEntry(adv_frame, placeholder_text="http://host:port", width=140)
        self.proxy_entry.grid(row=5, column=1, padx=10, pady=2, sticky="e")
        
        self.update_switch = ctk.CTkSwitch(adv_frame, text="Update Mode (Use Cache)")
        self.update_switch.select()  # Enable by default
        self.update_switch.grid(row=6, column=0, columnspan=2, padx=10, pady=5, sticky="w")
        
        self.debug_switch = ctk.CTkSwitch(adv_frame, text="Verbose Debug Mode")
        self.debug_switch.grid(row=7, column=0, columnspan=2, padx=10, pady=(5, 10), sticky="w")
        
        # Track entry widgets for bulk disable/enable
        self.all_input_fields = [
            self.email_entry, self.pass_entry, self.load_env_btn, self.ids_entry, self.output_entry,
            browse_btn, format_switch, self.max_chap_entry, self.throttle_entry,
            self.threads_box, self.lang_entry, self.proxy_entry, self.update_switch, self.debug_switch
        ]

    def _create_right_panel(self):
        right_frame = ctk.CTkFrame(self.tab_download, corner_radius=10)
        right_frame.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")
        right_frame.grid_propagate(False)
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(5, weight=1)
        
        ctrls_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        ctrls_frame.grid(row=0, column=0, padx=15, pady=(15, 8), sticky="ew")
        ctrls_frame.grid_columnconfigure((0, 1), weight=1)
        
        self.start_btn = ctk.CTkButton(ctrls_frame, text="Start Download", fg_color="green", hover_color="darkgreen", command=self.start_scraper)
        self.start_btn.grid(row=0, column=0, padx=(0, 6), pady=0, sticky="ew")
        
        self.cancel_btn = ctk.CTkButton(ctrls_frame, text="Cancel", fg_color="#D32F2F", hover_color="#B71C1C", state="disabled", text_color="#FFFFFF", command=self.cancel_scraper)
        self.cancel_btn.grid(row=0, column=1, padx=(6, 0), pady=0, sticky="ew")
        
        self.status_label = ctk.CTkLabel(right_frame, text="Status: Ready", font=ctk.CTkFont(size=16, weight="bold"))
        self.status_label.grid(row=1, column=0, padx=15, pady=(8, 2), sticky="w")

        self.progress_label = ctk.CTkLabel(right_frame, text="Progress: 0/0", font=ctk.CTkFont(size=14))
        self.progress_label.grid(row=2, column=0, padx=15, pady=(2, 4), sticky="w")

        self.progress_bar = ctk.CTkProgressBar(right_frame, orientation="horizontal", height=12, progress_color="#3A7EBF")
        self.progress_bar.set(0)
        self.progress_bar.grid(row=3, column=0, padx=15, pady=(0, 10), sticky="ew")
        
        log_lbl = ctk.CTkLabel(right_frame, text="Execution Console Log:", font=ctk.CTkFont(weight="normal"))
        log_lbl.grid(row=4, column=0, padx=15, pady=(10, 4), sticky="w")
        
        self.console_textbox = ctk.CTkTextbox(right_frame, wrap="word", font=ctk.CTkFont(family="Courier", size=13))
        self.console_textbox.grid(row=5, column=0, padx=15, pady=(0, 15), sticky="nsew")
        self.console_textbox.configure(state="disabled")

    def _create_library_panel(self):
        """Creates the layout structure for the Local Library management component."""
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(1, weight=1)
        
        top_bar = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        top_bar.grid(row=0, column=0, padx=15, pady=10, sticky="ew")
        top_bar.grid_columnconfigure(0, weight=1)
        top_bar.grid_columnconfigure(1, weight=1)
        top_bar.grid_columnconfigure(2, weight=0)
        top_bar.grid_columnconfigure(3, weight=0)
        
        lib_title = ctk.CTkLabel(top_bar, text="Local Library", font=ctk.CTkFont(size=18, weight="bold"))
        lib_title.grid(row=0, column=0, sticky="w")

        # Search input
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.on_search_changed())
        self.search_entry = ctk.CTkEntry(top_bar, placeholder_text="🔍 Search local novels...", textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, padx=(10, 20), sticky="ew")
        
        self.update_all_btn = ctk.CTkButton(top_bar, text="Check & Update All", width=150, fg_color="#2B6CB0", hover_color="#2C5282", command=self.update_all_library)
        self.update_all_btn.grid(row=0, column=2, padx=(0, 10), sticky="e")
        
        refresh_btn = ctk.CTkButton(top_bar, text="Scan & Refresh Folder", width=160, fg_color="#4A5568", hover_color="#2D3748", command=self.full_scan_library)
        refresh_btn.grid(row=0, column=3, sticky="e")
        
        self.library_scroll = ctk.CTkScrollableFrame(self.tab_library, label_text="Detected Local Novels")
        self.library_scroll.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="nsew")

        # Pagination controls
        self.pag_frame = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        self.pag_frame.grid(row=2, column=0, padx=15, pady=(0, 15), sticky="ew")
        self.pag_frame.grid_columnconfigure((0, 2), weight=1)
        self.pag_frame.grid_columnconfigure(1, weight=0)

        self.prev_btn = ctk.CTkButton(self.pag_frame, text="◀ Previous", width=100, command=self.prev_page)
        self.prev_btn.grid(row=0, column=0, sticky="w")

        self.pag_label = ctk.CTkLabel(self.pag_frame, text="Page 1 of 1 (0 items)", font=ctk.CTkFont(size=13))
        self.pag_label.grid(row=0, column=1, padx=20)

        self.next_btn = ctk.CTkButton(self.pag_frame, text="Next ▶", width=100, command=self.next_page)
        self.next_btn.grid(row=0, column=2, sticky="e")

    # ----------------------------
    # Helpers & UI Interactions
    # ----------------------------
    def browse_output_dir(self):
        dir_path = filedialog.askdirectory(initialdir=self.output_entry.get())
        if dir_path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, dir_path)

    def on_threads_warning(self, val):
        if int(val) > 1:
            self.append_to_log("[GUI-WARN] Choosing > 1 threads increases the risk of triggering Novelpia's strict IP rate-limits (HTTP 429). Leave at 1 for best results.")

    def import_from_env(self):
        load_dotenv(override=True)
        env_email = os.getenv("NOVELPIA_EMAIL", "").strip()
        env_pass = os.getenv("NOVELPIA_PASSWORD", "").strip()
        
        if env_email or env_pass:
            self.email_entry.delete(0, "end")
            self.pass_entry.delete(0, "end")
            if env_email:
                self.email_entry.insert(0, env_email)
            if env_pass:
                self.pass_entry.insert(0, env_pass)
            self.append_to_log("[GUI] Credentials successfully imported from .env file.")
            messagebox.showinfo("Success", "Credentials imported from .env!")
        else:
            self.append_to_log("[GUI-WARN] No NOVELPIA_EMAIL or NOVELPIA_PASSWORD declarations found in .env file.")
            messagebox.showwarning("Warning", "No credentials found inside .env file.")

    def on_nav_changed(self, value):
        """Swaps view panels visibility seamlessly mimicking native top-left anchored tabs."""
        if value == "Downloader":
            self.tab_library.pack_forget()
            self.tab_download.pack(fill="both", expand=True)
        elif value == "Local Library":
            self.tab_download.pack_forget()
            self.tab_library.pack(fill="both", expand=True)
            self.full_scan_library()

    def full_scan_library(self):
        """Scans the directories instantly on disk. Does NOT load JSON metadata yet."""
        out_dir = self.output_entry.get().strip() or "output"
        self.library_dirs = list_local_book_directories(out_dir)
        self.on_search_changed()

    def on_search_changed(self):
        """Filters the master list based on the search input query."""
        query = self.search_var.get().strip().lower()
        if not query:
            self.filtered_dirs = self.library_dirs
        else:
            self.filtered_dirs = [d for d in self.library_dirs if query in d.lower()]
        
        self.current_page = 0
        self.display_current_page()

    def display_current_page(self):
        """Renders only the current page of filtered items, reading JSON lazily."""
        for widget in self.library_scroll.winfo_children():
            widget.destroy()
            
        total_items = len(self.filtered_dirs)
        if total_items == 0:
            empty_lbl = ctk.CTkLabel(self.library_scroll, text="No downloaded novel folders matching criteria.", font=ctk.CTkFont(style="italic"))
            empty_lbl.pack(pady=20)
            self.pag_label.configure(text="Page 1 of 1 (0 items)")
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            return

        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page
        if self.current_page >= total_pages:
            self.current_page = max(0, total_pages - 1)

        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, total_items)
        page_slice = self.filtered_dirs[start_idx:end_idx]

        # Update button states and total label
        self.pag_label.configure(text=f"Page {self.current_page + 1} of {total_pages} (Novels {start_idx + 1}-{end_idx} of {total_items})")
        self.prev_btn.configure(state="normal" if self.current_page > 0 else "disabled")
        self.next_btn.configure(state="normal" if self.current_page < total_pages - 1 else "disabled")

        out_dir = self.output_entry.get().strip() or "output"

        for item in page_slice:
            info = load_local_book_info(os.path.join(out_dir, item))
            title = info.title
            author = info.author
            chapters = str(info.chapter_count) if info.has_metadata else "Unchecked"
            status = info.status
            novel_id = info.novel_id
            
            # Row layout container
            row = ctk.CTkFrame(self.library_scroll, corner_radius=6)
            row.pack(fill="x", padx=5, pady=4)
            
            row.grid_columnconfigure(0, weight=4, uniform="lib_cols") # Title Card Area
            row.grid_columnconfigure(1, weight=2, uniform="lib_cols") # Local Chapters Count
            row.grid_columnconfigure(2, weight=2, uniform="lib_cols") # Status flag
            row.grid_columnconfigure(3, weight=2, uniform="lib_cols") # Quick actions
            
            info_lbl = ctk.CTkLabel(row, text=f"{title}\nBy: {author}", font=ctk.CTkFont(size=13, weight="bold"), anchor="w", justify="left")
            info_lbl.grid(row=0, column=0, padx=15, pady=10, sticky="w")
            
            chap_lbl = ctk.CTkLabel(row, text=f"{chapters} Chapters", anchor="w")
            chap_lbl.grid(row=0, column=1, padx=10, pady=10, sticky="w")
            
            color = "green" if status.lower() == "completed" else "orange" if status.lower() == "ongoing" else "gray"
            status_lbl = ctk.CTkLabel(row, text=status, text_color=color, font=ctk.CTkFont(weight="bold"), anchor="w")
            status_lbl.grid(row=0, column=2, padx=10, pady=10, sticky="w")
            
            update_btn = ctk.CTkButton(
                row, 
                text="Check & Update", 
                width=110, 
                fg_color="#2B6CB0", 
                hover_color="#2C5282",
                state="normal" if novel_id else "disabled",
                command=lambda nid=novel_id: self.trigger_library_update(nid)
            )
            update_btn.grid(row=0, column=3, padx=15, pady=10, sticky="e")

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.display_current_page()

    def next_page(self):
        total_items = len(self.filtered_dirs)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.display_current_page()

    def trigger_library_update(self, novel_id):
        """Pushes target data back to download parameters tab and automatically starts processing updates."""
        if not novel_id:
            messagebox.showerror("Missing Novel ID", "This folder does not contain a valid Novel ID.")
            return
        self.format_var.set("EPUB")
        self.ids_entry.delete(0, "end")
        self.ids_entry.insert(0, str(novel_id))
        self.update_switch.select()

        # Switch tabs and begin background download
        self.nav_var.set("Downloader")
        self.on_nav_changed("Downloader")
        self.start_scraper()

    def update_all_library(self):
        """Scans for all local folders, collects valid novel IDs, and aggregates them into an automatic update pipeline."""
        out_dir = self.output_entry.get().strip() or "output"
        if not os.path.exists(out_dir):
            messagebox.showwarning("Warning", "Output directory does not exist yet.")
            return

        dirs = list_local_book_directories(out_dir)
        if not dirs:
            messagebox.showwarning("Warning", "No local novel folders located to update.")
            return

        valid_ids = [str(novel_id) for novel_id in local_library_novel_ids(out_dir)]
        if not valid_ids:
            messagebox.showwarning(
                "No IDs Found",
                "Could not locate valid Novel IDs in .novel_id markers or metadata of your local library folders.",
            )
            return

        self.format_var.set("EPUB")
        self.ids_entry.delete(0, "end")
        self.ids_entry.insert(0, ", ".join(valid_ids))
        self.update_switch.select()

        self.nav_var.set("Downloader")
        self.on_nav_changed("Downloader")
        self.start_scraper()

    def _load_saved_configurations(self):
        env_email = os.getenv("NOVELPIA_EMAIL", "")
        env_pass = os.getenv("NOVELPIA_PASSWORD", "")
        if env_email:
            self.email_entry.insert(0, env_email)
        if env_pass:
            self.pass_entry.insert(0, env_pass)
            
        # Inspect stored cookies
        cfg = load_config()
        stored_login = (cfg.get("login_at") or "").strip()
        stored_user = (cfg.get("userkey") or "").strip()
        
        if stored_login and stored_user:
            self.status_label.configure(text="Status: Loaded previous login tokens.")
            self.append_to_log("[GUI] Found active auth tokens stored locally. No password entry required unless logging into a different account.")
        else:
            self.append_to_log("[GUI] No stored login sessions found. Enter credentials to log in.")

    def append_to_log(self, text: str):
        self.console_textbox.configure(state="normal")
        self.console_textbox.insert("end", text + "\n")
        self.console_textbox.see("end")
        self.console_textbox.configure(state="disabled")

    def _set_input_states(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for widget in self.all_input_fields:
            if hasattr(widget, "configure"):
                widget.configure(state=state)

    # ----------------------------
    # Thread Processing Loop
    # ----------------------------
    def start_scraper(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Download in progress", "Wait for the current download or cancel it first.")
            return

        ids_raw = self.ids_entry.get().strip()
        if not ids_raw:
            messagebox.showerror("Validation Error", "Please input a valid Novel ID, range (e.g., 100-110), 'library', or 'recent'.")
            return
            
        email = self.email_entry.get().strip() or None
        password = self.pass_entry.get().strip() or None
        if bool(email) != bool(password):
            messagebox.showerror(
                "Authentication Error",
                "Enter both email and password, or leave both blank to use stored tokens."
            )
            return
        out_dir = self.output_entry.get().strip() or "output"
        txt_mode = (self.format_var.get() == "TXT")
        proxy = self.proxy_entry.get().strip() or None
        
        try:
            max_chapters = int(self.max_chap_entry.get().strip() or "0")
            throttle = float(self.throttle_entry.get().strip() or "1.5")
            threads = int(self.threads_box.get())
        except ValueError as e:
            messagebox.showerror("Configuration Error", f"Failed parsing numerical settings. Check Max Chapters / Delay values.\n({e})")
            return
        if max_chapters < 0 or throttle < 0 or threads < 1:
            messagebox.showerror(
                "Configuration Error",
                "Max Chapters and Delay must be zero or greater; Threads must be at least 1."
            )
            return
            
        lang = self.lang_entry.get().strip() or "en"
        update_mode = bool(self.update_switch.get())
        debug_mode = self.debug_switch.get()
        
        const.HTTP_LOG = bool(debug_mode)
        self.logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)
        
        self.progress_bar.set(0)
        self.progress_label.configure(text="Progress: 0/0")
        self.status_label.configure(text="Status: Initializing background thread...")
        
        self.cancel_event.clear()
        
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._set_input_states(False)
        
        # Spawn thread safely
        self.worker_thread = threading.Thread(
            target=self.run_background_scraper,
            args=(email, password, out_dir, max_chapters, throttle, threads, txt_mode, update_mode, debug_mode, lang, ids_raw, proxy),
            daemon=True
        )
        self.worker_thread.start()

    def run_background_scraper(self, email, password, out_dir, max_chapters, throttle, threads, txt_mode, update_mode, debug_mode, lang, ids_raw, proxy):
        """Worker function containing blocking calls. Traps all exceptions to prevent thread crashes."""
        try:
            engine = ScraperEngine(
                email=email,
                password=password,
                proxy=proxy,
                throttle=throttle,
                out_dir=out_dir,
                language=lang,
                max_chapters=max_chapters,
                threads=threads,
                txt_mode=txt_mode,
                update_mode=update_mode,
                debug_mode=debug_mode,
                status_callback=lambda msg: self.gui_queue.put(("status", msg)),
                progress_callback=lambda curr, tot, lbl: self.gui_queue.put(("progress", (curr, tot, lbl))),
                cancel_event=self.cancel_event
            )
            
            # Blocking calls
            success = engine.initialize_client()
            if not success:
                self.gui_queue.put(("error", "Could not initialize client. No credentials or saved tokens were found."))
                return
                
            if self.cancel_event.is_set():
                self.gui_queue.put(("done", {"success": 0, "skipped": 0, "failed": 0, "results": []}))
                return
                
            self.gui_queue.put(("status", "Resolving novel IDs..."))
            target_ids = engine.resolve_novel_ids(ids_raw)
            if self.cancel_event.is_set():
                self.gui_queue.put(("done", {"success": 0, "skipped": 0, "failed": 0, "results": []}))
                return
            if not target_ids:
                self.gui_queue.put(("error", f"No valid novels found for: {ids_raw}. Check the selected list or confirm IDs are valid."))
                return
                
            summary = engine.run_download_queue(target_ids)
            
            self.gui_queue.put(("done", summary))
            
        except Exception as e:
            if self.cancel_event.is_set():
                self.gui_queue.put(("done", {"success": 0, "skipped": 0, "failed": 0, "results": []}))
                return
            tb_msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self.gui_queue.put(("error", f"An exception occurred inside the background thread:\n{e}\n\n{tb_msg}"))

    def cancel_scraper(self):
        self.status_label.configure(text="Status: Triggering cancellation sequence...")
        self.append_to_log("[GUI-CANCEL] User sent interrupt command. Closing thread execution safely...")
        self.cancel_event.set()

    # ----------------------------
    # Queue Processing & Dispatching
    # ----------------------------
    def process_queue(self):
        """Thread-safe update handler running entirely on the main UI thread."""
        try:
            while True:
                task, data = self.gui_queue.get_nowait()
                if task == "log":
                    self.append_to_log(data)
                elif task == "status":
                    self.status_label.configure(text=f"Status: {data}")
                elif task == "progress":
                    current, total, label = data
                    percentage = current / total if total > 0 else 0
                    self.progress_bar.set(percentage)
                    self.progress_label.configure(text=f"Progress: {current}/{total} - {label}")
                elif task == "done":
                    self.on_download_complete(data)
                elif task == "error":
                    self.on_download_failed(data)
                self.gui_queue.task_done()
        except queue.Empty:
            pass
        self.after(50, self.process_queue)

    def on_download_complete(self, summary: Dict[str, Any]):
        if self.cancel_event.is_set():
            self.status_label.configure(text="Status: Cancelled")
            self.append_to_log("--- Process Cancelled ---")
            messagebox.showinfo("Cancelled", "Download queue was cancelled.")
        else:
            success = summary["success"]
            skipped = summary["skipped"]
            failed = summary["failed"]
            details = f"Succeeded: {success} | Up to date: {skipped} | Failed: {failed}"
            self.append_to_log(f"\n--- Process Complete ---\n{details}")
            if failed:
                self.status_label.configure(text="Status: Completed with errors")
                messagebox.showwarning("Completed with errors", details)
            else:
                self.status_label.configure(text="Status: Completed!")
                messagebox.showinfo("All completed", details)
        
        self._reset_ui()
        self.full_scan_library()

    def on_download_failed(self, err_msg: str):
        self.status_label.configure(text="Status: Thread Crashed!")
        self.append_to_log(f"\n[FATAL ERROR] Background thread encountered an unhandled exception:\n{err_msg}\n")
        messagebox.showerror("Background Thread Error", f"Download interrupted:\n\n{err_msg}")
        self._reset_ui()

    def _reset_ui(self):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._set_input_states(True)
        self.cancel_event.clear()
        self.worker_thread = None

if __name__ == "__main__":
    app = PiaScrapGUI()
    app.mainloop()
