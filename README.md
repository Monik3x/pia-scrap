# pia-scrap (Personal Fork)

A fork of [pia-scrap](https://github.com/bayue48/pia-scrap) by bayue48, personalised for ease of updating/maintaining novels. Refer to the original for more details and troubleshooting. Modifications from the original were made with AI assistance.

> **Provided "as is", for personal use only. Use responsibly. Do not redistribute the content. Follow Novelpia's Terms of Service and Copyright.**

---

## Features

- API-based fetch (no browser automation)
- Proper EPUB with cover, About page, per-chapter files, ToC, NCX/Nav
- Preserves inline images (downloaded and embedded)
- Handles token refresh and optional throttling to reduce rate limits
- **Smart Updating** — uses a local cache to fetch only new chapters when updating, skipping novels that are already up to date entirely.
- **Queueing** — supports downloading sequential ranges of novels automatically

---

## Requirements

- Python 3.9+
- Packages: `requests`, `beautifulsoup4`, `ebooklib`

```bash
pip install -r requirements.txt
```

---

## CLI

```bash
python main.py NOVEL_ID [--user EMAIL] [--pass PASSWORD]
               [--out DIR] [--max-chapters N]
               [--lang en] [--proxy URL] [--throttle SECONDS]
               [--debug] [--txt] [--update]
```

### Arguments

| Argument | Description |
|---|---|
| `NOVEL_ID` | Numeric or range `novel_no`, e.g. `49` or `47-50` |
| `--user`, `--pass` | Login credentials; tokens saved to `.api.json` for reuse |
| `--out` | Output directory (default: `output`) |
| `--max-chapters` | Fetch up to N episodes (`0` or unset = all) |
| `--lang` | EPUB language code (default: `en`) |
| `--proxy` | HTTP/HTTPS proxy, e.g. `http://host:port` |
| `--throttle` | Seconds to wait between episode/ticket/content calls (default: `2.0`) |
| `--debug` | Verbose request logs and optional JSON dumps for failures |
| `--txt` | Export as `.txt` per episode instead of EPUB |
| `--update` | Generate/access a local cache to update existing EPUBs without redownloading older chapters |

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
python main.py 100-110 --update
```

---

## Output

Files are written to `output/<title>/`:

```
output/<title>/<title>.epub
output/<title>/<episode-title>.txt   # if --txt is used
output/<title>/.raw_cache/           # if --update is used
```
