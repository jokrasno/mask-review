"""Bounded local configuration for manually transferred, private Drive assets."""
import json
import re
from pathlib import Path

ID = r"[A-Za-z0-9_-]{10,200}"
FOLDER = re.compile(r"https://drive\.google\.com/drive/folders/" + ID)
FILE = re.compile(r"https://drive\.google\.com/file/d/" + ID + r"/view")


def read_library(data):
    unavailable = {"available": False}
    try:
        path = Path(data) / "shared-library.json"
        if path.stat().st_size > 1_000_000:
            return unavailable
        value = json.loads(path.read_text("utf-8"))
        if value.get("format") != "mask-review-shared-library-v1":
            return unavailable
        folders, originals = value["folders"], value["originals"]
        if set(folders) != {"library", "originals", "labels", "incoming"}:
            return unavailable
        if not all(isinstance(url, str) and FOLDER.fullmatch(url) for url in folders.values()):
            return unavailable
        if not isinstance(originals, dict) or not all(re.fullmatch(r"[a-f0-9]{64}", sha) and isinstance(url, str) and FILE.fullmatch(url) for sha, url in originals.items()):
            return unavailable
        at = value.get("snapshot_at", "")
        if not isinstance(at, str) or (at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", at)):
            return unavailable
        return dict(available=True, folders=folders, originals=originals, snapshot_at=at)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unavailable
