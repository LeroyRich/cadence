import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tagger.library import LibraryError, tags_from_filename
from tagger.server import Handler


class FilenameImportTests(unittest.TestCase):
    def test_all_patterns_ignore_extension_and_preserve_title_punctuation(self):
        examples = [
            ("/Artist - Folder/Hello.world.FLAC", "title", {"title": "Hello.world"}),
            ("003 - Élève - Live.mp3", "track_title", {"track": "3", "title": "Élève - Live"}),
            ("Artist - Don't Stop - Live.ogg", "artist_title", {"artist": "Artist", "title": "Don't Stop - Live"}),
            ("Artist - Album - 103 - Song.m4a", "artist_album_track_title",
             {"artist": "Artist", "album": "Album", "track": "103", "title": "Song"}),
            ("Artist - Album - 01 - Song - Live.opus", "artist_album_track_title",
             {"artist": "Artist", "album": "Album", "track": "1", "title": "Song - Live"}),
            ("unchanged.mp3", "none", {}),
        ]
        for path, pattern, expected in examples:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(tags_from_filename(path, pattern), expected)

    def test_nonmatching_empty_zero_and_ambiguous_names_are_rejected(self):
        examples = [("01-Song.mp3", "track_title"), ("00 - Song.mp3", "track_title"),
                    ("Artist - .flac", "artist_title"), ("Song.mp3", "artist_title"),
                    ("Artist - Album - 01 - Song - 02 - Live.mp3", "artist_album_track_title"),
                    ("Artist - Extra - Album - 01 - Song.mp3", "artist_album_track_title"),
                    ("Artist - Album - xx - Song.mp3", "artist_album_track_title"),
                    ("Song.mp3", "guess")]
        for path, pattern in examples:
            with self.subTest(path=path, pattern=pattern), self.assertRaises(LibraryError):
                tags_from_filename(path, pattern)

    def test_api_forwards_import_option_on_all_edit_routes(self):
        library = Mock()
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(library=library))
        handler._session = lambda modify=False: None
        handler._send_json = lambda value, status=200: (status, value)
        body = {"paths": ["/Music/01 - Song.mp3"], "edits": {}, "import_tags": "track_title"}
        handler._body = lambda: body
        for route, method in (("/api/preview", "preview"), ("/api/queue/add", "queue_add")):
            handler.path = route
            handler._handle("POST")
            self.assertEqual(getattr(library, method).call_args.args[-1], "track_title")
        handler.path = "/api/queue/preview"
        handler._body = lambda: {"pending": body}
        handler._handle("POST")
        library.preview_queue.assert_called_once_with(body)
