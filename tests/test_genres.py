import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from mutagen.id3 import TCON, TXXX

from tagger.formats import apply_tags, open_audio, read_file
from tagger.genres import apply_genre_options, clean_genres, genre_needs_review, validate_genre_options
from tagger.library import Library, LibraryError
from tagger.server import Handler
import test_app
from test_app import FORMATS, make_audio


class GenreOptionTests(unittest.TestCase):
    def test_cleanup_is_explicit_and_keeps_compound_genres(self):
        self.assertEqual(clean_genres([" Rock ", "rock", "Pop", ""]), ["Rock", "Pop"])
        self.assertEqual(clean_genres(["Rock, Pop", "R&B/Soul"]), ["Rock, Pop", "R&B/Soul"])
        self.assertEqual(clean_genres(["Rock, Pop; rock", "R&B/Soul"], split=True), ["Rock", "Pop", "R&B/Soul"])
        self.assertEqual(apply_genre_options(["Rock, Pop", " Rock "], {}, {}), {})
        self.assertEqual(apply_genre_options(["Rock, Pop"], {}, {"split": True}), {"genre": ["Rock", "Pop"]})
        self.assertTrue(genre_needs_review(["Rock, Pop"]))
        self.assertTrue(genre_needs_review(["Rock", "rock"]))
        self.assertTrue(genre_needs_review(["Unknown"]))
        self.assertFalse(genre_needs_review(["Rock", "Pop", "R&B/Soul"]))

    def test_operations_keep_unrelated_tags_and_use_staged_genres(self):
        self.assertEqual(apply_genre_options(["Rock"], {"title": " Song "}, {"mode": "add", "values": ["Jazz"]}),
                         {"title": "Song", "genre": ["Rock", "Jazz"]})
        self.assertEqual(apply_genre_options(["Rock"], {"genre": ["Jazz"]}, {"mode": "add", "values": ["Pop"]}),
                         {"genre": ["Jazz", "Pop"]})
        self.assertEqual(apply_genre_options(["Rock", "Pop"], {}, {"mode": "remove", "values": ["rock"]}),
                         {"genre": ["Pop"]})
        self.assertEqual(apply_genre_options(["Rock"], {}, {"mode": "clear"}), {"genre": []})
        self.assertEqual(apply_genre_options(["Rock"], {}, {"mode": "replace", "values": ["Jazz"]}), {"genre": ["Jazz"]})

    def test_add_and_remove_noops_preserve_existing_genres(self):
        original = [" Rock ", "rock", "Pop"]
        self.assertEqual(apply_genre_options(original, {}, {"mode": "add", "values": ["ROCK"]}), {})
        self.assertEqual(apply_genre_options(original, {}, {"mode": "remove", "values": ["Jazz"]}), {})
        self.assertEqual(apply_genre_options(original, {}, {"mode": "add", "values": ["Jazz"]}),
                         {"genre": [" Rock ", "rock", "Pop", "Jazz"]})
        self.assertEqual(apply_genre_options(original, {}, {"mode": "remove", "values": ["Pop", "Jazz"]}),
                         {"genre": [" Rock ", "rock"]})
        self.assertEqual(apply_genre_options(["Rock\x00"], {}, {"mode": "remove", "values": ["Jazz"]}), {})

    def test_validation(self):
        for options in ([], {"mode": "guess"}, {"split": "yes"}, {"upgrade": 1}, {"values": [42]},
                        {"values": ["Rock\x00Pop"]}, {"values": ["x"] * 101}, {"unknown": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_genre_options(options)
        validate_genre_options({"mode": "replace", "values": ["Rock", "Folk"]})
        with self.assertRaisesRegex(ValueError, "Genre 2 contains a hidden control character"):
            validate_genre_options({"values": ["Rock", "Folk\n"]})


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed for audio fixtures")
class GenreLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.music = self.base / "music"
        self.music.mkdir()
        self.library = Library(self.base / "state", allowed_volume=self.music)
        self.library.add_root(str(self.music))

    def track(self, extension, genres, folder=""):
        directory = self.music / folder
        directory.mkdir(exist_ok=True)
        path = make_audio(directory, extension)
        apply_tags(path, {"title": "Song", "genre": genres})
        return path

    def scan(self):
        test_app.FormatTests._wait(self.library, self.library.start_scan())

    def commit(self, plan):
        job = self.library.start_commit(plan["id"])
        test_app.FormatTests._wait(self.library, job)
        self.assertEqual(self.library.job(job)["state"], "complete", self.library.job(job)["errors"])
        return job

    def test_native_multivalue_roundtrip_and_unrelated_edits_all_formats(self):
        for extension in FORMATS:
            with self.subTest(extension=extension):
                path = self.track(extension, ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                audio = open_audio(path)
                if extension in ("mp3", "wav", "aiff"):
                    self.assertEqual(audio.tags["TCON"].text, ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                elif extension == "m4a":
                    self.assertEqual(audio.tags["\xa9gen"], ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                elif extension == "wma":
                    self.assertEqual([str(value) for value in audio.tags["WM/Genre"]], ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                else:
                    self.assertEqual(audio.tags["GENRE"], ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                apply_tags(path, {"title": "Unrelated edit"})
                self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop", "R&B/Soul", "Rock, Pop"])
                apply_tags(path, {"genre": []})
                self.assertEqual(read_file(path)["tags"]["genre"], [])

    def test_mixed_batch_add_remove_and_clear_persist_and_commit(self):
        first = self.track("mp3", ["Rock", "Pop"])
        second = self.track("flac", ["Jazz"])
        self.scan()
        paths = [str(first), str(second)]
        self.assertNotIn("genre", self.library.common_tags(paths)["common"])
        self.library.queue_add(paths, {}, genre_options={"mode": "add", "values": ["Ambient", "rock"]})
        self.library.queue_add(paths, {}, genre_options={"mode": "remove", "values": ["POP"]})
        reopened = Library(self.base / "state", allowed_volume=self.music)
        queued = {item["path"]: item["edits"]["genre"] for item in reopened.queue_items()}
        self.assertEqual(queued, {str(first): ["Rock", "Ambient"], str(second): ["Jazz", "Ambient", "rock"]})
        self.assertEqual(read_file(first)["tags"]["genre"], ["Rock", "Pop"])
        plan = self.library.preview_queue()
        job = self.commit(plan)
        self.assertEqual(read_file(first)["tags"]["genre"], ["Rock", "Ambient"])
        self.assertEqual(read_file(second)["tags"]["genre"], ["Jazz", "Ambient", "rock"])
        details = self.library.job_details(job)
        self.assertIsInstance(json.loads(details["items"][0]["after_tags"])["genre"], list)
        self.assertEqual(self.library.queue_items(), [])
        cleared = self.library.preview_queue({"paths": paths, "genre_options": {"mode": "clear"}})
        self.commit(cleared)
        self.assertEqual(read_file(first)["tags"]["genre"], [])
        self.assertEqual(read_file(second)["tags"]["genre"], [])

    def test_add_remove_noops_are_silent_for_single_and_batch_edits(self):
        first = self.track("flac", ["Rock"])
        second = self.track("ogg", ["Jazz"])
        self.scan()
        first_path, second_path = str(first), str(second)
        for mode, value in (("add", "rock"), ("remove", "Blues")):
            with self.subTest(mode=mode):
                options = {"mode": mode, "values": [value]}
                self.assertEqual(self.library.preview([first_path], {}, genre_options=options),
                                 {"id": None, "items": [], "errors": []})
                self.assertEqual(self.library.preview_queue({"paths": [first_path], "genre_options": options}),
                                 {"id": None, "items": [], "errors": []})
                self.assertEqual(self.library.queue_add([first_path], {}, genre_options=options)["items"], [])
        plan = self.library.preview_queue({"paths": [first_path, second_path],
                                           "genre_options": {"mode": "add", "values": ["Rock"]}})
        self.assertEqual(plan["errors"], [])
        self.assertEqual([item["source"] for item in plan["items"]], [second_path])
        self.commit(plan)
        self.assertEqual(read_file(first)["tags"]["genre"], ["Rock"])
        self.assertEqual(read_file(second)["tags"]["genre"], ["Jazz", "Rock"])
        plan = self.library.preview_queue({"paths": [first_path, second_path],
                                           "genre_options": {"mode": "remove", "values": ["Jazz"]}})
        self.assertEqual(plan["errors"], [])
        self.assertEqual([item["source"] for item in plan["items"]], [second_path])
        self.commit(plan)
        self.assertEqual(read_file(first)["tags"]["genre"], ["Rock"])
        self.assertEqual(read_file(second)["tags"]["genre"], ["Rock"])

    def test_cleanup_queue_and_save_preview_are_identical_and_preserve_others(self):
        path = self.track("flac", ["Rock, Pop; rock", "R&B/Soul"])
        self.scan()
        pending = {"paths": [str(path)], "edits": {"comment": "Retained"}, "genre_options": {"split": True}}
        direct = self.library.preview_queue(pending)
        self.library.queue_add([str(path)], {"comment": "Retained"}, genre_options={"split": True})
        queued = self.library.preview_queue()
        self.assertEqual(direct["items"][0]["after"], queued["items"][0]["after"])
        self.assertEqual(queued["items"][0]["after"]["genre"], ["Rock", "Pop", "R&B/Soul"])
        self.commit(queued)
        self.assertEqual(read_file(path)["tags"]["comment"], "Retained")
        self.assertEqual(self.library.genres()["genres"], ["Pop", "R&B/Soul", "Rock"])

    def test_id3_upgrade_is_explicit_previewed_and_preserves_other_tags(self):
        for extension in ("mp3", "wav", "aiff"):
            with self.subTest(extension=extension):
                self.library.set_setting("auto_id3_upgrade", "true")
                path = self.track(extension, ["Rock"])
                audio = open_audio(path)
                audio.tags.add(TXXX(encoding=3, desc="Custom", text=["Preserve me"]))
                audio.save(v2_version=3)
                self.scan()
                self.assertEqual(read_file(path)["id3_version"], 3)
                with self.assertRaisesRegex(ValueError, "permission"):
                    apply_tags(path, {"genre": ["Rock", "Pop"]})
                automatic = self.library.preview_queue({"paths": [str(path)], "edits": {"genre": ["Rock", "Pop"]}})
                self.assertTrue(automatic["items"][0]["id3_upgrade"])
                self.library.set_setting("auto_id3_upgrade", "false")
                blocked = self.library.preview_queue({"paths": [str(path)], "edits": {"genre": ["Rock", "Pop"]}})
                self.assertEqual(blocked["items"], [])
                self.assertIn("ID3v2.4", blocked["errors"][0]["error"])
                self.assertEqual(read_file(path)["id3_version"], 3)
                self.library.queue_add([str(path)], {"genre": ["Rock", "Pop"]}, genre_options={"upgrade": True})
                plan = self.library.preview_queue()
                self.assertTrue(plan["items"][0]["id3_upgrade"])
                self.assertEqual(plan["items"][0]["id3_version_before"], 3)
                self.commit(plan)
                self.assertEqual(read_file(path)["id3_version"], 4)
                self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop"])
                self.assertEqual(open_audio(path).tags["TXXX:Custom"].text, ["Preserve me"])

    def test_automatic_upgrade_applies_to_existing_queue_at_preview_time(self):
        path = self.track("mp3", ["Rock"])
        audio = open_audio(path)
        audio.save(v2_version=3)
        self.scan()
        self.library.queue_add([str(path)], {"genre": ["Rock", "Folk"]})
        self.assertTrue(self.library.preview_queue()["items"][0]["id3_upgrade"])
        self.library.set_setting("auto_id3_upgrade", "false")
        blocked = self.library.preview_queue()
        self.assertEqual(blocked["items"], [])
        self.assertIn("Settings", blocked["errors"][0]["error"])
        self.library.set_setting("auto_id3_upgrade", "true")
        plan = self.library.preview_queue()
        self.assertTrue(plan["items"][0]["id3_upgrade"])
        self.commit(plan)
        self.assertEqual(read_file(path)["id3_version"], 4)
        self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Folk"])

    def test_id3_numeric_genres_are_decoded_and_v23_kept_when_untouched(self):
        for extension in ("mp3", "wav", "aiff"):
            path = self.track(extension, [])
            audio = open_audio(path)
            audio.tags.add(TCON(encoding=1, text=["(17)(13)"]))
            audio.save(v2_version=3)
            self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop"])
            apply_tags(path, {"title": "Changed"})
            self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop"])
            self.assertEqual(read_file(path)["id3_version"], 3)

    def test_id3_parentheses_roundtrip_and_reserved_codes_are_not_silently_reinterpreted(self):
        path = self.track("mp3", ["(17)", "(Custom)"])
        self.assertEqual(read_file(path)["tags"]["genre"], ["(17)", "(Custom)"])
        apply_tags(path, {"title": "Other edit"})
        self.assertEqual(read_file(path)["tags"]["genre"], ["(17)", "(Custom)"])
        self.scan()
        for value in ("17", "RX", "CR"):
            with self.assertRaisesRegex(LibraryError, "reserves"):
                self.library.preview([str(path)], {"genre": [value]})

    def test_filters_suggestions_and_lazy_upgrade_refresh(self):
        first = self.track("mp3", ["Rock", "Pop"])
        missing = self.track("flac", [])
        review = self.track("ogg", ["Rock, Pop"])
        self.scan()
        self.assertEqual([item["path"] for item in self.library.tracks(track_filter="missing_genre")["items"]], [str(missing)])
        self.assertEqual(self.library.track_paths(track_filter="genre_review")["paths"], [str(review)])
        self.assertEqual(self.library.genres("rock")["genres"], ["Rock", "Rock, Pop"])
        self.assertEqual(self.library.genres("%")["genres"], [])
        with self.library._db() as db:
            old_tags = read_file(first)["tags"]
            old_tags["genre"] = "Rock"
            db.execute("UPDATE tracks SET tags=?,genre_indexed=0 WHERE path=?", (json.dumps(old_tags), str(first)))
        self.assertTrue(self.library.tracks()["genres_need_scan"])
        self.assertEqual(self.library.common_tags([str(first)])["common"]["genre"], ["Rock", "Pop"])
        self.assertFalse(self.library.tracks()["genres_need_scan"])
        self.library.queue_add([str(first)], {}, genre_options={"mode": "replace", "values": ["Jazz"]})
        self.assertEqual(self.library.common_tags([str(first)])["common"]["genre"], ["Jazz"])

    def test_api_routes_accept_options_and_reject_malformed_edits(self):
        path = self.track("flac", ["Rock"])
        self.scan()
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        handler._session = lambda modify=False: None
        handler._send_json = lambda value, status=200: (status, value)
        for route in ("/api/preview", "/api/queue/add"):
            handler.path = route
            handler._body = lambda: {"paths": [str(path)], "edits": {}, "genre_options": {"mode": "add", "values": ["Jazz"]}}
            self.assertEqual(handler._handle("POST")[0], 200)
        handler.path = "/api/genres?q=roc"
        self.assertEqual(handler._handle("GET"), (200, {"genres": ["Rock"]}))
        for genre in (42, None, [False], ["bad\x00genre"]):
            with self.assertRaises(LibraryError):
                self.library.queue_add([str(path)], {"genre": genre})

    def test_replacing_queued_genres_with_two_chips_uses_genre_options(self):
        path = self.track("flac", ["Jazz"])
        self.scan()
        self.library.queue_add([str(path)], {"genre": ["Blues"]})
        self.library.queue_add([str(path)], {}, genre_options={"mode": "replace", "values": ["Rock", "Folk"]})
        self.assertEqual(self.library.queue_items()[0]["edits"]["genre"], ["Rock", "Folk"])
        self.assertEqual(self.library.common_tags([str(path)])["common"]["genre"], ["Rock", "Folk"])

    def test_genre_search_uses_saved_tags_and_tracks_index_updates(self):
        first = self.track("flac", ["Rock", "Folk"], folder="first")
        second = self.track("ogg", ["rock", "Jazz"], folder="second")
        self.track("flac", ["Rockabilly"], folder="third")
        other_root = self.music / "other"
        other_root.mkdir()
        other = make_audio(other_root, "mp3")
        apply_tags(other, {"genre": ["Rock"]})
        self.library.add_root(str(other_root))
        self.scan()
        choices = self.library.genre_choices(str(self.music))["genres"]
        self.assertEqual([(item["genre_key"], item["count"]) for item in choices],
                         [("folk", 1), ("jazz", 1), ("rock", 2), ("rockabilly", 1)])
        self.assertEqual({item["genre_key"]: item["count"] for item in self.library.genre_choices()["genres"]}["rock"], 3)
        self.assertEqual(self.library.tracks(track_filter="genre_search", genre="ROCK", root=str(self.music))["total"], 2)
        self.assertEqual(set(self.library.track_paths(track_filter="genre_search", genre="rock")["paths"]),
                         {str(first), str(second), str(other)})
        self.assertEqual(self.library.tracks(track_filter="genre_search")["total"], 0)
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        handler._session = lambda modify=False: None
        handler._send_json = lambda value, status=200: (status, value)
        handler.path = "/api/genres/list?" + urlencode({"root": str(self.music)})
        self.assertEqual(handler._handle("GET")[1]["genres"], choices)
        handler.path = "/api/tracks/paths?" + urlencode({"filter": "genre_search", "genre": "Folk"})
        self.assertEqual(handler._handle("GET"), (200, {"paths": [str(first)]}))
        self.library.queue_add([str(first)], {}, genre_options={"mode": "replace", "values": ["Jazz"]})
        self.assertIn("folk", [item["genre_key"] for item in self.library.genre_choices()["genres"]])
        self.commit(self.library.preview_queue())
        self.assertNotIn("folk", [item["genre_key"] for item in self.library.genre_choices()["genres"]])

    def test_legacy_queue_scalar_is_one_genre_and_unrelated_edits_keep_every_value(self):
        path = self.track("flac", ["Rock", "Pop"])
        self.scan()
        self.library.queue_add([str(path)], {"comment": "Only this changes"})
        unchanged = self.library.preview_queue()
        self.assertNotIn("genre", unchanged["items"][0]["edits"])
        self.commit(unchanged)
        self.assertEqual(read_file(path)["tags"]["genre"], ["Rock", "Pop"])
        legacy = {"edits": {"genre": "Jazz, Soul"}, "mode": "none", "rename": "none", "artwork_id": None}
        with self.library._db() as db:
            db.execute("INSERT INTO queued(path,data,revision) VALUES (?,?,?)", (str(path), json.dumps(legacy), "legacy"))
        self.assertEqual(self.library.common_tags([str(path)])["common"]["genre"], ["Jazz, Soul"])
        self.commit(self.library.preview_queue())
        self.assertEqual(read_file(path)["tags"]["genre"], ["Jazz, Soul"])

    def test_upgrade_reindexes_unchanged_files_and_rebuilds_genre_filters(self):
        path = self.track("flac", ["Rock", "Pop"])
        self.scan()
        with self.library._db() as db:
            old_tags = read_file(path)["tags"]
            old_tags["genre"] = "Rock"
            db.execute("UPDATE tracks SET tags=?,filter_flags=0", (json.dumps(old_tags),))
            db.execute("ALTER TABLE tracks DROP COLUMN genre_indexed")
            db.execute("DROP TABLE track_genres")
        self.library = Library(self.base / "state", allowed_volume=self.music)
        self.assertTrue(self.library.tracks()["genres_need_scan"])
        self.scan()
        self.assertFalse(self.library.tracks()["genres_need_scan"])
        self.assertEqual(self.library.track(str(path))["tags"]["genre"], ["Rock", "Pop"])
        self.assertEqual(self.library.genres()["genres"], ["Pop", "Rock"])

    def test_review_filter_detects_native_duplicates_and_suggestions_drop_removed_tracks(self):
        path = self.track("flac", [])
        audio = open_audio(path)
        audio.tags["GENRE"] = [" Rock ", "Rock", "rock"]
        audio.save()
        self.scan()
        self.assertEqual(self.library.track_paths(track_filter="genre_review")["paths"], [str(path)])
        self.assertEqual(self.library.genres()["genres"], ["Rock"])
        self.library.queue_add([str(path)], {}, genre_options={"clean": True})
        self.commit(self.library.preview_queue())
        self.assertEqual(self.library.track_paths(track_filter="genre_review")["paths"], [])
        path.unlink()
        self.scan()
        self.assertEqual(self.library.genres()["genres"], [])
