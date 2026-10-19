import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tagger.formats import FIELDS
from tagger.library import Library, LibraryError
from tagger.server import Handler


PNG = b"\x89PNG\r\n\x1a\nqueued cover"
OLD = hashlib.sha256(b"saved cover").hexdigest()


class PendingListTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "music"
        self.root.mkdir()
        self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)
        self.library.add_root(str(self.root))

    def add(self, name, root=None, signature=OLD, **edits):
        root = root or self.root
        path = root / name
        path.write_bytes(b"audio fixture")
        tags = {field: "" for field in FIELDS}
        tags.update(title=name, artist="Artist", album="Record", date="1999", genre=["Rock"])
        tags.update(edits)
        details = dict(tags=tags, format="MP3", duration=120, artwork=signature != "none", artwork_signature=signature)
        with patch("tagger.library.read_file", return_value=details), self.library._db() as db:
            self.library._record(db, path, root, 1)
        return str(path)

    def row(self, view="albums", root=""):
        rows = self.library.groups(view, root=root)["items"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def art(self, paths, data=PNG):
        return self.library.queue_add(paths, {}, artwork={"mime": "image/png", "data": base64.b64encode(data).decode()})

    def test_group_previews_match_editor_values_and_clear_with_queue(self):
        paths = [self.add("a.mp3"), self.add("b.mp3")]
        saved = self.row()
        edits = {"album": "New record", "artist": "New artist", "date": "2025"}
        self.library.queue_add(paths, edits)
        row = self.row()
        self.assertEqual(row["key"], saved["key"])
        self.assertEqual((row["label"], row["album_artist"], row["first_year"]), ("Record", "Artist", 1999))
        self.assertEqual(row["pending"], {"label": "New record", "album_artist": "New artist", "year": "2025"})
        self.assertEqual(row["queued_count"], 2)
        self.assertEqual(self.library.common_tags(paths)["common"]["album"], row["pending"]["label"])
        self.assertEqual(self.library.groups("albums", query="New record")["total"], 0)
        self.assertEqual(self.library.group_paths("albums", keys=[row["key"]])["paths"], paths)
        self.library.queue_clear()
        row = self.row()
        self.assertEqual(row["queued_count"], 0)
        self.assertNotIn("pending", row)

    def test_partial_group_changes_show_saved_and_pending_values_and_updated_year_range(self):
        first = self.add("a.mp3")
        self.add("b.mp3", date="2000")
        self.library.queue_add([first], {"album": "New record", "date": "2001"})
        row = self.row()
        self.assertEqual(row["pending"]["label"], "New record · Record")
        self.assertEqual(row["pending"]["year"], "2000–2001")
        self.assertEqual(row["tracks"], 2)
        self.assertEqual(row["queued_count"], 1)

    def test_pending_values_apply_to_each_group_view_without_regrouping(self):
        path = self.add("a.mp3")
        self.library.queue_add([path], {"album": "New record", "artist": "New artist", "date": "2024", "genre": ["Jazz", "Blues"]})
        for view, expected in (("albums", "New record"), ("artists", "New artist"), ("years", "2024"),
                               ("decades", "2020s"), ("genres", "Jazz; Blues")):
            self.assertEqual(self.row(view)["pending"]["label"], expected, view)

    def test_comment_only_changes_and_deletions_keep_displayed_metadata(self):
        path = self.add("a.mp3")
        self.library.queue_add([path], {"comment": "Pending comment"})
        self.assertEqual(self.row()["pending"], {})
        self.library.queue_add([path], {}, delete=True)
        self.assertEqual(self.row()["pending"], {})
        self.assertEqual(self.row()["queued_count"], 1)

    def test_cleared_fields_show_unknown_groups_and_empty_year(self):
        path = self.add("a.mp3")
        self.library.queue_add([path], {"album": "", "artist": "", "date": "", "genre": []})
        row = self.row()
        self.assertEqual(row["pending"], {"label": "Unknown album", "album_artist": "Unknown artist", "year": "—"})
        self.assertEqual(self.row("years")["pending"]["label"], "Unknown year")
        self.assertEqual(self.row("genres")["pending"]["label"], "No genre")

    def test_queued_artwork_previews_only_when_every_effective_cover_matches(self):
        paths = [self.add("a.mp3"), self.add("b.mp3")]
        original_path = self.row()["artwork_path"]
        self.art(paths[:1])
        self.assertIsNone(self.row()["pending"]["artwork"]["path"])
        self.art(paths[1:])
        row = self.row()
        pending = row["pending"]["artwork"]
        self.assertIn(pending["path"], paths)
        self.assertTrue(pending["queued"])
        self.assertEqual(self.library.queued_artwork_image(pending["path"], pending["revision"]), (PNG, "image/png"))
        self.assertEqual(row["artwork_path"], original_path)
        self.library.queue_clear()
        self.assertNotIn("pending", self.row())

    def test_replacement_can_match_saved_cover_and_handle_missing_or_unknown_members(self):
        fingerprint = hashlib.sha256(PNG).hexdigest()
        first = self.add("a.mp3", signature=fingerprint)
        second = self.add("b.mp3", signature="none")
        self.art([second])
        self.assertIsNotNone(self.row()["pending"]["artwork"]["path"])
        self.assertIsNone(self.row()["artwork_path"])
        with self.library._db() as db:
            db.execute("UPDATE tracks SET artwork_signature=NULL WHERE path=?", (first,))
        self.assertIsNone(self.row()["pending"]["artwork"]["path"])

    def test_preview_respects_library_scope_and_excludes_inflight_queue(self):
        self.add("a.mp3")
        other = self.base / "other"
        other.mkdir()
        self.library.add_root(str(other))
        path = self.add("b.mp3", root=other)
        self.library.queue_add([path], {"album": "New record"})
        self.assertNotIn("pending", self.row(root=str(self.root)))
        self.assertEqual(self.row(root=str(other))["pending"]["label"], "New record")
        self.assertEqual(self.row()["pending"]["label"], "New record · Record")
        with self.library._db() as db:
            db.execute("UPDATE queued SET inflight_job='job'")
        self.assertNotIn("pending", self.row())

    def test_group_previews_read_cached_metadata_and_queue_without_reading_audio(self):
        path = self.add("a.mp3")
        self.library.queue_add([path], {"album": "New record"})
        with patch("tagger.library.read_file", side_effect=AssertionError("Audio read")), patch(
                "tagger.library.inspect_artwork", side_effect=AssertionError("Artwork read")):
            self.assertEqual(self.row()["pending"]["label"], "New record")

    def test_queued_artwork_image_checks_revision_and_http_route(self):
        path = self.add("a.mp3")
        queued = self.art([path])["items"][0]
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        handler._session = lambda modify=False: None
        handler._send_image = lambda data, mime, etag: (data, mime, etag)
        handler.path = "/api/queue/art/image?path={}&revision={}".format(path, queued["revision"])
        image, mime, etag = handler._handle("GET")
        self.assertEqual((image, mime), (PNG, "image/png"))
        self.assertIn(queued["revision"], etag)
        self.art([path], PNG + b"new")
        with self.assertRaisesRegex(LibraryError, "changed"):
            self.library.queued_artwork_image(path, queued["revision"])
        self.library.queue_clear()
        with self.assertRaises(LibraryError):
            self.library.queued_artwork_image(path, queued["revision"])


if __name__ == "__main__":
    unittest.main()
