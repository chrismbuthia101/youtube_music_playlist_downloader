#!/usr/bin/env python3
# YouTube Music Playlist Downloader
version = "1.5.0"

import argparse
import concurrent.futures
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from langcodes import Language
from mutagen import id3
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from yt_dlp import YoutubeDL, postprocessor, utils

CONFIG_FILE_NAME = ".playlist_config.json"
HTTP_TIMEOUT = 30
SUPPORTED_METADATA_EXTENSIONS = {".mp3"}

# ID3 info:
# APIC: thumbnail
# TIT2: title
# TPE1: artist
# TRCK: track number
# TALB: album
# TDRC: upload date
# WOAR: link
# SYLT: synced lyrics
# USLT: unsynced lyrics

class FilePathCollector(postprocessor.common.PostProcessor):
    def __init__(self):
        super().__init__(None)
        self.file_paths = []

    def run(self, information):
        self.file_paths.append(information['filepath'])
        return [], information

class SongFileInfo:
    def __init__(self, video_id, name, file_name, file_path, track_num):
        self.video_id = video_id
        self.name = name
        self.file_name = file_name
        self.file_path = file_path
        self.track_num = track_num

_http_session = None
_ffmpeg_available = None
_ffmpeg_error_shown = False

INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
PLAYLIST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,}$")

def get_http_session():
    global _http_session
    if _http_session is None:
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        _http_session = requests.Session()
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
        _http_session.headers.update({
            "User-Agent": f"YouTubeMusicPlaylistDownloader/{version} (+https://github.com/onnowhere/youtube_music_playlist_downloader)"
        })
    return _http_session

def http_get(url, **kwargs):
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    return get_http_session().get(url, **kwargs)

def write_config(file, config: dict):
    # Write atomically so an interrupted update cannot corrupt a playlist config
    directory = os.path.dirname(os.path.abspath(file))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".playlist_config.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
            f.write("\n")
        os.replace(temp_path, file)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

def read_config(file):
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)

def check_ffmpeg():
    global _ffmpeg_available, _ffmpeg_error_shown
    if _ffmpeg_available is None:
        _ffmpeg_available = False
        if shutil.which("ffmpeg"):
            try:
                subprocess.run(
                    ["ffmpeg", "-version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=True,
                )
                _ffmpeg_available = True
            except Exception:
                pass

    if not _ffmpeg_available and not _ffmpeg_error_shown:
        _ffmpeg_error_shown = True
        print("\n".join([
            "[ERROR] ffmpeg not found. Please ensure ffmpeg is installed",
            "and you have included it in your PATH environment variable.",
            "Download ffmpeg here: https://www.ffmpeg.org/download.html.",
            "-----------------------------------------------------------",
        ]))
    return _ffmpeg_available

def require_ffmpeg():
    if not check_ffmpeg():
        raise RuntimeError("ffmpeg is required to download and convert songs")

def get_cookie_options(config: dict):
    cookie_file = (config.get("cookie_file") or "").strip()
    cookie_file = os.path.expanduser(cookie_file) if cookie_file else None
    if cookie_file and not os.path.isfile(cookie_file):
        print(f"[WARNING] Cookie file '{cookie_file}' does not exist. Continuing without it.")
        cookie_file = None

    browser = (config.get("cookies_from_browser") or "").strip()
    cookies_from_browser = None
    if browser:
        cookies_from_browser = tuple(part.strip() for part in browser.split(":") if part.strip())

    return cookie_file, cookies_from_browser

def get_common_ytdl_opts(config: dict, quiet: bool = True):
    cookie_file, cookies_from_browser = get_cookie_options(config)
    return {
        "quiet": quiet,
        "no_warnings": not config.get("verbose", False),
        "geo_bypass": True,
        "cookiefile": cookie_file,
        "cookiesfrombrowser": cookies_from_browser,
        "extractor_args": config.get("extractor_args") or {},
        "retries": config.get("retries", 3),
        "fragment_retries": config.get("retries", 3),
        "sleep_interval": config.get("sleep_interval", 0),
        "max_sleep_interval": config.get("max_sleep_interval", 0),
    }

def get_playlist_info(config: dict):
    ytdl_opts = {
        **get_common_ytdl_opts(config),
        "dump_single_json": True,
        "extract_flat": True,
        "playlistreverse": config["reverse_playlist"],
    }
    with YoutubeDL(ytdl_opts) as ytdl:
        info_dict = ytdl.extract_info(config["url"], download=False)

    if not info_dict:
        raise Exception(f"Unable to retrieve playlist information for '{config['url']}'")
    if info_dict.get("entries") is None:
        raise Exception("The provided URL does not point to a playlist")
    return info_dict

def normalize_playlist_url(url):
    if not isinstance(url, str):
        raise ValueError("Playlist URL must be text")

    url = url.strip().strip("\"'")
    if not url:
        raise ValueError("Playlist URL cannot be empty")

    if PLAYLIST_ID_PATTERN.fullmatch(url):
        return f"https://www.youtube.com/playlist?list={url}"

    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Invalid playlist URL: '{url}'")

    # Raises with a helpful message when the "list" parameter is missing
    get_playlist_id(url)
    return url

def convert_image_type(image, image_type):
    with BytesIO() as f:
        image.convert("RGB").save(f, format=image_type)
        return f.getvalue()

def update_track_num(file_path, track_num):
    tags = id3.ID3(file_path)
    tags.add(id3.TRCK(encoding=3, text=str(track_num)))
    tags.save(v2_version=3)

def safe_rename(src, dst):
    src_path = os.path.abspath(src)
    dst_path = os.path.abspath(dst)
    if src_path == dst_path:
        return dst

    if os.path.exists(dst):
        try:
            if os.path.samefile(src, dst):
                os.rename(src, dst)
                return dst
        except OSError:
            pass
        raise FileExistsError(f"Cannot rename '{src}' to '{dst}': destination already exists")

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    os.rename(src, dst)
    return dst

def update_file_order(playlist_name, song_file_info, track_num, config: dict, missing_video: bool):
    # Fix name if mismatching
    if config["track_num_in_name"]:
        song_file_name = re.sub(r"^[0-9]+\. ", "", song_file_info.file_name)
        file_name = f"{track_num}. {song_file_name}"
    else:
        file_name = song_file_info.file_name
    file_path = os.path.join(playlist_name, file_name)

    # Update song index if not matched
    if song_file_info.track_num != track_num and config["include_metadata"]["track"]:
        if missing_video:
            print(f"Reordering '{song_file_info.name}' from position {song_file_info.track_num} to {track_num} due to missing video link...")
        else:
            print(f"Reordering '{song_file_info.name}' from position {song_file_info.track_num} to {track_num}...")
        update_track_num(song_file_info.file_path, track_num)

    if song_file_info.file_path != file_path:
        if song_file_info.track_num == track_num:
            # Track num in name was incorrectly modified manually by user
            print(f"Renaming incorrect file name from '{song_file_info.file_name}' to '{file_name}'")
        safe_rename(song_file_info.file_path, file_path)

    song_file_info.file_name = file_name
    song_file_info.file_path = file_path
    song_file_info.track_num = track_num
    return file_path

def get_metadata_map():
    metadata_map = {
        "title": ["TIT2"],
        "cover": ["APIC:Front cover"],
        "track": ["TRCK"],
        "artist": ["TPE1"],
        "album": ["TALB"],
        "date": ["TDRC"],
        "url": ["WOAR"],
        "lyrics": ["SYLT", "USLT"]
    }

    return metadata_map

def flatten(l):
    return [item for sublist in l for item in sublist]

def get_metadata_dict(tags, config: dict):
    tag_list = flatten(get_metadata_map().values()) + list(config["custom_metadata"].keys())
    return {tag: tags.getall(tag) for tag in tag_list}

def valid_metadata(config: dict, metadata_dict: dict):
    include_metadata = config["include_metadata"].copy()

    # WOAR URL is required to identify video
    include_metadata["url"] = True

    selected_tags = flatten([value for key, value in get_metadata_map().items() if include_metadata[key]])

    # Add custom metadata tags if a corresponding value is specified
    for tag, value in config["custom_metadata"].items():
        if value:
            selected_tags.append(tag)

    return all([value for tag, value in metadata_dict.items() if tag in selected_tags])

def get_song_info_ytdl(track_num, config: dict):
    # Get ytdl for song info
    name_format = config["name_format"]
    if config["track_num_in_name"]:
        name_format = f"{track_num}. {name_format}"

    ytdl_opts = {
        **get_common_ytdl_opts(config),
        "outtmpl": name_format,
        "format": config["audio_format"],
        "noplaylist": True,
        "writesubtitles": True,
        "allsubtitles": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": config["audio_codec"],
            "preferredquality": config["audio_quality"],
        }]
    }

    return YoutubeDL(ytdl_opts)

def get_song_info(track_num, link, config: dict):
    # Get song metadata from youtube
    with get_song_info_ytdl(track_num, config) as ytdl:
        info_dict = ytdl.extract_info(link, download=False)

    if not info_dict:
        raise Exception(f"Unable to retrieve song information for '{link}'")
    return info_dict

def get_prepared_file_name(track_num, info_dict, config: dict):
    info_dict_with_audio_ext = dict(info_dict)
    info_dict_with_audio_ext["ext"] = config["audio_codec"]
    with get_song_info_ytdl(track_num, config) as ytdl:
        return ytdl.prepare_filename(info_dict_with_audio_ext)

def get_subtitles_url(subtitles, lang):
    for subtitle in subtitles.get(lang, []):
        if subtitle.get("ext") == "json3":
            return subtitle["url"]
    raise KeyError(f"No json3 subtitles available for language '{lang}'")

def normalize_lyrics_language(lang):
    try:
        return Language.get(lang).to_alpha3()
    except Exception:
        print(f"Saving unrecognized lyrics language '{lang}' as 'en'")
        return Language.get("en").to_alpha3()

def parse_lyrics_events(content):
    synced_lyrics = []
    unsynced_lyrics = []
    last_timestamp = -1
    last_lines = []

    for event in content.get("events", []):
        timestamp = event.get("tStartMs", 0)
        line = "".join(segment.get("utf8", "") for segment in event.get("segs", []))
        # Remove invalid zero-width characters
        line = line.replace("\u200b", "").replace("\u200c", "")

        if (timestamp - last_timestamp) < 1000 and line.strip() in last_lines:
            # Skip if line is repeated too quickly
            last_timestamp = timestamp
            continue

        if timestamp == last_timestamp and synced_lyrics:
            # Append line into previous line if same timestamp has multiple lines
            lyrics_line = list(synced_lyrics[-1])
            lyrics_line[0] += "\n" + line
            synced_lyrics[-1] = tuple(lyrics_line)
            unsynced_lyrics[-1] += "\n" + line
            last_lines.append(line.strip())
        else:
            synced_lyrics.append((line, timestamp))
            unsynced_lyrics.append(line)
            last_lines = [line.strip()]
        last_timestamp = timestamp

    return synced_lyrics, unsynced_lyrics

def fetch_lyrics(info_dict, config: dict):
    synced_lyrics = []
    unsynced_lyrics = []
    lang = "en"

    subtitles = info_dict.get("subtitles") or {}
    requested_subtitles = info_dict.get("requested_subtitles")
    if requested_subtitles is not None:
        # Filter out subtitles related to live chat
        requested_subtitles = {
            key: value for key, value in requested_subtitles.items()
            if not key.startswith("live")
        }

    if subtitles and requested_subtitles:
        available_languages = list(requested_subtitles.keys())
        selected_lang = None

        if not config["lyrics_langs"]:
            selected_lang = available_languages[0]
            print(f"Selecting first available language for lyrics: {selected_lang}")
        else:
            for lyrics_lang in config["lyrics_langs"]:
                for requested_lang in available_languages:
                    try:
                        if re.match(r"^" + lyrics_lang + r"$", requested_lang):
                            selected_lang = requested_lang
                            print(f"Selected language for lyrics: {selected_lang}")
                            break
                    except re.error as e:
                        print(f"Invalid lyrics language pattern '{lyrics_lang}': {e}")
                if selected_lang:
                    break

            if selected_lang is None:
                print(f"Lyrics unavailable for selected languages. Available languages: {available_languages}")
                if not config["strict_lang_match"]:
                    selected_lang = available_languages[0]
                    print(f"Selecting first available language for lyrics: {selected_lang}")

        if selected_lang:
            lang = selected_lang
            try:
                subtitles_url = get_subtitles_url(subtitles, selected_lang)
                response = http_get(subtitles_url)
                response.raise_for_status()
                synced_lyrics, unsynced_lyrics = parse_lyrics_events(response.json())
            except Exception as e:
                print(f"Unable to get lyrics. Error: {e}")

    lang = normalize_lyrics_language(lang)
    if len(synced_lyrics) == 0:
        synced_lyrics = [("Lyrics unavailable", 0)]
    if len(unsynced_lyrics) == 0:
        unsynced_lyrics = ["Lyrics unavailable"]

    return synced_lyrics, unsynced_lyrics, lang

def parse_override_lyrics(override_lyrics):
    synced_lyrics = []
    unsynced_lyrics = []
    for entry in override_lyrics:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ValueError(f"Invalid lyrics entry: {entry!r}")
        line = str(entry[0])
        timestamp = int(entry[1])
        synced_lyrics.append((line, timestamp))
        unsynced_lyrics.append(line)
    return synced_lyrics, unsynced_lyrics

def crop_to_square(image):
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))

def get_cover_data(thumbnail_url, override_cover_file, image_format):
    if override_cover_file:
        with Image.open(override_cover_file) as source:
            image = source.convert("RGB")
    else:
        if not thumbnail_url:
            raise ValueError("No thumbnail or override cover image available")
        response = http_get(thumbnail_url, stream=True)
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as source:
            image = source.convert("RGB")

    return convert_image_type(crop_to_square(image), image_format)

def format_upload_date(upload_date):
    if not upload_date:
        return time.strftime("%Y-%m-%d")
    upload_date = str(upload_date).strip()
    try:
        return time.strftime("%Y-%m-%d", time.strptime(upload_date, "%Y%m%d"))
    except ValueError:
        return upload_date

def generate_metadata(file_path, link, track_num, playlist_name, config: dict, regenerate_metadata: bool, force_update: bool):
    try:
        tags = id3.ID3(file_path)
    except Exception:
        # Unsupported audio codec for metadata
        force_update_file_name = ""
        if force_update:
            try:
                info_dict = get_song_info(track_num, link, config)
                force_update_file_name = get_prepared_file_name(track_num, info_dict, config)
            except Exception as e:
                raise Exception(f"Failed to get information for updated file name - {e}")
        return force_update_file_name

    # Generate only if metadata is missing or if explicitly flagged
    metadata_dict = get_metadata_dict(tags, config)

    force_update_file_name = ""
    if force_update:
        for tag in metadata_dict.keys():
            if tag != "WOAR":
                # WOAR URL is required to identify video
                tags.delall(tag)
                metadata_dict[tag] = []

    if regenerate_metadata or force_update or not valid_metadata(config, metadata_dict):
        try:
            info_dict = get_song_info(track_num, link, config)
            if force_update:
                force_update_file_name = get_prepared_file_name(track_num, info_dict, config)

            thumbnail = info_dict.get("thumbnail")
            upload_date = info_dict.get("upload_date")
            title = info_dict.get("title") or "Unknown Title"
            track = info_dict.get("track")
            uploader = info_dict.get("uploader") or "Unknown Artist"
            artist = info_dict.get("artist")
            album = info_dict.get("album")

            metadata_overrides = config.get("metadata_overrides") or {}
            override_title = metadata_overrides.get("title")
            override_cover_file = metadata_overrides.get("cover")
            if override_cover_file:
                override_cover_file = os.path.expanduser(str(override_cover_file))
            override_track_num = metadata_overrides.get("track")
            override_artist = metadata_overrides.get("artist")
            override_album = metadata_overrides.get("album")
            override_upload_date = metadata_overrides.get("date")
            override_lyrics = metadata_overrides.get("lyrics")
        except Exception as e:
            raise Exception(f"Failed to get information - {e}")

        try:
            # Generate tags
            print(f"Updating metadata for '{title}'...")
            include_metadata = config["include_metadata"]

            # These tags will not be regenerated in case of config changes
            if (not metadata_dict["APIC:Front cover"] or override_cover_file) and include_metadata["cover"]:
                try:
                    img_data = get_cover_data(thumbnail, override_cover_file, config["image_format"])
                    tags.add(id3.APIC(3, f"image/{config['image_format']}", 3, "Front cover", img_data))
                except Exception as e:
                    print(f"Unable to update cover art: {e}")

            if not metadata_dict["TRCK"] and include_metadata["track"]:
                tags.add(id3.TRCK(encoding=3, text=str(override_track_num or track_num)))

            if not metadata_dict["TDRC"] and include_metadata["date"]:
                tags.add(id3.TDRC(encoding=3, text=str(override_upload_date or format_upload_date(upload_date))))

            if not metadata_dict["WOAR"]:
                tags.add(id3.WOAR(link))

            if include_metadata["lyrics"] and (not metadata_dict["SYLT"] or not metadata_dict["USLT"]):
                if override_lyrics:
                    try:
                        synced_lyrics, unsynced_lyrics = parse_override_lyrics(override_lyrics)
                        lang = Language.get("en").to_alpha3()
                        tags.add(id3.SYLT(encoding=3, lang=lang, format=2, type=1, text=synced_lyrics))
                        tags.add(id3.USLT(encoding=3, lang=lang, text="\n".join(unsynced_lyrics)))
                    except Exception as e:
                        print(f"Unable to parse overridden lyrics: {e}")
                else:
                    synced_lyrics, unsynced_lyrics, lang = fetch_lyrics(info_dict, config)
                    tags.add(id3.SYLT(encoding=3, lang=lang, format=2, type=1, text=synced_lyrics))
                    tags.add(id3.USLT(encoding=3, lang=lang, text="\n".join(unsynced_lyrics)))

            # These tags can be regenerated in case of config changes
            if include_metadata["title"]:
                if override_title:
                    tags.add(id3.TIT2(encoding=3, text=str(override_title)))
                elif config["use_title"] or track is None:
                    tags.add(id3.TIT2(encoding=3, text=str(title)))
                else:
                    tags.add(id3.TIT2(encoding=3, text=str(track)))

            if include_metadata["artist"]:
                if override_artist:
                    tags.add(id3.TPE1(encoding=3, text=str(override_artist)))
                elif config["use_uploader"] or artist is None:
                    tags.add(id3.TPE1(encoding=3, text=str(uploader)))
                else:
                    tags.add(id3.TPE1(encoding=3, text=str(artist)))

            if include_metadata["album"]:
                if override_album:
                    tags.add(id3.TALB(encoding=3, text=str(override_album)))
                elif config["use_playlist_name"]:
                    tags.add(id3.TALB(encoding=3, text=str(playlist_name)))
                elif album is not None:
                    tags.add(id3.TALB(encoding=3, text=str(album)))
                else:
                    tags.add(id3.TALB(encoding=3, text="Unknown Album"))

            # Handle custom metadata
            for tag, value in config["custom_metadata"].items():
                if value:
                    try:
                        tags.add(id3.Frames[tag](encoding=3, text=value))
                    except Exception as e:
                        print(f"Unable to add custom metadata tag '{tag}' with value '{value}'. Error: {e}")

            tags.save(v2_version=3)
        except Exception as e:
            raise Exception(f"Unable to update song metadata: {e}")

    return force_update_file_name

def parse_time_str(time_str, default: float):
    if time_str is None:
        return default

    time_str = str(time_str).strip()
    if not time_str:
        return default

    from_end = time_str.startswith("-")
    if from_end:
        time_str = time_str[1:].strip()

    parts = time_str.split(":")
    if 1 <= len(parts) <= 3 and all(part.strip() for part in parts):
        try:
            total_seconds = float(parts[-1])
            if total_seconds < 0:
                raise ValueError("Seconds cannot be negative")
            if len(parts) >= 2:
                total_seconds += int(parts[-2]) * 60.0
            if len(parts) == 3:
                total_seconds += int(parts[-3]) * 3600.0
            return -total_seconds if from_end else total_seconds
        except (TypeError, ValueError):
            pass

    print(f"Using default time value '{default}' due to invalid time format in configs: '{time_str}'")
    return default

def download_song(link, playlist_name, track_num, config: dict):
    require_ffmpeg()

    directory = os.path.join(os.getcwd(), playlist_name)
    name_format = config["name_format"]
    if config["track_num_in_name"]:
        name_format = f"{track_num}. {name_format}"

    ytdl_opts = {
        **get_common_ytdl_opts(config, quiet=not config.get("verbose", False)),
        "outtmpl": os.path.join(directory, name_format),
        "ignoreerrors": True,
        "format": config["audio_format"],
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": config["audio_codec"],
            "preferredquality": config["audio_quality"],
        }],
    }

    start_time_str = config.get("start_time")
    end_time_str = config.get("end_time")
    if start_time_str or end_time_str:
        start_time = parse_time_str(start_time_str, default=0.0)
        end_time = parse_time_str(end_time_str, default=float("inf"))

        ytdl_opts["download_ranges"] = utils.download_range_func(None, [(start_time, end_time)])
        ytdl_opts["force_keyframes_at_cuts"] = True

    with YoutubeDL(ytdl_opts) as ytdl:
        file_path_collector = FilePathCollector()
        ytdl.add_post_processor(file_path_collector)
        result = ytdl.download([link])
        if len(file_path_collector.file_paths) == 0:
            raise Exception("No file download path found, video may be unavailable")
        file_path = file_path_collector.file_paths[0]

    if not os.path.isfile(file_path):
        raise Exception(f"Downloaded file does not exist: '{file_path}'")

    return result, file_path

def download_song_and_update(video_info, playlist, link, playlist_name, track_num, config: dict):
    try:
        result, file_path = download_song(link, playlist_name, track_num, config)

        # Check download failed and video is unavailable
        if result != 0 and video_info.get("channel_id") is None:
            # Video title indicates availability of video such as '[Private Video]'
            raise Exception(f"Video is unavailable - {video_info.get('title')}")

        generate_metadata(file_path, link, track_num, playlist.get("title") or playlist_name, config, False, False)
    except Exception as e:
        error_message = f"Unable to download video number {track_num} '{link}': {e}"
        return error_message, track_num
    return None, track_num

def update_song(video_info, song_file_info, file_path, link, track_num, playlist_name, config: dict, regenerate_metadata: bool, force_update: bool):
    # Generate metadata just in case it is missing
    video_unavailable = False
    error_message = []
    try:
        force_update_file_name = generate_metadata(file_path, link, track_num, playlist_name, config, regenerate_metadata, force_update)
        if force_update and force_update_file_name:
            force_update_file_path = os.path.join(playlist_name, force_update_file_name)
            if file_path != force_update_file_path:
                # Track name needs updating to proper format
                print(f"Renaming incorrect file name from '{Path(file_path).stem}' to '{Path(force_update_file_path).stem}'")
                try:
                    safe_rename(file_path, force_update_file_path)
                except Exception as e:
                    error_message.append(f"Unable to rename file for #{track_num} '{link}': {e}")
    except Exception as e:
        error_message.append(f"Unable to update metadata for #{track_num} '{link}': {e}")
        if "This video is not available" in str(e):
            video_unavailable = True

    # Check if video is unavailable
    if video_info.get("channel_id") is None or video_unavailable:
        if len(error_message) == 0:
            # Metadata was obtained successfully but some information is missing
            error_message.append(f"Unable to fully update metadata for #{track_num} '{link}'")
        error_text = f"The previous song '{song_file_info.name}' is unavailable but a local copy exists"
        if not video_unavailable and video_info.get("title"):
            # Video title indicates availability of video such as '[Private Video]'
            error_text += f" - {video_info['title']}"
        error_message.append(error_text)

    if len(error_message) > 0:
        return "\n".join(error_message)
    return None

def format_file_name(file_name):
    file_name = INVALID_FILENAME_CHARS.sub("_", str(file_name)).strip()
    file_name = file_name.rstrip(" .")
    if not file_name or file_name in {".", ".."}:
        file_name = "Untitled"

    # Windows does not allow reserved device names, even with an extension
    if file_name.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        file_name = "_" + file_name

    # Keep file names comfortably below common file system limits (255 bytes)
    while len(file_name.encode("utf-8")) > 200:
        file_name = file_name[:-1].rstrip(" .")

    return file_name or "Untitled"

def get_url_parameter(url, param):
    values = parse_qs(urlparse(str(url)).query).get(param)
    if not values:
        raise ValueError(f"URL is missing the '{param}' parameter: '{url}'")
    return values[0]

def get_playlist_id(url):
    if not isinstance(url, str):
        raise ValueError("Playlist URL must be text")

    value = url.strip()
    if PLAYLIST_ID_PATTERN.fullmatch(value):
        return value

    parsed = urlparse(value if "://" in value else "https://" + value)
    values = parse_qs(parsed.query).get("list")
    if not values or not values[0]:
        raise ValueError(f"Playlist URL is missing the 'list' parameter: '{url}'")
    return values[0]

def get_video_id_from_metadata(tags):
    links = tags.getall("WOAR")
    if not links or len(links) > 1:
        raise Exception("WOAR tag is in an invalid format")

    link = str(links[0])
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    video_id = None
    if query.get("v"):
        video_id = query["v"][0]
    elif parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]

    if not video_id:
        raise Exception(f"WOAR tag contains an unrecognized video URL: '{link}'")
    return video_id

def parse_track_number(value, default=0):
    match = re.match(r"\s*(-?\d+)", str(value))
    return int(match.group(1)) if match else default

def get_song_file_info(playlist_name, song_file_name):
    song_file_path = os.path.join(playlist_name, song_file_name)

    if Path(song_file_name).suffix.lower() not in SUPPORTED_METADATA_EXTENSIONS:
        return None
    if not os.path.isfile(song_file_path):
        return None

    try:
        tags = id3.ID3(song_file_path)
    except Exception:
        # File is not considered a song file if it contains no metadata
        return None

    try:
        song_video_id = get_video_id_from_metadata(tags)
        song_name = str(tags.get("TIT2", song_file_name) or song_file_name)
        song_track_num = parse_track_number(tags.get("TRCK", 0))
    except Exception as e:
        print(f"Song file '{song_file_name}' is in an invalid format and will be ignored ({e})")
        return None

    return SongFileInfo(song_video_id, song_name, song_file_name, song_file_path, song_track_num)

def get_song_file_infos(playlist_name):
    song_file_infos = {}
    duplicate_files = {}
    for file_name in sorted(os.listdir(playlist_name)):
        if file_name.startswith("."):
            continue
        song_file_info = get_song_file_info(playlist_name, file_name)
        if song_file_info is None:
            continue

        if song_file_info.video_id in song_file_infos:
            # Check for duplicate song files
            if song_file_info.video_id not in duplicate_files:
                duplicate_files[song_file_info.video_id] = [song_file_infos[song_file_info.video_id].file_name]

            duplicate_files[song_file_info.video_id].append(song_file_info.file_name)
            continue

        song_file_infos[song_file_info.video_id] = song_file_info

    if duplicate_files:
        exception_strings = []
        for song_video_id, file_names in duplicate_files.items():
            exception_strings.append("\n".join([
                f"The following files link to the same video id '{song_video_id}'",
                "\n".join(["- " + file_name for file_name in file_names])
            ]))

        raise Exception("\n".join([
            "",
            "===========================================================",
            "[ERROR] Duplicate song files found in this playlist folder!",
            "===========================================================",
            "\n\n".join(exception_strings),
            "===========================================================",
            "Please remove duplicate song files to resolve the conflict.",
            "===========================================================",
            ""
        ]))

    return song_file_infos

def setup_include_metadata_config():
    return {key: True for key in get_metadata_map().keys() if key != "url"}

def setup_custom_metadata(config: dict):
    if "custom_metadata" not in config or not isinstance(config["custom_metadata"], dict):
        return {}

    # Return any custom metadata from source config but remove tags that conflict with existing metadata tags
    existing_tags = set(flatten(get_metadata_map().values()))
    custom_metadata = {}
    for tag, value in config["custom_metadata"].items():
        if tag in existing_tags:
            continue
        if tag not in id3.Frames:
            print(f"[WARNING] Unknown custom metadata tag '{tag}' will be ignored")
            continue
        custom_metadata[tag] = value
    return custom_metadata

def setup_metadata_overrides_config(config: dict = None):
    metadata_overrides = {key: "" for key in get_metadata_map().keys() if key != "url"}
    metadata_overrides["track"] = 0
    metadata_overrides["lyrics"] = []
    return metadata_overrides

def normalize_image_format(image_format):
    image_format = str(image_format).strip().lower().lstrip(".")
    if image_format == "jpg":
        image_format = "jpeg"
    if image_format not in {"jpeg", "png", "webp"}:
        print(f"[WARNING] Unsupported image format '{image_format}', using 'jpeg'")
        image_format = "jpeg"
    return image_format

def validate_config(src_config: dict, dst_config: dict):
    copy_config(src_config, dst_config, only_validate=True)

def copy_config(src_config: dict, dst_config: dict, only_validate: bool=False, minimal_copy: bool=False):
    # Copy modified src_config values to the dst_config
    # If minimal_copy is True, remove any values in dst_config that are not present in src_config.
    for key in list(dst_config.keys()):
        dst_sub_dict = dst_config[key]

        if key not in src_config:
            if minimal_copy and not only_validate:
                del dst_config[key]
            continue

        # Handle nested dicts
        if isinstance(dst_sub_dict, dict):
            src_sub_dict = {}
            if key in src_config:
                if isinstance(src_config[key], dict):
                    src_sub_dict = src_config[key]
                elif only_validate and src_config[key] is not None:
                    raise Exception(f"Invalid config value type for key '{key}', expected dict but got {type(src_config[key]).__name__}: {src_config[key]}")

            if minimal_copy and not only_validate:
                for sub_key in list(dst_sub_dict.keys()):
                    if sub_key not in src_sub_dict:
                        del dst_sub_dict[sub_key]

            for sub_key in src_sub_dict:
                if key == "custom_metadata":
                    # Do not validate custom metadata tags and directly copy as is
                    if only_validate:
                        continue
                    dst_sub_dict[sub_key] = src_sub_dict[sub_key]
                elif sub_key in dst_sub_dict:
                    src_type = type(src_sub_dict[sub_key])
                    dst_type = type(dst_sub_dict[sub_key])
                    if dst_type == src_type:
                        if only_validate:
                            continue
                        dst_sub_dict[sub_key] = src_sub_dict[sub_key]
                    elif only_validate and src_sub_dict[sub_key] is not None:
                        raise Exception(f"Invalid config value type for key '{sub_key}', expected {dst_type.__name__} but got {src_type.__name__}: {src_sub_dict[sub_key]}")

            if only_validate:
                continue

            dst_config[key] = dst_sub_dict
        # Handle individual keys
        elif key in src_config:
            src_type = type(src_config[key])
            dst_type = type(dst_config[key])
            if dst_type == src_type:
                if only_validate:
                    continue

                dst_config[key] = src_config[key]
            elif only_validate and src_config[key] is not None:
                raise Exception(f"Invalid config value type for key '{key}', expected {dst_type.__name__} but got {src_type.__name__}: {src_config[key]}")

def get_override_config(video_id, base_config: dict):
    config = copy.deepcopy(base_config)
    overrides = base_config.get("overrides") or {}
    if video_id in overrides:
        copy_config(overrides[video_id], config)
    config.pop("overrides", None)

    return config

def setup_config(config: dict):
    new_config = {
        # Console config options
        "url": "",
        "reverse_playlist": False,

        "use_title": True,
        "use_uploader": True,
        "use_playlist_name": True,

        # File config options
        "sync_folder_name": True,
        "use_threading": True,
        "thread_count": 0,

        "retain_missing_order": False,
        "name_format": "%(title)s-%(id)s.%(ext)s",
        "track_num_in_name": True,
        "audio_format": "bestaudio/best",
        "audio_codec": "mp3",
        "audio_quality": "5",
        "image_format": "jpeg",
        "lyrics_langs": [],
        "strict_lang_match": False,
        "start_time": "",
        "end_time": "",

        "cookie_file": "",
        "cookies_from_browser": "",
        "extractor_args": {},

        # Network options
        "retries": 3,
        "sleep_interval": 0,
        "max_sleep_interval": 0,

        "verbose": False,

        "include_metadata": setup_include_metadata_config(),
        "metadata_overrides": setup_metadata_overrides_config(config),
        "custom_metadata": setup_custom_metadata(config)
    }

    # Warn about unknown options instead of silently dropping them
    known_keys = set(new_config) | {"overrides"}
    for key in config:
        if key not in known_keys:
            print(f"[WARNING] Unknown config option '{key}' will be ignored")

    # Copy config values to the new config
    validate_config(config, new_config)
    copy_config(config, new_config)
    new_config["image_format"] = normalize_image_format(new_config["image_format"])
    new_config["audio_codec"] = new_config["audio_codec"].strip().lower()
    if new_config["audio_codec"] != "mp3":
        print("[WARNING] Only MP3 files are supported for playlist tracking and metadata. "
              f"Audio codec '{new_config['audio_codec']}' may prevent files from being recognized on future updates.")

    # Create example song config override
    config_copy = copy.deepcopy(new_config)
    excluded_override_keys = ["url", "reverse_playlist", "sync_folder_name", "use_threading", "thread_count", "overrides"]
    for excluded_override_key in excluded_override_keys:
        if excluded_override_key in config_copy:
            config_copy.pop(excluded_override_key)
    new_config["overrides"] = {
        "EXAMPLE_VIDEO_ID_HERE": config_copy
    }

    # Setup individual song config overrides
    if "overrides" in config:
        if not isinstance(config["overrides"], dict):
            raise Exception("Config option 'overrides' must be a mapping of video ids to config overrides")
        for video_id, override_config in config["overrides"].items():
            if video_id != "EXAMPLE_VIDEO_ID_HERE":
                if not isinstance(override_config, dict):
                    raise Exception(f"Override config for video id '{video_id}' must be a mapping")
                for excluded_override_key in excluded_override_keys:
                    if excluded_override_key in override_config:
                        override_config.pop(excluded_override_key)

                new_override_config = copy.deepcopy(config_copy)
                try:
                    validate_config(override_config, new_override_config)
                except Exception as e:
                    raise Exception(f"Error in override config for video id '{video_id}': {e}")
                copy_config(override_config, new_override_config, minimal_copy=True)
                new_config["overrides"][video_id] = new_override_config

    return new_config

def create_playlist_folder(playlist_name, config_file_name, playlist_id=None):
    # Never merge a new playlist into an unrelated existing folder
    index = 1
    while True:
        candidate = playlist_name if index == 1 else f"{playlist_name} ({index})"
        if not os.path.exists(candidate):
            Path(candidate).mkdir(parents=True)
            return candidate

        if os.path.isdir(candidate):
            config_file = os.path.join(candidate, config_file_name)
            if os.path.isfile(config_file):
                try:
                    if playlist_id and get_playlist_id(read_config(config_file).get("url", "")) == playlist_id:
                        return candidate
                except Exception:
                    pass
            else:
                try:
                    if not os.listdir(candidate):
                        return candidate
                except OSError:
                    pass
        index += 1

def resolve_playlist_folder(playlist, base_config, config_file_name, update, current_playlist_name, single_playlist):
    if single_playlist:
        return "."

    playlist_title = playlist.get("title") or "Playlist"
    desired_name = format_file_name(playlist_title)
    try:
        playlist_id = get_playlist_id(base_config.get("url", ""))
    except ValueError:
        playlist_id = None

    if not update or not current_playlist_name:
        return create_playlist_folder(desired_name, config_file_name, playlist_id)

    if not os.path.isdir(current_playlist_name):
        raise Exception(f"Playlist folder '{current_playlist_name}' does not exist")

    if not base_config["sync_folder_name"]:
        return current_playlist_name

    if os.path.abspath(current_playlist_name) == os.path.abspath(desired_name):
        return current_playlist_name

    # Find a non-conflicting folder name before renaming
    index = 1
    while True:
        candidate = desired_name if index == 1 else f"{desired_name} ({index})"
        if os.path.abspath(candidate) == os.path.abspath(current_playlist_name):
            return current_playlist_name
        if not os.path.exists(candidate):
            print(f"Renaming playlist from '{current_playlist_name}' to '{candidate}'...")
            safe_rename(current_playlist_name, candidate)
            return candidate
        index += 1

def generate_default_config(config: dict, config_file_name: str):
    config = setup_config(config)

    # Get list of links in the playlist
    playlist = get_playlist_info(config)
    try:
        playlist_id = get_playlist_id(config["url"])
    except ValueError:
        playlist_id = None
    playlist_name = create_playlist_folder(
        format_file_name(playlist.get("title") or "Playlist"),
        config_file_name,
        playlist_id,
    )

    write_config(os.path.join(playlist_name, config_file_name), config)
    return playlist_name

def update_single_song_in_playlist(playlist_entries, playlist, playlist_name, song_file_infos, base_config, track_num_to_update, regenerate_metadata):
    for video_info in playlist_entries:
        if video_info is None:
            continue

        video_id = video_info.get("id")
        song_file_info = song_file_infos.get(video_id)
        if song_file_info is None or song_file_info.track_num != track_num_to_update:
            continue

        link = f"https://www.youtube.com/watch?v={video_id}"
        config = get_override_config(video_id, base_config)
        file_path = os.path.join(playlist_name, song_file_info.file_name)
        try:
            # Update all metadata but do not update the track num to avoid resorting playlist
            force_update_file_name = generate_metadata(
                file_path, link, song_file_info.track_num,
                playlist.get("title") or playlist_name, config, regenerate_metadata, True
            )
            force_update_file_path = os.path.join(playlist_name, force_update_file_name)
            if force_update_file_name and file_path != force_update_file_path:
                # Track name needs updating to proper format
                print(f"Renaming incorrect file name from '{Path(file_path).stem}' to '{Path(force_update_file_path).stem}'")
                safe_rename(file_path, force_update_file_path)
            print(f"Updated song #{track_num_to_update}: {song_file_info.name}")
        except Exception as e:
            print(f"Unable to update metadata: {e}")
        return

    print(f"Unable to update metadata for song #{track_num_to_update}: This song could not be found or is unavailable, please update the playlist first")

def generate_playlist(base_config: dict, config_file_name: str, update: bool, force_update: bool, regenerate_metadata: bool, single_playlist: bool, current_playlist_name=None, track_num_to_update=None):
    # Get list of links in the playlist
    playlist = get_playlist_info(base_config)
    if playlist.get("entries") is None:
        raise Exception("No videos found in playlist")
    playlist_entries = list(playlist["entries"])
    if len([entry for entry in playlist_entries if entry is not None]) == 0:
        raise Exception("No videos found in playlist")
    playlist_title = playlist.get("title") or "Playlist"

    previous_playlist_name = current_playlist_name
    playlist_name = resolve_playlist_folder(
        playlist, base_config, config_file_name, update, current_playlist_name, single_playlist
    )
    if (update and not single_playlist and previous_playlist_name and
            os.path.abspath(previous_playlist_name) != os.path.abspath(playlist_name) and
            base_config["use_playlist_name"]):
        # Regenerate metadata to update album tag with playlist name
        regenerate_metadata = True

    # Update config for playlist
    write_config(os.path.join(playlist_name, config_file_name), base_config)
    song_file_infos = get_song_file_infos(playlist_name)  # May raise exception for duplicate songs

    if track_num_to_update is not None:
        update_single_song_in_playlist(
            playlist_entries, playlist, playlist_name, song_file_infos,
            base_config, track_num_to_update, regenerate_metadata
        )
        return

    skipped_videos = 0
    updated_video_ids = set()
    stats = {"downloaded": 0, "skipped": 0, "failed_downloads": 0, "failed_updates": 0}

    # Insert dummy entries for songs that should retain index order
    for video_id in song_file_infos.keys():
        config = get_override_config(video_id, base_config)
        if not config["retain_missing_order"]:
            continue

        found = any(
            video_info is not None and video_info.get("id") == video_id
            for video_info in playlist_entries
        )
        if found:
            continue

        # Insert dummy entry without touching the network
        index = max(song_file_infos[video_id].track_num - 1, 0)
        while len(playlist_entries) < index:
            playlist_entries.append(None)
        playlist_entries.insert(index, {
            "id": video_id,
            "channel_id": None,
            "title": None,
            "_retained_missing": True,
        })

    # Prepare threading executor
    executor = None
    download_futures = []
    update_futures = []
    if base_config["use_threading"]:
        thread_count = base_config["thread_count"]
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=thread_count if thread_count > 0 else None)

    try:
        # Download each item in the list
        for i, video_info in enumerate(playlist_entries):
            if video_info is None:
                # Dummy spacer entry to retain index order
                continue

            original_track_num = i + 1
            track_num = original_track_num - skipped_videos
            video_id = video_info.get("id")
            if not video_id:
                continue
            link = f"https://www.youtube.com/watch?v={video_id}"
            song_file_info = song_file_infos.get(video_id)
            config = get_override_config(video_id, base_config)
            updated_video_ids.add(video_id)

            if video_info.get("_retained_missing"):
                # Song is no longer in the playlist; keep its current position untouched
                update_file_order(playlist_name, song_file_info, song_file_info.track_num, config, True)
                continue

            if song_file_info is None:
                # Download audio if not downloaded
                print(f"Downloading '{link}'... ({track_num}/{len(playlist_entries) - skipped_videos})")
                if executor:
                    download_futures.append((
                        executor.submit(
                            download_song_and_update, video_info, playlist, link,
                            playlist_name, track_num, config
                        ),
                        original_track_num,
                    ))
                else:
                    error_message, _ = download_song_and_update(
                        video_info, playlist, link, playlist_name, track_num, config
                    )
                    if error_message is not None:
                        print(error_message)
                        skipped_videos += 1
                        stats["failed_downloads"] += 1
                    else:
                        stats["downloaded"] += 1
            else:
                # Skip downloading audio if already downloaded
                print(f"Skipped downloading '{link}' ({track_num}/{len(playlist_entries) - skipped_videos})")
                stats["skipped"] += 1

                if executor:
                    # Defer updating track num when using threading
                    file_path = os.path.join(playlist_name, song_file_info.file_name)
                else:
                    # Update track num and get file path
                    file_path = update_file_order(playlist_name, song_file_info, track_num, config, False)

                # Generate metadata just in case it is missing
                if executor:
                    update_futures.append(executor.submit(
                        update_song, video_info, song_file_info, file_path, link, track_num,
                        playlist_title, config, regenerate_metadata, force_update
                    ))
                else:
                    error_message = update_song(
                        video_info, song_file_info, file_path, link, track_num,
                        playlist_title, config, regenerate_metadata, force_update
                    )
                    if error_message is not None:
                        print(error_message)
                        stats["failed_updates"] += 1

        if executor:
            failed_track_nums = set()

            # Gather all results in order of submission
            for task, original_track_num in download_futures:
                try:
                    error_message, _ = task.result()
                except Exception as e:
                    error_message = f"Unable to download video number {original_track_num}: {e}"
                if error_message is not None:
                    print(error_message)
                    failed_track_nums.add(original_track_num)
                    stats["failed_downloads"] += 1
                else:
                    stats["downloaded"] += 1

            for task in update_futures:
                try:
                    error_message = task.result()
                except Exception as e:
                    error_message = f"Unable to update song metadata: {e}"
                if error_message is not None:
                    print(error_message)
                    stats["failed_updates"] += 1

            # Get all new temporary song file infos for existing and newly downloaded songs and update
            temp_song_file_infos = get_song_file_infos(playlist_name)  # May raise exception for duplicate songs
            skipped_videos = 0
            for i, video_info in enumerate(playlist_entries):
                if video_info is None:
                    # Dummy spacer entry to retain index order
                    continue

                original_track_num = i + 1
                if video_info.get("_retained_missing"):
                    continue
                if original_track_num in failed_track_nums:
                    skipped_videos += 1
                    continue

                track_num = original_track_num - skipped_videos
                video_id = video_info.get("id")
                temp_song_file_info = temp_song_file_infos.get(video_id)
                if temp_song_file_info is not None:
                    # Update file path and track num
                    config = get_override_config(video_id, base_config)
                    update_file_order(playlist_name, temp_song_file_info, track_num, config, False)
    finally:
        if executor:
            executor.shutdown(wait=True)

    # Move songs that are missing (deleted/privated/etc.) to end of the list
    track_num = len(playlist_entries) - skipped_videos + 1
    for video_id, song_file_info in song_file_infos.items():
        if video_id not in updated_video_ids:
            # Update file path and track num
            config = get_override_config(video_id, base_config)
            update_file_order(playlist_name, song_file_info, track_num, config, True)
            track_num += 1

    print(
        "Download finished. "
        f"Downloaded: {stats['downloaded']}, skipped: {stats['skipped']}, "
        f"failed downloads: {stats['failed_downloads']}, failed metadata updates: {stats['failed_updates']}."
    )

def get_existing_playlists(directory: str, config_file_name: str):
    playlists_data = []
    playlists_name_dict = {}
    duplicate_playlists = {}
    for playlist_name in sorted(next(os.walk(directory))[1]):
        config_file = os.path.join(directory, playlist_name, config_file_name)
        if not os.path.exists(config_file):
            continue

        try:
            config = read_config(config_file)
        except (OSError, json.JSONDecodeError) as e:
            print(e)
            print(f"[ERROR] Config file '{config_file}' is in an invalid format. Please fix or remove the config file.")
            continue

        try:
            playlist_id = get_playlist_id(config["url"])
        except (KeyError, TypeError, ValueError):
            print(f"[ERROR] Playlist URL in config file '{config_file}' is in an invalid format. Please fix or remove the config file.")
            continue

        if playlist_id in playlists_name_dict:
            # Check for duplicate playlists
            if playlist_id not in duplicate_playlists:
                duplicate_playlists[playlist_id] = [playlists_name_dict[playlist_id]]

            duplicate_playlists[playlist_id].append(playlist_name)
            continue

        playlist_data = {
            "playlist_name": playlist_name,
            "config_file": config_file,
            "playlist_id": playlist_id,
            "url": config.get("url", ""),
            "last_updated": time.strftime('%x %X', time.localtime(os.path.getmtime(config_file)))
        }
        playlists_data.append(playlist_data)
        playlists_name_dict[playlist_id] = playlist_name

    if duplicate_playlists:
        exception_strings = []
        for playlist_id, playlist_names in duplicate_playlists.items():
            exception_strings.append("\n".join([
                "The following playlist folders link to the same playlist id",
                f"Duplicate Playlist ID: '{playlist_id}'",
                "\n".join(["- " + playlist_name for playlist_name in playlist_names])
            ]))

        raise FileExistsError("\n".join([
            "",
            "===========================================================",
            "[ERROR] Duplicate playlist folders found in this directory!",
            "===========================================================",
            "\n\n".join(exception_strings),
            "===========================================================",
            "Please remove duplicate playlists to resolve this conflict.",
            "===========================================================",
            ""
        ]))

    return playlists_data

def get_bool_option_response(prompt, default: bool):
    if default:
        prompt_choice = "Y/n"
    else:
        prompt_choice = "y/N"

    while True:
        response = input(f"{prompt} ({prompt_choice}): ").strip().lower()
        if response == "y" or (default and response == ""):
            return True
        elif response == "n" or (not default and response == ""):
            return False
        else:
            print("Invalid response, please type 'y' or 'n'.")

def get_index_option_response(prompt, count: int):
    if count <= 0:
        raise Exception("Count must be greater than 0")

    index = 0
    while True:
        selected_index = input(f"{prompt} (1 to {count}): ").strip()
        try:
            index = int(selected_index) - 1
            if index >= 0 and index < count:
                break
        except ValueError:
            pass

        print("Invalid response, please enter a valid number.")

    return index

def get_numeric_option_response(prompt, maximum=None):
    index = 0
    while True:
        selected_index = input(f"{prompt}: ").strip()
        try:
            index = int(selected_index)
            if index > 0 and (maximum is None or index <= maximum):
                break
        except ValueError:
            pass

        if maximum is None:
            print("Invalid response, please enter a valid number greater than 0.")
        else:
            print(f"Invalid response, please enter a number between 1 and {maximum}.")

    return index

def load_config_with_retry(config_file_name):
    while True:
        try:
            return read_config(config_file_name)
        except (KeyboardInterrupt, EOFError):
            raise
        except json.JSONDecodeError as e:
            print(f"\n{e}\n[ERROR] Config file '{config_file_name}' is in an invalid format. Please fix or remove the config file.")
        except OSError:
            print(f"\n[ERROR] Config file '{config_file_name}' could not be found. Please ensure the config file is present.")
        input("Press 'Enter' to continue after resolving this conflict or close this window to finish.")

def load_existing_playlists_with_retry(config_file_name):
    while True:
        try:
            return get_existing_playlists(".", config_file_name)
        except (KeyboardInterrupt, EOFError):
            raise
        except FileExistsError as e:
            print(e)
            input("Press 'Enter' to continue after resolving this conflict or close this window to finish.")
        except Exception as e:
            print(e)
            print("Failed to get a list of existing playlists")
            return []

def print_playlists(playlists_data):
    playlists_list = []
    for i, playlist_data in enumerate(playlists_data):
        playlists_list.append(
            f"{i + 1}. {playlist_data['playlist_name']} (Last Updated: {playlist_data['last_updated']})"
        )
    print("\n" + "\n".join(playlists_list) + "\n")

def select_playlist(playlists_data):
    print_playlists(playlists_data)
    update_index = get_index_option_response("Enter a playlist number", len(playlists_data))
    return playlists_data[update_index]

def find_playlist_by_url(playlists_data, url):
    try:
        playlist_id = get_playlist_id(url)
    except ValueError:
        return None

    for playlist_data in playlists_data:
        if playlist_data.get("playlist_id") == playlist_id:
            return playlist_data
    return None

def print_banner():
    print("\n".join([
        "YouTube Music Playlist Downloader v" + version,
        "-----------------------------------------------------------",
        "This program automatically downloads & updates a local copy",
        "of any YouTube playlist in the form of a music album folder",
        "- Songs are stored in album folders named by playlist title",
        "- Existing albums are updated with any new or missing songs",
        "- Missing songs are sent to end of album [toggle in config]",
        "- Song metadata is automatically generated using video info",
        "- Metadata includes Title/Artists/Album/Lyrics/Track Number",
        "- Cover art for songs are created by using video thumbnails",
        "",
        "Run this program with --help to see non-interactive options.",
        "",
        "[NOTE] This program and ffmpeg may be blocked by antivirus.",
        "If you run into any issues, you can try adding this program",
        "and your ffmpeg folder to the exclusions of your antivirus.",
        "-----------------------------------------------------------",
    ]))

def run_interactive(config_file_name):
    print_banner()

    OPTION_DOWNLOAD = "Download a playlist from YouTube"
    OPTION_UPDATE = "Update previously saved playlist"
    OPTION_SONG = "Update a single song in playlist"
    OPTION_MODIFY = "Modify previously saved playlist"
    OPTION_GENERATE = "Generate default playlist config"
    OPTION_CHANGE = "Change current working directory"
    OPTION_EXIT = "Exit"

    single_playlist = os.path.exists(config_file_name)
    if single_playlist:
        print(f"Current folder detected as a playlist. Running in single playlist mode.\nIf you did not expect this, please remove '{config_file_name}' from this folder.")

    quit_enabled = True
    while True:
        try:
            check_ffmpeg()

            playlists_data = []
            single_config = None
            selected_option = None
            quit_enabled = True

            if single_playlist:
                single_config = load_config_with_retry(config_file_name)
                options = [
                    OPTION_UPDATE,
                    OPTION_SONG,
                    OPTION_MODIFY,
                    OPTION_CHANGE,
                    OPTION_EXIT,
                ]
            else:
                playlists_data = load_existing_playlists_with_retry(config_file_name)
                options = [OPTION_DOWNLOAD]
                if len(playlists_data) > 0:
                    options += [OPTION_UPDATE, OPTION_SONG, OPTION_MODIFY]
                options += [OPTION_GENERATE, OPTION_CHANGE, OPTION_EXIT]

            options_formatted = [f"{i + 1}. {option}" for i, option in enumerate(options)]
            print("\n" + "\n".join(options_formatted) + "\n")
            selected_option = options[get_index_option_response("Select an option", len(options))]

            quit_enabled = False

            if selected_option in (OPTION_UPDATE, OPTION_SONG, OPTION_MODIFY):
                if single_playlist:
                    config = single_config
                    current_playlist_name = os.path.basename(os.path.abspath(os.getcwd()))
                else:
                    playlist_data = select_playlist(playlists_data)
                    current_playlist_name = playlist_data["playlist_name"]
                    config = read_config(playlist_data["config_file"])

                # In case settings were somehow missing
                config = setup_config(config)

                print("\n" + "\n".join([
                    f"Updating playlist: {current_playlist_name}",
                    f"URL: {config['url']}",
                ]) + "\n")

                regenerate_metadata = False
                force_update = False
                track_num_to_update = None

                if selected_option == OPTION_UPDATE:
                    generate_playlist(
                        config, config_file_name, True, False, False,
                        single_playlist, current_playlist_name, None
                    )

                elif selected_option == OPTION_SONG:
                    track_num_to_update = get_numeric_option_response("Enter a song track number to update")
                    generate_playlist(
                        config, config_file_name, True, False, False,
                        single_playlist, current_playlist_name, track_num_to_update
                    )

                else:
                    # Modify existing playlist
                    print("\n" + "\n".join([
                        "Playlist settings",
                        f"- Reverse playlist: {config['reverse_playlist']}",
                        f"- Use title instead of track name: {config['use_title']}",
                        f"- Use uploader instead of artist: {config['use_uploader']}",
                        f"- Use playlist name for album: {config['use_playlist_name']}",
                    ]) + "\n")

                    modify_settings = get_bool_option_response("Change playlist settings?", default=False)
                    single_song_update = False
                    if modify_settings:
                        last_use_title = config["use_title"]
                        last_use_uploader = config["use_uploader"]
                        last_use_playlist_name = config["use_playlist_name"]

                        config["reverse_playlist"] = get_bool_option_response("Reverse playlist?", default=False)
                        config["use_title"] = get_bool_option_response("Use title instead of track name?", default=True)
                        config["use_uploader"] = get_bool_option_response("Use uploader instead of artist?", default=True)
                        config["use_playlist_name"] = get_bool_option_response("Use playlist name for album?", default=True)

                        # Metadata needs to be regenerated if the settings have been changed
                        if (config["use_title"] != last_use_title or
                                config["use_uploader"] != last_use_uploader or
                                config["use_playlist_name"] != last_use_playlist_name):
                            regenerate_metadata = True
                    elif single_playlist:
                        single_song_update = get_bool_option_response("Update a single song?", default=False)
                        if single_song_update:
                            track_num_to_update = get_numeric_option_response("Enter a song track number to update")

                    if not single_song_update:
                        force_update = get_bool_option_response("Force update all names and metadata?", default=False)

                    generate_playlist(
                        config, config_file_name, True, force_update, regenerate_metadata,
                        single_playlist, current_playlist_name, track_num_to_update
                    )

                quit_enabled = True
                input("Finished updating. Press 'Enter' to return to main menu or close this window to finish.")

            elif selected_option == OPTION_DOWNLOAD:
                # Download new playlist
                config = setup_config({})
                config["url"] = normalize_playlist_url(
                    input("Please enter the URL of the playlist you wish to download: ")
                )

                # Check if playlist is already downloaded
                existing_playlist = find_playlist_by_url(playlists_data, config["url"])
                if existing_playlist:
                    print("\n" + f"> {existing_playlist['playlist_name']} (Last Updated: {existing_playlist['last_updated']})" + "\n")
                    if get_bool_option_response("This playlist is already downloaded. Update playlist?", default=True):
                        current_playlist_name = existing_playlist["playlist_name"]
                        config = setup_config(read_config(existing_playlist["config_file"]))
                        config["url"] = normalize_playlist_url(config["url"])
                        generate_playlist(
                            config, config_file_name, True, False, False,
                            False, current_playlist_name, None
                        )
                    else:
                        print("Not updating existing playlist.")
                else:
                    config["reverse_playlist"] = get_bool_option_response("Reverse playlist?", default=False)
                    config["use_title"] = get_bool_option_response("Use title instead of track name?", default=True)
                    config["use_uploader"] = get_bool_option_response("Use uploader instead of artist?", default=True)
                    config["use_playlist_name"] = get_bool_option_response("Use playlist name for album?", default=True)

                    generate_playlist(config, config_file_name, False, False, False, False, None, None)

                quit_enabled = True
                input("Finished downloading. Press 'Enter' to return to main menu or close this window to finish.")

            elif selected_option == OPTION_GENERATE:
                # Generate default playlist config
                config = setup_config({})
                config["url"] = normalize_playlist_url(
                    input("Please enter the URL of the playlist to generate config for: ")
                )

                existing_playlist = find_playlist_by_url(playlists_data, config["url"])
                if existing_playlist:
                    print(f"Playlist '{existing_playlist['playlist_name']}' is already downloaded.")
                else:
                    playlist_name = generate_default_config(config, config_file_name)
                    print(f"Generated default config in '{playlist_name}'.")

                quit_enabled = True
                input("Finished generating default config. Press 'Enter' to return to main menu or close this window to finish.")

            elif selected_option == OPTION_CHANGE:
                # Change current working directory
                target_path = input("Enter path of target playlists folder to change to: ").strip()
                target_path = os.path.expanduser(target_path)
                if not os.path.isdir(target_path):
                    raise Exception(f"Directory does not exist: '{target_path}'")
                os.chdir(target_path)
                single_playlist = os.path.exists(config_file_name)
                if single_playlist:
                    print(f"Current folder detected as a playlist. Running in single playlist mode.\nIf you did not expect this, please remove '{config_file_name}' from this folder.")
                else:
                    print(f"Changed current working directory to '{os.getcwd()}'.")

            elif selected_option == OPTION_EXIT:
                # Exit
                print("\nQuitting...")
                return 0

        except (KeyboardInterrupt, EOFError):
            if quit_enabled:
                print("\nQuitting...")
                break

            print("\nCancelling...\n(To exit, select Exit or press Ctrl+C again)")
            continue
        except Exception as e:
            print(e)
            print("Error encountered while generating. Please try again.")
            continue

    return 0

def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than 0")
    return number

def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if number < 0:
        raise argparse.ArgumentTypeError("Value cannot be negative")
    return number

def json_dict(value):
    try:
        result = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"Invalid JSON: {e}")
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("Expected a JSON object")
    return result

def comma_list(value):
    return [item.strip() for item in value.split(",") if item.strip()]

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Download and update YouTube playlists as local music album folders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Examples:",
            "  youtube_music_playlist_downloader.py",
            "  youtube_music_playlist_downloader.py --url \"https://www.youtube.com/playlist?list=...\"",
            "  youtube_music_playlist_downloader.py --update --folder \"My Playlist\"",
            "  youtube_music_playlist_downloader.py --song 5 --folder \"My Playlist\" --force",
            "  youtube_music_playlist_downloader.py --list",
        ]),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version}")
    parser.add_argument("--list", action="store_true", help="list playlists found in the current directory and exit")
    parser.add_argument("--url", metavar="URL", help="download or update the playlist at this URL")
    parser.add_argument("--update", action="store_true", help="update a previously downloaded playlist")
    parser.add_argument("--song", type=positive_int, metavar="N", help="update a single song by its track number")
    parser.add_argument("--folder", metavar="NAME", help="playlist folder to update (default: current or only folder)")
    parser.add_argument("--force", action="store_true", help="force refresh of all file names and metadata")
    parser.add_argument("--regenerate-metadata", action="store_true", help="regenerate metadata for all songs")
    parser.add_argument("--directory", metavar="DIR", help="change to this directory before running")
    parser.add_argument("--config-file", default=CONFIG_FILE_NAME, metavar="FILE", help=f"playlist config file name (default: {CONFIG_FILE_NAME})")

    parser.add_argument("--reverse", action=argparse.BooleanOptionalAction, default=None, help="reverse playlist order")
    parser.add_argument("--use-title", action=argparse.BooleanOptionalAction, default=None, help="use the track title from YouTube")
    parser.add_argument("--use-uploader", action=argparse.BooleanOptionalAction, default=None, help="use the uploader as the artist")
    parser.add_argument("--use-playlist-name", action=argparse.BooleanOptionalAction, default=None, help="use the playlist name as the album")
    parser.add_argument("--sync-folder-name", action=argparse.BooleanOptionalAction, default=None, help="rename the folder when the playlist title changes")
    parser.add_argument("--retain-missing-order", action=argparse.BooleanOptionalAction, default=None, help="keep positions of songs missing from the playlist")
    parser.add_argument("--track-num-in-name", action=argparse.BooleanOptionalAction, default=None, help="prefix file names with track numbers")
    parser.add_argument("--strict-lang-match", action=argparse.BooleanOptionalAction, default=None, help="do not fall back to another lyrics language")

    parser.add_argument("--name-format", metavar="FORMAT", help="yt-dlp output template for file names")
    parser.add_argument("--audio-format", metavar="FORMAT", help="yt-dlp audio format selection")
    parser.add_argument("--audio-codec", metavar="CODEC", help="audio codec to convert to (default: mp3)")
    parser.add_argument("--audio-quality", metavar="QUALITY", help="audio quality passed to ffmpeg")
    parser.add_argument("--image-format", metavar="FORMAT", help="cover art image format (jpeg, png, or webp)")
    parser.add_argument("--lyrics-langs", type=comma_list, metavar="LANGS", help="comma-separated lyrics language priority list")
    parser.add_argument("--start-time", metavar="TIME", help="start cut time, e.g. 0:30 or -1:00")
    parser.add_argument("--end-time", metavar="TIME", help="end cut time, e.g. 3:45 or -0:10")

    parser.add_argument("--threads", type=nonnegative_int, metavar="N", help="number of download threads (0 = automatic)")
    parser.add_argument("--no-threading", action="store_true", help="disable threaded downloads and metadata updates")

    parser.add_argument("--cookie-file", metavar="FILE", help="path to a Netscape cookie file")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER", help="browser to load cookies from, e.g. chrome:Default")
    parser.add_argument("--extractor-args", type=json_dict, metavar="JSON", help="yt-dlp extractor arguments as a JSON object")
    parser.add_argument("--verbose", action="store_true", help="enable verbose yt-dlp output")
    return parser

def apply_cli_overrides(config, args):
    if args.reverse is not None:
        config["reverse_playlist"] = args.reverse
    if args.use_title is not None:
        config["use_title"] = args.use_title
    if args.use_uploader is not None:
        config["use_uploader"] = args.use_uploader
    if args.use_playlist_name is not None:
        config["use_playlist_name"] = args.use_playlist_name
    if args.sync_folder_name is not None:
        config["sync_folder_name"] = args.sync_folder_name
    if args.retain_missing_order is not None:
        config["retain_missing_order"] = args.retain_missing_order
    if args.track_num_in_name is not None:
        config["track_num_in_name"] = args.track_num_in_name
    if args.strict_lang_match is not None:
        config["strict_lang_match"] = args.strict_lang_match

    if args.name_format:
        config["name_format"] = args.name_format
    if args.audio_format:
        config["audio_format"] = args.audio_format
    if args.audio_codec:
        config["audio_codec"] = args.audio_codec
    if args.audio_quality:
        config["audio_quality"] = args.audio_quality
    if args.image_format:
        config["image_format"] = args.image_format
    if args.lyrics_langs is not None:
        config["lyrics_langs"] = args.lyrics_langs
    if args.start_time is not None:
        config["start_time"] = args.start_time
    if args.end_time is not None:
        config["end_time"] = args.end_time

    if args.threads is not None:
        config["thread_count"] = args.threads
        config["use_threading"] = True
    if args.no_threading:
        config["use_threading"] = False

    if args.cookie_file is not None:
        config["cookie_file"] = args.cookie_file
    if args.cookies_from_browser is not None:
        config["cookies_from_browser"] = args.cookies_from_browser
    if args.extractor_args is not None:
        config["extractor_args"] = args.extractor_args
    if args.verbose:
        config["verbose"] = True

def resolve_cli_playlist(config_file_name, folder):
    if folder:
        folder = os.path.normpath(os.path.expanduser(folder))
        config_file = os.path.join(folder, config_file_name)
        if not os.path.isfile(config_file):
            raise Exception(f"No playlist config found in '{folder}'")
        if os.path.abspath(os.path.dirname(os.path.abspath(folder))) != os.path.abspath(os.getcwd()):
            raise Exception("--folder must be a playlist folder in the current directory. Use --directory to change location first.")
        if os.path.abspath(folder) == os.path.abspath(os.getcwd()):
            return config_file, os.path.basename(os.path.abspath(os.getcwd())), True
        return config_file, folder, False

    if os.path.isfile(config_file_name):
        return config_file_name, os.path.basename(os.path.abspath(os.getcwd())), True

    playlists_data = get_existing_playlists(".", config_file_name)
    if len(playlists_data) == 0:
        raise Exception("No previously downloaded playlists found in this directory")
    if len(playlists_data) > 1:
        names = ", ".join(f"'{data['playlist_name']}'" for data in playlists_data)
        raise Exception(f"Multiple playlists found ({names}). Use --folder to select one.")
    playlist_data = playlists_data[0]
    return playlist_data["config_file"], playlist_data["playlist_name"], False

def run_cli(args):
    config_file_name = args.config_file

    if args.list:
        if os.path.isfile(config_file_name):
            config = read_config(config_file_name)
            print(f"1. {os.path.basename(os.path.abspath(os.getcwd()))} (single playlist mode)")
            print(f"   URL: {config.get('url', '')}")
        else:
            playlists_data = get_existing_playlists(".", config_file_name)
            if not playlists_data:
                print("No playlists found in this directory.")
            else:
                print_playlists(playlists_data)
        return 0

    require_ffmpeg()

    if args.url:
        url = normalize_playlist_url(args.url)
        single_playlist = os.path.isfile(config_file_name)
        playlists_data = [] if single_playlist else get_existing_playlists(".", config_file_name)
        existing_playlist = find_playlist_by_url(playlists_data, url)

        if single_playlist:
            config = read_config(config_file_name)
            try:
                same_playlist = get_playlist_id(config.get("url", "")) == get_playlist_id(url)
            except ValueError:
                same_playlist = False

            if same_playlist:
                config = setup_config(config)
                config["url"] = url
                current_playlist_name = os.path.basename(os.path.abspath(os.getcwd()))
                update = True
            else:
                config = setup_config({})
                config["url"] = url
                current_playlist_name = None
                update = False
                single_playlist = False
        elif existing_playlist:
            config = setup_config(read_config(existing_playlist["config_file"]))
            config["url"] = url
            current_playlist_name = existing_playlist["playlist_name"]
            update = True
        else:
            config = setup_config({})
            config["url"] = url
            current_playlist_name = None
            update = False

        apply_cli_overrides(config, args)
        generate_playlist(
            config, config_file_name, update, args.force, args.regenerate_metadata,
            single_playlist, current_playlist_name, args.song
        )
        return 0

    if args.update or args.song:
        config_file, current_playlist_name, single_playlist = resolve_cli_playlist(
            config_file_name, args.folder
        )
        config = setup_config(read_config(config_file))
        apply_cli_overrides(config, args)
        generate_playlist(
            config, config_file_name, True, args.force, args.regenerate_metadata,
            single_playlist, current_playlist_name, args.song
        )
        return 0

    raise Exception("No action specified")

def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.directory:
            target_directory = os.path.expanduser(args.directory)
            if not os.path.isdir(target_directory):
                parser.error(f"Directory does not exist: '{target_directory}'")
            os.chdir(target_directory)

        if args.list or args.url or args.update or args.song:
            return run_cli(args)
        return run_interactive(args.config_file)
    except (KeyboardInterrupt, EOFError):
        print("\nQuitting...")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
