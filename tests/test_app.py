import base64
import http.client
import hashlib
from io import BytesIO
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.id3 import APIC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from tagger.formats import apply_tags, artwork_bytes, open_audio, read_file
from tagger.library import Library, LibraryError, apply_title_options, normalize_track_title, tags_from_filename
from tagger.server import Application, Handler


FORMATS = {"mp3": "libmp3lame", "flac": "flac", "m4a": "aac", "ogg": "libvorbis",
           "opus": "libopus", "wav": "pcm_s16le", "aiff": "pcm_s16be", "wma": "wmav2"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")


def make_audio(directory, extension, duration=0.4):
    path = directory / ("track." + extension)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration={}".format(duration), "-c:a", FORMATS[extension], "-y", str(path)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed for audio fixtures")
class FormatTests(unittest.TestCase):
    def test_filename_import_queues_and_commits_all_audio_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            paths = []
            for extension in FORMATS:
                original = make_audio(music, extension)
                path = music / ("New Artist - New Album - 03 - New Title." + extension)
                original.rename(path)
                apply_tags(path, {"title": "Old title", "artist": "Old artist", "album": "Old album", "genre": "Jazz"})
                paths.append(str(path))
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            library.queue_add(paths, {"title": "Earlier staged title", "date": "1999"})
            pending = {"paths": paths, "edits": {}, "import_tags": "artist_album_track_title"}
            preview = library.preview_queue(pending)
            self.assertEqual(len(preview["items"]), len(FORMATS))
            for item in preview["items"]:
                self.assertEqual(item["after"]["title"], "New Title")
                self.assertEqual(item["after"]["genre"], ["Jazz"])
                self.assertEqual(read_file(item["source"])["tags"]["title"], "Old title")
            library.queue_add(paths, {}, import_tags="artist_album_track_title")
            reopened = Library(base / "state", allowed_volume=music)
            self.assertEqual(reopened.common_tags(paths)["common"]["title"], "New Title")
            plan = reopened.preview_queue()
            self.assertEqual([item["after"] for item in plan["items"]],
                             [item["after"] for item in preview["items"]])
            job_id = reopened.start_commit(plan["id"])
            self._wait(reopened, job_id)
            self.assertEqual(reopened.job(job_id)["state"], "complete", reopened.job(job_id)["errors"])
            for path in paths:
                tags = read_file(path)["tags"]
                self.assertEqual({field: tags[field] for field in ("title", "artist", "album", "track", "genre", "date")},
                                 {"title": "New Title", "artist": "New Artist", "album": "New Album",
                                  "track": "3", "genre": ["Jazz"], "date": "1999"})
            self.assertEqual(reopened.queue_items(), [])

    def test_filename_import_validation_and_title_folder_rename_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            original = make_audio(music, "mp3")
            valid = music / "Artist - Album - 07 - hELLO (Live).mp3"
            original.rename(valid)
            invalid = make_audio(music, "flac")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            selected = [str(valid), str(invalid)]
            library.queue_add([str(valid)], {"genre": "Jazz"})
            previous = library.queue_items()
            with self.assertRaisesRegex(LibraryError, "does not match"):
                library.queue_add(selected, {}, import_tags="artist_album_track_title")
            self.assertEqual(library.queue_items(), previous)
            with self.assertRaisesRegex(LibraryError, "import option"):
                library.queue_add(selected, {}, import_tags="guess")
            pending = {"paths": selected, "edits": {"date": "1999"}, "import_tags": "artist_album_track_title",
                       "mode": "artist_album", "rename": "track_title", "normalize_title": True,
                       "remove_title_text": " (Live)"}
            plan = library.preview_queue(pending)
            self.assertEqual(len(plan["items"]), 1)
            self.assertEqual(len(plan["errors"]), 1)
            self.assertEqual(plan["errors"][0]["path"], str(invalid))
            item = plan["items"][0]
            self.assertEqual(item["after"]["title"], "Hello")
            self.assertEqual(item["after"]["genre"], ["Jazz"])
            self.assertEqual(item["destination"], str(music / "Artist" / "Album (1999)" / "07 - Hello.mp3"))
            direct = library.preview([str(valid)], {"date": "1999", "genre": "Jazz"}, "artist_album",
                                     rename="track_title", normalize_title=True, remove_title_text=" (Live)",
                                     import_tags="artist_album_track_title")
            self.assertEqual(direct["items"][0]["after"], item["after"])
            library.queue_add([str(valid)], {"date": "1999"}, mode="artist_album", rename="track_title",
                              normalize_title=True, remove_title_text=" (Live)", import_tags="artist_album_track_title")
            self.assertEqual(library.preview_queue()["items"][0]["after"], item["after"])

    def test_title_normalization_preserves_word_boundaries(self):
        self.assertEqual(normalize_track_title("hELLO WORLD - DON'T STOP (ÉLÈVE)"),
                         "Hello World - Don't Stop (Élève)")
        self.assertEqual(normalize_track_title("rock’n’ROLL / AC_DC 3RD"),
                         "Rock’n’roll / Ac_Dc 3rd")

    def test_exact_title_text_removal_is_case_sensitive_and_precedes_normalization(self):
        self.assertEqual(apply_title_options("hELLO (Live) WORLD (Live)", " (Live)", True),
                         "Hello World")
        self.assertEqual(apply_title_options("Song (live) Mix", " (Live)", False),
                         "Song (live) Mix")
        self.assertEqual(apply_title_options("Song (Live)", " (Live)", False), "Song")

    def test_audio_preview_supports_byte_ranges(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "preview.mp3"
            source.write_bytes(b"0123456789")
            handler = object.__new__(Handler)
            responses = []
            headers = {}
            handler.send_response = responses.append
            handler.send_header = lambda name, value: headers.__setitem__(name, value)
            handler.end_headers = lambda: None
            handler.wfile = BytesIO()
            handler.headers = {"Range": "bytes=2-5"}
            handler._send_audio(source, "audio/mpeg")
            self.assertEqual(responses, [206])
            self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(handler.wfile.getvalue(), b"2345")
            responses.clear()
            headers.clear()
            handler.wfile = BytesIO()
            handler.headers = {"Range": "bytes=20-"}
            handler._send_audio(source, "audio/mpeg")
            self.assertEqual(responses, [416])
            self.assertEqual(headers["Content-Range"], "bytes */10")

    def test_supported_formats_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            for extension in FORMATS:
                with self.subTest(extension=extension):
                    directory = Path(temporary) / extension
                    directory.mkdir()
                    path = make_audio(directory, extension)
                    apply_tags(path, {"title": "Test song", "artist": "Test artist", "album": "Test album",
                                      "album_artist": "Various Artists", "track": "2", "disc": "1",
                                      "date": "2024", "genre": "Jazz", "comment": "Keep me"})
                    result = read_file(path)
                    for field in ("title", "artist", "album", "album_artist", "track", "disc", "date", "genre", "comment"):
                        self.assertEqual(result["tags"][field], "Test song" if field == "title" else {
                            "artist": "Test artist", "album": "Test album", "album_artist": "Various Artists",
                            "track": "2", "disc": "1", "date": "2024", "genre": ["Jazz"], "comment": "Keep me"}[field])
                    apply_tags(path, {"genre": ""})
                    self.assertEqual(read_file(path)["tags"]["genre"], [])
                    if extension != "wma":
                        apply_tags(path, {}, (PNG, "image/png"))
                        self.assertTrue(read_file(path)["artwork"])
                        self.assertEqual(artwork_bytes(path), (PNG, "image/png"))
                    else:
                        audio = ASF(str(path))
                        picture = bytes([3]) + len(PNG).to_bytes(4, "little") + "image/png\0\0".encode("utf-16-le") + PNG
                        audio.tags["WM/Picture"] = [ASFByteArrayAttribute(picture)]
                        audio.save()
                        self.assertTrue(read_file(path)["artwork"])
                        self.assertEqual(artwork_bytes(path), (PNG, "image/png"))

    def test_empty_disc_edit_previews_queues_and_removes_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            paths = [make_audio(music, extension) for extension in ("mp3", "m4a")]
            for path in paths:
                apply_tags(path, {"disc": "2/3"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            selected = [str(path) for path in paths]
            pending = {"paths": selected, "edits": {"disc": ""}}
            direct = library.preview_queue(pending)
            self.assertEqual(len(direct["items"]), 2)
            self.assertTrue(all(item["before"]["disc"] == "2/3" and item["after"]["disc"] == ""
                                for item in direct["items"]))
            queued = library.queue_add(selected, {"disc": ""})["items"]
            self.assertEqual(len(queued), 2)
            self.assertTrue(all(item["edits"]["disc"] == "" for item in queued))
            self.assertTrue(all(read_file(path)["tags"]["disc"] == "2/3" for path in paths))
            job = library.start_commit(library.preview_queue()["id"])
            self._wait(library, job)
            self.assertEqual(library.job(job)["state"], "complete")
            self.assertTrue(all(read_file(path)["tags"]["disc"] == "" for path in paths))

    def test_legacy_ogg_coverart_is_detected_and_previewed(self):
        with tempfile.TemporaryDirectory() as temporary:
            for extension, audio_type in (("ogg", OggVorbis), ("opus", OggOpus)):
                with self.subTest(extension=extension):
                    path = make_audio(Path(temporary), extension)
                    audio = audio_type(str(path))
                    audio.tags["COVERART"] = [base64.b64encode(PNG).decode("ascii")]
                    audio.tags["COVERARTMIME"] = ["image/png"]
                    audio.tags["METADATA_BLOCK_PICTURE"] = ["invalid picture data"]
                    audio.save()
                    self.assertTrue(read_file(path)["artwork"])
                    self.assertEqual(artwork_bytes(path), (PNG, "image/png"))

    def test_usable_front_cover_wins_over_invalid_embedded_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = make_audio(Path(temporary), "mp3")
            audio = open_audio(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=0, desc="Broken", data=b"not an image"))
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front", data=PNG))
            audio.save()
            self.assertTrue(read_file(path)["artwork"])
            self.assertEqual(artwork_bytes(path), (PNG, "image/png"))
            self.assertEqual(read_file(path)["artwork_signature"], hashlib.sha256(PNG).hexdigest())

    def test_artwork_signatures_across_formats_and_unpreviewable_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            for extension in FORMATS:
                with self.subTest(extension=extension):
                    path = make_audio(Path(temporary), extension)
                    self.assertEqual(read_file(path)["artwork_signature"], "none")
                    if extension == "wma":
                        audio = ASF(str(path))
                        picture = bytes([3]) + len(PNG).to_bytes(4, "little") + "image/png\0\0".encode("utf-16-le") + PNG
                        audio.tags["WM/Picture"] = [ASFByteArrayAttribute(picture)]
                        audio.save()
                    else:
                        apply_tags(path, {}, (PNG, "image/png"))
                    self.assertEqual(read_file(path)["artwork_signature"], hashlib.sha256(PNG).hexdigest())
            path = make_audio(Path(temporary), "mp3")
            audio = open_audio(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Broken", data=b"not an image"))
            audio.save()
            self.assertTrue(read_file(path)["artwork_signature"].startswith("unpreviewable:"))

    def test_scan_refreshes_old_artwork_index_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "ogg")
            audio = OggVorbis(str(path))
            audio.tags["COVERART"] = [base64.b64encode(PNG).decode("ascii")]
            audio.save()
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            with library._db() as db:
                db.execute("UPDATE tracks SET artwork=0,artwork_indexed=0 WHERE path=?", (str(path),))
            self._wait(library, library.start_scan())
            self.assertTrue(library.track(str(path))["artwork"])
            with library._db() as db:
                self.assertEqual(db.execute("SELECT artwork_indexed FROM tracks WHERE path=?", (str(path),)).fetchone()[0], 1)

    def test_preview_commit_without_retained_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "flac")
            apply_tags(path, {"title": "Old title", "artist": "Artist", "album": "Album"})
            original = path.read_bytes()
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            self.assertEqual(library.tracks()["total"], 1)
            plan = library.preview([str(path)], {"title": "New title"}, "artist_album")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(plan["items"][0]["destination"], str(music / "Artist" / "Album" / path.name))
            with patch("tagger.library.shutil.copy2", side_effect=AssertionError("Copy attempted")):
                job_id = library.start_commit(plan["id"])
                self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "complete")
            destination = music / "Artist" / "Album" / path.name
            self.assertFalse(path.exists())
            self.assertEqual(read_file(destination)["tags"]["title"], "New title")
            self.assertFalse((base / "state" / "backups").exists())
            self.assertEqual(list(music.rglob(".tagger-*")), [])

    def test_collision_blocks_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "mp3")
            apply_tags(path, {"artist": "Artist"})
            collision = music / "Artist"
            collision.mkdir()
            shutil.copy2(path, collision / path.name)
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            with self.assertRaises(LibraryError):
                library.preview([str(path)], {}, "artist")

    def test_browse_stays_inside_approved_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            album = music / "album"
            album.mkdir(parents=True)
            library = Library(base / "state", allowed_volume=music)
            self.assertEqual(library.browse(str(music))["folders"], [str(album)])
            with self.assertRaises(LibraryError):
                library.browse(str(base))
            with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(LibraryError, "Cadence cannot browse"):
                    library.browse(str(music))
            with patch("tagger.library.os.access", return_value=False):
                with self.assertRaisesRegex(LibraryError, "Read/Write access"):
                    library.add_root(str(music))

    def test_failed_in_place_write_reports_partial_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "flac")
            original = path.read_bytes()
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            plan = library.preview([str(path)], {"title": "New title"})

            def fail_write(source, edits, artwork):
                Path(source).write_bytes(b"partial in-place write")
                raise OSError("write interrupted")

            with patch("tagger.library.apply_tags", side_effect=fail_write):
                with self.assertRaisesRegex(LibraryError, "may have been written"):
                    library._commit_one("test", plan["items"][0], {"title": "New title"}, None)

            self.assertNotEqual(path.read_bytes(), original)
            self.assertEqual(path.read_bytes(), b"partial in-place write")
            self.assertEqual(list(music.glob(".tagger-*")), [])
            self.assertFalse((base / "state" / "backups").exists())

    def test_failed_move_after_tag_write_refreshes_source_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "flac")
            apply_tags(path, {"artist": "Artist", "title": "Old title"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            plan = library.preview([str(path)], {"title": "New title"}, "artist")

            with patch("tagger.library.os.rename", side_effect=OSError("move interrupted")):
                with self.assertRaisesRegex(LibraryError, "may have been written"):
                    library._commit_one("test", plan["items"][0], {"title": "New title"}, None)

            self.assertTrue(path.exists())
            self.assertFalse((music / "Artist" / path.name).exists())
            self.assertEqual(read_file(path)["tags"]["title"], "New title")
            self.assertEqual(library.track(str(path))["tags"]["title"], "New title")

    def test_upgrade_removes_legacy_backups_and_undo_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            state.mkdir()
            backups = state / "backups"
            backups.mkdir()
            (backups / "original.mp3").write_bytes(b"old audio copy")
            with closing(sqlite3.connect(str(state / "library.sqlite3"))) as db, db:
                db.execute("CREATE TABLE history(id TEXT PRIMARY KEY, backup TEXT)")
                db.execute("INSERT INTO history VALUES (?,?)", ("old", str(backups / "original.mp3")))
            library = Library(state, allowed_volume=base)
            self.assertFalse(backups.exists())
            with library._db() as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='history'").fetchone())
            self.assertTrue(library.db_path.exists())

    def test_organization_and_renaming_without_tag_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            artist = music / "Artist"
            artist.mkdir(parents=True)
            path = make_audio(artist, "mp3")
            apply_tags(path, {"title": "Good / Song", "artist": "Artist",
                              "album": "Album", "track": "2/10"})
            original = path.read_bytes()
            original_inode = path.stat().st_ino
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            plan = library.preview([str(path)], {"artist": "Artist"}, "album_in_artist", rename="track_title")
            destination = artist / "Album" / "02 - Good _ Song.mp3"
            self.assertEqual(plan["items"][0]["destination"], str(destination))
            self.assertEqual(path.read_bytes(), original)
            with patch("tagger.library.shutil.copy2", side_effect=AssertionError("Copy attempted")), \
                    patch("tagger.library.apply_tags", side_effect=AssertionError("Unchanged tags rewritten")):
                job_id = library.start_commit(plan["id"])
                self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "complete")
            self.assertTrue(destination.exists())
            self.assertEqual(destination.stat().st_ino, original_inode)
            path.write_bytes(original)

            artist_root = Library(base / "other-state", allowed_volume=artist)
            artist_root.add_root(str(artist))
            album_plan = artist_root.preview([str(path)], {}, "album_in_artist")
            self.assertEqual(album_plan["items"][0]["destination"], str(artist / "Album" / path.name))

    def test_queued_mixed_edits_persist_and_commit_in_one_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            first_folder = music / "first"
            second_folder = music / "second"
            first_folder.mkdir(parents=True)
            second_folder.mkdir(parents=True)
            first = make_audio(first_folder, "mp3")
            second = make_audio(second_folder, "flac")
            apply_tags(first, {"title": "First", "genre": "Jazz"})
            apply_tags(second, {"title": "Second", "genre": "Rock"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(first), str(second)], {"genre": ""})
            library.queue_add([str(first)], {"title": "Queued first"})
            library.queue_add([str(second)], {}, artwork={"mime": "image/png", "data": base64.b64encode(PNG).decode("ascii")})
            reopened = Library(base / "state", allowed_volume=music)
            queued = {item["path"]: item for item in reopened.queue_items()}
            self.assertEqual(queued[str(first)]["edits"], {"genre": [], "title": "Queued first"})
            self.assertEqual(queued[str(second)]["edits"], {"genre": []})
            self.assertEqual(base64.b64decode(reopened.queue_artwork(str(second))["data"]), PNG)
            plan = reopened.preview_queue()
            self.assertEqual(len(plan["items"]), 2)
            self.assertEqual(read_file(first)["tags"]["title"], "First")
            self.assertEqual(read_file(second)["tags"]["genre"], ["Rock"])
            job_id = reopened.start_commit(plan["id"])
            self._wait(reopened, job_id)
            self.assertEqual(reopened.job(job_id)["state"], "complete", reopened.job(job_id)["errors"])
            self.assertEqual(read_file(first)["tags"]["title"], "Queued first")
            self.assertEqual(read_file(first)["tags"]["genre"], [])
            self.assertEqual(read_file(second)["tags"]["genre"], [])
            self.assertTrue(read_file(second)["artwork"])
            self.assertEqual(reopened.queue_items(), [])
            self.assertFalse((base / "state" / "backups").exists())

    def test_batch_editor_common_tags_include_queued_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            first_folder = music / "first"
            second_folder = music / "second"
            first_folder.mkdir(parents=True)
            second_folder.mkdir(parents=True)
            first = make_audio(first_folder, "mp3")
            second = make_audio(second_folder, "flac")
            apply_tags(first, {"title": "First", "artist": "Artist", "album": "Album", "genre": "Jazz"})
            apply_tags(second, {"title": "Second", "artist": "Artist", "album": "Album", "genre": "Rock"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            paths = [str(first), str(second)]

            common = library.common_tags(paths)["common"]
            self.assertEqual(common["artist"], "Artist")
            self.assertEqual(common["album"], "Album")
            self.assertEqual(common["date"], "")
            self.assertNotIn("title", common)
            self.assertNotIn("genre", common)

            library.queue_add([str(first)], {"artist": "New artist", "genre": "Rock"})
            common = library.common_tags(paths)["common"]
            self.assertNotIn("artist", common)
            self.assertEqual(common["genre"], ["Rock"])
            library.queue_add([str(second)], {"artist": "New artist"})
            self.assertEqual(library.common_tags(paths)["common"]["artist"], "New artist")
            self.assertEqual(read_file(first)["tags"]["artist"], "Artist")

            with self.assertRaisesRegex(LibraryError, "no longer indexed"):
                library.common_tags(paths + [str(music / "missing.mp3")])
            with self.assertRaisesRegex(LibraryError, "distinct"):
                library.common_tags(paths + [str(first)])

    def test_title_normalization_previews_and_queues_each_track_individually(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            folders = [music / name for name in ("first", "second", "third")]
            for folder in folders:
                folder.mkdir(parents=True)
            paths = [make_audio(folder, "mp3") for folder in folders]
            apply_tags(paths[0], {"title": "hELLO WORLD", "artist": "Artist"})
            apply_tags(paths[1], {"title": "DON'T STOP", "artist": "Artist"})
            apply_tags(paths[2], {"title": "Already Proper", "artist": "Artist"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            selected = [str(path) for path in paths]

            plan = library.preview(selected, {}, normalize_title=True)
            self.assertEqual([item["after"]["title"] for item in plan["items"]],
                             ["Hello World", "Don't Stop"])
            self.assertEqual([item["edits"]["title"] for item in plan["items"]],
                             ["Hello World", "Don't Stop"])
            self.assertEqual(len(plan["errors"]), 1)
            self.assertEqual(read_file(paths[0])["tags"]["title"], "hELLO WORLD")

            queued = library.queue_add(selected, {}, normalize_title=True)["items"]
            self.assertEqual({item["path"]: item["edits"]["title"] for item in queued},
                             {selected[0]: "Hello World", selected[1]: "Don't Stop"})
            self.assertEqual(len(library.preview_queue()["items"]), 2)

            library.queue_add([selected[0]], {"title": "aNOTHER SONG"})
            pending = {"paths": [selected[0]], "edits": {}, "mode": None, "rename": "title",
                       "normalize_title": True}
            preview = library.preview_queue(pending)
            self.assertEqual(preview["items"][0]["after"]["title"], "Another Song")
            self.assertEqual(Path(preview["items"][0]["destination"]).name, "Another Song.mp3")
            with self.assertRaisesRegex(LibraryError, "normalization"):
                library.queue_add([selected[0]], {}, normalize_title="yes")
            job_id = library.start_commit(preview["id"])
            self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "complete")
            self.assertEqual(read_file(folders[0] / "Another Song.mp3")["tags"]["title"], "Another Song")

    def test_exact_title_removal_stages_per_file_and_previews_renamed_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            folders = [music / name for name in ("first", "second", "third")]
            for folder in folders:
                folder.mkdir(parents=True)
            paths = [make_audio(folder, "mp3") for folder in folders]
            apply_tags(paths[0], {"title": "hELLO (Live) WORLD"})
            apply_tags(paths[1], {"title": "Another (Live) (Live) Song"})
            apply_tags(paths[2], {"title": "No (live) Suffix"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            selected = [str(path) for path in paths]

            plan = library.preview(selected, {}, remove_title_text=" (Live)")
            self.assertEqual([item["after"]["title"] for item in plan["items"]],
                             ["hELLO WORLD", "Another Song"])
            self.assertEqual(len(plan["errors"]), 1)
            queued = library.queue_add(selected, {}, remove_title_text=" (Live)")["items"]
            self.assertEqual({item["path"]: item["edits"]["title"] for item in queued},
                             {selected[0]: "hELLO WORLD", selected[1]: "Another Song"})

            pending = {"paths": [selected[0]], "edits": {}, "mode": None, "rename": "title",
                       "normalize_title": True, "remove_title_text": " (Live)"}
            preview = library.preview_queue(pending)
            self.assertEqual(preview["items"][0]["after"]["title"], "Hello World")
            self.assertEqual(Path(preview["items"][0]["destination"]).name, "Hello World.mp3")
            with self.assertRaisesRegex(LibraryError, "title text"):
                library.queue_add([selected[0]], {}, remove_title_text=42)
            job_id = library.start_commit(preview["id"])
            self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "complete")
            self.assertEqual(read_file(folders[0] / "Hello World.mp3")["tags"]["title"], "Hello World")

    def test_queued_folder_and_filename_changes_without_tag_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            nested = music / "incoming"
            nested.mkdir(parents=True)
            source = make_audio(nested, "mp3")
            apply_tags(source, {"title": "Song", "artist": "Artist", "album": "Album", "track": "3", "date": "1999"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(source)], {}, mode="artist_album", rename="artist_album_track_title")
            plan = library.preview_queue()
            destination = music / "Artist" / "Album (1999)" / "Artist - Album - 03 - Song.mp3"
            self.assertEqual(plan["items"][0]["destination"], str(destination))
            self.assertTrue(source.exists())
            job_id = library.start_commit(plan["id"])
            self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "complete")
            self.assertTrue(destination.exists())
            self.assertEqual(library.queue_items(), [])

    def test_queue_hides_running_commit_and_restores_skipped_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            first_folder = music / "first"
            second_folder = music / "second"
            first_folder.mkdir(parents=True)
            second_folder.mkdir(parents=True)
            first = make_audio(first_folder, "mp3")
            second = make_audio(second_folder, "mp3")
            apply_tags(first, {"title": "First"})
            apply_tags(second, {"title": "Second"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(first)], {"title": "Updated"})
            library.queue_add([str(second)], {"title": "Second"})
            plan = library.preview_queue()
            self.assertEqual(len(plan["items"]), 1)
            self.assertEqual(len(plan["errors"]), 1)
            started = Event()
            release = Event()
            commit_one = library._commit_one

            def delayed_commit(*arguments):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Timed out waiting to finish the test commit")
                return commit_one(*arguments)

            with patch.object(library, "_commit_one", side_effect=delayed_commit):
                try:
                    job_id = library.start_commit(plan["id"])
                    self.assertTrue(started.wait(5))
                    self.assertEqual(library.queue_items(), [])
                    self.assertEqual(library.queue_clear()["items"], [])
                finally:
                    release.set()
                self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "partial")
            self.assertEqual(read_file(first)["tags"]["title"], "Updated")
            self.assertEqual([item["path"] for item in library.queue_items()], [str(second)])
            details = library.job_details(job_id)
            self.assertEqual(details["remaining"], 0)
            self.assertEqual([item["state"] for item in details["items"]], ["complete", "skipped"])
            self.assertEqual(library.job_details(job_id, offset=1, limit=1)["items"][0]["state"], "skipped")

    def test_second_commit_waits_behind_running_job_and_records_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            first_folder = music / "first"
            second_folder = music / "second"
            first_folder.mkdir(parents=True)
            second_folder.mkdir(parents=True)
            first = make_audio(first_folder, "mp3")
            second = make_audio(second_folder, "mp3")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            first_plan = library.preview([str(first)], {"title": "First edit"})
            started = Event()
            release = Event()
            commit_one = library._commit_one

            def delayed_first(*arguments):
                if arguments[1]["source"] == str(first):
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("Timed out waiting for the first commit")
                return commit_one(*arguments)

            with patch.object(library, "_commit_one", side_effect=delayed_first):
                try:
                    first_job = library.start_commit(first_plan["id"])
                    self.assertTrue(started.wait(5))
                    with self.assertRaisesRegex(LibraryError, "Wait for queued or running jobs"):
                        library.remove_root(str(music))
                    with self.assertRaisesRegex(LibraryError, "already in a queued or running job"):
                        library.queue_add([str(first)], {"artist": "Busy"})
                    library.queue_add([str(second)], {"title": "Second edit"})
                    second_plan = library.preview_queue()
                    second_job = library.start_commit(second_plan["id"])
                    self.assertEqual(library.job(second_job)["state"], "queued")
                    self.assertIsNone(library.job(second_job)["started_at"])
                    self.assertIsNone(library.job(second_job)["stopped_at"])
                    self.assertEqual(library.job_details(second_job)["remaining"], 1)
                    self.assertEqual(library.queue_items(), [])
                finally:
                    release.set()
                self._wait(library, first_job)
                self._wait(library, second_job)
            self.assertEqual(library.job(first_job)["state"], "complete")
            self.assertEqual(library.job(second_job)["state"], "complete")
            self.assertEqual(read_file(first)["tags"]["title"], "First edit")
            self.assertEqual(read_file(second)["tags"]["title"], "Second edit")
            details = library.job_details(second_job)
            self.assertEqual(details["remaining"], 0)
            self.assertEqual(details["items"][0]["state"], "complete")
            self.assertLessEqual(details["job"]["created"], details["job"]["started_at"])
            self.assertLessEqual(details["job"]["started_at"], details["items"][0]["completed_at"])
            self.assertLessEqual(details["items"][0]["completed_at"], details["job"]["stopped_at"])
            self.assertEqual(json.loads(details["items"][0]["after_tags"])["title"], "Second edit")
            self.assertEqual(library.clear_completed_jobs(), 3)
            self.assertEqual(library.jobs(), [])

    def test_failed_commit_returns_queued_change_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            source = make_audio(music, "mp3")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(source)], {"title": "New title"})
            plan = library.preview_queue()
            with patch.object(library, "_commit_one", side_effect=OSError("write failed")):
                job_id = library.start_commit(plan["id"])
                self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "partial")
            self.assertEqual(library.queue_items()[0]["edits"], {"title": "New title"})
            details = library.job_details(job_id)
            self.assertEqual(details["items"][0]["state"], "failed")
            self.assertIsNone(details["items"][0]["completed_at"])
            self.assertLessEqual(details["job"]["started_at"], details["job"]["stopped_at"])
            self.assertIn("write failed", details["items"][0]["error"])
            self.assertEqual(library.clear_completed_jobs(), 1)
            self.assertEqual(library.job(job_id)["state"], "partial")
            library.clear_job(job_id)
            with self.assertRaisesRegex(LibraryError, "Job not found"):
                library.job(job_id)
            self.assertEqual(library.queue_items()[0]["edits"], {"title": "New title"})

    def test_clearing_waiting_job_returns_its_edits_to_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            first_folder = music / "first"
            second_folder = music / "second"
            first_folder.mkdir(parents=True)
            second_folder.mkdir(parents=True)
            first = make_audio(first_folder, "mp3")
            second = make_audio(second_folder, "mp3")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(first)], {"title": "First edit"})
            first_plan = library.preview_queue()
            started = Event()
            release = Event()
            commit_one = library._commit_one

            def delayed_first(*arguments):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Timed out waiting for the first commit")
                return commit_one(*arguments)

            with patch.object(library, "_commit_one", side_effect=delayed_first):
                try:
                    first_job = library.start_commit(first_plan["id"])
                    self.assertTrue(started.wait(5))
                    library.queue_add([str(second)], {"title": "Second edit"})
                    second_job = library.start_commit(library.preview_queue()["id"])
                    self.assertEqual(library.job(second_job)["state"], "queued")
                    with self.assertRaisesRegex(LibraryError, "Cancel or wait"):
                        library.clear_job(first_job)
                    library.clear_job(second_job)
                    self.assertEqual([item["path"] for item in library.queue_items()], [str(second)])
                    with self.assertRaisesRegex(LibraryError, "Job not found"):
                        library.job(second_job)
                finally:
                    release.set()
                self._wait(library, first_job)

    def test_waiting_commit_survives_library_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            source = make_audio(music, "mp3")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(source)], {"title": "After restart"})
            with patch.object(library, "_launch_next_job"):
                job_id = library.start_commit(library.preview_queue()["id"])
            self.assertEqual(library.job(job_id)["state"], "queued")
            self.assertEqual(library.queue_items(), [])
            setup_library = Library(base / "state", allowed_volume=music, resume_jobs=False)
            self.assertEqual(setup_library.job(job_id)["state"], "queued")
            reopened = Library(base / "state", allowed_volume=music)
            self._wait(reopened, job_id)
            self.assertEqual(reopened.job(job_id)["state"], "complete")
            self.assertEqual(read_file(source)["tags"]["title"], "After restart")
            self.assertEqual(reopened.job_details(job_id)["items"][0]["state"], "complete")

    def test_cancelling_waiting_job_returns_staged_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            source = make_audio(music, "mp3")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(source)], {"title": "Pending"})
            with patch.object(library, "_launch_next_job"):
                job_id = library.start_commit(library.preview_queue()["id"])
                library.cancel_job(job_id)
            self.assertEqual(library.job(job_id)["state"], "cancelled")
            self.assertIsNone(library.job(job_id)["started_at"])
            self.assertIsNotNone(library.job(job_id)["stopped_at"])
            self.assertEqual(library.job_details(job_id)["items"][0]["state"], "cancelled")
            self.assertEqual(library.queue_items()[0]["edits"], {"title": "Pending"})
            self.assertEqual(library.clear_completed_jobs(), 1)
            self.assertEqual(library.job(job_id)["state"], "cancelled")

    def test_scan_errors_stay_visible_until_cleared_individually(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            broken = music / "broken.mp3"
            broken.write_bytes(b"not an audio file")
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            job_id = library.start_scan()
            self._wait(library, job_id)
            self.assertEqual(library.job(job_id)["state"], "partial")
            details = library.job_details(job_id)
            self.assertLessEqual(details["job"]["started_at"], details["job"]["stopped_at"])
            self.assertEqual(details["items"][0]["source"], str(broken))
            self.assertEqual(details["items"][0]["state"], "failed")
            self.assertTrue(details["items"][0]["error"])
            self.assertEqual(library.clear_completed_jobs(), 0)
            library.clear_job(job_id)
            self.assertEqual(library.jobs(), [])

    def test_adding_folder_starts_scan_and_manual_rescan_remains_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            first = make_audio(music, "mp3")
            library = Library(base / "state", allowed_volume=music)

            result = library.add_root_and_scan(str(music))
            self.assertEqual(result["path"], str(music))
            self.assertEqual(library.job(result["job"])["kind"], "scan")
            self._wait(library, result["job"])
            self.assertEqual(library.tracks()["items"][0]["path"], str(first))

            second_folder = music / "new album"
            second_folder.mkdir()
            second = make_audio(second_folder, "flac")
            self._wait(library, library.start_scan())
            self.assertEqual({item["path"] for item in library.tracks()["items"]},
                             {str(first), str(second)})

    def test_batched_scan_updates_and_removes_tracks_across_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            original = make_audio(music, "mp3")
            copies = [music / "track-{:02d}.mp3".format(index) for index in range(60)]
            for copy in copies:
                shutil.copy2(original, copy)
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            self._wait(library, library.start_scan())
            self.assertEqual(library.tracks()["total"], 61)

            copies[0].unlink()
            apply_tags(copies[-1], {"title": "Updated"})
            self._wait(library, library.start_scan())
            self.assertEqual(library.tracks()["total"], 60)
            self.assertEqual(library.track(str(copies[-1]))["tags"]["title"], "Updated")
            self.assertNotIn(str(copies[0]), {item["path"] for item in library.tracks()["items"]})
            with library._db() as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_save_preview_merges_queued_values_and_stale_queue_cannot_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "mp3")
            apply_tags(path, {"title": "Original", "artist": "Old artist"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            library.queue_add([str(path)], {"title": "Queued title"})
            plan = library.preview_queue({"paths": [str(path)], "edits": {"artist": "New artist"},
                                          "mode": None, "rename": None, "artwork": None})
            self.assertEqual(plan["items"][0]["after"]["title"], "Queued title")
            self.assertEqual(plan["items"][0]["after"]["artist"], "New artist")
            self.assertEqual(read_file(path)["tags"]["title"], "Original")
            old_revision = library.queue_items()[0]["revision"]
            library.queue_add([str(path)], {"title": "Changed again"})
            with self.assertRaisesRegex(LibraryError, "queue changed"):
                library.start_commit(plan["id"])
            with self.assertRaisesRegex(LibraryError, "queue changed"):
                library.queue_add([str(path)], {"artist": "Outdated"}, revisions={str(path): old_revision})
            library.queue_remove(str(path))
            self.assertEqual(library.queue_items(), [])
            library.queue_add([str(path)], {"title": "Original"})
            unchanged = library.preview_queue()
            self.assertEqual(unchanged["items"], [])
            self.assertEqual(len(unchanged["errors"]), 1)
            library.queue_clear()
            self.assertEqual(library.queue_items(), [])

    def test_nested_organization_and_artist_album_track_filename_use_library_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            nested = music / "Artist" / "Old album" / "Extras"
            nested.mkdir(parents=True)
            path = make_audio(nested, "mp3")
            apply_tags(path, {"title": "A Song", "artist": "Artist", "album": "New album",
                              "track": "3/12", "date": "1999-06-01"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            plan = library.preview([str(path)], {}, "album_in_artist", rename="artist_album_track_title")
            self.assertEqual(plan["items"][0]["destination"],
                             str(music / "Artist" / "New album (1999)" / "Artist - New album - 03 - A Song.mp3"))
            self.assertTrue(path.exists())

    def test_album_folder_uses_staged_year_without_duplicating_existing_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "mp3")
            apply_tags(path, {"artist": "Artist", "album": "Example Album Name"})
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            plan = library.preview([str(path)], {"date": "1999"}, "artist_album")
            self.assertEqual(plan["items"][0]["destination"],
                             str(music / "Artist" / "Example Album Name (1999)" / path.name))
            plan = library.preview([str(path)], {"album": "Example Album Name (1999)", "date": "1999"},
                                   "artist_album")
            self.assertEqual(plan["items"][0]["destination"],
                             str(music / "Artist" / "Example Album Name (1999)" / path.name))
            plan = library.preview([str(path)], {"date": "unknown"}, "artist_album")
            self.assertEqual(plan["items"][0]["destination"],
                             str(music / "Artist" / "Example Album Name" / path.name))

    def test_relative_paths_sort_across_library_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            volume = base / "volume"
            first_root = volume / "a-root"
            second_root = volume / "z-root"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            first = make_audio(first_root, "mp3")
            second = make_audio(second_root, "flac")
            first_renamed = first_root / "z-track.mp3"
            second_renamed = second_root / "a-track.flac"
            first.rename(first_renamed)
            second.rename(second_renamed)
            library = Library(base / "state", allowed_volume=volume)
            library.add_root(str(first_root))
            library.add_root(str(second_root))
            scan_id = library.start_scan()
            self._wait(library, scan_id)
            items = library.tracks(sort="path")["items"]
            self.assertEqual([item["relative_path"] for item in items], ["a-track.flac", "z-track.mp3"])
            self.assertEqual([item["root"] for item in items], [str(second_root), str(first_root)])
            self.assertEqual([item["relative_path"] for item in library.tracks(sort="path", direction="desc")["items"]],
                             ["z-track.mp3", "a-track.flac"])

    def test_track_paths_selects_filtered_results_across_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            volume = base / "volume"
            first_root = volume / "first"
            second_root = volume / "second"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            library = Library(base / "state", allowed_volume=volume)
            library.add_root(str(first_root))
            library.add_root(str(second_root))

            def row(root, index, title):
                path = root / "track-{:04d}.mp3".format(index)
                return (str(path), str(root), "MP3", json.dumps({"title": title}), 1, 0,
                        100, 1, 1, title.casefold(), "", "", path.name.casefold())

            with library._db() as db:
                db.executemany("""INSERT INTO tracks
                    (path,root,format,tags,duration,artwork,size,mtime_ns,seen,sort_title,sort_artist,sort_album,sort_path)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [row(first_root, index, "Keep") for index in range(125)] +
                    [row(first_root, index, "Skip") for index in range(125, 135)] +
                    [row(second_root, index, "Keep") for index in range(5)])

            selected = library.track_paths(query="Keep", root=str(first_root))["paths"]
            pages = library.tracks(query="Keep", root=str(first_root), limit=100)["items"] + \
                library.tracks(query="Keep", root=str(first_root), offset=100, limit=100)["items"]
            self.assertEqual(selected, [item["path"] for item in pages])
            self.assertEqual(len(selected), 125)
            self.assertEqual(len(library.track_paths(query="Keep")["paths"]), 130)
            self.assertEqual(len(library.track_paths(root=str(second_root))["paths"]), 5)
            self.assertEqual(library.track_paths(query="Keep", root=str(first_root), direction="desc")["paths"],
                             selected[::-1])

            with library._db() as db:
                db.executemany("""INSERT INTO tracks
                    (path,root,format,tags,duration,artwork,size,mtime_ns,seen,sort_title,sort_artist,sort_album,sort_path)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row(first_root, index, "Keep") for index in range(135, 5011)))
            with self.assertRaisesRegex(LibraryError, "5,000-track batch limit"):
                library.track_paths(query="Keep")

    def test_library_page_size_supports_one_thousand_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            library = Library(base / "state", allowed_volume=music)
            library.add_root(str(music))
            with library._db() as db:
                db.executemany("""INSERT INTO tracks
                    (path,root,format,tags,duration,artwork,size,mtime_ns,seen,sort_title,sort_artist,sort_album,sort_path)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ((str(music / "track-{:04d}.mp3".format(index)), str(music), "MP3", "{}", 1, 0,
                      100, 1, 1, "", "", "", "track-{:04d}.mp3".format(index))
                     for index in range(1005)))
            self.assertEqual(len(library.tracks()["items"]), 100)
            self.assertEqual(len(library.tracks(limit=250)["items"]), 250)
            self.assertEqual(len(library.tracks(limit=500)["items"]), 500)
            self.assertEqual(len(library.tracks(limit=1000)["items"]), 1000)
            self.assertEqual(len(library.tracks(limit=5000)["items"]), 1000)
            self.assertEqual(len(library.tracks(offset=1000, limit=1000)["items"]), 5)

    def test_sorting_across_pages_and_legacy_index_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            fixtures = [
                ("A", "mp3", 0.4, "Zulu", "Beta", "Two"),
                ("B", "flac", 1.4, "Alpha", "Zulu", "One"),
                ("C", "ogg", 2.4, "Mike", "Alpha", "Three"),
            ]
            for name, extension, duration, title, artist, album in fixtures:
                directory = music / name
                directory.mkdir()
                path = make_audio(directory, extension, duration)
                apply_tags(path, {"title": title, "artist": artist, "album": album})
            state = base / "state"
            state.mkdir()
            with closing(sqlite3.connect(str(state / "library.sqlite3"))) as db, db:
                db.execute("""CREATE TABLE tracks(path TEXT PRIMARY KEY, root TEXT NOT NULL, format TEXT NOT NULL,
                    tags TEXT NOT NULL, duration INTEGER NOT NULL, artwork INTEGER NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, seen INTEGER NOT NULL)""")
                for name, extension, duration, title, artist, album in fixtures:
                    path = music / name / ("track." + extension)
                    stat = path.stat()
                    db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?)",
                               (str(path), str(music), extension, json.dumps({"title": title, "artist": artist,
                                                                               "album": album}), round(duration),
                                0, stat.st_size, stat.st_mtime_ns, 1))
            library = Library(state, allowed_volume=music)
            expected = {"path": "ABC", "title": "BCA", "artist": "CAB", "album": "BCA",
                        "format": "BAC", "duration": "ABC"}
            for key, order in expected.items():
                with self.subTest(sort=key):
                    first = library.tracks(sort=key, limit=2)["items"]
                    last = library.tracks(sort=key, offset=2, limit=2)["items"]
                    ascending = "".join(Path(item["path"]).parent.name for item in first + last)
                    descending = "".join(Path(item["path"]).parent.name for item in
                                         library.tracks(sort=key, direction="desc")["items"])
                    self.assertEqual(ascending, order)
                    self.assertEqual(descending, order[::-1])

    def test_embedded_artwork_is_served_for_library_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / "music"
            music.mkdir()
            path = make_audio(music, "mp3")
            apply_tags(path, {"title": "Covered"}, (PNG, "image/png"))
            application = Application(base / "state", allowed_volume=music)
            application.set_password("a-long-test-password")
            application.library.add_root(str(music))
            scan_id = application.library.start_scan()
            self._wait(application.library, scan_id)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            except PermissionError:
                self.skipTest("Loopback sockets are unavailable")
            server.app = application
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("POST", "/api/login", json.dumps({"password": "a-long-test-password"}),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()
                self.assertEqual(response.status, 200)
                from urllib.parse import urlencode
                connection.request("GET", "/api/art/image?" + urlencode({"path": str(path)}),
                                   headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/png")
                self.assertEqual(response.read(), PNG)
                with application.library._db() as db:
                    db.execute("UPDATE tracks SET artwork=0 WHERE path=?", (str(path),))
                connection.request("GET", "/api/art/image?" + urlencode({"path": str(path)}),
                                   headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), PNG)
                self.assertTrue(application.library.track(str(path))["artwork"])
                with application.library._db() as db:
                    db.execute("UPDATE tracks SET artwork=0 WHERE path=?", (str(path),))
                connection.request("GET", "/api/art?" + urlencode({"path": str(path)}),
                                   headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(base64.b64decode(json.loads(response.read())["data"]), PNG)
                self.assertTrue(application.library.track(str(path))["artwork"])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    @staticmethod
    def _wait(library, job_id):
        for _ in range(200):
            if library.job(job_id)["state"] not in ("queued", "running", "cancelling"):
                return
            time.sleep(0.025)
        raise AssertionError("Job did not finish")


if __name__ == "__main__":
    unittest.main()
