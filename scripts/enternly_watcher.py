"""
Enternly CV Watcher — monitors a folder (default: ~/Downloads) and
automatically uploads files named *_Resume.(pdf|docx|doc) to the
Enternly CV Repository.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (run once on recruiter's PC)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Install dependencies:
       pip install watchdog requests

2. Get your API token from Enternly:
       Log in → click "CV Repository" in the sidebar → click your name
       top-right → "Generate API Token".  Copy the token shown.

3. Edit CONFIG below:
       ENTERNLY_URL  — your Enternly server URL (e.g. https://ats.yourcompany.com)
       API_TOKEN  — the token you just copied
       WATCH_DIR  — folder to watch (default ~/Downloads)

4. Run:
       python enternly_watcher.py

   Or to run in background on Windows:
       pythonw enternly_watcher.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ── CONFIG — edit these three lines ──────────────────────────────────────────
ENTERNLY_URL  = "http://localhost:8080"           # no trailing slash
API_TOKEN  = "paste-your-api-token-here"       # from CV Repository → Generate Token
WATCH_DIR  = None                               # None = ~/Downloads
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import re
import time
from pathlib import Path

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("enternly-watcher")

_RESUME_PATTERN = re.compile(r'.*_resume\.(pdf|docx|doc)$', re.IGNORECASE)
_WRITE_SETTLE   = 2   # seconds to wait after a file event before uploading


def _upload(path: Path):
    """Upload a single file to Enternly CV Repository."""
    if not _RESUME_PATTERN.match(path.name):
        return
    log.info(f"Detected: {path.name}")
    time.sleep(_WRITE_SETTLE)   # wait for write to complete
    if not path.exists():
        log.warning(f"File disappeared before upload: {path}")
        return
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                f"{ENTERNLY_URL}/api/cv/upload",
                headers={"Authorization": f"Bearer {API_TOKEN}"},
                files={"files": (path.name, fh, _mime(path.suffix))},
                timeout=60,
            )
        if resp.status_code == 200:
            data = resp.json()
            mapped = sum(1 for d in data.get("details", []) if d.get("mapped"))
            log.info(
                f"Uploaded {path.name} — "
                f"processed={data.get('processed')}, "
                f"duplicates={data.get('duplicates')}, "
                f"matched candidate={mapped > 0}"
            )
        else:
            log.error(f"Upload failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as exc:
        log.error(f"Upload error for {path.name}: {exc}")


def _mime(suffix: str) -> str:
    return {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc":  "application/msword",
    }.get(suffix.lower(), "application/octet-stream")


class _Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            _upload(Path(event.src_path))

    def on_moved(self, event):
        # Handles browser saves that first write to .crdownload then rename
        if not event.is_directory:
            _upload(Path(event.dest_path))


def main():
    watch = Path(WATCH_DIR) if WATCH_DIR else Path.home() / "Downloads"
    if not watch.is_dir():
        log.error(f"Watch directory not found: {watch}")
        return

    log.info(f"Watching: {watch}")
    log.info(f"Server:   {ENTERNLY_URL}")
    log.info("Waiting for *_Resume.pdf / .docx / .doc files…  (Ctrl-C to stop)")

    observer = Observer()
    observer.schedule(_Handler(), str(watch), recursive=False)
    observer.start()
    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        log.info("Watcher stopped.")


if __name__ == "__main__":
    main()
