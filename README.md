# pia-scrap (Personal Fork)

A fork of [pia-scrap](https://github.com/bayue48/pia-scrap) by bayue48, personalised for ease of updating/maintaining novels. Refer to the original for more details and troubleshooting. Modifications from the original were made with AI assistance.

> **Provided "as is", for personal use only. Use responsibly. Do not redistribute the content. Follow Novelpia's Terms of Service and Copyright.**

---

## New Features

- **Smart Updating** — uses a local cache to fetch only new chapters when updating, skipping novels that are already up to date entirely.
- **Queueing** — supports downloading sequential ranges of novels automatically
- **Improved threading** - added thread stagger, implemented safe KeyboardInterrupt thread cancellation, reduced aggressive throttle penalty 
- **Image scraping actually works** - injects neccesary CloudFront keys to download images

---



## Requirements

- Python 3.9+
- Packages: `curl_cffi`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`

```bash
pip install -r requirements.txt
```

---

## CLI

```bash
python main.py NOVEL_ID [--user EMAIL] [--pass PASSWORD]
               [--out DIR] [--max-chapters N]
               [--lang en] [--proxy URL] [--throttle SECONDS]
               [--debug] [--txt] [--update] [--threads]
```

### Arguments

| Argument | Description |
|---|---|
| `NOVEL_ID` | Mixed numeric or range `novel_no`, e.g. `49` or `40,47-50` |
| `--user`, `--pass` | Login credentials; tokens saved to `.api.json` for reuse |
| `--out` | Output directory (default: `output`) |
| `--max-chapters` | Fetch up to N episodes (`0` or unset = all) |
| `--lang` | EPUB language code (default: `en`) |
| `--proxy` | HTTP/HTTPS proxy, e.g. `http://host:port` |
| `--throttle` | Seconds to wait between episode/ticket/content calls (default: `2.0`) |
| `--debug` | Verbose request logs and optional JSON dumps for failures |
| `--txt` | Export as `.txt` per episode instead of EPUB |
| `--update` | Generate/access a local cache to update existing EPUBs without redownloading older chapters | `--threads` | Number of workers sending requests, recommended to leave as is (default: `1`) |

---

## Quick Start

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

---

## Output

Files are written to `output/<title>/`:

```
output/<title>/<title>.epub
output/<title>/<episode-title>.txt   # if --txt is used
output/<title>/.raw_cache/           # if --update is used
```
---

## Quick Notes

Parallel fetching (multithreading) is pretty much fundementally incompatible with Novelpia's API. It has a low threshold concerning rate limits and requests per second. Even with stagger it will immediately throw out a HTTP 429 (Too Many Requests) error. Parallel downloading only works if the server allows high concurrency. Therefore, 99% of the time, **it's slower than sending requests sequentially**. Don't ask me why it's there.

Also the max-chapters flag doesn't work.
