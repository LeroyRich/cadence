"""Library index and explicit, staged file operations."""

import base64
import binascii
import json
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from .deletion import DeletionMixin
from .formats import EXTENSIONS, FIELDS, apply_tags, id3_genre_frame, read_file, inspect_artwork
from .genres import (apply_genre_options, clean_genres, genre_needs_review, genre_values,
                     normalize_edits, validate_genre_options, validate_genres)


class LibraryError(ValueError):
    pass


def clean_component(value, label="Artist or album"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if value in ("", ".", ".."):
        raise LibraryError("{} is missing".format(label))
    return value[:180]


def album_folder_component(tags):
    album = clean_component(tags.get("album") or "", "Album")
    date = (tags.get("date") or "").strip()
    year = re.match(r"^([12][0-9]{3})(?:$|[-/. ])", date)
    if not year or re.search(r" \([12][0-9]{3}\)$", album):
        return album
    suffix = " ({})".format(year.group(1))
    return album[:180 - len(suffix)].rstrip(" .") + suffix


def sort_values(path, root, tags):
    return ((tags.get("title") or Path(path).name).casefold(),
            (tags.get("artist") or "").casefold(),
            (tags.get("album") or "").casefold(),
            Path(path).relative_to(root).as_posix().casefold())


def tag_number(tags, field):
    value = json.loads(tags).get(field) or ""
    match = re.match(r"\s*([0-9]+)", str(value))
    return int(match.group(1)) if match else -1


TAG_FILTERS = {"missing_title": 1, "missing_artist": 2, "missing_album": 4,
               "missing_track": 8, "missing_year": 16, "lowercase_title": 32,
               "uppercase_title": 64, "missing_genre": 128, "genre_review": 256,
               "invalid_characters": 512}


def tags_have_invalid_characters(tags):
    for field in FIELDS:
        value = tags.get(field)
        values = value if isinstance(value, list) else (value,)
        for item in values:
            if isinstance(item, str) and any(
                    character == "\ufffd" or unicodedata.category(character) in ("Cc", "Cs")
                    for character in item):
                return True
    return False


def tag_filter_flags(tags):
    flags = 0
    if tags_have_invalid_characters(tags):
        flags |= TAG_FILTERS["invalid_characters"]
    genres = genre_values(tags.get("genre"))
    if not any(value.strip() for value in genres):
        flags |= TAG_FILTERS["missing_genre"]
    if genre_needs_review(genres):
        flags |= TAG_FILTERS["genre_review"]
    for field in ("title", "artist", "album", "track"):
        if not (tags.get(field) or "").strip():
            flags |= TAG_FILTERS["missing_" + field]
    if not re.match(r"^[12][0-9]{3}(?:$|[-/. ])", (tags.get("date") or "").strip()):
        flags |= TAG_FILTERS["missing_year"]
    title = tags.get("title") or ""
    if title.islower():
        flags |= TAG_FILTERS["lowercase_title"]
    if title.isupper():
        flags |= TAG_FILTERS["uppercase_title"]
    return flags


def normalize_track_title(value):
    return re.sub(r"[^\W_]+(?:['’][^\W_]+)*",
                  lambda match: match.group(0)[:1].upper() + match.group(0)[1:].lower(), value)


def apply_title_options(value, remove_text, normalize):
    result = value.replace(remove_text, "") if remove_text else value
    if normalize:
        result = normalize_track_title(result)
    return result.strip() if result != value else result


def tags_from_filename(path, pattern):
    if pattern == "none":
        return {}
    patterns = {
        "title": r"(?P<title>.+)",
        "track_title": r"(?P<track>[0-9]+) - (?P<title>.+)",
        "artist_title": r"(?P<artist>.+?) - (?P<title>.+)",
        "artist_album_track_title": r"(?P<artist>.+?) - (?P<album>.+?) - (?P<track>[0-9]+) - (?P<title>.+)",
    }
    if pattern not in patterns:
        raise LibraryError("Unknown filename import option")
    stem = Path(path).stem
    match = re.fullmatch(patterns[pattern], stem)
    if not match:
        raise LibraryError("Filename does not match the selected import format: {}".format(Path(path).name))
    tags = {field: value.strip() for field, value in match.groupdict().items()}
    if any(not value or len(value) > 1000 for value in tags.values()):
        raise LibraryError("Filename contains an empty or oversized tag: {}".format(Path(path).name))
    if "track" in tags:
        if int(tags["track"]) < 1:
            raise LibraryError("Filename track number must be greater than zero: {}".format(Path(path).name))
        tags["track"] = str(int(tags["track"]))
    if pattern == "artist_album_track_title":
        parts = stem.split(" - ")
        candidates = [index for index in range(2, len(parts) - 1) if re.fullmatch(r"[0-9]+", parts[index])]
        if len(candidates) != 1 or candidates[0] != 2:
            raise LibraryError("Filename has ambiguous separators: {}".format(Path(path).name))
    return tags


class Library(DeletionMixin):
    operation_error = LibraryError

    def __init__(self, state_dir, allowed_volume=None, resume_jobs=True):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.stat().st_uid == os.geteuid():
            self.state_dir.chmod(0o700)
        self.db_path = self.state_dir / "library.sqlite3"
        if self.db_path.is_symlink():
            raise LibraryError("State database cannot be a symbolic link")
        backup_dir = self.state_dir / "backups"
        self.allowed_volume = Path(allowed_volume).resolve() if allowed_volume else None
        self.lock = threading.RLock()
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS tracks(
                    path TEXT PRIMARY KEY, root TEXT NOT NULL, format TEXT NOT NULL,
                    tags TEXT NOT NULL, duration INTEGER NOT NULL, artwork INTEGER NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, seen INTEGER NOT NULL,
                    sort_title TEXT, sort_artist TEXT, sort_album TEXT, sort_path TEXT
                );
                CREATE INDEX IF NOT EXISTS tracks_root ON tracks(root);
                CREATE TABLE IF NOT EXISTS empty_folders(
                    path TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT NOT NULL,
                    relative_path TEXT NOT NULL, identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, created REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    done INTEGER NOT NULL, total INTEGER NOT NULL, errors TEXT NOT NULL, created REAL NOT NULL,
                    started_at REAL, stopped_at REAL);
                CREATE TABLE IF NOT EXISTS job_items(
                    job TEXT NOT NULL, ordinal INTEGER NOT NULL, source TEXT NOT NULL,
                    destination TEXT NOT NULL, before_tags TEXT NOT NULL, after_tags TEXT NOT NULL,
                    artwork_changed INTEGER NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY(job,ordinal)
                );
                CREATE INDEX IF NOT EXISTS job_items_source ON job_items(source);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS remembered_sessions(
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remembered_sessions_expires ON remembered_sessions(expires);
                CREATE TABLE IF NOT EXISTS queued(
                    path TEXT PRIMARY KEY, data TEXT NOT NULL, revision TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queued_artwork(
                    id TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS track_genres(path TEXT NOT NULL, genre TEXT NOT NULL,
                    genre_key TEXT NOT NULL, PRIMARY KEY(path,genre_key));
                CREATE INDEX IF NOT EXISTS track_genres_key ON track_genres(genre_key);
            """)
            if resume_jobs:
                db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('running','cancelling')")
                db.execute("UPDATE job_items SET state='interrupted' WHERE state IN ('pending','running') "
                           "AND job IN (SELECT id FROM jobs WHERE state='interrupted')")
                db.execute("DELETE FROM plans WHERE id IN (SELECT id FROM jobs WHERE state='interrupted')")
            db.execute("DROP TABLE IF EXISTS history")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for column in ("started_at", "stopped_at"):
                if column not in job_columns:
                    db.execute("ALTER TABLE jobs ADD COLUMN {} REAL".format(column))
            item_columns = {row["name"] for row in db.execute("PRAGMA table_info(job_items)")}
            if "completed_at" not in item_columns:
                db.execute("ALTER TABLE job_items ADD COLUMN completed_at REAL")
            for column, default in (("operation", "edit"), ("item_kind", "track")):
                if column not in item_columns:
                    db.execute("ALTER TABLE job_items ADD COLUMN {} TEXT NOT NULL DEFAULT '{}'".format(column, default))
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tracks)")}
            for column in ("sort_title", "sort_artist", "sort_album", "sort_path"):
                if column not in columns:
                    db.execute("ALTER TABLE tracks ADD COLUMN {} TEXT".format(column))
            for column in ("device", "inode"):
                if column not in columns:
                    db.execute("ALTER TABLE tracks ADD COLUMN {} INTEGER".format(column))
            if "filter_flags" not in columns:
                db.execute("ALTER TABLE tracks ADD COLUMN filter_flags INTEGER")
                db.execute("UPDATE tracks SET size=-1 WHERE format='WMA/ASF'")
            if "genre_indexed" not in columns:
                db.execute("ALTER TABLE tracks ADD COLUMN genre_indexed INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE tracks SET filter_flags=NULL")
            if "artwork_indexed" not in columns:
                db.execute("ALTER TABLE tracks ADD COLUMN artwork_indexed INTEGER NOT NULL DEFAULT 0")
            flags_version = db.execute("SELECT value FROM settings WHERE key='tag_filter_flags_version'").fetchone()
            if flags_version is None or flags_version[0] != "2":
                db.execute("UPDATE tracks SET filter_flags=cadence_tag_flags(tags)")
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('tag_filter_flags_version','2')")
            queue_columns = {row["name"] for row in db.execute("PRAGMA table_info(queued)")}
            if "inflight_job" not in queue_columns:
                db.execute("ALTER TABLE queued ADD COLUMN inflight_job TEXT")
            if resume_jobs:
                db.execute("UPDATE queued SET inflight_job=NULL WHERE inflight_job IS NOT NULL "
                           "AND inflight_job NOT IN (SELECT id FROM jobs WHERE state='queued')")
        if self.db_path.stat().st_uid == os.geteuid():
            self.db_path.chmod(0o600)
        if backup_dir.is_symlink() or backup_dir.is_file():
            backup_dir.unlink()
        elif backup_dir.is_dir():
            shutil.rmtree(backup_dir)
        while True:
            with self._db() as db:
                rows = db.execute("""SELECT path,root,tags FROM tracks
                    WHERE sort_title IS NULL OR sort_artist IS NULL OR sort_album IS NULL OR sort_path IS NULL
                    OR filter_flags IS NULL
                    LIMIT 2000""").fetchall()
                if not rows:
                    break
                db.executemany("""UPDATE tracks SET sort_title=?,sort_artist=?,sort_album=?,sort_path=?,filter_flags=? WHERE path=?""",
                               [(*sort_values(row["path"], row["root"], json.loads(row["tags"])),
                                 tag_filter_flags(json.loads(row["tags"])), row["path"])
                                for row in rows])
        with self._db() as db:
            for column in ("sort_title", "sort_artist", "sort_album", "sort_path", "format", "duration"):
                db.execute("CREATE INDEX IF NOT EXISTS tracks_sort_{} ON tracks({})".format(column, column))
        if resume_jobs:
            self._launch_next_job()

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("cadence_tag_flags", 1, lambda tags: tag_filter_flags(json.loads(tags)))
            connection.create_function("cadence_tag_number", 2, tag_number)
            with connection:
                yield connection
        finally:
            connection.close()

    def get_setting(self, key, default=""):
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def set_password_and_revoke_sessions(self, value):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('password',?)", (value,))
            db.execute("DELETE FROM remembered_sessions")

    def remember_session(self, token_hash, csrf, created, expires):
        with self._db() as db:
            db.execute("DELETE FROM remembered_sessions WHERE expires<=?", (created,))
            db.execute("INSERT INTO remembered_sessions VALUES (?,?,?,?)",
                       (token_hash, csrf, created, expires))

    def remembered_session(self, token_hash, now):
        with self._db() as db:
            row = db.execute("SELECT csrf,expires FROM remembered_sessions WHERE token_hash=?",
                             (token_hash,)).fetchone()
            if row and row["expires"] <= now:
                db.execute("DELETE FROM remembered_sessions WHERE token_hash=?", (token_hash,))
                return None
        return row["csrf"] if row else None

    def forget_session(self, token_hash):
        with self._db() as db:
            db.execute("DELETE FROM remembered_sessions WHERE token_hash=?", (token_hash,))

    def roots(self):
        with self._db() as db:
            return [row[0] for row in db.execute("SELECT path FROM roots ORDER BY path")]

    def browse(self, value=None):
        base = self.allowed_volume or Path("/")
        try:
            folder = Path(value or base).resolve(strict=True)
            if folder != base and base not in folder.parents:
                raise LibraryError("Folder is outside the configured NAS volume")
            if base == Path("/") and folder != base and not re.match(r"^/volume[0-9]+(/|$)", str(folder)):
                raise LibraryError("Browse folders on DSM volumes only")
            if not folder.is_dir():
                raise LibraryError("Path is not a folder")
            folders = []
            for entry in folder.iterdir():
                try:
                    if folder == Path("/") and not re.fullmatch(r"volume[0-9]+", entry.name):
                        continue
                    if entry.is_dir() and not entry.is_symlink() and os.access(str(entry), os.R_OK | os.X_OK):
                        folders.append(str(entry))
                except PermissionError:
                    continue
            return {"path": str(folder), "folders": sorted(folders)[:500]}
        except PermissionError as error:
            raise LibraryError("Cadence cannot browse this folder. Grant the Cadence system internal user access to its shared folder in DSM.") from error

    def add_root(self, value):
        try:
            path = Path(value).resolve(strict=True)
        except PermissionError as error:
            raise LibraryError("Cadence cannot open this folder. Grant the Cadence system internal user Read/Write access in DSM.") from error
        if not path.is_dir() or not os.access(str(path), os.R_OK | os.W_OK | os.X_OK):
            raise LibraryError("Cadence needs Read/Write access to this folder. Grant it to the Cadence system internal user in DSM.")
        if self.allowed_volume:
            if path != self.allowed_volume and self.allowed_volume not in path.parents:
                raise LibraryError("Folder must be inside the approved NAS volume")
        elif not re.match(r"^/volume[0-9]+(/|$)", str(path)):
            raise LibraryError("Folder must be on a DSM volume")
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO roots VALUES (?)", (str(path),))
        return str(path)

    def add_root_and_scan(self, value):
        path = self.add_root(value)
        job_id = self.start_scan()
        return {"path": path, "job": job_id, "state": self.job(job_id)["state"]}

    def remove_root(self, value):
        with self.lock, self._db() as db:
            active = db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','cancelling') LIMIT 1").fetchone()
            if active:
                raise LibraryError("Wait for queued or running jobs before removing a library folder")
            db.execute("DELETE FROM queued WHERE path IN (SELECT path FROM tracks WHERE root=?)", (value,))
            db.execute("DELETE FROM queued WHERE path IN (SELECT path FROM empty_folders WHERE root=?)", (value,))
            db.execute("DELETE FROM empty_folders WHERE root=?", (value,))
            db.execute("DELETE FROM roots WHERE path=?", (value,))
            db.execute("DELETE FROM tracks WHERE root=?", (value,))
            db.execute("DELETE FROM track_genres WHERE path NOT IN (SELECT path FROM tracks)")
            self._cleanup_queue_artwork(db)

    def root_for(self, path, roots=None):
        resolved = Path(path).resolve(strict=True)
        candidates = roots if roots is not None else [Path(root) for root in self.roots()]
        matches = [root for root in candidates if resolved == root or root in resolved.parents]
        if not matches:
            raise LibraryError("File is outside approved folders")
        return max(matches, key=lambda item: len(item.parts))

    def _record(self, db, path, root, seen):
        details = read_file(path)
        stat = path.stat()
        sort_title, sort_artist, sort_album, sort_path = sort_values(path, root, details["tags"])
        db.execute("""INSERT OR REPLACE INTO tracks
            (path,root,format,tags,duration,artwork,size,mtime_ns,seen,sort_title,sort_artist,sort_album,sort_path,filter_flags,genre_indexed,artwork_indexed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
            (str(path), str(root), details["format"], json.dumps(details["tags"]),
             details["duration"], int(details["artwork"]), stat.st_size, stat.st_mtime_ns, seen,
             sort_title, sort_artist, sort_album, sort_path, tag_filter_flags(details["tags"])))
        db.execute("UPDATE tracks SET device=?,inode=? WHERE path=?", (stat.st_dev, stat.st_ino, str(path)))
        db.execute("DELETE FROM track_genres WHERE path=?", (str(path),))
        db.executemany("INSERT INTO track_genres VALUES (?,?,?)",
                       [(str(path), genre, genre.casefold()) for genre in clean_genres(details["tags"].get("genre"))])

    def scan(self, job_id):
        seen = time.time_ns()
        total = 0
        errors = []
        with self._db() as db:
            for root_text in self.roots():
                root = Path(root_text)
                if not root.is_dir():
                    errors.append({"path": root_text, "error": "Folder is unavailable"})
                    continue
                for current, directories, filenames in os.walk(str(root), followlinks=False):
                    directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
                    for filename in filenames:
                        path = Path(current) / filename
                        if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                            continue
                        try:
                            row = db.execute("SELECT size,mtime_ns,genre_indexed,artwork_indexed FROM tracks WHERE path=?", (str(path),)).fetchone()
                            stat = path.stat()
                            if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns and row[2] and row[3]:
                                db.execute("UPDATE tracks SET seen=?,root=?,sort_path=?,device=?,inode=? WHERE path=?",
                                           (seen, str(root), path.relative_to(root).as_posix().casefold(), stat.st_dev, stat.st_ino, str(path)))
                            else:
                                self._record(db, path, root, seen)
                            total += 1
                            if total % 25 == 0:
                                db.commit()
                            if total % 50 == 0:
                                self._update_job(job_id, done=total)
                                if self.job(job_id)["state"] == "cancelling":
                                    self._finish_scan(job_id, "cancelled", total, errors)
                                    return
                        except Exception as error:
                            errors.append({"path": str(path), "error": str(error)})
                db.commit()
                db.execute("DELETE FROM tracks WHERE root=? AND seen != ?", (root_text, seen))
                db.execute("DELETE FROM track_genres WHERE path NOT IN (SELECT path FROM tracks)")
                db.commit()
        folder_errors, cancelled, _ = self._discover_empty_folders(job_id)
        errors.extend(folder_errors)
        state = "cancelled" if cancelled or self.job(job_id)["state"] == "cancelling" else "partial" if errors else "complete"
        self._finish_scan(job_id, state, total, errors)

    def _finish_scan(self, job_id, state, total, errors):
        with self._db() as db:
            db.executemany("""INSERT INTO job_items
                (job,ordinal,source,destination,before_tags,after_tags,artwork_changed,state,error)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                           ((job_id, ordinal, issue["path"], issue["path"], "{}", "{}", 0, "failed", issue["error"])
                            for ordinal, issue in enumerate(errors)))
        self._update_job(job_id, state=state, done=total, total=total, errors=errors[:100])

    @staticmethod
    def _track_filter(query, root, track_filter="all", genre=""):
        clauses = []
        args = []
        if query:
            clauses.append("(path LIKE ? OR tags LIKE ?)")
            args.extend(["%" + query + "%"] * 2)
        if root:
            clauses.append("root=?")
            args.append(root)
        if track_filter in TAG_FILTERS:
            clauses.append("(COALESCE(filter_flags,cadence_tag_flags(tags)) & ?) != 0")
            args.append(TAG_FILTERS[track_filter])
        elif track_filter == "no_artwork":
            clauses.append("artwork=0")
        elif track_filter == "no_album_folder":
            relative = "substr(path,length(root)+2)"
            clauses.append("length({0})-length(replace({0},'/', ''))=1".format(relative))
        elif track_filter == "genre_search":
            if not isinstance(genre, str) or len(genre) > 1000:
                raise LibraryError("Invalid genre filter")
            if genre:
                clauses.append("EXISTS (SELECT 1 FROM track_genres WHERE track_genres.path=tracks.path AND genre_key=?)")
                args.append(genre.casefold())
            else:
                clauses.append("0")
        elif track_filter != "all":
            raise LibraryError("Unknown library filter")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    @staticmethod
    def _track_order(sort, direction):
        sort_columns = {"path": "sort_path", "title": "sort_title", "artist": "sort_artist",
                        "album": "sort_album", "track": "cadence_tag_number(tags, 'track')",
                        "disc": "cadence_tag_number(tags, 'disc')",
                        "format": "format", "duration": "duration"}
        if sort not in sort_columns or direction not in ("asc", "desc"):
            raise LibraryError("Invalid sort order")
        return " ORDER BY {} {}, path {}".format(sort_columns[sort], direction.upper(), direction.upper())

    def tracks(self, query="", root="", offset=0, limit=100, sort="path", direction="asc",
               track_filter="all", genre=""):
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        where, args = self._track_filter(query, root, track_filter, genre)
        order = self._track_order(sort, direction)
        with self._db() as db:
            count = db.execute("SELECT count(*) FROM tracks" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM tracks" + where + order + " LIMIT ? OFFSET ?",
                              args + [limit, offset]).fetchall()
            needs_scan = db.execute("SELECT 1 FROM tracks WHERE genre_indexed=0 LIMIT 1").fetchone() is not None
        return {"total": count, "items": [self._track_dict(row) for row in rows], "genres_need_scan": needs_scan}

    def genres(self, query=""):
        query = query.strip().casefold()[:1000].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._db() as db:
            rows = db.execute("""SELECT min(genre) AS genre FROM track_genres
                JOIN tracks ON tracks.path=track_genres.path
                WHERE genre_key LIKE ? ESCAPE '\\' GROUP BY genre_key ORDER BY genre_key LIMIT 50""",
                              ("%" + query + "%",)).fetchall()
        return {"genres": [row["genre"] for row in rows]}

    def genre_choices(self, root=""):
        where = " WHERE tracks.root=?" if root else ""
        with self._db() as db:
            rows = db.execute("""SELECT min(track_genres.genre) AS genre, genre_key, count(*) AS count
                FROM track_genres JOIN tracks ON tracks.path=track_genres.path{}
                GROUP BY genre_key ORDER BY genre_key""".format(where), (root,) if root else ()).fetchall()
        return {"genres": [dict(row) for row in rows]}

    def _ensure_genres(self, paths):
        roots = [Path(root) for root in self.roots()]
        with self._db() as db:
            for start in range(0, len(paths), 500):
                chunk = paths[start:start + 500]
                rows = db.execute("SELECT path,root,seen FROM tracks WHERE genre_indexed=0 AND path IN ({})".format(
                    ",".join("?" for _ in chunk)), chunk).fetchall()
                for row in rows:
                    root = self.root_for(row["path"], roots)
                    self._record(db, Path(row["path"]), root, row["seen"])
                db.commit()

    def track_paths(self, query="", root="", sort="path", direction="asc",
                    track_filter="all", genre=""):
        where, args = self._track_filter(query, root, track_filter, genre)
        order = self._track_order(sort, direction)
        with self._db() as db:
            rows = db.execute("SELECT path FROM tracks" + where + order + " LIMIT 5001", args).fetchall()
        if len(rows) > 5000:
            raise LibraryError("This list exceeds the 5,000-track batch limit. Narrow the search or folder filter.")
        return {"paths": [row[0] for row in rows]}

    def common_tags(self, paths):
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 5000 or
                not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            raise LibraryError("Select 1 to 5,000 distinct files")
        self._ensure_genres(paths)
        common = None
        found = 0
        with self._db() as db:
            for start in range(0, len(paths), 500):
                chunk = paths[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = db.execute("""SELECT tracks.tags, queued.data FROM tracks
                    JOIN roots ON roots.path=tracks.root
                    LEFT JOIN queued ON queued.path=tracks.path AND queued.inflight_job IS NULL
                    WHERE tracks.path IN ({})""".format(placeholders), chunk)
                for row in rows:
                    tags = json.loads(row["tags"])
                    if row["data"]:
                        tags.update(json.loads(row["data"])["edits"])
                    values = {field: tags.get(field, "") for field in FIELDS}
                    values["genre"] = genre_values(values["genre"])
                    if common is None:
                        common = values
                    else:
                        common = {field: value for field, value in common.items() if values[field] == value}
                    found += 1
        if found != len(paths):
            raise LibraryError("A selected file is no longer indexed in an approved folder")
        return {"common": common}

    def common_artwork(self, paths, expected=None):
        """Compare a bounded batch; callers carry the signature across batches.

        Return image bytes only for the first batch. This keeps large selections
        from repeatedly transferring the same cover, and permits cancelling a
        selection check between requests without a long-running server task.
        """
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 25 or
                not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            raise LibraryError("Choose 1 to 25 distinct tracks per artwork request")
        if expected is not None and (not isinstance(expected, str) or
                                    (expected != "none" and not re.fullmatch(r"[0-9a-f]{64}", expected))):
            raise LibraryError("Invalid artwork signature")
        tracks = {path: self.track(path) for path in paths}
        with self._db() as db:
            queued = {row["path"]: json.loads(row["data"]) for row in db.execute(
                "SELECT path,data FROM queued WHERE inflight_job IS NULL AND path IN ({})".format(
                    ",".join("?" for _ in paths)), paths)}
        signature, sample = expected, None
        changed, replacement = False, False
        cached = {}
        for path in paths:
            art_id = queued.get(path, {}).get("artwork_id")
            if art_id:
                if art_id not in cached:
                    with self._db() as db:
                        row = db.execute("SELECT data,mime FROM queued_artwork WHERE id=?", (art_id,)).fetchone()
                    if not row:
                        raise LibraryError("Queued artwork is missing; reopen the editor")
                    cached[art_id] = (row["data"], row["mime"])
                artwork = cached[art_id]
                present = True
                replacement = True
            else:
                present, artwork = inspect_artwork(path)
                if present != tracks[path]["artwork"]:
                    self.set_artwork_presence(path, present)
                    changed = True
            if present and not artwork:
                return {"state": "unavailable", "indexed_changed": changed}
            current = hashlib.sha256(artwork[0]).hexdigest() if artwork else "none"
            if signature is None:
                signature, sample = current, artwork
            elif current != signature:
                return {"state": "mixed", "indexed_changed": changed}
        result = {"state": "none" if signature == "none" else "shared", "signature": signature,
                  "queued": replacement, "indexed_changed": changed}
        if sample:
            result["artwork"] = {"data": base64.b64encode(sample[0]).decode("ascii"), "mime": sample[1]}
        return result

    @staticmethod
    def _track_dict(row):
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item["tags"]["genre"] = genre_values(item["tags"].get("genre"))
        item["artwork"] = bool(item["artwork"])
        item["relative_path"] = Path(item["path"]).relative_to(item["root"]).as_posix()
        return item

    def track(self, value):
        self.root_for(value)
        with self._db() as db:
            row = db.execute("SELECT * FROM tracks WHERE path=?", (value,)).fetchone()
        if not row:
            raise LibraryError("File is not indexed")
        return self._track_dict(row)

    def set_artwork_presence(self, path, present):
        with self.lock, self._db() as db:
            db.execute("UPDATE tracks SET artwork=?,artwork_indexed=1 WHERE path=?",
                       (int(present), path))

    def _destination(self, source, root, tags, mode, rename):
        if mode == "none":
            folder = source.parent
        elif mode == "artist":
            artist = clean_component(tags.get("album_artist") or tags.get("artist") or "")
            folder = root / artist
        elif mode == "artist_album":
            artist = clean_component(tags.get("album_artist") or tags.get("artist") or "")
            album = album_folder_component(tags)
            folder = root / artist / album
        elif mode == "album_in_artist":
            album = album_folder_component(tags)
            relative = source.relative_to(root)
            if len(relative.parts) == 1:
                folder = root / album
            else:
                folder = root / relative.parts[0] / album
        else:
            raise LibraryError("Unknown organization mode")
        if rename == "none":
            filename = source.name
        else:
            title = clean_component(tags.get("title") or "", "Title")
            if rename == "title":
                stem = title
            elif rename == "artist_title":
                artist = clean_component(tags.get("artist") or "", "Artist")
                stem = "{} - {}".format(artist, title)
            elif rename in ("track_title", "artist_album_track_title"):
                track = (tags.get("track") or "").split("/", 1)[0].strip()
                if not track.isdigit() or int(track) < 1:
                    raise LibraryError("Track number is missing or invalid for filename")
                if rename == "artist_album_track_title":
                    artist = clean_component(tags.get("artist") or "", "Artist")
                    album = clean_component(tags.get("album") or "", "Album")
                    stem = "{} - {} - {:02d} - {}".format(artist, album, int(track), title)
                else:
                    stem = "{:02d} - {}".format(int(track), title)
            else:
                raise LibraryError("Unknown filename option")
            filename = clean_component(stem) + source.suffix
        return folder / filename

    @staticmethod
    def _validate_change(edits, mode, rename, artwork, normalize_title=False, remove_title_text="", import_tags="none",
                         genre_options=None):
        try:
            validate_genre_options(genre_options if genre_options is not None else {})
        except ValueError as error:
            raise LibraryError(str(error)) from error
        if import_tags not in ("none", "title", "track_title", "artist_title", "artist_album_track_title"):
            raise LibraryError("Unknown filename import option")
        if not isinstance(normalize_title, bool):
            raise LibraryError("Invalid title normalization option")
        if not isinstance(remove_title_text, str) or len(remove_title_text) > 1000:
            raise LibraryError("Invalid title text to remove")
        if mode is not None and mode not in ("none", "artist", "artist_album", "album_in_artist"):
            raise LibraryError("Unknown organization mode")
        if rename is not None and rename not in ("none", "title", "track_title", "artist_title", "artist_album_track_title"):
            raise LibraryError("Unknown filename option")
        if not isinstance(edits, dict) or set(edits) - set(FIELDS):
            raise LibraryError("Invalid tag edits")
        if "genre" in edits:
            try:
                validate_genres(edits["genre"])
            except ValueError as error:
                raise LibraryError(str(error)) from error
        if not all(isinstance(value, str) and len(value) <= 1000 for field, value in edits.items() if field != "genre"):
            raise LibraryError("Tag values must be text under 1,000 characters")
        if artwork is not None and (not isinstance(artwork, dict) or
                                   artwork.get("mime") not in ("image/jpeg", "image/png") or
                                   not isinstance(artwork.get("data"), str) or
                                   len(artwork["data"]) > 15_000_000):
            raise LibraryError("Invalid artwork")

    def queue_items(self):
        with self._db() as db:
            rows = db.execute("SELECT path,data,revision FROM queued WHERE inflight_job IS NULL ORDER BY path").fetchall()
        return [{"path": row["path"], "operation": "edit", "item_kind": "track", **json.loads(row["data"]), "revision": row["revision"]}
                for row in rows]

    @staticmethod
    def _cleanup_queue_artwork(db):
        used = {json.loads(row[0]).get("artwork_id") for row in db.execute("SELECT data FROM queued")}
        for row in db.execute("SELECT id FROM queued_artwork").fetchall():
            if row[0] not in used:
                db.execute("DELETE FROM queued_artwork WHERE id=?", (row[0],))

    @staticmethod
    def _check_queue_revisions(paths, expected, actual):
        if expected is None:
            return
        if not isinstance(expected, dict):
            raise LibraryError("Invalid queue revisions")
        for path in paths:
            if expected.get(path) != actual.get(path):
                raise LibraryError("The queue changed while you were editing; reopen the editor")

    def queue_add(self, paths, edits, mode=None, artwork=None, rename=None, revisions=None,
                  normalize_title=False, remove_title_text="", import_tags="none", genre_options=None,
                  delete=None, item_kind="track"):
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 5000 or
                not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            raise LibraryError("Select 1 to 5,000 distinct files")
        self._validate_delete_options(delete, item_kind)
        cancelling_only = (delete is False and not edits and mode in (None, "none") and rename in (None, "none")
                           and not artwork and not normalize_title and not remove_title_text and import_tags == "none"
                           and (not genre_options or (genre_options.get("mode", "keep") == "keep"
                                and not genre_options.get("clean") and not genre_options.get("split"))))
        if delete is True or item_kind == "folder" or cancelling_only:
            return self._queue_delete(paths, delete, item_kind, revisions)
        self._validate_change(edits, mode, rename, artwork, normalize_title, remove_title_text, import_tags, genre_options)
        # Preserved deletions must still refer to the same file staged earlier.
        preserved_deletions = set()
        for queued in self.queue_items():
            if queued['path'] in paths and queued.get('operation') == 'delete' and delete is None:
                self._deletion_snapshot(queued['path'], 'track', queued['identity'])
                preserved_deletions.add(queued['path'])
        genre_options = genre_options if genre_options is not None else {}
        genre_action = genre_options.get("mode", "keep") != "keep" or genre_options.get("clean") or genre_options.get("split")
        imported = {path: {} if path in preserved_deletions else tags_from_filename(path, import_tags) for path in paths}
        roots = [Path(root) for root in self.roots()]
        for path in paths:
            self.root_for(path, roots)
        indexed = set()
        indexed_titles = {}
        indexed_genres = {}
        if genre_action:
            self._ensure_genres([path for path in paths if path not in preserved_deletions])
        with self._db() as db:
            for start in range(0, len(paths), 500):
                chunk = paths[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                columns = "path,tags" if normalize_title or remove_title_text or genre_action else "path"
                for row in db.execute("SELECT {} FROM tracks WHERE path IN ({})".format(columns, placeholders), chunk):
                    indexed.add(row["path"])
                    if normalize_title or remove_title_text:
                        indexed_titles[row["path"]] = json.loads(row["tags"]).get("title", "")
                    if genre_action:
                        indexed_genres[row["path"]] = genre_values(json.loads(row["tags"]).get("genre"))
        if len(indexed) != len(paths):
            raise LibraryError("File is not indexed")
        artwork_id = None
        artwork_data = None
        if artwork:
            try:
                artwork_data = base64.b64decode(artwork["data"], validate=True)
            except (ValueError, binascii.Error) as error:
                raise LibraryError("Invalid artwork data") from error
            if len(artwork_data) > 10 * 1024 * 1024:
                raise LibraryError("Artwork is too large")
            artwork_id = uuid.uuid4().hex
        with self.lock, self._db() as db:
            count = db.execute("SELECT count(*) FROM queued WHERE inflight_job IS NULL").fetchone()[0]
            selected_paths = set(paths)
            active_sources = {row[0] for row in db.execute("""SELECT DISTINCT item.source FROM job_items AS item
                JOIN jobs AS job ON job.id=item.job WHERE job.state IN ('queued','running','cancelling')""")}
            if selected_paths & active_sources:
                raise LibraryError("A selected track is already in a queued or running job")
            rows = {row["path"]: row for row in db.execute("SELECT path,data,revision,inflight_job FROM queued")
                    if row["path"] in selected_paths}
            if any(row["inflight_job"] for row in rows.values()):
                raise LibraryError("A selected track is already in a queued or running job")
            self._check_queue_revisions(paths, revisions,
                                        {path: row["revision"] for path, row in rows.items()})
            existing = {path: json.loads(row["data"]) for path, row in rows.items()}
            if count + len(set(paths) - set(existing)) > 5000:
                raise LibraryError("The queue is limited to 5,000 tracks")
            if artwork_id:
                db.execute("INSERT INTO queued_artwork VALUES (?,?,?)",
                           (artwork_id, artwork["mime"], artwork_data))
            for path in paths:
                current = existing.get(path, {"edits": {}, "mode": "none", "rename": "none", "artwork_id": None})
                if current.get("operation") == "delete":
                    if delete is None:
                        continue
                    current = {"edits": {}, "mode": "none", "rename": "none", "artwork_id": None}
                updated_edits = apply_genre_options(indexed_genres.get(path, []),
                                                    {**current["edits"], **edits, **imported[path]}, genre_options)
                if normalize_title or remove_title_text:
                    title = updated_edits.get("title", indexed_titles[path]) or ""
                    transformed = apply_title_options(title, remove_title_text, normalize_title)
                    if transformed != title:
                        updated_edits["title"] = transformed
                updated = {"operation": "edit", "item_kind": "track", "edits": updated_edits,
                           "genre_upgrade": genre_options.get("upgrade", current.get("genre_upgrade", False)),
                           "mode": current["mode"] if mode is None else mode,
                           "rename": current["rename"] if rename is None else rename,
                           "artwork_id": current.get("artwork_id") if artwork_id is None else artwork_id}
                if not updated["edits"] and updated["mode"] == "none" and updated["rename"] == "none" and not updated["artwork_id"]:
                    db.execute("DELETE FROM queued WHERE path=?", (path,))
                else:
                    db.execute("INSERT OR REPLACE INTO queued(path,data,revision) VALUES (?,?,?)",
                               (path, json.dumps(updated), uuid.uuid4().hex))
            self._cleanup_queue_artwork(db)
        return {"items": self.queue_items()}

    def queue_remove(self, path):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM queued WHERE path=? AND inflight_job IS NULL", (path,))
            self._cleanup_queue_artwork(db)
        return {"items": self.queue_items()}

    def queue_clear(self):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM queued WHERE inflight_job IS NULL")
            self._cleanup_queue_artwork(db)
        return {"items": []}

    def queue_artwork(self, path):
        with self._db() as db:
            queued = db.execute("SELECT data FROM queued WHERE path=? AND inflight_job IS NULL", (path,)).fetchone()
            art_id = json.loads(queued[0]).get("artwork_id") if queued else None
            row = db.execute("SELECT mime,data FROM queued_artwork WHERE id=?", (art_id,)).fetchone() if art_id else None
        if not row:
            raise LibraryError("No queued artwork")
        return {"mime": row["mime"], "data": base64.b64encode(row["data"]).decode("ascii")}

    def preview_queue(self, pending=None):
        if pending is not None:
            if not isinstance(pending, dict):
                raise LibraryError("Invalid pending changes")
            paths = pending.get("paths")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 5000 or len(set(paths)) != len(paths):
                raise LibraryError("Select 1 to 5,000 distinct files")
            self._validate_delete_options(pending.get("delete"), pending.get("item_kind", "track"))
            edits = pending.get("edits", {})
            mode = pending.get("mode")
            rename = pending.get("rename")
            artwork = pending.get("artwork")
            normalize_title = pending.get("normalize_title", False)
            remove_title_text = pending.get("remove_title_text", "")
            import_tags = pending.get("import_tags", "none")
            genre_options = pending.get("genre_options", {})
            self._validate_change(edits, mode, rename, artwork, normalize_title, remove_title_text, import_tags, genre_options)
        with self._db() as db:
            queued = {row["path"]: (json.loads(row["data"]), row["revision"]) for row in
                      db.execute("SELECT path,data,revision FROM queued WHERE inflight_job IS NULL")}
        paths = paths if pending is not None else list(queued)
        if not paths:
            raise LibraryError("The queue is empty")
        if pending is not None:
            self._check_queue_revisions(paths, pending.get("revisions"),
                                        {path: row[1] for path, row in queued.items()})
        artworks = {}
        artwork_bytes = 0
        operations = []
        for path in paths:
            current, revision = queued.get(path, ({"edits": {}, "mode": "none", "rename": "none", "artwork_id": None}, None))
            kind = pending.get("item_kind", "track") if pending is not None else current.get("item_kind", "track")
            if revision and kind != current.get("item_kind", "track"):
                raise LibraryError("Queued item kind changed")
            choice = pending.get("delete") if pending is not None else None
            deleting = choice if choice is not None else current.get("operation") == "delete"
            if deleting:
                operations.append({"path": path, "operation": "delete", "item_kind": kind,
                                   "identity": current.get("identity") if current.get("operation") == "delete" else None,
                                   "queue_revision": revision})
                continue
            cancelling_delete = current.get("operation") == "delete" and choice is False
            if cancelling_delete:
                current = {"edits": {}, "mode": "none", "rename": "none", "artwork_id": None}
            if kind == "folder" or (cancelling_delete and not edits and mode in (None, "none") and rename in (None, "none")
                                    and not artwork and not normalize_title and not remove_title_text and import_tags == "none"
                                    and not any(genre_options.get(key) for key in ("clean", "split"))
                                    and genre_options.get("mode", "keep") == "keep"):
                if cancelling_delete:
                    operations.append({"path": path, "operation": "cancel_delete", "item_kind": kind, "queue_revision": revision})
                continue
            merged = {"path": path,
                      "edits": {**current["edits"], **(edits if pending else {})},
                      "genre_options": {"upgrade": current.get("genre_upgrade", False),
                                        **(genre_options if pending is not None else {})},
                      "mode": current["mode"] if pending is None or mode is None else mode,
                      "rename": current["rename"] if pending is None or rename is None else rename,
                      "normalize_title": normalize_title if pending is not None else False,
                      "remove_title_text": remove_title_text if pending is not None else "",
                      "import_tags": import_tags if pending is not None else "none",
                      "queue_revision": revision}
            if pending and artwork:
                merged["artwork"] = artwork
                merged["artwork_key"] = "pending"
            elif current.get("artwork_id"):
                art_id = current["artwork_id"]
                if art_id not in artworks:
                    with self._db() as db:
                        row = db.execute("SELECT mime,data FROM queued_artwork WHERE id=?", (art_id,)).fetchone()
                    if not row:
                        raise LibraryError("Queued artwork is missing")
                    artwork_bytes += len(row["data"])
                    if artwork_bytes > 256 * 1024 * 1024:
                        raise LibraryError("Queued artwork exceeds 256 MB; process fewer tracks at once")
                    artworks[art_id] = {"mime": row["mime"], "data": base64.b64encode(row["data"]).decode("ascii")}
                merged["artwork"] = artworks[art_id]
                merged["artwork_key"] = art_id
            operations.append(merged)
        queued_revisions = {path: queued[path][1] for path in paths if path in queued}
        return self._preview_operations(operations, queue_checked=True, queued_revisions=queued_revisions)

    def preview(self, paths, edits, mode="none", artwork=None, rename="none", normalize_title=False,
                remove_title_text="", import_tags="none", genre_options=None):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 5000 or len(set(paths)) != len(paths):
            raise LibraryError("Select 1 to 5,000 distinct files")
        self._validate_change(edits, mode, rename, artwork, normalize_title, remove_title_text, import_tags, genre_options)
        return self._preview_operations([{"path": path, "edits": edits, "mode": mode, "rename": rename,
                                          "artwork": artwork, "artwork_key": "shared",
                                          "normalize_title": normalize_title,
                                          "import_tags": import_tags,
                                          "genre_options": genre_options if genre_options is not None else {},
                                          "remove_title_text": remove_title_text} for path in paths])

    def _preview_operations(self, operations, queue_checked=False, queued_revisions=None):
        plan = []
        destinations = set()
        errors = []
        artworks = {}
        roots = [Path(root) for root in self.roots()]
        auto_id3_upgrade = self.get_setting("auto_id3_upgrade", "true") != "false"
        for operation in operations:
            value = operation["path"]
            try:
                if operation.get("operation") == "delete":
                    plan.append(self._preview_delete(operation))
                    continue
                if operation.get("operation") == "cancel_delete":
                    plan.append({"source": value, "destination": value, "before": {}, "after": {}, "format": "",
                                 "artwork_changed": False, "edits": {}, **operation})
                    continue
                source = Path(value).resolve(strict=True)
                if source.is_symlink() or not source.is_file():
                    raise LibraryError("Not a regular audio file")
                root = self.root_for(source, roots)
                current = read_file(source)
                edits = apply_genre_options(current["tags"].get("genre"), operation["edits"], operation.get("genre_options", {}))
                edits.update(tags_from_filename(source, operation.get("import_tags", "none")))
                mode = operation["mode"]
                rename = operation["rename"]
                artwork = operation.get("artwork")
                if artwork and current["format"] == "WMA/ASF":
                    raise LibraryError("Embedded artwork is unavailable for WMA/ASF")
                proposed = dict(current["tags"])
                proposed.update(normalize_edits(edits))
                if "genre" in edits and current.get("id3_version") is not None:
                    id3_genre_frame(proposed["genre"])
                id3_upgrade = ("genre" in edits and len(proposed["genre"]) > 1 and
                               current.get("id3_version") is not None and current["id3_version"] < 4)
                if id3_upgrade and not (auto_id3_upgrade or operation.get("genre_options", {}).get("upgrade")):
                    raise LibraryError("Multiple genres need ID3v2.4; allow upgrades in Settings or in the genre editor")
                if operation.get("normalize_title") or operation.get("remove_title_text"):
                    title = proposed.get("title", "") or ""
                    transformed = apply_title_options(title, operation.get("remove_title_text", ""),
                                                      operation.get("normalize_title", False))
                    if transformed != title:
                        proposed["title"] = transformed
                        edits["title"] = transformed
                destination = self._destination(source, root, proposed, mode, rename)
                if destination == source and not artwork and not id3_upgrade and all(
                        current["tags"][field] == proposed[field] for field in edits):
                    if (operation.get("normalize_title") or operation.get("remove_title_text")) and not edits:
                        raise LibraryError("No title change needed for these options")
                    if operation.get("genre_options", {}).get("mode") in ("add", "remove"):
                        continue
                    raise LibraryError("Tags and file location already match")
                if destination != source and (destination.exists() or str(destination) in destinations):
                    raise LibraryError("Destination already exists")
                if destination != source and destination.parent != root and root not in destination.parents:
                    raise LibraryError("Destination leaves approved folder")
                destinations.add(str(destination))
                stat = source.stat()
                artwork_key = operation.get("artwork_key", str(id(artwork))) if artwork else None
                if artwork_key:
                    artworks[artwork_key] = artwork
                plan.append({"operation": "edit", "item_kind": "track", "source": str(source), "destination": str(destination), "root": str(root),
                             "before": current["tags"], "after": proposed, "format": current["format"],
                             "artwork_before": current["artwork"], "artwork_after": bool(artwork) if artwork else current["artwork"],
                             "artwork_changed": bool(artwork),
                             "id3_upgrade": id3_upgrade,
                             "id3_version_before": current.get("id3_version"),
                             "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                             "edits": edits, "artwork_key": artwork_key,
                             "queue_revision": operation.get("queue_revision")})
            except Exception as error:
                errors.append({"path": value, "error": str(error), "operation": operation.get("operation", "edit"),
                               "item_kind": operation.get("item_kind", "track")})
        if not plan:
            if not errors:
                return {"id": None, "items": [], "errors": []}
            if queue_checked and errors:
                return {"id": None, "items": [], "errors": errors}
            raise LibraryError("No files can be changed: {}".format(errors[0]["error"] if errors else "nothing to do"))
        plan_id = uuid.uuid4().hex
        payload = {"items": plan, "artworks": artworks, "errors": errors,
                   "queue_checked": queue_checked, "queued_revisions": queued_revisions or {}}
        with self._db() as db:
            db.execute("DELETE FROM plans WHERE created<? AND id NOT IN "
                       "(SELECT id FROM jobs WHERE state IN ('queued','running','cancelling'))",
                       (time.time() - 3600,))
            db.execute("INSERT INTO plans VALUES (?,?,?)", (plan_id, time.time(), json.dumps(payload)))
        return {"id": plan_id, "items": plan, "errors": errors}

    def new_job(self, kind, total=0):
        job_id = uuid.uuid4().hex
        with self.lock, self._db() as db:
            db.execute("INSERT INTO jobs (id,kind,state,done,total,errors,created) VALUES (?,?,?,?,?,?,?)",
                       (job_id, kind, "queued", 0, total, "[]", time.time()))
        self._launch_next_job()
        return job_id

    def _launch_next_job(self):
        with self.lock, self._db() as db:
            active = db.execute("SELECT 1 FROM jobs WHERE state IN ('running','cancelling') LIMIT 1").fetchone()
            if active:
                return
            row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return
            job_id = row["id"]
            db.execute("UPDATE jobs SET state='running',started_at=? WHERE id=?", (time.time(), job_id))
        threading.Thread(target=self._run_job, args=(job_id,), daemon=True).start()

    def _run_job(self, job_id):
        kind = None
        try:
            with self._db() as db:
                row = db.execute("SELECT kind FROM jobs WHERE id=?", (job_id,)).fetchone()
                kind = row["kind"] if row else None
                payload = db.execute("SELECT data FROM plans WHERE id=?", (job_id,)).fetchone()
            if self.job(job_id)["state"] == "cancelling":
                if kind == "commit":
                    self._finish_commit(job_id, "cancelled", 0, [])
                else:
                    self._update_job(job_id, state="cancelled")
                return
            if kind in ("scan", "scheduled_scan"):
                self.scan(job_id)
            elif kind == "folder_scan" and payload:
                self.scan_empty_folders(job_id, json.loads(payload["data"]).get("root", ""))
            elif kind == "commit" and payload:
                self._commit(job_id, json.loads(payload["data"]))
            else:
                raise LibraryError("Job payload is missing")
        except Exception as error:
            if kind == "commit":
                current = self.job(job_id)
                self._finish_commit(job_id, "failed", current["done"], current["errors"] + [str(error)])
            else:
                self._update_job(job_id, state="failed", errors=[str(error)])
        finally:
            with self._db() as db:
                db.execute("DELETE FROM plans WHERE id=?", (job_id,))
            self._launch_next_job()

    def _update_job(self, job_id, state=None, done=None, total=None, errors=None):
        fields = []
        values = []
        for key, value in (("state", state), ("done", done), ("total", total), ("errors", json.dumps(errors) if errors is not None else None)):
            if value is not None:
                fields.append(key + "=?")
                values.append(value)
        if state in ("complete", "partial", "failed", "cancelled"):
            fields.append("stopped_at=?")
            values.append(time.time())
        with self._db() as db:
            db.execute("UPDATE jobs SET " + ",".join(fields) + " WHERE id=?", values + [job_id])

    def job(self, job_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise LibraryError("Job not found")
        result = dict(row)
        result["errors"] = json.loads(result["errors"])
        return result

    def jobs(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created DESC,rowid DESC").fetchall()
        return [{**dict(row), "errors": json.loads(row["errors"])} for row in rows]

    def job_details(self, job_id, offset=0, limit=50):
        try:
            offset, limit = int(offset), int(limit)
        except (TypeError, ValueError) as error:
            raise LibraryError("Invalid details page") from error
        if offset < 0 or not 1 <= limit <= 100:
            raise LibraryError("Invalid details page")
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise LibraryError("Job not found")
            total = db.execute("SELECT count(*) FROM job_items WHERE job=?", (job_id,)).fetchone()[0]
            remaining = db.execute("SELECT count(*) FROM job_items WHERE job=? AND state IN ('pending','running')",
                                   (job_id,)).fetchone()[0]
            items = db.execute("SELECT * FROM job_items WHERE job=? ORDER BY ordinal LIMIT ? OFFSET ?",
                               (job_id, limit, offset)).fetchall()
        return {"job": {**dict(row), "errors": json.loads(row["errors"])}, "items": [dict(item) for item in items],
                "total_items": total, "remaining": remaining, "offset": offset, "limit": limit}

    def clear_job(self, job_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise LibraryError("Job not found")
            if row["state"] in ("running", "cancelling"):
                raise LibraryError("Cancel or wait for this job before clearing it")
            if row["state"] == "queued":
                db.execute("UPDATE queued SET inflight_job=NULL WHERE inflight_job=?", (job_id,))
            db.execute("DELETE FROM plans WHERE id=?", (job_id,))
            db.execute("DELETE FROM job_items WHERE job=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self._launch_next_job()

    def clear_completed_jobs(self):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM plans WHERE id IN (SELECT id FROM jobs WHERE state='complete')")
            db.execute("DELETE FROM job_items WHERE job IN (SELECT id FROM jobs WHERE state='complete')")
            count = db.execute("DELETE FROM jobs WHERE state='complete'").rowcount
        return count

    def start_scan(self):
        return self.new_job("scan")

    def start_scheduled_scan(self, day):
        with self.lock, self._db() as db:
            enabled = db.execute("SELECT value FROM settings WHERE key='daily_scan_enabled'").fetchone()
            if not enabled or enabled[0] != "true":
                return None
            last = db.execute("SELECT value FROM settings WHERE key='daily_scan_last_date'").fetchone()
            if last and last[0] >= day:
                return None
            db.execute("INSERT OR REPLACE INTO settings VALUES ('daily_scan_last_date',?)", (day,))
            if not db.execute("SELECT 1 FROM roots LIMIT 1").fetchone():
                return None
            active = db.execute("""SELECT id FROM jobs WHERE kind IN ('scan','scheduled_scan')
                AND state IN ('queued','running') ORDER BY created,rowid LIMIT 1""").fetchone()
            if active:
                return active[0]
            job_id = uuid.uuid4().hex
            db.execute("""INSERT INTO jobs(id,kind,state,done,total,errors,created)
                VALUES (?,'scheduled_scan','queued',0,0,'[]',?)""", (job_id, time.time()))
        self._launch_next_job()
        return job_id

    def start_commit(self, plan_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT created,data FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row or time.time() - row[0] > 3600:
                raise LibraryError("Preview expired; review changes again")
            payload = json.loads(row[1])
            if payload.get("queue_checked"):
                if "queued_revisions" not in payload:
                    payload["queued_revisions"] = {item["source"]: item["queue_revision"] for item in payload["items"]
                                                   if item.get("queue_revision")}
                for path, revision in payload.get("queued_revisions", {}).items():
                    queued = db.execute("SELECT revision FROM queued WHERE path=? AND inflight_job IS NULL", (path,)).fetchone()
                    if not queued or queued[0] != revision:
                        raise LibraryError("The queue changed after preview; review it again")
                for item in payload["items"]:
                    queued = db.execute("SELECT revision FROM queued WHERE path=? AND inflight_job IS NULL", (item["source"],)).fetchone()
                    if (queued[0] if queued else None) != item.get("queue_revision"):
                        raise LibraryError("The queue changed after preview; review it again")
            for item in payload["items"]:
                reserved = db.execute("SELECT inflight_job FROM queued WHERE path=?", (item["source"],)).fetchone()
                active_source = db.execute("""SELECT 1 FROM job_items AS item JOIN jobs AS job ON job.id=item.job
                    WHERE item.source=? AND job.state IN ('queued','running','cancelling') LIMIT 1""",
                    (item["source"],)).fetchone()
                if (reserved and reserved["inflight_job"]) or active_source:
                    raise LibraryError("A selected track is already in a queued or running job")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs (id,kind,state,done,total,errors,created) VALUES (?,?,?,?,?,?,?)",
                       (job_id, "commit", "queued", 0, len(payload["items"]), "[]", time.time()))
            for ordinal, item in enumerate(payload["items"]):
                db.execute("""INSERT INTO job_items
                    (job,ordinal,source,destination,before_tags,after_tags,artwork_changed,state,error)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                           (job_id, ordinal, item["source"], item["destination"], json.dumps(item["before"]),
                            json.dumps(item["after"]), int(item["artwork_changed"]), "pending", ""))
                db.execute("UPDATE job_items SET operation=?,item_kind=? WHERE job=? AND ordinal=?",
                           (item.get("operation", "edit"), item.get("item_kind", "track"), job_id, ordinal))
            for ordinal, error in enumerate(payload["errors"], start=len(payload["items"])):
                db.execute("""INSERT INTO job_items
                    (job,ordinal,source,destination,before_tags,after_tags,artwork_changed,state,error)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                           (job_id, ordinal, error["path"], error["path"], "{}", "{}", 0, "skipped", error["error"]))
                db.execute("UPDATE job_items SET operation=?,item_kind=? WHERE job=? AND ordinal=?",
                           (error.get("operation", "edit"), error.get("item_kind", "track"), job_id, ordinal))
            for path, revision in payload.get("queued_revisions", {}).items():
                db.execute("UPDATE queued SET inflight_job=? WHERE path=? AND revision=?",
                           (job_id, path, revision))
            db.execute("UPDATE plans SET id=?,created=? WHERE id=?", (job_id, time.time(), plan_id))
        self._launch_next_job()
        return job_id

    def _commit(self, job_id, payload):
        errors = list(payload["errors"])
        done = 0
        for ordinal, item in enumerate(payload["items"]):
            with self._db() as db:
                cancelling = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "cancelling"
                if not cancelling:
                    db.execute("UPDATE job_items SET state='running' WHERE job=? AND ordinal=?", (job_id, ordinal))
            if cancelling:
                self._finish_commit(job_id, "cancelled", done, errors)
                return
            try:
                artwork = (payload.get("artworks") or {}).get(item.get("artwork_key"))
                self._commit_one(job_id, item, item.get("edits", payload.get("edits", {})),
                                 artwork if "artworks" in payload else payload.get("artwork"))
            except Exception as error:
                errors.append({"path": item["source"], "error": str(error)})
                with self._db() as db:
                    db.execute("UPDATE job_items SET state='failed',error=? WHERE job=? AND ordinal=?",
                               (str(error), job_id, ordinal))
                    db.execute("UPDATE jobs SET done=?,errors=? WHERE id=?",
                               (done + 1, json.dumps(errors[:100]), job_id))
            else:
                with self.lock, self._db() as db:
                    db.execute("UPDATE job_items SET state='complete',completed_at=? WHERE job=? AND ordinal=?",
                               (time.time(), job_id, ordinal))
                    if payload.get("queue_checked") and item.get("queue_revision"):
                        db.execute("DELETE FROM queued WHERE path=? AND revision=? AND inflight_job=?",
                                   (item["source"], item["queue_revision"], job_id))
                    db.execute("UPDATE jobs SET done=?,errors=? WHERE id=?",
                               (done + 1, json.dumps(errors[:100]), job_id))
            done += 1
        if any(item.get("operation") == "delete" for item in payload["items"]):
            folder_errors, cancelled, _ = self._discover_empty_folders(job_id)
            errors.extend(folder_errors)
        self._finish_commit(job_id, "cancelled" if self.job(job_id)["state"] == "cancelling" else "complete" if not errors else "partial", done, errors)

    def _finish_commit(self, job_id, state, done, errors):
        with self.lock, self._db() as db:
            if state in ("cancelled", "failed"):
                db.execute("UPDATE job_items SET state=? WHERE job=? AND state IN ('pending','running')",
                           (state, job_id))
            db.execute("UPDATE queued SET inflight_job=NULL WHERE inflight_job=?", (job_id,))
            self._cleanup_queue_artwork(db)
            db.execute("UPDATE jobs SET state=?,done=?,errors=?,stopped_at=? WHERE id=?",
                       (state, done, json.dumps(errors[:100]), time.time(), job_id))

    def _commit_one(self, job_id, item, edits, artwork):
        if item.get("operation") == "delete":
            self._commit_delete(item)
            return
        if item.get("operation") == "cancel_delete":
            return
        source = Path(item["source"])
        destination = Path(item["destination"])
        root = self.root_for(source)
        if str(root) != item["root"] or source.is_symlink():
            raise LibraryError("Source folder changed")
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
            raise LibraryError("Source changed after preview")
        if destination != source and destination.exists():
            raise LibraryError("Destination now exists")
        if not os.access(str(source), os.R_OK | os.W_OK):
            raise LibraryError("No write permission on source")
        resolved_parent = destination.parent.resolve(strict=False)
        if resolved_parent != root and root not in resolved_parent.parents:
            raise LibraryError("Destination leaves approved folder")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(str(destination.parent), os.W_OK):
            raise LibraryError("No write permission on destination")
        art = None
        if artwork:
            art = (base64.b64decode(artwork["data"], validate=True), artwork["mime"])
        changed_edits = {field: value for field, value in normalize_edits(edits).items()
                         if (genre_values(item["before"].get(field)) if field == "genre" else item["before"].get(field)) != value}
        writing = False
        moved = False
        try:
            if changed_edits or art or item.get("id3_upgrade"):
                writing = True
                if item.get("id3_upgrade"):
                    apply_tags(source, changed_edits, art, upgrade_id3=True)
                else:
                    apply_tags(source, changed_edits, art)
            if destination != source:
                os.rename(str(source), str(destination))
                moved = True
            with self._db() as db:
                if moved:
                    db.execute("DELETE FROM tracks WHERE path=?", (str(source),))
                    db.execute("DELETE FROM track_genres WHERE path=?", (str(source),))
                self._record(db, destination, root, time.time_ns())
        except Exception as error:
            if writing or moved:
                try:
                    current = destination if moved else source
                    with self._db() as db:
                        if moved:
                            db.execute("DELETE FROM tracks WHERE path=?", (str(source),))
                            db.execute("DELETE FROM track_genres WHERE path=?", (str(source),))
                        self._record(db, current, root, time.time_ns())
                except Exception:
                    pass
                raise LibraryError("An in-place change may have been written; scan the folder before retrying: {}".format(error)) from error
            raise

    def cancel_job(self, job_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] not in ("queued", "running"):
                raise LibraryError("Job is not queued or running")
            if row["state"] == "queued":
                db.execute("UPDATE queued SET inflight_job=NULL WHERE inflight_job=?", (job_id,))
                db.execute("UPDATE job_items SET state='cancelled' WHERE job=? AND state='pending'", (job_id,))
                db.execute("DELETE FROM plans WHERE id=?", (job_id,))
                db.execute("UPDATE jobs SET state='cancelled',stopped_at=? WHERE id=?", (time.time(), job_id))
            else:
                db.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (job_id,))
        self._launch_next_job()
