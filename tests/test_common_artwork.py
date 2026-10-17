import base64
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tagger.formats import FIELDS
from tagger.library import Library, LibraryError
from tagger.server import Handler


PNG = b'\x89PNG\r\n\x1a\ncover one'
OTHER = b'\x89PNG\r\n\x1a\ncover two'


class CommonArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'music'; self.root.mkdir()
        self.library = Library(Path(self.temp.name) / 'state', allowed_volume=self.root, resume_jobs=False)
        self.library.add_root(str(self.root))
        self.paths = []
        for i in range(3):
            path = self.root / ('track%d.mp3' % i); path.write_bytes(b'audio fixture')
            tags = {field: '' for field in FIELDS}; tags['genre'] = []
            with patch('tagger.library.read_file', return_value={'tags': tags, 'format': 'MP3', 'duration': 1, 'artwork': False}):
                with self.library._db() as db:
                    self.library._record(db, path, self.root, time.time_ns())
            self.paths.append(str(path))

    def stage_art(self, paths, data=PNG):
        self.library.queue_add(paths, {}, artwork={'mime': 'image/png', 'data': base64.b64encode(data).decode()})

    def test_identical_covers_are_shared_and_not_staged_as_replacements(self):
        with patch('tagger.library.inspect_artwork', return_value=(True, (PNG, 'image/png'))):
            result = self.library.common_artwork(self.paths)
        self.assertEqual(result['state'], 'shared')
        self.assertEqual(base64.b64decode(result['artwork']['data']), PNG)
        self.assertEqual(result['signature'], hashlib.sha256(PNG).hexdigest())
        self.assertFalse(result['queued'])
        self.assertTrue(result['indexed_changed'])
        self.assertEqual(self.library.queue_items(), [])
        self.assertTrue(all(self.library.track(path)['artwork'] for path in self.paths))

    def test_different_artwork_stops_comparison_and_returns_no_image(self):
        with patch('tagger.library.inspect_artwork', side_effect=[(True, (PNG, 'image/png')), (True, (OTHER, 'image/png'))]) as read:
            result = self.library.common_artwork(self.paths)
        self.assertEqual(result['state'], 'mixed'); self.assertNotIn('artwork', result)
        self.assertEqual(read.call_count, 2)

    def test_missing_and_present_artwork_is_mixed_in_both_orders(self):
        for values in [[(False, None), (True, (PNG, 'image/png'))], [(True, (PNG, 'image/png')), (False, None)]]:
            with patch('tagger.library.inspect_artwork', side_effect=values):
                self.assertEqual(self.library.common_artwork(self.paths[:2])['state'], 'mixed')

    def test_all_missing_artwork(self):
        with patch('tagger.library.inspect_artwork', return_value=(False, None)):
            result = self.library.common_artwork(self.paths)
        self.assertEqual(result['state'], 'none'); self.assertEqual(result['signature'], 'none')
        self.assertNotIn('artwork', result)

    def test_queued_replacements_override_embedded_covers(self):
        self.stage_art(self.paths)
        with patch('tagger.library.inspect_artwork') as read:
            result = self.library.common_artwork(self.paths)
        read.assert_not_called()
        self.assertEqual(result['state'], 'shared'); self.assertTrue(result['queued'])
        self.assertEqual(base64.b64decode(result['artwork']['data']), PNG)

    def test_different_queue_ids_with_identical_bytes_still_share_art(self):
        for path in self.paths: self.stage_art([path])
        self.assertEqual(self.library.common_artwork(self.paths)['state'], 'shared')

    def test_queued_and_embedded_artwork_compare_by_image_content(self):
        self.stage_art(self.paths[:1])
        with patch('tagger.library.inspect_artwork', return_value=(True, (PNG, 'image/png'))):
            self.assertEqual(self.library.common_artwork(self.paths)['state'], 'shared')
        with patch('tagger.library.inspect_artwork', return_value=(True, (OTHER, 'image/png'))):
            self.assertEqual(self.library.common_artwork(self.paths)['state'], 'mixed')

    def test_inflight_replacements_are_not_shown_as_queued_edits(self):
        self.stage_art(self.paths)
        with self.library._db() as db: db.execute("UPDATE queued SET inflight_job='running-job'")
        with patch('tagger.library.inspect_artwork', return_value=(False, None)):
            result = self.library.common_artwork(self.paths)
        self.assertEqual(result['state'], 'none'); self.assertFalse(result['queued'])

    def test_later_batches_do_not_resend_the_image(self):
        with patch('tagger.library.inspect_artwork', return_value=(True, (PNG, 'image/png'))):
            first = self.library.common_artwork(self.paths[:1])
            second = self.library.common_artwork(self.paths[1:], first['signature'])
        self.assertEqual(second['state'], 'shared'); self.assertNotIn('artwork', second)
        with patch('tagger.library.inspect_artwork', return_value=(False, None)):
            self.assertEqual(self.library.common_artwork(self.paths, first['signature'])['state'], 'mixed')
        with patch('tagger.library.inspect_artwork', return_value=(True, (PNG, 'image/png'))):
            self.assertEqual(self.library.common_artwork(self.paths, 'none')['state'], 'mixed')

    def test_unpreviewable_artwork_is_not_claimed_to_be_shared(self):
        with patch('tagger.library.inspect_artwork', return_value=(True, None)):
            result = self.library.common_artwork(self.paths)
        self.assertEqual(result['state'], 'unavailable'); self.assertNotIn('artwork', result)

    def test_invalid_selection_signatures_and_unindexed_files_rejected(self):
        for paths in [[], None, [None], self.paths[:1] * 2, ['x%d' % i for i in range(26)]]:
            with self.assertRaises(LibraryError): self.library.common_artwork(paths)
        for expected in ['', False, 'not-a-signature']:
            with self.assertRaises(LibraryError): self.library.common_artwork(self.paths, expected)
        unindexed = self.root / 'unindexed.mp3'; unindexed.touch()
        with self.assertRaises(LibraryError): self.library.common_artwork([str(unindexed)])

    def test_endpoint_forwards_batch_and_requires_authenticated_session(self):
        handler = object.__new__(Handler)
        handler.path = '/api/art/common'
        handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        calls = []
        handler._session = lambda modify=False: calls.append(modify)
        handler._body = lambda: {'paths': self.paths, 'expected': 'none'}
        handler._send_json = lambda value, status=200: (status, value)
        with patch('tagger.library.inspect_artwork', return_value=(False, None)):
            status, result = handler._handle('POST')
        self.assertEqual(calls, [True]); self.assertEqual(status, 200); self.assertEqual(result['state'], 'none')
