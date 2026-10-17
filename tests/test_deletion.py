import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tagger.formats import FIELDS
from tagger.library import Library, LibraryError


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'music'
        self.root.mkdir()
        self.library = Library(self.base / 'state', allowed_volume=self.base, resume_jobs=False)
        self.library.add_root(str(self.root))

    def track(self, name='song.mp3'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test audio')
        tags = {key: '' for key in FIELDS}
        tags.update(title=path.stem, genre=[])
        with patch('tagger.library.read_file', return_value={'tags': tags, 'format': 'MP3', 'duration': 10, 'artwork': False}):
            with self.library._db() as db:
                self.library._record(db, path, self.root, time.time_ns())
        return path

    def folder(self, name='Artist/Empty'):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def wait(self, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.library.job(job)
            if result['state'] not in ('queued', 'running', 'cancelling'):
                return result
            time.sleep(.01)
        self.fail('Job timed out')

    def scan(self, root=''):
        return self.wait(self.library.start_folder_scan(root))

    def stage(self, path, kind='track', **kwargs):
        return self.library.queue_add([str(path)], {}, delete=True, item_kind=kind, **kwargs)

    def commit(self, plan=None):
        return self.wait(self.library.start_commit((plan or self.library.preview_queue())['id']))

    def folder_paths(self, **kwargs):
        return self.library.empty_folders(paths_only=True, **kwargs)['paths']

    def test_strict_emptiness_and_no_symlink_traversal(self):
        leaf = self.folder()
        hidden = self.folder('hidden'); (hidden / '.keep').touch()
        sidecar = self.folder('artwork'); (sidecar / 'cover.jpg').touch()
        links = self.folder('links'); (links / 'broken').symlink_to(self.base / 'missing')
        outside = self.base / 'outside'; outside.mkdir(); (outside / 'empty').mkdir()
        (self.root / 'alias').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.scan()['state'], 'complete')
        self.assertEqual(self.folder_paths(), [str(leaf)])
        self.assertEqual(self.library.tracks()['items'], [])

    def test_overlapping_roots_deduplicate_and_protect_all_roots(self):
        leaf = self.folder('nested/leaf')
        self.library.add_root(str(leaf.parent))
        empty_root = self.folder('empty-root'); self.library.add_root(str(empty_root))
        self.scan()
        self.assertEqual(self.folder_paths(), [str(leaf)])
        self.assertEqual(self.folder_paths(root=str(self.root)), [str(leaf)])
        self.assertEqual(self.library.empty_folders(root=str(self.root))['items'][0]['relative_path'], 'nested/leaf')
        for root in (self.root, leaf.parent, empty_root):
            with self.assertRaisesRegex(LibraryError, 'roots cannot be deleted'):
                self.stage(root, 'folder')

    def test_search_sort_and_pagination(self):
        for name in ('Zebra', 'alpha', 'Beta'):
            self.folder(name)
        self.scan()
        data = self.library.empty_folders(sort='title', limit=1, offset=1)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['items'][0]['name'], 'Beta')
        self.assertEqual(len(self.folder_paths(query='ALPHA')), 1)
        self.assertEqual(self.library.empty_folders(sort='title', direction='desc')['items'][0]['name'], 'Zebra')
        with self.assertRaises(LibraryError):
            self.library.empty_folders(sort='artist')

    def test_stage_delete_replaces_edit_and_does_not_touch_disk(self):
        path = self.track()
        self.library.queue_add([str(path)], {'title': 'new title'})
        self.stage(path)
        queued = self.library.queue_items()[0]
        self.assertEqual(queued['operation'], 'delete')
        self.assertEqual(queued['edits'], {})
        self.assertTrue(path.exists())
        plan = self.library.preview_queue()
        self.assertTrue(path.exists())
        self.assertEqual(plan['items'][0]['operation'], 'delete')
        result = self.commit(plan)
        self.assertEqual(result['state'], 'complete', result['errors'])
        self.assertFalse(path.exists())
        self.assertEqual(self.library.tracks()['total'], 0)
        self.assertEqual(self.library.queue_items(), [])
        detail = self.library.job_details(result['id'])['items'][0]
        self.assertEqual((detail['operation'], detail['item_kind'], detail['state']), ('delete', 'track', 'complete'))
        self.assertIsNotNone(detail['completed_at'])

    def test_folder_delete_refreshes_new_empty_parent_without_deleting_it(self):
        leaf = self.folder(); self.scan(); self.stage(leaf, 'folder')
        result = self.commit()
        self.assertEqual(result['state'], 'complete', result['errors'])
        self.assertFalse(leaf.exists()); self.assertTrue(leaf.parent.exists())
        self.assertEqual(self.folder_paths(), [str(leaf.parent)])

    def test_cancel_deletion_by_queue_or_review_keeps_target(self):
        for kind, path in (('track', self.track()), ('folder', self.folder())):
            self.scan(); self.stage(path, kind)
            self.library.queue_add([str(path)], {}, delete=False, item_kind=kind)
            self.assertEqual(self.library.queue_items(), [])
            self.stage(path, kind)
            plan = self.library.preview_queue({'paths': [str(path)], 'delete': False, 'item_kind': kind})
            self.assertEqual(plan['items'][0]['operation'], 'cancel_delete')
            self.assertEqual(self.commit(plan)['state'], 'complete')
            self.assertTrue(path.exists()); self.assertEqual(self.library.queue_items(), [])

    def test_mixed_file_folder_queue_and_untouched_actions(self):
        path = self.track(); other = self.track('other.mp3'); folder = self.folder()
        self.scan(); self.stage(path); self.stage(folder, 'folder')
        self.library.queue_add([str(other)], {'title': 'updated'})
        # Editing an ordinary field in a mixed selection preserves an existing deletion.
        self.library.queue_add([str(path), str(other)], {'artist': 'Artist'})
        items = {item['path']: item for item in self.library.queue_items()}
        self.assertEqual(items[str(path)]['operation'], 'delete')
        self.assertEqual(items[str(other)]['operation'], 'edit')
        with patch('tagger.library.read_file', return_value={'tags': {key: [] if key == 'genre' else '' for key in FIELDS}, 'format': 'MP3', 'duration': 1, 'artwork': False}):
            plan = self.library.preview_queue()
            with patch('tagger.library.apply_tags') as apply:
                result = self.commit(plan)
                self.assertEqual(apply.call_count, 1)
        self.assertEqual(result['state'], 'complete', result['errors'])
        self.assertFalse(path.exists()); self.assertFalse(folder.exists()); self.assertTrue(other.exists())

    def test_changed_file_between_stage_and_review_is_rejected(self):
        path = self.track(); self.stage(path); path.write_bytes(b'changed')
        plan = self.library.preview_queue()
        self.assertEqual(plan['items'], [])
        self.assertIn('changed', plan['errors'][0]['error'])
        self.assertTrue(path.exists())

    def test_replaced_file_with_same_size_and_mtime_is_rejected(self):
        path = self.track(); self.stage(path); plan = self.library.preview_queue()
        original = path.stat(); path.rename(self.base / 'original')
        path.write_bytes(b'test audio'); os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        result = self.commit(plan)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(path.exists()); self.assertEqual(len(self.library.queue_items()), 1)

    def test_folder_becoming_nonempty_after_review_is_not_removed(self):
        folder = self.folder(); self.scan(); self.stage(folder, 'folder'); plan = self.library.preview_queue()
        child = folder / '.keep'; child.touch()
        result = self.commit(plan)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(child.exists()); self.assertEqual(self.folder_paths(), [])

    def test_symlink_target_and_ancestor_replacement_are_rejected(self):
        path = self.track('artist/song.mp3'); self.stage(path); plan = self.library.preview_queue()
        outside = self.base / 'outside'; path.parent.rename(outside)
        path.parent.symlink_to(outside, target_is_directory=True)
        result = self.commit(plan)
        self.assertEqual(result['state'], 'partial'); self.assertTrue((outside / path.name).exists())
        self.library.queue_clear(); path.parent.unlink(); outside.rename(path.parent)
        path.rename(self.base / 'original'); path.symlink_to(self.base / 'original')
        with self.assertRaises(LibraryError):
            self.stage(path)

    def test_unindexed_outside_and_wrong_type_rejected(self):
        path = self.root / 'unindexed.mp3'; path.touch()
        with self.assertRaises(LibraryError): self.stage(path)
        outside = self.base / 'outside.mp3'; outside.touch()
        with self.assertRaises(LibraryError): self.stage(outside)
        folder = self.folder(); self.scan()
        with self.assertRaises(LibraryError): self.stage(folder)
        track = self.track()
        with self.assertRaises(LibraryError): self.stage(track, 'folder')
        for delete in ('true', 1, []):
            with self.assertRaises(ValueError): self.library.queue_add([str(track)], {}, delete=delete)

    def test_queue_revisions_reservations_and_cancellation(self):
        path = self.track(); self.stage(path)
        revision = self.library.queue_items()[0]['revision']
        self.stage(path)
        with self.assertRaisesRegex(LibraryError, 'queue changed'):
            self.stage(path, revisions={str(path): revision})
        plan = self.library.preview_queue(); self.stage(path)
        with self.assertRaisesRegex(LibraryError, 'queue changed'):
            self.library.start_commit(plan['id'])
        with patch.object(self.library, '_launch_next_job'):
            job = self.library.start_commit(self.library.preview_queue()['id'])
            self.assertEqual(self.library.queue_items(), [])
            with self.assertRaisesRegex(LibraryError, 'already'):
                self.stage(path)
            self.library.cancel_job(job)
        self.assertTrue(path.exists()); self.assertEqual(len(self.library.queue_items()), 1)

    def test_queued_deletion_survives_restart(self):
        path = self.track(); self.stage(path)
        with patch.object(self.library, '_launch_next_job'):
            job = self.library.start_commit(self.library.preview_queue()['id'])
        self.library = Library(self.base / 'state', allowed_volume=self.base)
        self.assertEqual(self.wait(job)['state'], 'complete')
        self.assertFalse(path.exists())

    def test_cancel_running_batch_does_not_delete_remaining_targets(self):
        first = self.track('first.mp3'); second = self.track('second.mp3')
        self.stage(first); self.stage(second)
        original = self.library._commit_delete
        def deleting(item):
            original(item)
            with self.library._db() as db:
                db.execute("UPDATE jobs SET state='cancelling' WHERE state='running'")
        with patch.object(self.library, '_commit_delete', side_effect=deleting):
            result = self.commit()
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(first.exists()); self.assertTrue(second.exists())
        self.assertEqual([item['path'] for item in self.library.queue_items()], [str(second)])

    def test_permission_errors_are_partial_not_empty_results(self):
        folder = self.folder()
        original = os.open
        def denied(path, *args, **kwargs):
            if str(path) == self.root.name:
                raise PermissionError('denied')
            return original(path, *args, **kwargs)
        with patch('os.open', side_effect=denied):
            result = self.scan()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(self.folder_paths(), []); self.assertTrue(folder.exists())

    def test_cancel_scan_preserves_previous_snapshot(self):
        folder = self.folder(); self.scan()
        with patch.object(self.library, '_launch_next_job'):
            job = self.library.start_folder_scan()
        with self.library._db() as db:
            db.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (job,))
        self.library.scan_empty_folders(job)
        self.assertEqual(self.library.job(job)['state'], 'cancelled')
        self.assertEqual(self.folder_paths(), [str(folder)])

    def test_batch_limit_counts_files_and_folders(self):
        path = self.track(); folder = self.folder(); self.scan()
        with self.library._db() as db:
            db.executemany('INSERT INTO queued(path,data,revision) VALUES (?,?,?)',
                           [(str(self.root / ('queued%d.mp3' % i)), json.dumps({'edits': {}, 'mode': 'none', 'rename': 'none'}), str(i)) for i in range(4999)])
        self.stage(path)
        with self.assertRaisesRegex(LibraryError, '5,000'):
            self.stage(folder, 'folder')
        self.assertEqual(len(self.library.queue_items()), 5000)
        self.assertTrue(path.exists()); self.assertTrue(folder.exists())

    def test_migration_defaults_old_jobs_and_requires_scan_for_legacy_track_identity(self):
        path = self.track()
        with self.library._db() as db:
            db.execute('UPDATE tracks SET device=NULL,inode=NULL')
            db.execute("INSERT INTO job_items(job,ordinal,source,destination,before_tags,after_tags,artwork_changed,state,error) VALUES ('old',0,?,?,'{}','{}',0,'complete','')", (str(path), str(path)))
        self.library = Library(self.base / 'state', allowed_volume=self.base, resume_jobs=False)
        with self.library._db() as db:
            row = db.execute("SELECT * FROM job_items WHERE job='old'").fetchone()
        self.assertEqual((row['operation'], row['item_kind']), ('edit', 'track'))
        with self.assertRaisesRegex(LibraryError, 'Scan folders'):
            self.stage(path)

    def test_file_deletion_does_not_remove_its_parent(self):
        path = self.track('artist/song.mp3'); self.stage(path)
        self.assertEqual(self.commit()['state'], 'complete')
        self.assertTrue(path.parent.exists()); self.assertIn(str(path.parent), self.folder_paths())

    def test_folder_scan_request_is_coalesced_and_survives_restart(self):
        self.folder()
        with patch.object(self.library, '_launch_next_job'):
            job = self.library.start_folder_scan()
            self.assertEqual(self.library.start_folder_scan(), job)
        self.library = Library(self.base / 'state', allowed_volume=self.base)
        self.assertEqual(self.wait(job)['state'], 'complete')
        self.assertEqual(len(self.folder_paths()), 1)

    def test_missing_file_deletion_can_be_cancelled_without_filesystem_access(self):
        path = self.track(); self.stage(path); path.unlink()
        self.library.queue_add([str(path)], {}, delete=False)
        self.assertEqual(self.library.queue_items(), [])

    def test_folder_replaced_since_discovery_requires_new_scan(self):
        folder = self.folder(); self.scan()
        folder.rename(self.base / 'original-folder'); folder.mkdir()
        with self.assertRaisesRegex(LibraryError, 'changed since discovery'):
            self.stage(folder, 'folder')
        self.scan(); self.stage(folder, 'folder')
        self.assertEqual(self.commit()['state'], 'complete')
        self.assertTrue((self.base / 'original-folder').exists())

    def test_nonrecursive_remove_rejects_late_arriving_contents(self):
        folder = self.folder(); self.scan(); self.stage(folder, 'folder'); plan = self.library.preview_queue()
        original = os.rmdir
        def arrival(name, *args, **kwargs):
            (folder / 'new-file').touch()
            return original(name, *args, **kwargs)
        with patch('tagger.deletion.os.rmdir', side_effect=arrival):
            result = self.commit(plan)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue((folder / 'new-file').exists())

    def test_scoped_refresh_preserves_other_roots(self):
        first = self.folder()
        other_root = self.base / 'other'; other_root.mkdir(); other = other_root / 'empty'; other.mkdir()
        self.library.add_root(str(other_root)); self.scan()
        (first / 'keep').touch(); self.scan(str(self.root))
        self.assertEqual(self.folder_paths(), [str(other)])
        with self.assertRaises(LibraryError): self.library.start_folder_scan(str(self.base))

    def test_actual_legacy_job_table_migration(self):
        with self.library._db() as db:
            db.execute('DROP TABLE job_items')
            db.execute('CREATE TABLE job_items(job TEXT, ordinal INTEGER, source TEXT, destination TEXT, '
                       'before_tags TEXT, after_tags TEXT, artwork_changed INTEGER, state TEXT, error TEXT)')
            db.execute("INSERT INTO job_items VALUES ('old',0,'source','destination','{}','{}',0,'complete','')")
        self.library = Library(self.base / 'state', allowed_volume=self.base, resume_jobs=False)
        with self.library._db() as db:
            row = db.execute("SELECT * FROM job_items WHERE job='old'").fetchone()
        self.assertEqual(row['operation'], 'edit'); self.assertEqual(row['item_kind'], 'track')
        self.assertIsNone(row['completed_at'])

    def test_select_entire_folder_list_has_combined_batch_limit(self):
        with self.library._db() as db:
            db.executemany('INSERT INTO empty_folders VALUES (?,?,?,?,?)',
                           [(str(self.root / str(i)), str(self.root), str(i), str(i), '{}') for i in range(5001)])
        self.assertEqual(self.library.empty_folders(limit=100)['total'], 5001)
        with self.assertRaisesRegex(LibraryError, '5,000'):
            self.folder_paths()

    def test_confirm_setting_does_not_change_backend_deletion_semantics(self):
        for setting in ('true', 'false'):
            path = self.track(setting + '.mp3')
            self.library.set_setting('confirm_commit', setting)
            self.stage(path)
            plan = self.library.preview_queue()
            self.assertTrue(path.exists())
            self.assertEqual(self.commit(plan)['state'], 'complete')
            self.assertFalse(path.exists())

    def test_mixed_import_does_not_parse_files_already_queued_for_deletion(self):
        deleting = self.track('does not match.mp3')
        editing = self.track('03 - Matching title.mp3')
        self.stage(deleting)
        self.library.queue_add([str(deleting), str(editing)], {}, import_tags='track_title')
        queued = {item['path']: item for item in self.library.queue_items()}
        self.assertEqual(queued[str(deleting)]['operation'], 'delete')
        self.assertEqual(queued[str(editing)]['edits']['title'], 'Matching title')
