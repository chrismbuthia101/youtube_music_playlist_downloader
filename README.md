# YouTube Music Playlist Downloader

This program automatically downloads & updates a local copy
of any YouTube playlist in the form of a music album folder
- Songs are stored in album folders named by playlist title
- Existing albums are updated with any new or missing songs
- Songs no longer in the playlist are moved to end of album
- Song metadata is automatically generated using video info
- Metadata includes Title/Artists/Album/Lyrics/Track Number
- Cover art for songs are created by using video thumbnails

[NOTE] This program and ffmpeg may be blocked by antivirus.
If you run into any issues, you can try adding this program
and your ffmpeg folder to the exclusions of your antivirus.

## Requirements
- Python 3.11 or newer (when running from source)
- FFMPEG: Make sure you can run `ffmpeg` from the command line. If you are on Windows check your PATH.

## Install
For Windows, you can directly download the executable from the following link:

[![Windows](https://img.shields.io/badge/-Windows_x64-blue.svg?style=for-the-badge&logo=windows)](https://github.com/onnowhere/youtube_music_playlist_downloader/releases/latest/download/youtube_music_playlist_downloader.exe)

Alternatively, you can clone and install from source with Python 3.
```
git clone https://github.com/onnowhere/youtube_music_playlist_downloader
cd youtube_music_playlist_downloader
python -m pip install -r requirements.txt
```

## Usage
Double click the executable to run and follow the instructions that are presented.

If using the source files, double click the file to run with Python or run from command line.
```
python youtube_music_playlist_downloader.py
```

### Command line usage
The interactive menu is still the default, but the same actions can be run non-interactively.
```
# Download a new playlist (or update it if it is already in this folder)
python youtube_music_playlist_downloader.py --url "https://www.youtube.com/playlist?list=..."

# List playlists in the current folder
python youtube_music_playlist_downloader.py --list

# Update the only playlist in the current folder
python youtube_music_playlist_downloader.py --update

# Update one playlist by folder name, refreshing all metadata
python youtube_music_playlist_downloader.py --update --folder "My Playlist" --regenerate-metadata

# Update a single song by track number
python youtube_music_playlist_downloader.py --song 5 --folder "My Playlist"

# Work in another playlists folder
python youtube_music_playlist_downloader.py --directory "D:\Music" --update --folder "My Playlist"
```

Useful command line options:
- `--url URL`: download a playlist, or update it when it is already in the current folder
- `--update`: update a previously downloaded playlist
- `--song N`: update metadata for one song, selected by track number
- `--folder NAME`: select a playlist folder in the current directory
- `--directory DIR`: change to this directory before doing anything
- `--list`: list detected playlists and exit
- `--force`: force refresh all file names and metadata
- `--regenerate-metadata`: regenerate metadata without renaming files
- `--reverse`, `--use-title`, `--use-uploader`, `--use-playlist-name`: override the corresponding playlist settings
- `--threads N` / `--no-threading`: control concurrent downloads
- `--cookie-file`, `--cookies-from-browser`, `--extractor-args`: pass authentication options to yt-dlp

Run `python youtube_music_playlist_downloader.py --help` for the complete list.

## Notice
If you are running into issues with downloads such as `Sign in to confirm you’re not a bot.`, please see https://github.com/yt-dlp/yt-dlp/wiki/Extractors. The following options for cookies and PO Tokens are provided in the config file to pass along to yt-dlp: `cookie_file`, `cookies_from_browser`, `extractor_args`.

## Build
To build the standalone executable, you must have Python and `pyinstaller`. Once you do, follow the steps below.

```
python -m pip install -U pyinstaller -r requirements.txt
git submodule update --init --recursive
python scripts/build.py
```

On some systems, you may need to use `py` or `python3` instead of `python`.

## Tests
Run the unit tests from the repository root:
```
python -m unittest discover -s tests
```

## Config
A `.playlist_config.json` file is generated for all album folders and contains the following adjustable fields.

### Options adjustable using the program
- `url`: The URL of the playlist to download songs from (default: set during download)
- `reverse_playlist`: Whether to reverse the order of songs in the playlist when downloading (default: `false`)
- `use_title`: Whether to use the title or track name provided by YouTube where possible as the title for downloaded songs (default: `true`)
- `use_uploader`: Whether to use the uploader or the artist name provided by YouTube where possible as the artist for downloaded songs (default: `true`)
- `use_playlist_name`: Whether to use the playlist or the album name provided by YouTube where possible as the album for downloaded songs (default: `true`)

### Additional options adjustable through the config file
- `sync_folder_name`: Whether to automatically sync the name of the playlist folder to the YouTube playlist name (default: `true`)
- `use_threading`: Whether to use threading for faster song downloading and updating at the cost of more CPU and memory usage (default: `true`)
- `thread_count`: Number of threads to use for threading - if set to 0, this value will be dynamically determined (default: `0`)
- `retain_missing_order`: Whether to retain the current order of missing or deleted songs if a local copy exists or move them to the end of the album (default: `false`)
- `name_format`: The name format used to generate file names in yt-dlp output template format (default: `"%(title)s-%(id)s.%(ext)s"`)
- `track_num_in_name`: Whether to include the track number at the start of all file names (default: `true`)
- `audio_format`: The audio format used by yt-dlp when downloading songs (default: `"bestaudio/best"`)
- `audio_codec`: The audio codec used by yt-dlp when downloading songs (default: `"mp3"`)
- `audio_quality`: The audio quality used by yt-dlp when converting audio formats (default: `"5"`)
- `image_format`: The cover art image format - for better quality but larger file size, use `"png"` (default: `"jpeg"`)
- `lyrics_langs`: A list of language codes in order of priority to determine which lyrics to select if any are unavailable - leave empty for automatic selection (default: `[]`)
    - Language codes support regex and are matched to the full string, for example: `"en.*"` is interpreted as `"^en.*$"` before regex matching
    - Example: `["en.*", "ja"]` means select the first language with prefix `en`, else select the language that exactly matches `ja` if it exists
- `strict_lang_match`: Whether to avoid selecting lyrics automatically given no language matches (default: `false`)
- `start_time`: The start time to cut from if specified, in the form of `(-)HH:MM:SS.ms` with lenient parsing (for example `12:34:56`), where a negative value seeks from the end versus the start (default: `""`)
- `end_time`: The end time to cut to if specified, in the form of `(-)HH:MM:SS.ms` with lenient parsing (for example `-01:23.456`), where a negative value seeks from the end versus the start (default: `""`)
- `cookie_file`: The path to the cookie file for yt-dlp to refer to (default: `""`)
- `cookies_from_browser`: The name of the browser for yt-dlp to load cookies from (default: `""`)
- `extractor_args`: The extractor arguments, such as PO Token, for yt-dlp to run with, provided as a dict (default: `{}`)
    - Example incorrect format: `--extractor-args "youtubetab:skip=webpage" --extractor-args "youtube:player_skip=webpage,configs;visitor_data=VISITOR_DATA_VALUE_HERE"`
    - Example correct converted format: `{"youtubetab":{"skip":["webpage"]},"youtube":{"player_skip":["webpage","configs"],"visitor_data":["VISITOR_DATA_VALUE_HERE"]}}`
- `retries`: Number of retries for downloads and HTTP requests (default: `3`)
- `sleep_interval`: Minimum number of seconds to sleep before each download (default: `0`)
- `max_sleep_interval`: Maximum number of seconds to sleep before each download; values greater than `sleep_interval` add a random delay (default: `0`)
- `verbose`: Whether to enable more verbose debug information from yt-dlp (default: `false`)
- `include_metadata`: A mapping of metadata fields and whether to include them in song metadata
    - `title`: Include title metadata (default: `true`)
    - `cover`: Include cover art (default: `true`)
    - `track`: Include track number metadata (default: `true`)
    - `artist`: Include artist metadata (default: `true`)
    - `album`: Include album metadata (default: `true`)
    - `date`: Include date metadata (default: `true`)
    - `lyrics`: Include lyrics metadata (default: `true`)
- `metadata_overrides`: Overrides for metadata fields
    - `title`: Title name, not overridden if left blank (default: `""`)
    - `cover`: Cover art file path, not overridden if left blank (default: `""`)
    - `track`: Track number, not overridden if left as 0 (default: `0`)
    - `artist`: Artist name, not overridden if left blank (default: `""`)
    - `album`: Album name, not overridden if left blank (default: `""`)
    - `date`: Upload date, for example `2025-01-31`, not overridden if left blank (default: `""`)
    - `lyrics`: List of lyric lines and timestamps in milliseconds (ex.: `[["line 1", 1000], ["line 2", 2000]]`), not overridden if left empty (default: `[]`)
- `custom_metadata`: A mapping of metadata tags and the custom value to be applied for it (default: `{}`)
    - Cannot be a tag that is already handled by the script: `APIC`, `TIT2`, `TPE1`, `TRCK`, `TALB`, `TDRC`, `WOAR`, `SYLT`, `USLT`
    - To see available tags, refer to [mutagen docs](https://mutagen.readthedocs.io/en/latest/api/id3_frames.html#id3v2-3-4-frames)
    - Example: `{"TEXT": "Lyricist Name", "TCOM": ["Composer A Name", "Composer B Name"]}`
- `overrides`: A mapping of custom individual song config overrides - additional entries can be added for each song
    - `[video_id]`: A mapping of overridden config values for this specific song - a unique alphanumeric YouTube video id
        - `...`: All config values from above are valid here with exception to `url`, `reverse_playlist`, `sync_folder_name`, `use_threading`, `thread_count`, and `overrides`

### Metadata format support
Playlist tracking and automatic metadata updates currently support MP3 files only. If `audio_codec` is set to another format, songs can still be downloaded, but they will not be recognized as part of the playlist on later updates. Existing MP3 libraries are unaffected.

## License
Licensed under MIT (See [LICENSE](LICENSE))

## Disclaimer
Please respect the copyright of any videos you download. The creator of this program will not be held liable for any copyright violations caused by the usage of this program.
