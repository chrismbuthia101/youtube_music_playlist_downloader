#!/usr/bin/env python3
"""Download a single song as an MP3 with basic link metadata.

This is a small standalone helper. For playlists, use
``youtube_music_playlist_downloader.py`` instead.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mutagen.id3 import ID3, WOAR
from mutagen.id3 import error as ID3Error
from yt_dlp import YoutubeDL, postprocessor

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class FilePathCollector(postprocessor.common.PostProcessor):
    def __init__(self):
        super().__init__(None)
        self.file_paths = []

    def run(self, information):
        self.file_paths.append(information["filepath"])
        return [], information


def check_ffmpeg() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
    except Exception:
        return False
    return True


def get_video_id(value: str) -> str:
    value = value.strip().strip("\"'")
    if VIDEO_ID_PATTERN.fullmatch(value):
        return value

    parsed = urlparse(value if "://" in value else "https://" + value)
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
        if VIDEO_ID_PATTERN.fullmatch(candidate):
            return candidate

    candidate = parse_qs(parsed.query).get("v", [""])[0]
    if VIDEO_ID_PATTERN.fullmatch(candidate):
        return candidate

    raise ValueError(f"Could not find a YouTube video id in '{value}'")


def generate_metadata(file_path: str, link: str) -> None:
    if Path(file_path).suffix.lower() != ".mp3":
        print(f"[WARNING] Skipping link metadata for non-MP3 file '{file_path}'.")
        return
    try:
        tags = ID3(file_path)
    except ID3Error:
        tags = ID3()
    tags.delall("WOAR")
    tags.add(WOAR(link))
    tags.save(file_path, v2_version=3)


def download_video(link: str, output_dir: str, audio_quality: str) -> str:
    ytdl_opts = {
        "outtmpl": os.path.join(output_dir, "%(title)s-%(id)s.%(ext)s"),
        "ignoreerrors": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": audio_quality,
        }],
        "geo_bypass": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
    }

    with YoutubeDL(ytdl_opts) as ytdl:
        file_path_collector = FilePathCollector()
        ytdl.add_post_processor(file_path_collector)
        result = ytdl.download([link])

    if result != 0 or not file_path_collector.file_paths:
        raise RuntimeError("Download failed, the video may be unavailable")
    file_path = file_path_collector.file_paths[0]
    if not os.path.isfile(file_path):
        raise RuntimeError(f"Downloaded file does not exist: '{file_path}'")
    return file_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download a single YouTube song as an MP3.")
    parser.add_argument("link", nargs="?", help="YouTube video URL or 11-character video id")
    parser.add_argument("-o", "--output", default=".", help="output directory (default: current directory)")
    parser.add_argument("--audio-quality", default="5", metavar="QUALITY",
                        help="ffmpeg audio quality passed to yt-dlp (default: 5)")
    args = parser.parse_args(argv)

    if not check_ffmpeg():
        print("[ERROR] ffmpeg was not found. Install ffmpeg and make sure it is on your PATH.")
        return 1

    link = args.link or input("Link: ")
    try:
        video_id = get_video_id(link)
        output_dir = os.path.abspath(os.path.expanduser(args.output))
        os.makedirs(output_dir, exist_ok=True)
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        file_path = download_video(watch_url, output_dir, args.audio_quality)
        generate_metadata(file_path, watch_url)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1

    print(f"Saved '{file_path}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
