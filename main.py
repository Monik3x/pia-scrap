import argparse
import sys
import logging
from dotenv import load_dotenv
from src.engine import ScraperEngine
from src import const

# ----------------------------
# Main Function
# ----------------------------

def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Novelpia to EPUB packer (API)")
    ap.add_argument("novel_ids", help=("Novel ID (e.g. 1072) or Range (e.g. 1000-1050) or mixed strings (47,49,51-55), 'library' for favorites, or 'recent' for K-Premium novels among the 30 latest listings"),)
    ap.add_argument("--user", "--email", "-u", "-e", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("--pass", "--password", "-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--max-chapters", "-max", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("--lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("--proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("--debug", "-v", action="store_true", help="Enable verbose diagnostics and request-failure logs")
    ap.add_argument("--throttle", type=float, default=1.5, help="Seconds delay between episode requests (default: 1.5; 0 disables)")
    ap.add_argument("--txt", "-txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    ap.add_argument(
        "--update",
        action="store_true",
        help="Reuse the local episode cache to skip unchanged chapters",
    )
    ap.add_argument("--threads", type=int, default=1, help="Number of workers sending requests (default: 1), recommended to leave as is")
    args = ap.parse_args()

    if args.max_chapters < 0:
        ap.error("--max-chapters must be zero or greater")
    if args.throttle < 0:
        ap.error("--throttle must be zero or greater")
    if args.threads < 1:
        ap.error("--threads must be at least 1")
    if bool(args.email) != bool(args.password):
        ap.error("provide both --user and --pass, or neither to use stored tokens")

    # Configure Logging based on debug mode
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    const.HTTP_LOG = bool(args.debug)

    engine = ScraperEngine(
        email=args.email,
        password=args.password,
        proxy=args.proxy,
        throttle=args.throttle,
        out_dir=args.out,
        language=args.lang,
        max_chapters=args.max_chapters,
        threads=args.threads,
        txt_mode=args.txt,
        update_mode=args.update,
        debug_mode=args.debug
    )

    # Initialize client (reusing or saving tokens)
    try:
        if not engine.initialize_client():
            print("[error] No credentials or stored tokens found. Provide --user and --pass to login once.")
            sys.exit(2)
    except Exception as e:
        print(f"[error] Failed client initialization: {e}")
        sys.exit(2)

    # Resolve IDs from input queue, library, or recent public listings
    try:
        target_ids = engine.resolve_novel_ids(args.novel_ids)
        if not target_ids:
            print("[warn] Queue is empty or failed to parse. Exiting.")
            sys.exit(0)
    except Exception as e:
        print(f"[error] Failed to parse or retrieve the requested novel list: {e}")
        sys.exit(1)

    print(f"[info] Queue size: {len(target_ids)} novels")

    # Run the scraper loop
    results = engine.run_download_queue(target_ids)
    sys.exit(0 if results["failed"] == 0 else 1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
