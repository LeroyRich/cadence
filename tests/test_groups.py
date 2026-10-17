import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tagger.formats import apply_tags
from tagger.library import Library, LibraryError, sort_values, tag_filter_flags
from tagger.server import Handler


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "music"
        self.root.mkdir()
        self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        self.library.add_root(str(self.root))

    def add(self, name, root=None, artwork=True, index=True, **tags):
        root = root or self.root
        path = str(root / name)
        with self.library._db() as db:
            db.execute("""INSERT INTO tracks
                (path,root,format,tags,duration,artwork,size,mtime_ns,seen,
                 sort_title,sort_artist,sort_album,sort_path,filter_flags,genre_indexed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (path, str(root), "MP3", json.dumps(tags), 120, int(artwork), 100, 1, 1,
                 *sort_values(Path(path), root, tags), tag_filter_flags(tags)))
            if index:
                self.library._index_groups(db, path, tags)
        return path

    def test_album_identity_and_summary_use_album_artist_then_artist(self):
        first = self.add("a.mp3", title="A", album=" Record ", album_artist=" Band ", artist="Guest", date="1999")
        second = self.add("b.mp3", title="B", album="record", artist="band", date="2001-02-03")
        self.add("c.mp3", album="Record", artist="Other")
        rows = self.library.groups("albums")["items"]
        self.assertEqual(len(rows), 2)
        row = next(row for row in rows if row["tracks"] == 2)
        self.assertEqual((row["artists"], row["duration"], row["first_year"], row["last_year"]), (2, 240, 1999, 2001))
        self.assertEqual(self.library.group_paths("albums", keys=[row["key"]])["paths"], [first, second])

    def test_track_artist_names_are_not_split(self):
        self.add("a.mp3", artist="One & Two", album_artist="Various Artists")
        self.add("b.mp3", artist="one & two", album_artist="Different")
        result = self.library.groups("artists")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["tracks"], 2)

    def test_genres_overlap_and_resolution_deduplicates_tracks(self):
        both = self.add("a.mp3", genre=[" Rock ", "ROCK", "Folk"])
        rock = self.add("b.mp3", genre=["rock"])
        rows = self.library.groups("genres")
        self.assertEqual((rows["total"], rows["total_tracks"]), (2, 2))
        self.assertEqual(sorted(row["tracks"] for row in rows["items"]), [1, 2])
        self.assertEqual(self.library.group_paths("genres", all_matching=True)["paths"], [both, rock])

    def test_unknown_groups_and_real_names_do_not_collide(self):
        self.add("missing.mp3")
        self.add("named.mp3", artist="Unknown artist", album="Unknown album", genre=["No genre"], date="bad")
        for view in ("artists", "albums", "genres"):
            self.assertEqual(self.library.groups(view)["total"], 2)
        self.assertEqual(self.library.groups("years")["items"][0]["label"], "Unknown year")
        self.assertEqual(self.library.groups("decades")["items"][0]["label"], "Unknown decade")

    def test_years_decades_and_date_recognition(self):
        for index, date in enumerate(["1989", "1990-01", "1999/12", "2000.01", "2001 ", "circa 1990", "1999x"]):
            self.add(str(index) + ".mp3", date=date)
        decades = {row["label"]: row["tracks"] for row in self.library.groups("decades")["items"]}
        self.assertEqual(decades, {"1980s": 1, "1990s": 2, "2000s": 2, "Unknown decade": 2})
        self.assertEqual(self.library.groups("years", query="199")["total_tracks"], 2)

    def test_group_name_search_does_not_search_track_metadata(self):
        self.add("Hidden.mp3", title="Hidden", album="Record", artist="Straße", comment="Secret")
        self.assertEqual(self.library.groups("albums", query="Hidden")["total"], 0)
        self.assertEqual(self.library.groups("albums", query="STRASSE")["total"], 1)
        self.assertEqual(self.library.groups("artists", query="Record")["total"], 0)
        self.assertEqual(self.library.groups("albums", query="Secret", track_filter="tag_search", tag_field="comment")["total"], 1)

    def test_filter_and_tag_search_must_match_same_member_but_edit_whole_group(self):
        missing = self.add("a.mp3", album="Record", artist="Artist", title="First", artwork=False)
        present = self.add("b.mp3", album="Record", artist="Artist", title="Second")
        rows = self.library.groups("albums", track_filter="no_artwork")
        self.assertEqual(rows["items"][0]["tracks"], 2)
        self.assertEqual(self.library.group_paths("albums", track_filter="no_artwork", all_matching=True)["paths"], [missing, present])
        self.assertEqual(self.library.groups("albums", query="Second", track_filter="no_artwork")["total"], 0)
        found = self.library.group_paths("albums", query="Second", track_filter="tag_search", tag_field="title", all_matching=True)
        self.assertEqual(found["paths"], [missing, present])

    def test_root_scope_limits_members_and_group_counts(self):
        inside = self.add("inside.mp3", album="Record", artist="Artist")
        other = self.base / "other"
        other.mkdir()
        self.library.add_root(str(other))
        self.add("outside.mp3", root=other, album="Record", artist="Artist")
        self.assertEqual(self.library.groups("albums")["items"][0]["tracks"], 2)
        scoped = self.library.groups("albums", root=str(self.root))
        self.assertEqual(scoped["items"][0]["tracks"], 1)
        result = self.library.group_paths("albums", root=str(self.root), keys=[scoped["items"][0]["key"]])
        self.assertEqual(result["paths"], [inside])
        self.assertEqual(result["track"]["path"], inside)

    def test_tag_search_is_field_specific_literal_and_unicode_aware(self):
        path = self.add("not-metadata.mp3", title="100%_Straße", artist="Élève", album="Record", album_artist="Band",
                        genre=["Rock", "Folk"], comment="Live", track="3/12", disc="1/2", date="1999-01")
        queries = {"title": "100%_STRASSE", "artist": "ÉLÈVE", "album": "record", "album_artist": "band",
                   "genre": "folk", "comment": "live", "track": "3/", "disc": "1/", "date": "1999", "all": "live"}
        for field, query in queries.items():
            options = dict(track_filter="tag_search", tag_field=field, query=query)
            self.assertEqual(self.library.track_paths(**options)["paths"], [path], field)
            self.assertEqual(self.library.tracks(**options)["total"], 1)
        self.assertEqual(self.library.tracks(query="Live", track_filter="tag_search", tag_field="title")["total"], 0)
        self.assertEqual(self.library.tracks(query="not-metadata", track_filter="tag_search")["total"], 0)
        self.assertEqual(self.library.tracks(query="title", track_filter="tag_search")["total"], 0)
        self.assertEqual(self.library.tracks(track_filter="tag_search")["total"], 1)
        self.assertEqual(self.library.groups("albums", track_filter="tag_search")["total"], 1)
        self.assertEqual(self.library.tracks(query="%_", track_filter="tag_search")["total"], 1)
        self.assertEqual(self.library.tracks(query="%x", track_filter="tag_search")["total"], 0)

    def test_pagination_sorting_and_selection_agree(self):
        for index in range(107):
            self.add("{:03d}.mp3".format(index), artist="Artist {:03d}".format(index), album="Record")
        first = self.library.groups("artists", limit=100)
        last = self.library.groups("artists", offset=100)
        self.assertEqual((first["total"], len(first["items"]), len(last["items"])), (107, 100, 7))
        ascending = first["items"] + last["items"]
        self.assertEqual(self.library.groups("artists", direction="desc")["items"][0]["key"], ascending[-1]["key"])
        self.assertEqual(len(self.library.group_paths("artists", all_matching=True)["paths"]), 107)
        for view in ("albums", "artists", "genres", "years", "decades"):
            for sort in ("label", "album_artist", "year", "artists", "albums", "tracks", "duration"):
                self.library.groups(view, sort=sort)

    def test_group_membership_migration_uses_cached_tags(self):
        self.add("legacy.mp3", artist="Legacy", genre=["Folk"], index=False)
        with patch("tagger.library.read_file", side_effect=AssertionError("must not read audio")):
            self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        self.assertEqual(self.library.groups("artists")["items"][0]["label"], "Legacy")
        self.assertEqual(self.library.groups("genres")["items"][0]["label"], "Folk")

    def test_record_refresh_and_removal_update_membership(self):
        path = Path(self.add("a.mp3", artist="Before", genre=["Rock"]))
        path.touch()
        details = dict(format="MP3", tags={"artist": "After", "genre": ["Folk"]}, duration=1, artwork=False)
        with patch("tagger.library.read_file", return_value=details), self.library._db() as db:
            self.library._record(db, path, self.root, 2)
        self.assertEqual(self.library.groups("artists", query="Before")["total"], 0)
        self.assertEqual(self.library.groups("genres")["items"][0]["label"], "Folk")
        self.library.remove_root(str(self.root))
        self.assertEqual(self.library.groups("artists")["total"], 0)
        with self.library._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM track_groups").fetchone()[0], 0)

    def test_batch_limit_checks_expanded_tracks_and_rejects_atomically(self):
        path = self.add("first.mp3", album="Huge", artist="Artist")
        with self.library._db() as db:
            # Use SQL copies to keep this boundary fixture inexpensive.
            for index in range(1, 5001):
                copy = str(self.root / (str(index) + ".mp3"))
                db.execute("""INSERT INTO tracks SELECT ?,root,format,tags,duration,artwork,size,mtime_ns,seen,
                    sort_title,sort_artist,sort_album,sort_path,device,inode,filter_flags,genre_indexed,artwork_indexed
                    FROM tracks WHERE path=?""", (copy, path))
                db.execute("INSERT INTO track_groups SELECT ?,view,key,label,credit,year FROM track_groups WHERE path=?", (copy, path))
        row = self.library.groups("albums")["items"][0]
        self.assertEqual(row["tracks"], 5001)
        for options in ({"keys": [row["key"]]}, {"all_matching": True}):
            with self.assertRaisesRegex(LibraryError, "5,000-track"):
                self.library.group_paths("albums", **options)
        with self.library._db() as db:
            db.execute("DELETE FROM tracks WHERE path=?", (path,))
        self.assertEqual(len(self.library.group_paths("albums", keys=[row["key"]])["paths"]), 5000)

    def test_invalid_and_stale_requests_are_rejected(self):
        for options in ({"view": "bad"}, {"view": "artists", "sort": "bad"},
                        {"view": "albums", "track_filter": "empty_folders"},
                        {"view": "artists", "track_filter": "tag_search", "tag_field": "bad"}):
            with self.assertRaises(LibraryError):
                self.library.groups(**options)
        for keys in (None, [], ["missing"], ["x", "x"], [None]):
            with self.assertRaises(LibraryError):
                self.library.group_paths("artists", keys=keys)

    def test_http_group_listing_resolution_and_track_tag_search(self):
        path = self.add("a.mp3", artist="Artist", album="Record", comment="Live")
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        handler._session = lambda modify=False: None
        handler._send_json = lambda value, status=200: (status, value)
        handler.path = "/api/groups?view=albums&filter=tag_search&tag_field=comment&q=live"
        status, result = handler._handle("GET")
        self.assertEqual((status, result["total"]), (200, 1))
        handler.path = "/api/groups/paths"
        handler._body = lambda: {"view": "albums", "keys": [result["items"][0]["key"]]}
        status, result = handler._handle("POST")
        self.assertEqual((status, result["paths"]), (200, [path]))
        for endpoint in ("/api/tracks", "/api/tracks/paths"):
            handler.path = endpoint + "?filter=tag_search&tag_field=comment&q=live"
            self.assertEqual(handler._handle("GET")[0], 200)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed for audio fixtures")
class GroupEditingTests(unittest.TestCase):
    def wait(self, library, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = library.job(job)
            if result["state"] not in ("queued", "running", "cancelling"):
                self.assertEqual(result["state"], "complete", result)
                return
            time.sleep(.01)
        self.fail("Job did not complete")

    def test_group_edit_queue_review_commit_move_and_delete(self):
        from test_app import make_audio
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "music"
            root.mkdir()
            for index in range(2):
                directory = root / str(index)
                directory.mkdir()
                path = make_audio(directory, "mp3")
                apply_tags(path, {"title": "Song " + str(index), "artist": "Artist", "album": "Record", "genre": ["Rock", "Folk"], "date": "1999"})
            library = Library(base / "state", allowed_volume=base)
            library.add_root(str(root))
            self.wait(library, library.start_scan())
            row = library.groups("albums")["items"][0]
            paths = library.group_paths("albums", keys=[row["key"]])["paths"]
            library.queue_add(paths, {"album": "Renamed", "genre": ["Jazz"]}, mode="artist_album", rename="title")
            self.assertEqual(library.common_tags(paths)["common"]["album"], "Renamed")
            self.assertEqual(library.groups("albums")["items"][0]["label"], "Record")
            self.assertEqual(library.groups("albums")["items"][0]["queued_count"], 2)
            preview = library.preview_queue()
            self.assertEqual(len(preview["items"]), 2)
            self.wait(library, library.start_commit(preview["id"]))
            row = library.groups("albums")["items"][0]
            self.assertEqual((row["label"], row["tracks"]), ("Renamed", 2))
            self.assertEqual(library.groups("genres")["items"][0]["label"], "Jazz")
            moved = library.group_paths("albums", keys=[row["key"]])["paths"]
            self.assertTrue(all("Renamed (1999)" in path for path in moved))
            library.queue_add(moved, {}, delete=True)
            preview = library.preview_queue()
            self.wait(library, library.start_commit(preview["id"]))
            self.assertEqual(library.groups("albums")["total"], 0)
            with library._db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM track_groups").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
