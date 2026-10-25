#!/usr/bin/env python3
"""Build a standalone executable with PyInstaller.

Usage:
    python -m pip install -U pyinstaller -r requirements.txt
    git submodule update --init --recursive
    python scripts/build.py
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YTDLP_DIR = ROOT / "scripts" / "yt-dlp-master"
YTDLP_MAIN = YTDLP_DIR / "yt_dlp" / "__main__.py"


def main() -> int:
    if "idlelib.run" in sys.modules and sys.stdin is not sys.__stdin__:
        print("Do not run this from IDLE. Run this script from a console instead.")
        return 1

    if not YTDLP_MAIN.is_file():
        print(
            "The yt-dlp submodule is not initialized.\n"
            "Run the following command from the repository root first:\n"
            "    git submodule update --init --recursive"
        )
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Install it with:\n"
              "    python -m pip install -U pyinstaller -r requirements.txt")
        return 1

    # Generate the lazy extractor list expected by PyInstaller.
    lazy_script = YTDLP_DIR / "devscripts" / "make_lazy_extractors.py"
    if lazy_script.is_file():
        print("Generating lazy yt-dlp extractors...")
        subprocess.run([sys.executable, str(lazy_script)], cwd=YTDLP_DIR, check=True)
    else:
        print(f"[WARNING] Lazy extractor generator not found at '{lazy_script}', skipping.")

    os.chdir(ROOT)
    opts = [
        "-F",
        "--upx-exclude=vcruntime140.dll",
        "--noconfirm",
        "--additional-hooks-dir=scripts/yt-dlp-master/yt_dlp/__pyinstaller",
        "youtube_music_playlist_downloader.py",
        "scripts/yt-dlp-master/yt_dlp/__main__.py",
    ]

    print(f"Running PyInstaller with {opts}")
    from PyInstaller.__main__ import run as run_pyinstaller

    run_pyinstaller(opts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
