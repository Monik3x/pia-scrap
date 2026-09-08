# pia-scrap (Personal Fork)

A fork of [pia-scrap](https://github.com/bayue48/pia-scrap) by bayue48, was personalised for ease of updating/maintaining novels back when the project was not being maintained and key features were missing. Check upstream, it will probably be more up to date and feature rich. Modifications from the original were heavily made with AI assistance. Updates are made whenever I pick up reading novels again. 

> **Provided "as is", for personal use only. Use responsibly. Do not redistribute the content. Follow Novelpia's Terms of Service and Copyright.**

---

## Requirements

- Python 3.9+
- Packages: `curl_cffi`, `beautifulsoup4`, `ebooklib`, `python-dotenv`, `customtkinter`

```bash
pip install -r requirements.txt
```

---

## Usage

### Graphical Interface

```bash
python gui.py
```

<img width="1270" height="959" alt="image" src="https://github.com/user-attachments/assets/4e802f78-0a73-49f6-8e01-35a9ef30fc5c" />

---

### Command Line Interface

```bash
python main.py NOVEL_ID [--user EMAIL] [--pass PASSWORD]
               [--out DIR] [--max-chapters N]
               [--lang en] [--proxy URL] [--throttle SECONDS]
               [--debug] [--txt] [--update] [--threads N]
```

### CLI Arguments

| Argument | Description |
|---|---|
| `NOVEL_ID` | Mixed numeric/range `novel_no` (for example `49` or `40,47-50`), `mybook`/`library` for user favorites, or `recent`/`latest` for K-Premium novels among the 30 latest public listings |
| `--user`, `--pass` | Login credentials; tokens saved to `.api.json` for reuse |
| `--out` | Output directory (default: `output`) |
| `--max-chapters` | Fetch up to N episodes (`0` or unset = all) |
| `--lang` | EPUB language code (default: `en`) |
| `--proxy` | HTTP/HTTPS proxy, e.g. `http://host:port` |
| `--throttle` | Seconds to wait between episode/ticket/content calls (default: `1.5`; `0` disables the delay) |
| `--debug` | Verbose diagnostics and request failure logs |
| `--txt` | Export as `.txt` per episode instead of EPUB |
| `--update` | Reuse the local episode cache to skip unchanged chapters. EPUB also reuses images from the existing book. | 
| `--threads` | Number of workers sending requests, recommended to leave as is (default: `1`) |

---

## CLI Quick Start

**1. First run** — provide your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python main.py 49 --user you@example.com --pass "your-password"
```

**2. Subsequent runs** — reuse stored tokens (no password needed on the command line):

```bash
python main.py 49
```

**3. Download a range** — skip any novels that are already up to date:

```bash
python main.py 95,100-110 --update
```

**4. Download library** — access user favorites:

```bash
python main.py library --update
```

**5. Download recent K-Premium releases** — inspect the 30 latest public listings and download only K-Premium titles:

```bash
python main.py recent --update
```

---

## Output

Files are written to `output/<title>/`:

```text
output/<title>/<title>.epub
output/<title>/<episode-title>.txt          # if --txt is used
output/<title>/.raw_cache/                  # episode JSON if --update is used
output/<title>/.raw_cache/image_index.json  # URL to content-hash map for EPUB image reuse
output/<title>/chapters.jsonl               # chapter URLs and per-episode revision markers
output/last_run_report.txt                  # end-of-run failure/warning recap (overwritten each queue)
```

After a queue finishes, CLI and GUI both show a short run recap: failed or skipped novel IDs with reasons, plus intra-novel warnings captured during the run (for example dropped images). If the queue is cancelled, the recap says it was interrupted and names any in-progress novel. The same text is written to `last_run_report.txt` under the output directory so an overnight run is still reviewable after the window is closed.

---

## Quick Notes

Parallel fetching (multithreading) is pretty much fundamentally incompatible with Novelpia's API. It has a low threshold concerning rate limits and requests per second. Even with stagger it will immediately throw out a HTTP 429 (Too Many Requests) error. Parallel downloading only works if the server allows high concurrency. Therefore, 99% of the time, **it's slower than sending requests sequentially**. Don't ask me why it's there.

---

## Development

Running the downloader only needs `requirements.txt`. Tests, OpenAPI specs, and agent docs ship in the same tree and are unused at runtime.

```bash
pip install -r requirements-dev.txt
python -m pytest
```
