#!/usr/bin/env python3
"""Add missing YouTube link metadata to songs created by version 1.2.0.

This legacy migration script is kept for users upgrading old libraries.
Newer versions of the main program add link metadata automatically.
"""

import re
import sys
from pathlib import Path

from mutagen import id3

VIDEO_ID_PATTERN = re.compile(r"([A-Za-z0-9_-]{11})$")
CONFIG_FILE_NAME = ".playlist_config.json"


def update_link(file_path: Path) -> bool:
    if file_path.suffix.lower() != ".mp3":
        return False

    try:
        tags = id3.ID3(file_path)
    except Exception:
        return False

    if tags.getall("WOAR"):
        return False

    match = VIDEO_ID_PATTERN.search(file_path.stem)
    if not match:
        print(f"[WARNING] Unable to determine video id from '{file_path.name}', skipping.")
        return False

    video_id = match.group(1)
    title_frame = tags.get("TIT2")
    video_title = title_frame.text[0] if title_frame and title_frame.text else file_path.stem
    print(f"Updating link metadata for '{video_title}'...")
    tags.add(id3.WOAR(f"https://www.youtube.com/watch?v={video_id}"))
    tags.save(v2_version=3)
    return True


def update_links(playlist_name: Path) -> int:
    updated = 0
    for file_path in sorted(playlist_name.iterdir()):
        if file_path.name.startswith(".") or not file_path.is_file():
            continue
        try:
            if update_link(file_path):
                updated += 1
        except Exception as e:
            print(f"[ERROR] Unable to update '{file_path}': {e}")
    return updated


def get_existing_playlist_names(directory: Path):
    for playlist_name in sorted(directory.iterdir()):
        if not playlist_name.is_dir():
            continue
        if (playlist_name / CONFIG_FILE_NAME).is_file():
            yield playlist_name


def main() -> int:
    directory = Path.cwd()
    total = 0
    for playlist_name in get_existing_playlist_names(directory):
        total += update_links(playlist_name)
    print(f"Updated {total} file(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
