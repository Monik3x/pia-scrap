import os
import queue
import logging
import threading
import traceback
import subprocess
import sys
from typing import Dict, Any

from dotenv import load_dotenv
import customtkinter as ctk
from tkinter import filedialog, messagebox, ttk

from src.api import DownloadCancelled
from src.engine import ScraperEngine, format_run_recap
from src.helper import kebab, list_local_book_directories, load_config, load_local_book_info, local_library_novel_ids
from src import const
from src import reading_list
from src.library_preview import find_book_epub, load_book_preview

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


def _compact_marker(idx, title):
    text = (title or "").strip()
    part = ""
    if text.endswith(")") and "(" in text:
        inner = text[text.rfind("(") + 1:-1].strip()
        if inner.isdigit():
            part = f" part{int(inner)}"
    try:
        number = int(idx)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return f"c{number}{part}"
    return text

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
        
        self.current_page = 0
        self.items_per_page = 50
        self.library_dirs = []
        self.filtered_dirs = []
        self.label_filter = "All"
        self._label_map = {}
        self._label_root = None
        self._labels_unreadable = False
        self._page_rows = {}
        self._library_generation = 0
        self._library_row_cache = {}
        self._library_cache_lock = threading.Lock()
        self._library_jobs = queue.Queue()
        self._search_after = None
        self._preview_after = None
        self._preview_row = None
        self._preview_token = 0
        self._preview_window = None

        self._create_left_panel()
        self._create_right_panel()
        self._create_library_panel()
        
        self._load_saved_configurations()
        
        self.after(50, self.process_queue)
        threading.Thread(
            target=self._library_loader, name="library-loader", daemon=True
        ).start()

    def _create_left_panel(self):
        left_frame = ctk.CTkFrame(self.tab_download, corner_radius=10)
        left_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)
        
        title_lbl = ctk.CTkLabel(left_frame, text="Options", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")
        
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
        self.update_switch.select()
        self.update_switch.grid(row=6, column=0, columnspan=2, padx=10, pady=5, sticky="w")
        
        self.debug_switch = ctk.CTkSwitch(adv_frame, text="Verbose Debug Mode")
        self.debug_switch.grid(row=7, column=0, columnspan=2, padx=10, pady=(5, 10), sticky="w")
        
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
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(2, weight=1)
        secondary = dict(
            fg_color=("#E2E5E9", "#34383E"),
            hover_color=("#D1D6DC", "#454B53"),
            text_color=("#28313B", "#E3E6EB"),
            text_color_disabled=("#9299A2", "#858C96"),
        )
        blue = dict(
            fg_color="#2B6CB0",
            hover_color="#2C5282",
            text_color="#FFFFFF",
            text_color_disabled=("#A2AFBE", "#9AA9BA"),
        )
        menu_colors = dict(
            fg_color=secondary["fg_color"],
            button_color=("#D1D6DC", "#454B53"),
            button_hover_color=("#C3CAD2", "#555D67"),
            text_color=secondary["text_color"],
            dropdown_fg_color=("#F2F3F5", "#2B2F35"),
            dropdown_hover_color=("#DDE2E8", "#454B53"),
            dropdown_text_color=secondary["text_color"],
        )

        top_bar = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        top_bar.grid(row=0, column=0, padx=15, pady=(10, 6), sticky="ew")
        top_bar.grid_columnconfigure(0, weight=1)

        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.on_search_changed())
        self.search_entry = ctk.CTkEntry(top_bar, placeholder_text="Search local novels...", textvariable=self.search_var)
        self.search_entry.grid(row=0, column=0, padx=(0, 12), sticky="ew")

        self.label_filter_var = ctk.StringVar(value="All")
        self.label_filter_menu = ctk.CTkOptionMenu(
            top_bar, values=["All", "Unlabeled", *reading_list.BUILTIN_LABELS],
            variable=self.label_filter_var, command=self.on_label_filter_changed,
            width=140, **menu_colors,
        )
        self.label_filter_menu.grid(row=0, column=1, padx=(0, 8))

        refresh_btn = ctk.CTkButton(top_bar, text="\u21bb", width=34, font=ctk.CTkFont(size=22), command=self.full_scan_library, **secondary)
        refresh_btn.grid(row=0, column=2, sticky="e")

        action_bar = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        action_bar.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="ew")
        self.move_var = ctk.StringVar(value="Move to")
        self.move_menu = ctk.CTkOptionMenu(
            action_bar,
            values=["Move to", "Clear label", *reading_list.BUILTIN_LABELS],
            variable=self.move_var,
            command=self._move_selected,
            width=150, **menu_colors,
        )
        self.move_menu.pack(side="left")

        chapter_label = ctk.CTkLabel(action_bar, text="Chapter")
        chapter_label.pack(side="left", padx=(16, 6))
        self.chapter_entry = ctk.CTkEntry(action_bar, width=72)
        self.chapter_entry.pack(side="left")
        self.chapter_entry.bind("<Return>", self._set_selected_chapter_event)
        self.library_set_btn = ctk.CTkButton(action_bar, text="Set", width=52, state="disabled", command=self.set_selected_chapter, **secondary)
        self.library_set_btn.pack(side="left", padx=(8, 0))
        update_actions = ctk.CTkFrame(action_bar, fg_color="transparent")
        update_actions.pack(side="right", padx=(16, 0))
        self.library_update_btn = ctk.CTkButton(
            update_actions,
            text="Update selected",
            width=120,
            state="disabled",
            command=self.update_selected_library,
            **secondary,
        )
        self.library_update_btn.pack(side="left", padx=(0, 8))
        self.update_all_btn = ctk.CTkButton(
            update_actions, text="Update all", width=100,
            command=self.update_all_library, **blue,
        )
        self.update_all_btn.pack(side="left")

        self.library_table = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        self.library_table.grid(row=2, column=0, padx=15, pady=(0, 8), sticky="nsew")
        self.library_table.grid_columnconfigure(0, weight=1)
        self.library_table.grid_rowconfigure(0, weight=1)

        self._style_library_tree()
        self.library_tree = ttk.Treeview(
            self.library_table,
            columns=("series", "progress", "latest", "status"),
            show="headings",
            selectmode="extended",
            style="Library.Treeview",
        )
        self.library_tree.heading("series", text="Series (0)", anchor="w")
        self.library_tree.heading("progress", text="Progress", anchor="w")
        self.library_tree.heading("latest", text="Latest", anchor="w")
        self.library_tree.heading("status", text="Status", anchor="w")
        self.library_tree.column("series", width=420, minwidth=180, stretch=True, anchor="w")
        self.library_tree.column("progress", width=120, minwidth=80, stretch=False, anchor="w")
        self.library_tree.column("latest", width=140, minwidth=80, stretch=False, anchor="w")
        self.library_tree.column("status", width=100, minwidth=70, stretch=False, anchor="w")
        self.library_tree.grid(row=0, column=0, sticky="nsew")
        self.library_tree.bind("<<TreeviewSelect>>", self._on_library_select)
        self.library_tree.bind("<Double-1>", self._open_library_epub)
        self.library_tree.bind("<Motion>", self._on_library_hover)
        for sequence in ("<Leave>", "<ButtonPress>", "<MouseWheel>", "<Unmap>"):
            self.library_tree.bind(sequence, self._hide_library_preview, add="+")

        self.library_scrollbar = ttk.Scrollbar(self.library_table, orient="vertical", command=self.library_tree.yview)
        self.library_scrollbar.grid(row=0, column=1, sticky="ns")
        self.library_tree.configure(yscrollcommand=self.library_scrollbar.set)

        self.library_empty = ctk.CTkLabel(
            self.tab_library,
            text="No downloaded novel folders matching criteria.",
            font=ctk.CTkFont(slant="italic"),
        )
        self.library_empty.grid(row=2, column=0, padx=15, pady=(0, 8), sticky="n")
        self.library_empty.grid_remove()

        self.pag_frame = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        self.pag_frame.grid(row=3, column=0, padx=15, pady=(0, 15), sticky="ew")
        self.pag_frame.grid_columnconfigure((0, 2), weight=1)
        self.pag_frame.grid_columnconfigure(1, weight=0)

        self.prev_btn = ctk.CTkButton(self.pag_frame, text="◀ Previous", width=100, command=self.prev_page, **blue)
        self.prev_btn.grid(row=0, column=0, sticky="w")

        self.pag_label = ctk.CTkLabel(self.pag_frame, text="Page 1 of 1 (0 items)", font=ctk.CTkFont(size=13))
        self.pag_label.grid(row=0, column=1, padx=20)

        self.next_btn = ctk.CTkButton(self.pag_frame, text="Next ▶", width=100, command=self.next_page, **blue)
        self.next_btn.grid(row=0, column=2, sticky="e")

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
        if value == "Downloader":
            self.tab_library.pack_forget()
            self.tab_download.pack(fill="both", expand=True)
        elif value == "Local Library":
            self.tab_download.pack_forget()
            self.tab_library.pack(fill="both", expand=True)
            self.full_scan_library()

    def full_scan_library(self):
        """List book folders without reading metadata.json."""
        out_dir = self.output_entry.get().strip() or "output"
        self.library_dirs = list_local_book_directories(out_dir)
        label_root = os.path.abspath(out_dir)
        if label_root != self._label_root:
            self._label_map = {}
            self._label_root = label_root
        self._library_jobs.put(("load_labels", out_dir, [], None))
        if self._search_after is not None:
            self.after_cancel(self._search_after)
            self._search_after = None
        self._apply_library_query(reset_page=True)

    def on_search_changed(self):
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(200, self._search_library)

    def _search_library(self):
        self._search_after = None
        self._apply_library_query(reset_page=True)

    def on_label_filter_changed(self, value):
        if value == self.label_filter:
            return
        self.label_filter = value
        self._apply_library_query(reset_page=True)

    def _labels_for_view(self, out_dir):
        return self._label_map

    def _filter_values(self, labels):
        custom = []
        for value in labels.values():
            if value in reading_list.BUILTIN_LABELS or value in custom:
                continue
            if value in ("All", "Unlabeled"):
                continue
            custom.append(value)
        custom.sort(key=str.casefold)
        return ["All", "Unlabeled", *reading_list.BUILTIN_LABELS, *custom]

    def _sync_label_filter(self, labels):
        values = self._filter_values(labels)
        self.label_filter_menu.configure(values=values)
        move_values = ["Move to", "Clear label", *reading_list.BUILTIN_LABELS]
        for extra in labels.values():
            if extra not in move_values and extra not in ("All", "Unlabeled"):
                move_values.append(extra)
        self.move_menu.configure(values=move_values)
        if self.move_var.get() not in move_values:
            self.move_var.set("Move to")
        if self.label_filter not in values:
            self.label_filter = "All"
        self.label_filter_var.set(self.label_filter)

    def _style_library_tree(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        dark = ctk.get_appearance_mode() == "Dark"
        if dark:
            bg, fg, head_bg, head_fg, sel = (
                "#272B30", "#E0E3E8", "#30353B", "#AEB7C2", "#2B6CB0",
            )
        else:
            bg, fg, head_bg, head_fg, sel = (
                "#f4f7fb", "#1c2430", "#d5e0ec", "#334155", "#c5d8ee",
            )
        style.configure(
            "Library.Treeview",
            background=bg,
            fieldbackground=bg,
            foreground=fg,
            rowheight=28,
            borderwidth=0,
            font=("Segoe UI", 11),
        )
        style.configure(
            "Library.Treeview.Heading",
            background=head_bg,
            foreground=head_fg,
            relief="flat",
            font=("Segoe UI", 11),
        )
        style.map(
            "Library.Treeview",
            background=[("selected", sel)],
            foreground=[("selected", "#ffffff" if dark else "#102033")],
        )

    def _show_library_table(self, visible):
        if visible:
            self.library_empty.grid_remove()
            self.library_table.grid()
        else:
            self.library_table.grid_remove()
            self.library_empty.grid()

    def _apply_library_query(self, reset_page):
        out_dir = self.output_entry.get().strip() or "output"
        labels = self._labels_for_view(out_dir)
        self._label_map = labels
        self._sync_label_filter(labels)

        names = self.library_dirs
        query = self.search_var.get().strip().lower()
        if query:
            slug_query = kebab(query) if any(char.isalnum() for char in query) else ""
            names = [
                name for name in names
                if query in name.lower() or (slug_query and slug_query in name.lower())
            ]
        chosen = self.label_filter
        if chosen == "Unlabeled":
            names = [name for name in names if name not in labels]
        elif chosen != "All":
            names = [name for name in names if labels.get(name) == chosen]
        self.filtered_dirs = names
        if reset_page:
            self.current_page = 0
        self.display_current_page()

    def display_current_page(self):
        """Draw the visible page from memory. File reads run on the library worker."""
        self._hide_library_preview()
        selected_folders = {
            self._page_rows[row_id]["folder"] for row_id in self.library_tree.selection()
            if row_id in self._page_rows
        }
        self._library_generation += 1
        generation = self._library_generation
        self._page_rows = {}
        children = self.library_tree.get_children()
        if children:
            self.library_tree.delete(*children)
        total_items = len(self.filtered_dirs)
        self.library_tree.heading("series", text=f"Series ({total_items})")
        if total_items == 0:
            self._show_library_table(False)
            self.pag_label.configure(text="Page 1 of 1 (0 items)")
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            self._on_library_select()
            return

        self._show_library_table(True)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page
        if self.current_page >= total_pages:
            self.current_page = max(0, total_pages - 1)

        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, total_items)
        page_slice = self.filtered_dirs[start_idx:end_idx]

        self.pag_label.configure(text=f"Page {self.current_page + 1} of {total_pages} (Novels {start_idx + 1}-{end_idx} of {total_items})")
        self.prev_btn.configure(state="normal" if self.current_page > 0 else "disabled")
        self.next_btn.configure(state="normal" if self.current_page < total_pages - 1 else "disabled")

        out_dir = self.output_entry.get().strip() or "output"
        labels = self._label_map

        for index, item in enumerate(page_slice):
            row_id = f"row-{index}"
            cached = self._cached_library_row(out_dir, item)
            if cached is None:
                self._page_rows[row_id] = {
                    "folder": item,
                    "novel_id": None,
                    "chapter_count": None,
                    "has_metadata": False,
                    "has_chapters": False,
                    "progress_idx": "",
                }
                values = (item, "", "", "")
            else:
                self._page_rows[row_id] = self._page_row_info(cached)
                values = self._library_values(cached, labels)
            self.library_tree.insert("", "end", iid=row_id, values=values)
        self.library_tree.selection_set([
            row_id for row_id, info in self._page_rows.items()
            if info["folder"] in selected_folders
        ])
        self._on_library_select()
        self._library_jobs.put(("page", generation, out_dir, list(page_slice)))

    def _library_cache_id(self, out_dir, folder):
        return (os.path.abspath(out_dir), folder)

    def _cached_library_row(self, out_dir, folder):
        with self._library_cache_lock:
            row = self._library_row_cache.get(self._library_cache_id(out_dir, folder))
            if row is None:
                return None
            return dict(row)

    def _page_row_info(self, row):
        return {
            "folder": row["folder"],
            "novel_id": row["novel_id"],
            "chapter_count": row["chapter_count"],
            "has_metadata": row["has_metadata"],
            "has_chapters": row["has_chapters"],
            "progress_idx": row["progress_idx"],
        }

    def _library_values(self, row, labels):
        return (
            row["title"],
            row["progress"],
            row["latest"],
            row["status"],
        )

    def _library_file_key(self, book_dir):
        def stamp(path):
            try:
                stat = os.stat(path)
            except OSError:
                return (0, 0)
            return (stat.st_mtime_ns, stat.st_size)

        # The usual EPUB is named after the folder. List the directory only to
        # find one EPUB that is not.
        base_name = os.path.basename(os.path.normpath(book_dir)) + ".epub"
        direct = os.path.join(book_dir, base_name)
        if os.path.isfile(direct):
            epub_stamp = stamp(direct)
        else:
            epub_stamp = (0, 0)
            try:
                names = os.listdir(book_dir)
            except OSError:
                names = []
            epubs = [
                name for name in names
                if name.lower().endswith(".epub") and not name.lower().endswith(".tmp")
            ]
            chosen = None
            for name in epubs:
                if name.casefold() == base_name.casefold():
                    chosen = name
                    break
            if chosen is None and len(epubs) == 1:
                chosen = epubs[0]
            if chosen:
                epub_stamp = stamp(os.path.join(book_dir, chosen))
        return (
            stamp(os.path.join(book_dir, ".novel_id")),
            stamp(os.path.join(book_dir, ".retired")),
            stamp(os.path.join(book_dir, "metadata.json")),
            stamp(os.path.join(book_dir, "calibre_bookmarks.txt")),
            stamp(os.path.join(book_dir, "chapters.jsonl")),
            epub_stamp,
        )

    def _library_loader(self):
        while True:
            job = self._library_jobs.get()
            kind, *args = job
            if kind != "page":
                self._library_edit(kind, *args)
                continue
            generation, out_dir, folders = args
            for folder in folders:
                if generation != self._library_generation:
                    break
                row = self._load_library_row(out_dir, folder)
                if row is not None and generation == self._library_generation:
                    self.gui_queue.put(("library_row", (generation, row)))
            if generation == self._library_generation:
                try:
                    reading_list.flush_embedded_cache()
                except OSError as exc:
                    self.logger.warning(f"Could not store the library progress cache: {exc}")

    def _library_edit(self, kind, out_dir, folders, value):
        try:
            if kind == "labels":
                labels = reading_list.set_labels(out_dir, folders, value)
            elif kind == "load_labels":
                labels = reading_list.read_labels(out_dir)
            elif kind == "chapter":
                book_dir = os.path.join(out_dir, folders[0])
                line = reading_list.read_chapter_line(book_dir, value)
                try:
                    episode_no = int(line["episode_no"])
                except (TypeError, KeyError, ValueError):
                    raise ValueError("That chapter is not in this book.")
                reading_list.write_bookmark(book_dir, value, episode_no)
                with self._library_cache_lock:
                    self._library_row_cache.pop(self._library_cache_id(out_dir, folders[0]), None)
        except (OSError, reading_list.LabelsUnreadable, ValueError) as exc:
            if kind == "load_labels":
                self.gui_queue.put(("library_labels_unreadable", out_dir))
                return
            self.gui_queue.put(("library_edit_error", ("Label" if kind == "labels" else "Chapter", str(exc))))
            return
        self.gui_queue.put(("library_edit_done", (kind, out_dir, labels if kind in ("labels", "load_labels") else None)))

    def _load_library_row(self, out_dir, folder):
        book_dir = os.path.join(out_dir, folder)
        try:
            key = self._library_file_key(book_dir)
            cache_id = self._library_cache_id(out_dir, folder)
            with self._library_cache_lock:
                cached = self._library_row_cache.get(cache_id)
            if cached is not None and cached.get("key") == key:
                return dict(cached)
            reading_list.sync_embedded_progress(book_dir)
            info = load_local_book_info(book_dir)
            progress_text, progress_idx = self._progress_line(book_dir)
            latest = self._latest_marker(book_dir, info)
            first_chapter = reading_list.read_chapter_line(book_dir, 1)
            key = self._library_file_key(book_dir)
        except Exception as exc:
            self.logger.warning(f"Could not read library row {folder}: {exc}")
            return None
        row = {
            "folder": folder,
            "novel_id": info.novel_id,
            "chapter_count": info.chapter_count,
            "has_metadata": info.has_metadata,
            "has_chapters": first_chapter is not None and first_chapter.get("episode_no") is not None,
            "progress_idx": progress_idx,
            "key": key,
            "title": info.title,
            "progress": progress_text,
            "latest": latest,
            "status": info.status,
        }
        with self._library_cache_lock:
            self._library_row_cache[cache_id] = row
            return dict(row)

    def _apply_library_row(self, generation, row):
        if generation != self._library_generation:
            return
        folder = row["folder"]
        selected = self.library_tree.selection()
        touched = None
        for row_id, info in self._page_rows.items():
            if info.get("folder") != folder:
                continue
            info.update(self._page_row_info(row))
            self.library_tree.item(
                row_id, values=self._library_values(row, self._label_map)
            )
            touched = row_id
            break
        if touched is None or touched not in selected:
            return
        progress_idx = row.get("progress_idx")
        if len(selected) == 1 and progress_idx and not self.chapter_entry.get().strip():
            self.chapter_entry.insert(0, str(progress_idx))
        self._update_library_actions()

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

    def _progress_line(self, book_dir):
        bookmark = reading_list.read_bookmark(book_dir)
        if bookmark is None:
            return "", ""
        resolved = reading_list.resolve_bookmark(book_dir, bookmark)
        if resolved is None:
            return "", ""
        idx, _episode_no, title = resolved
        return _compact_marker(idx, title), str(idx)

    def _latest_marker(self, book_dir, info):
        last = reading_list.read_last_chapter(book_dir)
        if last is not None:
            return _compact_marker(last.get("idx"), last.get("title") or "")
        if info.chapter_count:
            return f"c{info.chapter_count}"
        if info.has_metadata:
            return ""
        return "Unchecked"

    def _hide_library_preview(self, _event=None):
        self._preview_token += 1
        self._preview_row = None
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
            self._preview_after = None
        if self._preview_window is not None:
            self._preview_window.destroy()
            self._preview_window = None

    def _on_library_hover(self, event):
        row = self.library_tree.identify_row(event.y)
        if self.library_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            row = None
        if row == self._preview_row:
            return
        self._hide_library_preview()
        if row not in self._page_rows:
            return
        self._preview_row = row
        token = self._preview_token
        book_dir = os.path.join(self.output_entry.get().strip() or "output",
                                self._page_rows[row]["folder"])
        self._preview_after = self.after(450, lambda: self._request_library_preview(token, book_dir))

    def _request_library_preview(self, token, book_dir):
        self._preview_after = None
        def load():
            preview = load_book_preview(book_dir)
            self.gui_queue.put(("library_preview", (token, preview)))
        threading.Thread(target=load, name="library-preview", daemon=True).start()

    def _show_library_preview(self, token, preview):
        if token != self._preview_token or self._preview_row not in self._page_rows:
            return
        info = self._page_rows[self._preview_row]
        popup = ctk.CTkToplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        self._preview_window = popup
        card = ctk.CTkFrame(popup, corner_radius=8, border_width=1)
        card.pack(fill="both", expand=True)
        cover = preview["cover"]
        image = ctk.CTkImage(light_image=cover, dark_image=cover, size=cover.size) if cover else None
        ctk.CTkLabel(card, text="" if image else "No cover saved", image=image,
                     width=140, height=210).grid(row=0, column=0, rowspan=4, padx=14, pady=14, sticky="n")
        ctk.CTkLabel(card, text=preview["title"], font=ctk.CTkFont(size=16, weight="bold"),
                     wraplength=330, justify="left").grid(row=0, column=1, padx=(0, 14), pady=(12, 0), sticky="w")
        ctk.CTkLabel(card, text=preview["author"], wraplength=330, justify="left").grid(
            row=1, column=1, padx=(0, 14), sticky="w")
        label = self._label_map.get(info["folder"], "Unlabeled")
        ctk.CTkLabel(card, text=f"Label: {label}", text_color=("#176345", "#75d9b1"),
                     wraplength=330, justify="left").grid(row=2, column=1, padx=(0, 14), sticky="w")
        description = preview["description"]
        description = description[:700].rstrip() + "…" if len(description) > 700 else description
        ctk.CTkLabel(card, text=description, wraplength=330, justify="left", anchor="nw").grid(
            row=3, column=1, padx=(0, 14), pady=(6, 14), sticky="nw")
        popup.update_idletasks()
        width, height = popup.winfo_reqwidth(), popup.winfo_reqheight()
        x, y = self.winfo_pointerxy()
        x = x + 18 if x + 18 + width < self.winfo_screenwidth() else x - width - 18
        y = min(y + 16, self.winfo_screenheight() - height - 8)
        popup.geometry(f"+{max(0, x)}+{max(0, y)}")
        popup.deiconify()

    def _open_library_epub(self, event):
        self._hide_library_preview()
        if self.library_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        row_id = self.library_tree.identify_row(event.y)
        info = self._page_rows.get(row_id)
        if not info:
            return
        out_dir = self.output_entry.get().strip() or "output"
        book_dir = os.path.abspath(os.path.join(out_dir, info["folder"]))
        threading.Thread(
            target=self._launch_library_epub, args=(book_dir,), daemon=True,
        ).start()
        return "break"

    def _launch_library_epub(self, book_dir):
        try:
            path = find_book_epub(book_dir)
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.run(
                    ["open" if sys.platform == "darwin" else "xdg-open", path],
                    check=True, capture_output=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self.gui_queue.put(("library_open_error", f"Could not open EPUB:\n{exc}"))

    def _on_library_select(self, _event=None):
        selected = self.library_tree.selection()
        folders = frozenset(self._page_rows[row_id]["folder"] for row_id in selected if row_id in self._page_rows)
        if folders != getattr(self, "_chapter_selection", frozenset()):
            self._chapter_selection = folders
            self.chapter_entry.delete(0, "end")
            if len(selected) == 1:
                progress_idx = self._page_rows.get(selected[0], {}).get("progress_idx") or ""
                if progress_idx:
                    self.chapter_entry.insert(0, progress_idx)
        self._update_library_actions()

    def _update_library_actions(self):
        selected = self.library_tree.selection()
        info = self._page_rows.get(selected[0], {}) if len(selected) == 1 else {}
        ready = info.get("has_metadata") and (info.get("chapter_count") or 0) > 0 and info.get("has_chapters")
        self.library_set_btn.configure(state="normal" if ready else "disabled")
        state = "normal" if self._selected_novel_ids() else "disabled"
        self.library_update_btn.configure(state=state)

    def _selected_novel_ids(self):
        ids = []
        for name in self.library_tree.selection():
            novel_id = self._page_rows.get(name, {}).get("novel_id")
            if novel_id:
                ids.append(str(novel_id))
        return ids

    def _move_selected(self, value):
        self.move_var.set("Move to")
        if value in ("Move to", ""):
            return
        selected = list(self.library_tree.selection())
        if not selected:
            messagebox.showerror("Label", "Select a novel.")
            return
        label = "" if value == "Clear label" else value
        if label in ("All", "Unlabeled"):
            messagebox.showerror("Label", "All and Unlabeled are filters. Use another name.")
            return
        out_dir = self.output_entry.get().strip() or "output"
        folders = [self._page_rows[name]["folder"] for name in selected if name in self._page_rows]
        self._library_jobs.put(("labels", out_dir, folders, label))

    def _set_selected_chapter_event(self, _event):
        self.set_selected_chapter()
        return "break"

    def set_selected_chapter(self):
        selected = self.library_tree.selection()
        if len(selected) != 1:
            messagebox.showerror("Chapter", "Select one novel.")
            return
        info = self._page_rows.get(selected[0])
        if not info or not info.get("has_metadata") or (info.get("chapter_count") or 0) <= 0 or not info.get("has_chapters"):
            return
        self.set_row_chapter(
            info["folder"],
            self.chapter_entry,
            info["chapter_count"],
            info["has_metadata"],
        )

    def update_selected_library(self):
        ids = self._selected_novel_ids()
        if not ids:
            messagebox.showerror("Missing Novel ID", "Select a novel that has an ID.")
            return
        self.trigger_library_update(", ".join(ids))

    def set_row_chapter(self, folder_name, box, chapter_count, has_metadata):
        out_dir = self.output_entry.get().strip() or "output"
        raw = box.get().strip()
        try:
            idx = int(raw)
        except ValueError:
            messagebox.showerror("Chapter", "Enter a chapter number.")
            return
        if not has_metadata or chapter_count is None or idx < 1 or idx > chapter_count:
            messagebox.showerror("Chapter", "That chapter is not in this book.")
            return
        self._library_jobs.put(("chapter", out_dir, [folder_name], idx))

    def trigger_library_update(self, novel_id):
        if not novel_id:
            messagebox.showerror("Missing Novel ID", "This folder does not contain a valid Novel ID.")
            return
        self.format_var.set("EPUB")
        self.ids_entry.delete(0, "end")
        self.ids_entry.insert(0, str(novel_id))
        self.update_switch.select()

        self.nav_var.set("Downloader")
        self.on_nav_changed("Downloader")
        self.start_scraper()

    def update_all_library(self):
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
        
        self.worker_thread = threading.Thread(
            target=self.run_background_scraper,
            args=(email, password, out_dir, max_chapters, throttle, threads, txt_mode, update_mode, debug_mode, lang, ids_raw, proxy),
            daemon=True
        )
        self.worker_thread.start()

    def run_background_scraper(self, email, password, out_dir, max_chapters, throttle, threads, txt_mode, update_mode, debug_mode, lang, ids_raw, proxy):
        """Catch worker exceptions so they cannot kill the UI thread."""
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
            
            success = engine.initialize_client()
            if not success:
                self.gui_queue.put(("error", "Could not initialize client. No credentials or saved tokens were found."))
                return
                
            if self.cancel_event.is_set():
                self.gui_queue.put((
                    "done",
                    {"success": 0, "skipped": 0, "failed": 0, "results": [], "cancelled": True},
                ))
                return
                
            self.gui_queue.put(("status", "Resolving novel IDs..."))
            target_ids = engine.resolve_novel_ids(ids_raw)
            if self.cancel_event.is_set():
                self.gui_queue.put((
                    "done",
                    {"success": 0, "skipped": 0, "failed": 0, "results": [], "cancelled": True},
                ))
                return
            if not target_ids:
                self.gui_queue.put(("error", f"No valid novels found for: {ids_raw}. Check the selected list or confirm IDs are valid."))
                return
                
            summary = engine.run_download_queue(target_ids)
            
            self.gui_queue.put(("done", summary))
            
        except DownloadCancelled:
            self.gui_queue.put((
                "done",
                {"success": 0, "skipped": 0, "failed": 0, "results": [], "cancelled": True},
            ))
            return
        except Exception as e:
            tb_msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self.gui_queue.put(("error", f"An exception occurred inside the background thread:\n{e}\n\n{tb_msg}"))

    def cancel_scraper(self):
        self.status_label.configure(text="Status: Triggering cancellation sequence...")
        self.append_to_log("[GUI-CANCEL] User sent interrupt command. Closing thread execution safely...")
        self.cancel_event.set()

    def process_queue(self):
        """Apply worker events on the UI thread."""
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
                elif task == "library_row":
                    generation, row = data
                    self._apply_library_row(generation, row)
                elif task == "library_edit_error":
                    title, error = data
                    messagebox.showerror(title, error)
                elif task == "library_edit_done":
                    kind, out_dir, labels = data
                    if os.path.abspath(self.output_entry.get().strip() or "output") == os.path.abspath(out_dir):
                        if kind in ("labels", "load_labels"):
                            self._labels_unreadable = False
                            self._label_map = labels
                            self._apply_library_query(reset_page=False)
                        else:
                            self.display_current_page()
                elif task == "library_labels_unreadable":
                    if os.path.abspath(self.output_entry.get().strip() or "output") == os.path.abspath(data):
                        if not self._labels_unreadable:
                            self.append_to_log("[GUI-WARN] reading_labels.json could not be read. Labels stay hidden until that file is fixed.")
                        self._labels_unreadable = True
                elif task == "library_open_error":
                    messagebox.showerror("Open EPUB", data)
                elif task == "library_preview":
                    self._show_library_preview(*data)
                self.gui_queue.task_done()
        except queue.Empty:
            pass
        self.after(50, self.process_queue)

    def on_download_complete(self, summary: Dict[str, Any]):
        dialog_recap = format_run_recap(summary, max_lines=40)
        report_path = summary.get("report_path")
        if self.cancel_event.is_set():
            self.status_label.configure(text="Status: Cancelled")
            self.append_to_log("--- Process Cancelled ---")
            if report_path:
                self.append_to_log(f"Report: {report_path}")
            messagebox.showinfo(
                "Cancelled",
                "Download queue was cancelled.\n\n" + dialog_recap,
            )
        else:
            success = summary["success"]
            skipped = summary["skipped"]
            failed = summary["failed"]
            has_issues = bool(summary.get("run_issues")) or any(
                item.get("issues") for item in summary.get("results") or []
            )
            details = f"Succeeded: {success} | Up to date: {skipped} | Failed: {failed}"
            self.append_to_log(f"\n--- Process Complete ---\n{details}")
            if report_path:
                self.append_to_log(f"Report: {report_path}")
            if failed:
                self.status_label.configure(text="Status: Completed with errors")
                messagebox.showwarning("Completed with errors", dialog_recap)
            elif has_issues:
                self.status_label.configure(text="Status: Completed with warnings")
                messagebox.showwarning("Completed with warnings", dialog_recap)
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
