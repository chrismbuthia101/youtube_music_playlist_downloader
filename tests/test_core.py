import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mutagen import id3  # noqa: E402

import youtube_music_playlist_downloader as ympd  # noqa: E402

PLAYLIST_ID = "PL1234567890abcdefghijklmnopqrstuvwxyz"
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
VIDEO_ID = "dQw4w9WgXcQ"


class TempDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self._temp_dir.name)
        os.chdir(self.temp_dir)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._temp_dir.cleanup()

    def create_song(self, relative_path, title="Title", track="1", video_id=VIDEO_ID):
        path = self.temp_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        tags = id3.ID3()
        tags.add(id3.TIT2(encoding=3, text=title))
        tags.add(id3.TRCK(encoding=3, text=track))
        tags.add(id3.WOAR(f"https://www.youtube.com/watch?v={video_id}"))
        tags.save(path, v2_version=3)
        return path


class FileNameTests(unittest.TestCase):
    def test_replaces_invalid_characters(self):
        self.assertEqual(ympd.format_file_name('a<b>c:d"e/f\\g|h?i*j'), 'a_b_c_d_e_f_g_h_i_j')

    def test_removes_control_characters_and_trailing_dots(self):
        self.assertEqual(ympd.format_file_name("song\x00name. . "), "song_name")

    def test_empty_name_gets_fallback(self):
        self.assertEqual(ympd.format_file_name("   "), "Untitled")
        self.assertEqual(ympd.format_file_name(".."), "Untitled")

    def test_windows_reserved_names(self):
        self.assertEqual(ympd.format_file_name("CON"), "_CON")
        self.assertEqual(ympd.format_file_name("lpt1.mp3"), "_lpt1.mp3")
        self.assertEqual(ympd.format_file_name("CONSOLE"), "CONSOLE")

    def test_limits_utf8_length(self):
        result = ympd.format_file_name("é" * 150)
        self.assertLessEqual(len(result.encode("utf-8")), 200)


class UrlTests(unittest.TestCase):
    def test_raw_playlist_id(self):
        self.assertEqual(ympd.get_playlist_id(PLAYLIST_ID), PLAYLIST_ID)
        self.assertEqual(ympd.normalize_playlist_url(PLAYLIST_ID), PLAYLIST_URL)

    def test_playlist_urls(self):
        self.assertEqual(ympd.get_playlist_id(PLAYLIST_URL), PLAYLIST_ID)
        self.assertEqual(
            ympd.get_playlist_id(f"https://music.youtube.com/playlist?list={PLAYLIST_ID}&si=abc"),
            PLAYLIST_ID,
        )
        self.assertEqual(
            ympd.normalize_playlist_url(f"music.youtube.com/playlist?list={PLAYLIST_ID}"),
            f"https://music.youtube.com/playlist?list={PLAYLIST_ID}",
        )

    def test_invalid_playlist_urls(self):
        with self.assertRaises(ValueError):
            ympd.get_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(ValueError):
            ympd.normalize_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(ValueError):
            ympd.normalize_playlist_url("   ")

    def test_video_id_from_metadata(self):
        tags = id3.ID3()
        tags.add(id3.WOAR(f"https://www.youtube.com/watch?v={VIDEO_ID}"))
        self.assertEqual(ympd.get_video_id_from_metadata(tags), VIDEO_ID)

        tags = id3.ID3()
        tags.add(id3.WOAR(f"https://youtu.be/{VIDEO_ID}?t=10"))
        self.assertEqual(ympd.get_video_id_from_metadata(tags), VIDEO_ID)

        tags = id3.ID3()
        tags.add(id3.WOAR("https://example.com/not-a-video"))
        with self.assertRaises(Exception):
            ympd.get_video_id_from_metadata(tags)

    def test_video_id_from_metadata_rejects_duplicates(self):
        tags = id3.ID3()
        tags.add(id3.WOAR(f"https://www.youtube.com/watch?v={VIDEO_ID}"))
        tags.add(id3.WOAR("https://www.youtube.com/watch?v=aaaaaaaaaaa"))
        with self.assertRaises(Exception):
            ympd.get_video_id_from_metadata(tags)


class ParsingTests(unittest.TestCase):
    def test_parse_track_number(self):
        self.assertEqual(ympd.parse_track_number("3/12"), 3)
        self.assertEqual(ympd.parse_track_number(" 07 "), 7)
        self.assertEqual(ympd.parse_track_number("not a number"), 0)
        self.assertEqual(ympd.parse_track_number(None, default=1), 1)

    def test_parse_time_str(self):
        self.assertEqual(ympd.parse_time_str(None, 1.5), 1.5)
        self.assertEqual(ympd.parse_time_str("", 2), 2)
        self.assertEqual(ympd.parse_time_str("30", 0), 30)
        self.assertEqual(ympd.parse_time_str("1:02.5", 0), 62.5)
        self.assertEqual(ympd.parse_time_str("1:02:03", 0), 3723)
        self.assertEqual(ympd.parse_time_str("-0:30", 0), -30)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ympd.parse_time_str("invalid", 9), 9)

    def test_parse_lyrics_events(self):
        content = {"events": [
            {"tStartMs": 0, "segs": [{"utf8": "hello"}]},
            {"tStartMs": 1000, "segs": [{"utf8": "world"}]},
            {"tStartMs": 1000, "segs": [{"utf8": "again"}]},
            {"tStartMs": 1100, "segs": [{"utf8": "hello"}]},
            {"tStartMs": 1200, "segs": [{"utf8": "hello"}]},
        ]}
        synced, unsynced = ympd.parse_lyrics_events(content)
        self.assertEqual(synced, [("hello", 0), ("world\nagain", 1000), ("hello", 1100)])
        self.assertEqual(unsynced, ["hello", "world\nagain", "hello"])

    def test_parse_override_lyrics(self):
        synced, unsynced = ympd.parse_override_lyrics([["line 1", 1000], ["line 2", 2000]])
        self.assertEqual(synced, [("line 1", 1000), ("line 2", 2000)])
        self.assertEqual(unsynced, ["line 1", "line 2"])

        with self.assertRaises(ValueError):
            ympd.parse_override_lyrics([["missing timestamp"]])

    def test_normalize_lyrics_language(self):
        self.assertEqual(ympd.normalize_lyrics_language("en"), "eng")
        self.assertEqual(ympd.normalize_lyrics_language("ja"), "jpn")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ympd.normalize_lyrics_language("not a language"), "eng")

    def test_normalize_image_format(self):
        self.assertEqual(ympd.normalize_image_format("JPG"), "jpeg")
        self.assertEqual(ympd.normalize_image_format(".png"), "png")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ympd.normalize_image_format("gif"), "jpeg")


class ConfigTests(TempDirectoryTestCase):
    def test_write_and_read_config(self):
        config = {"url": PLAYLIST_URL, "title": "Ünicode", "values": [1, 2, 3]}
        ympd.write_config("config.json", config)
        self.assertEqual(ympd.read_config("config.json"), config)

        leftovers = [name for name in os.listdir(".") if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_setup_config_defaults(self):
        config = ympd.setup_config({})
        self.assertEqual(config["name_format"], "%(title)s-%(id)s.%(ext)s")
        self.assertEqual(config["retries"], 3)
        self.assertEqual(config["sleep_interval"], 0)
        self.assertEqual(config["max_sleep_interval"], 0)
        self.assertTrue(config["include_metadata"]["title"])
        self.assertNotIn("url", config["include_metadata"])
        self.assertIn("EXAMPLE_VIDEO_ID_HERE", config["overrides"])

    def test_setup_config_validation(self):
        with self.assertRaises(Exception):
            ympd.setup_config({"thread_count": "many"})
        with self.assertRaises(Exception):
            ympd.setup_config({"overrides": []})
        with self.assertRaises(Exception):
            ympd.setup_config({"overrides": {VIDEO_ID: "not a mapping"}})

    def test_setup_config_warns_about_unknown_options(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            config = ympd.setup_config({"unknown_option": True})
        self.assertNotIn("unknown_option", config)
        self.assertIn("Unknown config option", output.getvalue())

    def test_setup_config_warns_for_non_mp3(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ympd.setup_config({"audio_codec": "opus"})
        self.assertIn("Only MP3 files are supported", output.getvalue())

    def test_setup_config_normalizes_audio_codec(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            config = ympd.setup_config({"audio_codec": " MP3 "})
        self.assertEqual(config["audio_codec"], "mp3")
        self.assertNotIn("Only MP3 files are supported", output.getvalue())

    def test_override_config_is_minimal(self):
        source = {
            "url": PLAYLIST_URL,
            "use_title": False,
            "metadata_overrides": {"title": "Override"},
            "overrides": {
                VIDEO_ID: {
                    "use_uploader": False,
                    "url": "https://example.com/should-be-removed",
                }
            },
        }
        config = ympd.setup_config(source)
        override = config["overrides"][VIDEO_ID]
        self.assertNotIn("url", override)
        self.assertNotIn("sync_folder_name", override)
        self.assertFalse(override["use_uploader"])

        merged = ympd.get_override_config(VIDEO_ID, config)
        self.assertFalse(merged["use_uploader"])
        self.assertFalse(merged["use_title"])
        self.assertEqual(merged["metadata_overrides"]["title"], "Override")
        self.assertNotIn("overrides", merged)

    def test_copy_config_minimal_copy(self):
        source = {"a": 1, "nested": {"keep": 2}}
        destination = {"a": 9, "b": 8, "nested": {"keep": 0, "drop": 1}}
        ympd.copy_config(source, destination, minimal_copy=True)
        self.assertEqual(destination, {"a": 1, "nested": {"keep": 2}})

    def test_setup_custom_metadata(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            custom = ympd.setup_custom_metadata({"custom_metadata": {
                "TEXT": "Lyricist",
                "TIT2": "conflict",
                "NOPE": "unknown",
            }})
        self.assertEqual(custom, {"TEXT": "Lyricist"})
        self.assertIn("Unknown custom metadata tag", output.getvalue())


class FolderTests(TempDirectoryTestCase):
    def test_create_playlist_folder_avoids_collisions(self):
        first = ympd.create_playlist_folder("Album", ".playlist_config.json", PLAYLIST_ID)
        ympd.write_config(os.path.join(first, ".playlist_config.json"), {"url": "https://www.youtube.com/playlist?list=OTHERPLAYLIST"})
        self.assertEqual(ympd.create_playlist_folder("Album", ".playlist_config.json", PLAYLIST_ID), "Album (2)")

    def test_create_playlist_folder_reuses_matching_playlist(self):
        first = ympd.create_playlist_folder("Album", ".playlist_config.json", PLAYLIST_ID)
        ympd.write_config(os.path.join(first, ".playlist_config.json"), {"url": PLAYLIST_URL})
        second = ympd.create_playlist_folder("Album", ".playlist_config.json", PLAYLIST_ID)
        self.assertEqual(first, second)

    def test_create_playlist_folder_reuses_empty_folder(self):
        os.mkdir("Album")
        self.assertEqual(ympd.create_playlist_folder("Album", ".playlist_config.json", PLAYLIST_ID), "Album")

    def test_resolve_playlist_folder_renames_folder(self):
        os.mkdir("Old Name")
        playlist = {"title": "New Name"}
        config = {"url": PLAYLIST_URL, "sync_folder_name": True}
        result = ympd.resolve_playlist_folder(
            playlist, config, ".playlist_config.json", True, "Old Name", False
        )
        self.assertEqual(result, "New Name")
        self.assertTrue(os.path.isdir("New Name"))
        self.assertFalse(os.path.exists("Old Name"))

    def test_resolve_playlist_folder_respects_sync_disabled(self):
        os.mkdir("Old Name")
        playlist = {"title": "New Name"}
        config = {"url": PLAYLIST_URL, "sync_folder_name": False}
        result = ympd.resolve_playlist_folder(
            playlist, config, ".playlist_config.json", True, "Old Name", False
        )
        self.assertEqual(result, "Old Name")

    def test_safe_rename_rejects_existing_destination(self):
        Path("a").write_text("a")
        Path("b").write_text("b")
        with self.assertRaises(FileExistsError):
            ympd.safe_rename("a", "b")

    def test_safe_rename_creates_parent_directories(self):
        Path("a").write_text("a")
        ympd.safe_rename("a", "nested/b")
        self.assertEqual(Path("nested/b").read_text(), "a")


class SongFileTests(TempDirectoryTestCase):
    def test_get_song_file_info(self):
        self.create_song("Album/1. Song.mp3", title="Song", track="3/12")
        info = ympd.get_song_file_info("Album", "1. Song.mp3")
        self.assertIsNotNone(info)
        self.assertEqual(info.video_id, VIDEO_ID)
        self.assertEqual(info.name, "Song")
        self.assertEqual(info.track_num, 3)

    def test_ignores_hidden_and_unrelated_files(self):
        self.create_song("Album/.hidden.mp3")
        Path("Album/cover.jpg").write_bytes(b"not an image")
        Path("Album/no-metadata.mp3").touch()
        self.assertEqual(ympd.get_song_file_infos("Album"), {})

    def test_duplicate_video_ids_raise(self):
        self.create_song("Album/1. First.mp3", title="First", track="1")
        self.create_song("Album/2. Second.mp3", title="Second", track="2")
        with self.assertRaises(Exception) as context:
            ympd.get_song_file_infos("Album")
        self.assertIn("Duplicate song files", str(context.exception))

    def test_update_file_order(self):
        self.create_song("Album/1. Song.mp3", title="Song", track="1")
        config = ympd.setup_config({})
        info = ympd.get_song_file_info("Album", "1. Song.mp3")
        result = ympd.update_file_order("Album", info, 4, config, False)
        self.assertEqual(result, os.path.join("Album", "4. Song.mp3"))
        self.assertEqual(id3.ID3(result).get("TRCK").text, ["4"])


class MetadataTests(TempDirectoryTestCase):
    def test_generate_metadata(self):
        path = self.temp_dir / "song.mp3"
        path.touch()
        id3.ID3().save(path)
        config = ympd.setup_config({})
        config["use_title"] = False
        config["use_uploader"] = False
        config["metadata_overrides"]["date"] = "2025-01-31"

        info = {
            "id": VIDEO_ID,
            "title": "YouTube Title",
            "track": "Track Title",
            "artist": "YouTube Artist",
            "album": "YouTube Album",
            "uploader": "Uploader",
            "upload_date": "20240102",
            "thumbnail": "https://example.com/thumb.jpg",
        }
        with unittest.mock.patch.object(ympd, "get_song_info", return_value=info), \
             unittest.mock.patch.object(ympd, "get_cover_data", return_value=b"fake-image"), \
             unittest.mock.patch.object(ympd, "fetch_lyrics", return_value=([("line", 0)], ["line"], "eng")):
            result = ympd.generate_metadata(str(path), PLAYLIST_URL, 2, "Album", config, False, False)

        self.assertEqual(result, "")
        tags = id3.ID3(path)
        self.assertEqual(tags.get("TIT2").text, ["Track Title"])
        self.assertEqual(tags.get("TPE1").text, ["YouTube Artist"])
        self.assertEqual(tags.get("TALB").text, ["Album"])
        self.assertEqual(tags.get("TRCK").text, ["2"])
        self.assertEqual(str(tags.get("TDRC").text[0]), "2025-01-31")
        self.assertEqual(tags.getall("WOAR")[0].url, PLAYLIST_URL)
        self.assertEqual(tags.getall("APIC")[0].data, b"fake-image")
        self.assertEqual(tags.getall("USLT")[0].text, "line")

    def test_generate_metadata_skips_valid_existing_tags(self):
        self.create_song("song.mp3", title="Existing", track="1")
        config = ympd.setup_config({})
        config["include_metadata"] = {key: False for key in config["include_metadata"]}
        with unittest.mock.patch.object(ympd, "get_song_info") as get_song_info:
            ympd.generate_metadata("song.mp3", PLAYLIST_URL, 1, "Album", config, False, False)
        get_song_info.assert_not_called()

    def test_force_update_returns_prepared_file_name(self):
        path = self.temp_dir / "old.mp3"
        path.touch()
        id3.ID3().save(path)
        config = ympd.setup_config({})
        info = {"id": VIDEO_ID, "title": "Title", "ext": "mp3"}
        with unittest.mock.patch.object(ympd, "get_song_info", return_value=info), \
             unittest.mock.patch.object(ympd, "get_prepared_file_name", return_value="1. Title-dQw4w9WgXcQ.mp3"), \
             unittest.mock.patch.object(ympd, "get_cover_data", return_value=b"image"), \
             unittest.mock.patch.object(ympd, "fetch_lyrics", return_value=([("line", 0)], ["line"], "eng")):
            result = ympd.generate_metadata(str(path), PLAYLIST_URL, 1, "Album", config, False, True)
        self.assertEqual(result, "1. Title-dQw4w9WgXcQ.mp3")


class PlaylistTests(TempDirectoryTestCase):
    def test_generate_new_playlist(self):
        config = ympd.setup_config({"url": PLAYLIST_URL, "use_threading": False})
        entries = [
            {"id": "aaaaaaaaaaa", "channel_id": "channel", "title": "First"},
            {"id": "bbbbbbbbbbb", "channel_id": "channel", "title": "Second"},
        ]
        playlist = {"title": "My Album", "entries": entries}

        def fake_download(video_info, playlist, link, playlist_name, track_num, config):
            file_name = f"{track_num}. {video_info['title']}-{video_info['id']}.mp3"
            self.create_song(os.path.join(playlist_name, file_name),
                             title=video_info["title"], track=str(track_num), video_id=video_info["id"])
            return None, track_num

        with unittest.mock.patch.object(ympd, "get_playlist_info", return_value=playlist), \
             unittest.mock.patch.object(ympd, "download_song_and_update", side_effect=fake_download), \
             contextlib.redirect_stdout(io.StringIO()):
            ympd.generate_playlist(config, ".playlist_config.json", False, False, False, False)

        self.assertTrue(Path("My Album/.playlist_config.json").is_file())
        self.assertTrue(Path("My Album/1. First-aaaaaaaaaaa.mp3").is_file())
        self.assertTrue(Path("My Album/2. Second-bbbbbbbbbbb.mp3").is_file())
        self.assertEqual(id3.ID3("My Album/2. Second-bbbbbbbbbbb.mp3").get("TRCK").text, ["2"])

    def test_update_playlist_reorders_existing_and_new_songs(self):
        config = ympd.setup_config({"url": PLAYLIST_URL, "use_threading": False})
        self.create_song("My Album/1. First-aaaaaaaaaaa.mp3", title="First", track="1", video_id="aaaaaaaaaaa")
        self.create_song("My Album/2. Second-bbbbbbbbbbb.mp3", title="Second", track="2", video_id="bbbbbbbbbbb")

        entries = [
            {"id": "aaaaaaaaaaa", "channel_id": "channel", "title": "First"},
            {"id": "ccccccccccc", "channel_id": "channel", "title": "Third"},
            {"id": "bbbbbbbbbbb", "channel_id": "channel", "title": "Second"},
        ]
        playlist = {"title": "My Album", "entries": entries}

        def fake_download(video_info, playlist, link, playlist_name, track_num, config):
            file_name = f"{track_num}. {video_info['title']}-{video_info['id']}.mp3"
            self.create_song(os.path.join(playlist_name, file_name),
                             title=video_info["title"], track=str(track_num), video_id=video_info["id"])
            return None, track_num

        with unittest.mock.patch.object(ympd, "get_playlist_info", return_value=playlist), \
             unittest.mock.patch.object(ympd, "download_song_and_update", side_effect=fake_download), \
             unittest.mock.patch.object(ympd, "update_song", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            ympd.generate_playlist(
                config, ".playlist_config.json", True, False, False, False, "My Album", None
            )

        self.assertTrue(Path("My Album/1. First-aaaaaaaaaaa.mp3").is_file())
        self.assertTrue(Path("My Album/2. Third-ccccccccccc.mp3").is_file())
        self.assertTrue(Path("My Album/3. Second-bbbbbbbbbbb.mp3").is_file())
        self.assertFalse(Path("My Album/2. Second-bbbbbbbbbbb.mp3").exists())
        self.assertEqual(id3.ID3("My Album/3. Second-bbbbbbbbbbb.mp3").get("TRCK").text, ["3"])


class CliTests(TempDirectoryTestCase):
    def test_parser_defaults(self):
        parser = ympd.build_arg_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.url)
        self.assertFalse(args.update)
        self.assertEqual(args.config_file, ".playlist_config.json")

    def test_parser_rejects_invalid_values(self):
        parser = ympd.build_arg_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--song", "0"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--extractor-args", "[]"])

    def test_apply_cli_overrides(self):
        parser = ympd.build_arg_parser()
        config = ympd.setup_config({})

        ympd.apply_cli_overrides(config, parser.parse_args(["--threads", "0"]))
        self.assertTrue(config["use_threading"])
        self.assertEqual(config["thread_count"], 0)

        ympd.apply_cli_overrides(config, parser.parse_args(["--threads", "4"]))
        self.assertTrue(config["use_threading"])
        self.assertEqual(config["thread_count"], 4)

        ympd.apply_cli_overrides(config, parser.parse_args(["--no-threading"]))
        self.assertFalse(config["use_threading"])

        ympd.apply_cli_overrides(config, parser.parse_args([
            "--no-reverse", "--no-use-title", "--use-uploader",
            "--audio-codec", "opus", "--lyrics-langs", "en, ja",
        ]))
        self.assertFalse(config["reverse_playlist"])
        self.assertFalse(config["use_title"])
        self.assertTrue(config["use_uploader"])
        self.assertEqual(config["audio_codec"], "opus")
        self.assertEqual(config["lyrics_langs"], ["en", "ja"])

    def test_list_with_no_playlists(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ympd.main(["--list"]), 0)
        self.assertIn("No playlists found", output.getvalue())

    def test_list_single_playlist(self):
        ympd.write_config(".playlist_config.json", {"url": PLAYLIST_URL})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ympd.main(["--list"]), 0)
        self.assertIn("single playlist mode", output.getvalue())
        self.assertIn(PLAYLIST_URL, output.getvalue())

    def test_missing_action_returns_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ympd.main(["--update"]), 1)
        self.assertIn("No previously downloaded playlists", output.getvalue())


if __name__ == "__main__":
    unittest.main()
