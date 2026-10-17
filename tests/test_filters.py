import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tagger.library import Library, LibraryError, sort_values, tag_filter_flags
from tagger.server import Handler


class LibraryFilterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "music"
        self.root.mkdir()
        self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        self.library.add_root(str(self.root))
        self.tags = {"title": "Song", "artist": "Artist", "album": "Album", "track": "3/12", "date": "1999-03-01"}

    def add_track(self, relative, tags=None, artwork=True, root=None, cached=True):
        root = root or self.root
        tags = self.tags if tags is None else tags
        path = root / relative
        with self.library._db() as db:
            db.execute("""INSERT INTO tracks
                (path,root,format,tags,duration,artwork,size,mtime_ns,seen,
                 sort_title,sort_artist,sort_album,sort_path,filter_flags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(path), str(root), "MP3", json.dumps(tags), 120, int(artwork), 100, 1, 1,
                 *sort_values(path, root, tags), tag_filter_flags(tags) if cached else None))
        return str(path)

    def paths(self, track_filter, **options):
        return self.library.track_paths(track_filter=track_filter, **options)["paths"]

    def test_separate_missing_fields_and_unicode_case(self):
        for field in ("title", "artist", "album", "track", "date"):
            self.add_track(field + ".mp3", {**self.tags, field: "  "})
        for field in ("title", "artist", "album", "track"):
            self.assertEqual(self.paths("missing_" + field), [str(self.root / (field + ".mp3"))])
        self.assertEqual(self.paths("missing_year"), [str(self.root / "date.mp3")])
        self.add_track("lower.mp3", {**self.tags, "title": "élève don't stop (live) 123"})
        self.add_track("upper.mp3", {**self.tags, "title": "ÉLÈVE DON'T STOP! 123"})
        self.add_track("numeric.mp3", {**self.tags, "title": "123?!"})
        self.add_track("mixed.mp3", {**self.tags, "title": "élève LIVE"})
        self.assertEqual(self.paths("lowercase_title"), [str(self.root / "lower.mp3")])
        self.assertEqual(self.paths("uppercase_title"), [str(self.root / "upper.mp3")])

    def test_album_depth_and_embedded_artwork(self):
        self.add_track("root.mp3", artwork=False)
        direct = self.add_track("Artist/direct.mp3", artwork=False)
        other_artist = self.add_track("Other Artist/direct.mp3", {"title": "Different", "artist": "Wrong", "album": "Different"})
        self.add_track("Artist/Album/organized.mp3")
        self.add_track("Artist/Album/Disc 1/disc.mp3")
        self.assertEqual(set(self.paths("no_album_folder")), {direct, other_artist})
        self.assertEqual(set(self.paths("no_artwork")), {str(self.root / "root.mp3"), direct})

    def test_invalid_characters_filter_covers_tag_fields_and_selection(self):
        control = self.add_track("Artist/Control.mp3", {**self.tags, "comment": "Live\nversion"})
        replacement = self.add_track("Artist/Replacement.mp3", {**self.tags, "artist": "Art\ufffdist"})
        genre = self.add_track("Artist/Genre.mp3", {**self.tags, "genre": ["Rock", "Folk\x7f"]})
        self.add_track("Artist/Valid.mp3", {**self.tags, "title": "¿Qué? / Étoile: ★", "genre": ["R&B"]})
        other = self.base / "other"
        other.mkdir()
        self.library.add_root(str(other))
        self.add_track("Elsewhere.mp3", {**self.tags, "disc": "2\x00"}, root=other)
        options = {"track_filter": "invalid_characters", "root": str(self.root)}
        self.assertEqual(set(self.paths(**options)), {control, replacement, genre})
        self.assertEqual(self.library.tracks(**options, limit=2)["total"], 3)
        self.assertEqual(self.library.track_paths(**options, query="Replacement")["paths"], [replacement])

    def test_new_filter_reindexes_existing_cached_tags_without_reading_audio(self):
        path = self.add_track("Cached.mp3", {**self.tags, "album_artist": "Name\ufffd"})
        with self.library._db() as db:
            db.execute("UPDATE tracks SET filter_flags=0 WHERE path=?", (path,))
            db.execute("DELETE FROM settings WHERE key='tag_filter_flags_version'")
        with patch("tagger.library.read_file", side_effect=AssertionError("audio read during migration")):
            self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        self.assertEqual(self.paths("invalid_characters"), [path])
        self.assertEqual(self.library.get_setting("tag_filter_flags_version"), "2")

    def test_track_and_disc_sort_use_numeric_tag_values(self):
        first = self.add_track("first.mp3", {**self.tags, "track": "2/12", "disc": "1/2"})
        second = self.add_track("second.mp3", {**self.tags, "track": "10/12", "disc": "2/2"})
        missing = self.add_track("missing.mp3", {**self.tags, "track": "", "disc": ""})
        self.assertEqual(self.library.track_paths(sort="track")["paths"], [missing, first, second])
        self.assertEqual(self.library.track_paths(sort="disc", direction="desc")["paths"],
                         [second, first, missing])
        self.assertEqual([item["path"] for item in self.library.tracks(sort="track")["items"]],
                         [missing, first, second])

    def test_genre_choices_are_not_limited_to_autocomplete_results(self):
        path = self.add_track("many-genres.mp3")
        with self.library._db() as db:
            db.executemany("INSERT INTO track_genres VALUES (?,?,?)",
                           [(path, "Genre {}".format(index), "genre {}".format(index)) for index in range(60)])
        self.assertEqual(len(self.library.genre_choices()["genres"]), 60)
        self.assertEqual(self.library.tracks(track_filter="genre_search", genre="Genre 59")["total"], 1)

    def test_filtered_paging_search_roots_and_select_all_agree(self):
        expected = [self.add_track("Artist/Keep-{:03d}.mp3".format(index), artwork=False)
                    for index in range(125)]
        self.add_track("Artist/Skip.mp3", artwork=False)
        self.add_track("Artist/Album/Keep-art.mp3")
        other = self.base / "other"
        other.mkdir()
        self.library.add_root(str(other))
        self.add_track("Keep-other.mp3", artwork=False, root=other)
        options = {"query": "Keep", "root": str(self.root), "track_filter": "no_album_folder"}
        first = self.library.tracks(**options, limit=100)
        second = self.library.tracks(**options, offset=100, limit=100)
        self.assertEqual(first["total"], 125)
        self.assertEqual([item["path"] for item in first["items"] + second["items"]], expected)
        self.assertEqual(self.library.track_paths(**options)["paths"], expected)
        self.assertEqual(self.library.track_paths(**options, direction="desc")["paths"], expected[::-1])

    def test_obsolete_mismatch_filter_is_rejected(self):
        for options in ({"track_filter": "unknown"}, {"track_filter": "path_mismatch"}):
            with self.assertRaises(LibraryError):
                self.library.tracks(**options)

    def test_migration_backfills_flags_and_record_refreshes_them(self):
        path = Path(self.add_track("lower.mp3", {**self.tags, "title": "song"}, cached=False))
        self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        with self.library._db() as db:
            self.assertIsNotNone(db.execute("SELECT filter_flags FROM tracks WHERE path=?", (str(path),)).fetchone()[0])
        self.assertEqual(self.paths("lowercase_title"), [str(path)])
        path.touch()
        with patch("tagger.library.read_file", return_value={"format": "MP3", "tags": self.tags,
                                                           "duration": 120, "artwork": False}):
            with self.library._db() as db:
                self.library._record(db, path, self.root, 2)
        self.assertEqual(self.paths("lowercase_title"), [])
        self.assertEqual(self.paths("no_artwork"), [str(path)])

    def test_http_list_and_select_all_forward_artist_folder_filter(self):
        matching = self.add_track("Artist/Loose.mp3")
        self.add_track("Artist/Album (1999)/03 - Song.mp3")
        self.add_track("root.mp3")
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        handler._session = lambda modify=False: None
        handler._send_json = lambda value, status=200: (status, value)
        query = "?filter=no_album_folder"
        handler.path = "/api/tracks" + query
        status, result = handler._handle("GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["path"] for item in result["items"]], [matching])
        handler.path = "/api/tracks/paths" + query
        status, result = handler._handle("GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["paths"], [matching])

    def test_legacy_jobs_gain_nullable_timestamps(self):
        legacy_dir = self.base / "legacy"
        legacy_dir.mkdir()
        with sqlite3.connect(legacy_dir / "library.sqlite3") as db:
            db.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                done INTEGER NOT NULL, total INTEGER NOT NULL, errors TEXT NOT NULL, created REAL NOT NULL)""")
            db.execute("""CREATE TABLE job_items(job TEXT NOT NULL, ordinal INTEGER NOT NULL, source TEXT NOT NULL,
                destination TEXT NOT NULL, before_tags TEXT NOT NULL, after_tags TEXT NOT NULL,
                artwork_changed INTEGER NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL,
                PRIMARY KEY(job,ordinal))""")
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", ("old", "commit", "complete", 1, 1, "[]", 100.0))
            db.execute("INSERT INTO job_items VALUES (?,?,?,?,?,?,?,?,?)",
                       ("old", 0, "/old.mp3", "/old.mp3", "{}", "{}", 0, "complete", ""))
        migrated = Library(legacy_dir, allowed_volume=self.base, resume_jobs=False)
        details = migrated.job_details("old")
        self.assertIsNone(details["job"]["started_at"])
        self.assertIsNone(details["job"]["stopped_at"])
        self.assertIsNone(details["items"][0]["completed_at"])


if __name__ == "__main__":
    unittest.main()
