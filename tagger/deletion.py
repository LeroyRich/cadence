"""Empty-directory discovery and guarded, explicitly staged deletion operations."""
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class DeletionMixin:
    # LibraryError is supplied by Library to keep this module independent of tag parsing.
    @contextmanager
    def _directory_fd(self, path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts:
            raise self.operation_error('Invalid absolute path')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _identity(info, parent):
        return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
                'parent_device': parent.st_dev, 'parent_inode': parent.st_ino}

    def _deletion_target(self, value, kind, expected=None, remove=False):
        if kind not in ('track', 'folder'):
            raise self.operation_error('Invalid item kind')
        path = Path(value)
        if not path.is_absolute() or str(path) != value or '..' in path.parts:
            raise self.operation_error('Invalid deletion path')
        roots = [Path(root) for root in self.roots()]
        if path in roots:
            raise self.operation_error('Library roots cannot be deleted')
        matches = [root for root in roots if root in path.parents]
        if not matches:
            raise self.operation_error('Item is outside approved folders')
        root = max(matches, key=lambda item: len(item.parts))
        with self._directory_fd(path.parent) as parent_fd:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            valid_type = stat.S_ISDIR(info.st_mode) if kind == 'folder' else stat.S_ISREG(info.st_mode)
            if not valid_type:
                raise self.operation_error('Deletion target type changed or is a symbolic link')
            identity = self._identity(info, os.fstat(parent_fd))
            if expected is not None and identity != expected:
                raise self.operation_error('Deletion target changed; scan and review it again')
            if kind == 'folder':
                folder_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    if self._identity(os.fstat(folder_fd), os.fstat(parent_fd)) != identity:
                        raise self.operation_error('Folder changed during validation')
                    if os.listdir(folder_fd):
                        raise self.operation_error('Folder is no longer empty')
                finally:
                    os.close(folder_fd)
            # Check again immediately before the directory-relative operation.
            if remove:
                if self._identity(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False), os.fstat(parent_fd)) != identity:
                    raise self.operation_error('Deletion target changed during validation')
                if kind == 'folder':
                    os.rmdir(path.name, dir_fd=parent_fd)
                else:
                    os.unlink(path.name, dir_fd=parent_fd)
        return str(root), identity

    def _deletion_snapshot(self, path, kind, expected=None):
        root, identity = self._deletion_target(path, kind, expected)
        with self._db() as db:
            table = 'empty_folders' if kind == 'folder' else 'tracks'
            row = db.execute('SELECT * FROM {} WHERE path=?'.format(table), (path,)).fetchone()
        if not row:
            raise self.operation_error('Folder is not indexed; refresh Empty folders' if kind == 'folder' else 'File is not indexed')
        if kind == 'folder':
            indexed = json.loads(row['identity'])
            if identity != indexed:
                raise self.operation_error('Folder changed since discovery; refresh Empty folders')
        else:
            if row['device'] is None or row['inode'] is None:
                raise self.operation_error('Run Scan folders once before deleting files indexed by an older version')
            if any(identity[key] != row[key] for key in ('device', 'inode', 'size', 'mtime_ns')):
                raise self.operation_error('File changed since indexing; scan and review it again')
        return {'root': root, 'identity': identity,
                'before': json.loads(row['tags']) if kind == 'track' else {},
                'format': row['format'] if kind == 'track' else ''}

    def start_folder_scan(self, root=''):
        if root and root not in self.roots():
            raise self.operation_error('Unknown library root')
        job_id = uuid.uuid4().hex
        with self.lock, self._db() as db:
            for row in db.execute("SELECT jobs.id,plans.data FROM jobs JOIN plans ON plans.id=jobs.id "
                                  "WHERE jobs.kind='folder_scan' AND jobs.state IN ('queued','running')"):
                if json.loads(row['data']).get('root', '') == root:
                    return row['id']
            db.execute('INSERT INTO plans VALUES (?,?,?)', (job_id, time.time(), json.dumps({'root': root})))
            db.execute("INSERT INTO jobs(id,kind,state,done,total,errors,created) VALUES (?,'folder_scan','queued',0,0,'[]',?)",
                       (job_id, time.time()))
        self._launch_next_job()
        return job_id

    def _discover_empty_folders(self, job_id, root=''):
        roots = self.roots()
        if root and root not in roots:
            raise self.operation_error('Library root was removed')
        scope = [root] if root else roots
        found, errors = {}, []
        done = 0
        for root_text in scope:
            try:
                with self._directory_fd(root_text) as root_fd:
                    def onerror(error):
                        errors.append({'path': root_text, 'error': str(error)})
                    for current, directories, files, fd in os.fwalk('.', dir_fd=root_fd, follow_symlinks=False, onerror=onerror):
                        if done % 32 == 0 and self.job(job_id)['state'] == 'cancelling':
                            return errors, True, done
                        done += 1
                        path = str(Path(root_text) / current)
                        if not directories and not files and path not in roots:
                            try:
                                owner, identity = self._deletion_target(path, 'folder')
                                found[path] = (path, owner, Path(path).name, str(Path(path).relative_to(owner)), json.dumps(identity))
                            except OSError as error:
                                errors.append({'path': path, 'error': str(error)})
                            except self.operation_error as error:
                                errors.append({'path': path, 'error': str(error)})
                        if done % 100 == 0 and self.job(job_id)['kind'] == 'folder_scan':
                            self._update_job(job_id, done=done)
            except OSError as error:
                errors.append({'path': root_text, 'error': str(error)})
        # Publish a complete traversal, never a cancelled partial snapshot. Failed
        # subtrees are omitted rather than represented as empty.
        with self.lock, self._db() as db:
            if root:
                db.execute('DELETE FROM empty_folders WHERE instr(path,?)=1', (root.rstrip('/') + '/',))
            else:
                db.execute('DELETE FROM empty_folders')
            db.executemany('INSERT OR REPLACE INTO empty_folders VALUES (?,?,?,?,?)', found.values())
        return errors, False, done

    def scan_empty_folders(self, job_id, root=''):
        errors, cancelled, done = self._discover_empty_folders(job_id, root)
        self._update_job(job_id, state='cancelled' if cancelled else 'partial' if errors else 'complete',
                         done=done, total=done, errors=errors[:100])

    def empty_folders(self, query='', root='', offset=0, limit=100, sort='path', direction='asc', paths_only=False):
        if root and root not in self.roots():
            raise self.operation_error('Unknown library root')
        if sort not in ('title', 'path') or direction not in ('asc', 'desc'):
            raise self.operation_error('Folders can be sorted by Title / File or Path')
        where, args = [], []
        if root:
            where.append('instr(path,?)=1')
            args.append(root.rstrip('/') + '/')
        # Python casefold matches the track search convention, including Unicode.
        with self._db() as db:
            rows = db.execute('SELECT * FROM empty_folders' + (' WHERE ' + ' AND '.join(where) if where else ''), args).fetchall()
        items = []
        for row in rows:
            relative = str(Path(row['path']).relative_to(root or row['root']))
            if query.strip().casefold() not in (row['name'] + ' ' + relative).casefold():
                continue
            items.append({'path': row['path'], 'root': row['root'], 'relative_path': relative,
                          'name': row['name'], 'item_kind': 'folder', 'tags': {}, 'artwork': False})
        items.sort(key=lambda item: ((item['name'] if sort == 'title' else item['relative_path']).casefold(), item['path']),
                   reverse=direction == 'desc')
        if paths_only:
            if len(items) > 5000:
                raise self.operation_error('This list exceeds the 5,000-item batch limit. Narrow the search or library filter.')
            return {'paths': [item['path'] for item in items]}
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 1000))
        return {'items': items[offset:offset + limit], 'total': len(items), 'genres_need_scan': False}

    @staticmethod
    def _validate_delete_options(delete, item_kind):
        if delete is not None and not isinstance(delete, bool):
            raise ValueError('Invalid deletion option')
        if item_kind not in ('track', 'folder'):
            raise ValueError('Invalid item kind')

    def _queue_delete(self, paths, delete, item_kind, revisions):
        snapshots = {path: self._deletion_snapshot(path, item_kind) for path in paths} if delete else {}
        with self.lock, self._db() as db:
            rows = {row['path']: row for row in db.execute('SELECT * FROM queued') if row['path'] in paths}
            self._check_queue_revisions(paths, revisions, {path: row['revision'] for path, row in rows.items()})
            active = {row[0] for row in db.execute("SELECT item.source FROM job_items item JOIN jobs job ON item.job=job.id "
                                                  "WHERE job.state IN ('queued','running','cancelling')")}
            if active.intersection(paths) or any(row['inflight_job'] for row in rows.values()):
                raise self.operation_error('A selected item is already in a queued or running job')
            count = db.execute('SELECT count(*) FROM queued WHERE inflight_job IS NULL').fetchone()[0]
            if delete and count + len(set(paths) - set(rows)) > 5000:
                raise self.operation_error('The queue is limited to 5,000 items')
            for path in paths:
                current = json.loads(rows[path]['data']) if path in rows else {}
                if current and current.get('item_kind', 'track') != item_kind:
                    raise self.operation_error('Queued item kind changed')
                if delete is False:
                    if current.get('operation') == 'delete':
                        db.execute('DELETE FROM queued WHERE path=?', (path,))
                elif delete is True:
                    updated = {'operation': 'delete', 'item_kind': item_kind, 'identity': snapshots[path]['identity'],
                               'edits': {}, 'mode': 'none', 'rename': 'none', 'artwork_id': None}
                    db.execute('INSERT OR REPLACE INTO queued(path,data,revision) VALUES (?,?,?)',
                               (path, json.dumps(updated), uuid.uuid4().hex))
                elif current.get('operation') == 'delete':
                    self._deletion_snapshot(path, item_kind, current['identity'])
            self._cleanup_queue_artwork(db)
        return {'items': self.queue_items()}

    def _preview_delete(self, operation):
        kind = operation['item_kind']
        snapshot = self._deletion_snapshot(operation['path'], kind, operation.get('identity'))
        return {'source': operation['path'], 'destination': operation['path'], 'operation': 'delete',
                'item_kind': kind, 'root': snapshot['root'], 'identity': snapshot['identity'],
                'before': snapshot['before'], 'after': {}, 'format': snapshot['format'],
                'artwork_changed': False, 'edits': {}, 'queue_revision': operation.get('queue_revision')}

    def _commit_delete(self, item):
        # Keep approved-root membership stable during validation and removal.
        with self.lock:
            snapshot = self._deletion_snapshot(item['source'], item['item_kind'], item['identity'])
            if snapshot['root'] != item['root']:
                raise self.operation_error('Approved folder changed after preview')
            self._deletion_target(item['source'], item['item_kind'], item['identity'], remove=True)
            with self._db() as db:
                db.execute('DELETE FROM tracks WHERE path=?', (item['source'],))
                db.execute('DELETE FROM track_genres WHERE path=?', (item['source'],))
                db.execute('DELETE FROM empty_folders WHERE path=?', (item['source'],))
            # Refresh the immediate parent even if the rest of this batch is later
            # cancelled. Discovering it never authorizes removing it.
            parent = Path(item['source']).parent
            try:
                owner, identity = self._deletion_target(str(parent), 'folder')
            except (OSError, self.operation_error):
                pass
            else:
                with self._db() as db:
                    db.execute('INSERT OR REPLACE INTO empty_folders VALUES (?,?,?,?,?)',
                               (str(parent), owner, parent.name, str(parent.relative_to(owner)), json.dumps(identity)))
