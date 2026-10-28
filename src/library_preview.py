import io
import json
import logging
import os
import zipfile

from PIL import Image


logger = logging.getLogger("pia_scrap")


def find_book_epub(book_dir):
    with os.scandir(book_dir) as entries:
        epubs = [entry.path for entry in entries
                 if entry.name.lower().endswith(".epub") and entry.is_file()]
    expected = os.path.basename(os.path.normpath(book_dir)) + ".epub"
    path = next(
        (path for path in epubs if os.path.basename(path).casefold() == expected.casefold()),
        epubs[0] if len(epubs) == 1 else None,
    )
    if path is None:
        if epubs:
            raise OSError("Multiple EPUBs found; none matches the book folder name.")
        raise OSError("No EPUB found for this novel. Download it as EPUB first.")
    return path


def load_book_preview(book_dir):
    preview = {
        "title": os.path.basename(os.path.normpath(book_dir)),
        "author": "Unknown Author",
        "description": "No description saved.",
        "cover": None,
    }
    try:
        with open(os.path.join(book_dir, "metadata.json"), encoding="utf-8") as stream:
            metadata = json.load(stream)
        if isinstance(metadata, dict):
            for key in ("title", "author", "description"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    preview[key] = value.strip()
    except (OSError, ValueError):
        pass
    try:
        with zipfile.ZipFile(find_book_epub(book_dir)) as archive:
            cover = next((item for item in archive.infolist()
                          if os.path.basename(item.filename).lower() in
                          ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "cover.gif")), None)
            if cover is not None and cover.file_size <= 20 * 1024 * 1024:
                with Image.open(io.BytesIO(archive.read(cover))) as image:
                    image.thumbnail((140, 210))
                    preview["cover"] = image.convert("RGB")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, Image.DecompressionBombError) as exc:
        logger.debug("Could not load local preview cover for %s: %s", book_dir, exc)
    return preview
