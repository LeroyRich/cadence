"""Saved-metadata group memberships and library browsing."""

import json
import re

from .formats import FIELDS
from .genres import genre_values


GROUP_VIEWS = ("albums", "artists", "genres", "decades", "years")


def contains(value, query):
    return str(query).casefold() in str(value or "").casefold()


def tag_matches(encoded, field, query):
    tags = json.loads(encoded)
    return any(contains(value, query) for name in (FIELDS if field == "all" else (field,))
               for value in (genre_values(tags.get(name)) if name == "genre" else [tags.get(name)]))


def memberships(tags):
    artist = (tags.get("artist") or "").strip()
    album_artist = (tags.get("album_artist") or "").strip() or artist
    album = (tags.get("album") or "").strip()
    match = re.match(r"^([12][0-9]{3})(?:$|[-/. ])", (tags.get("date") or "").strip())
    year = int(match.group(1)) if match else None
    decade = year // 10 * 10 if year is not None else None
    groups = [("albums", [album.casefold(), album_artist.casefold()], album or "Unknown album",
               album_artist or "Unknown artist"),
              ("artists", artist.casefold(), artist or "Unknown artist", ""),
              ("years", year, str(year) if year is not None else "Unknown year", ""),
              ("decades", decade, str(decade) + "s" if decade is not None else "Unknown decade", "")]
    genres = {value.strip().casefold(): value.strip() for value in reversed(genre_values(tags.get("genre")))
              if value.strip()}
    groups.extend(("genres", key, value, "") for key, value in genres.items())
    if not genres:
        groups.append(("genres", None, "No genre", ""))
    return [(view, json.dumps(identity, ensure_ascii=True, separators=(",", ":")), label, credit, year)
            for view, identity, label, credit in groups]


class GroupMixin:
    def _init_groups(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS track_groups(
                path TEXT NOT NULL, view TEXT NOT NULL, key TEXT NOT NULL,
                label TEXT NOT NULL, credit TEXT NOT NULL, year INTEGER,
                PRIMARY KEY(path,view,key));
            CREATE INDEX IF NOT EXISTS track_groups_lookup ON track_groups(view,key,path);
            CREATE TRIGGER IF NOT EXISTS tracks_delete_groups AFTER DELETE ON tracks
                BEGIN DELETE FROM track_groups WHERE path=OLD.path; END;
        """)
        while True:
            rows = db.execute("""SELECT path,tags FROM tracks WHERE NOT EXISTS
                (SELECT 1 FROM track_groups g WHERE g.path=tracks.path AND g.view='artists') LIMIT 1000""").fetchall()
            if not rows:
                break
            for row in rows:
                self._index_groups(db, row["path"], json.loads(row["tags"]))
            db.commit()

    @staticmethod
    def _index_groups(db, path, tags):
        db.execute("DELETE FROM track_groups WHERE path=?", (str(path),))
        db.executemany("INSERT INTO track_groups VALUES (?,?,?,?,?,?)",
                       [(str(path), *group) for group in memberships(tags)])

    def _group_query(self, view, query="", root="", track_filter="all", tag_field="all"):
        if view not in GROUP_VIEWS:
            raise self.operation_error("Unknown library view")
        if not all(isinstance(value, str) for value in (query, root, track_filter, tag_field)):
            raise self.operation_error("Invalid group search")
        where, args = self._track_filter(query if track_filter == "tag_search" else "", root,
                                         track_filter, tag_field=tag_field)
        name_search = ""
        if query and track_filter != "tag_search":
            name_search = " AND (cadence_contains(g.label,?) OR cadence_contains(g.credit,?))"
            args.extend([query, query])
        scope = " AND tracks.root=?" if root else ""
        # Eligibility uses matching tracks. Membership and summaries always use the full scoped group.
        sql = """WITH eligible AS (
            SELECT DISTINCT g.key FROM track_groups g WHERE g.view=?
            AND g.path IN (SELECT path FROM tracks{where}){search}
        ), members AS (
            SELECT g.*, tracks.duration FROM track_groups g JOIN tracks ON tracks.path=g.path
            WHERE g.view=? AND g.key IN (SELECT key FROM eligible){scope}
        ) """.format(where=where, search=name_search, scope=scope)
        return sql, [view] + args + [view] + ([root] if root else [])

    def groups(self, view, query="", root="", offset=0, limit=100, sort="label", direction="asc",
               track_filter="all", tag_field="all"):
        sorts = {"label": "cadence_casefold(label)", "album_artist": "cadence_casefold(album_artist)",
                 "year": "first_year", "tracks": "tracks", "albums": "albums",
                 "artists": "artists", "duration": "duration"}
        if sort not in sorts or direction not in ("asc", "desc"):
            raise self.operation_error("Invalid sort order")
        sql, args = self._group_query(view, query, root, track_filter, tag_field)
        with self._db() as db:
            totals = db.execute(sql + "SELECT count(DISTINCT key),count(DISTINCT path) FROM members", args).fetchone()
            rows = db.execute(sql + """SELECT * FROM (SELECT m.key, min(m.label) AS label, min(m.credit) AS album_artist,
                min(m.year) AS first_year, max(m.year) AS last_year,
                count(*) AS tracks, sum(m.duration) AS duration,
                count(DISTINCT a.key) AS artists, count(DISTINCT b.key) AS albums,
                sum(EXISTS(SELECT 1 FROM queued q WHERE q.path=m.path AND q.inflight_job IS NULL)) AS queued_count
                FROM members m
                JOIN track_groups a ON a.path=m.path AND a.view='artists'
                JOIN track_groups b ON b.path=m.path AND b.view='albums'
                GROUP BY m.key) ORDER BY {sort} {direction}, key {direction} LIMIT ? OFFSET ?""".format(
                    sort=sorts[sort], direction=direction.upper()),
                args + [max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
            needs_scan = db.execute("SELECT 1 FROM tracks WHERE genre_indexed=0 LIMIT 1").fetchone() is not None
        return {"items": [dict(row) for row in rows], "total": totals[0], "total_tracks": totals[1],
                "genres_need_scan": needs_scan}

    def group_paths(self, view, root="", keys=None, all_matching=False, query="", track_filter="all",
                    tag_field="all"):
        if view not in GROUP_VIEWS:
            raise self.operation_error("Unknown library view")
        if not isinstance(root, str):
            raise self.operation_error("Invalid library scope")
        if not isinstance(all_matching, bool):
            raise self.operation_error("Invalid group selection")
        if all_matching:
            if keys is not None:
                raise self.operation_error("Choose group keys or all matching groups")
            sql, args = self._group_query(view, query, root, track_filter, tag_field)
        else:
            if (not isinstance(keys, list) or not keys or
                    not all(isinstance(key, str) for key in keys) or
                    len(set(keys)) != len(keys)):
                raise self.operation_error("Select distinct group keys")
            # A temporary table avoids the SQLite host-parameter limit for large selections.
            sql, args = "", []
        with self._db() as db:
            if not all_matching:
                db.execute("CREATE TEMP TABLE selected_groups(key TEXT PRIMARY KEY)")
                db.executemany("INSERT INTO selected_groups VALUES (?)", [(key,) for key in keys])
                sql = """WITH members AS (SELECT g.path,g.key FROM track_groups g
                    JOIN tracks ON tracks.path=g.path WHERE g.view=?
                    AND g.key IN (SELECT key FROM selected_groups)""" + (" AND tracks.root=?" if root else "") + ") "
                args = [view] + ([root] if root else [])
            rows = db.execute(sql + "SELECT DISTINCT path FROM members ORDER BY path LIMIT 5001", args).fetchall()
            if len(rows) > 5000:
                raise self.operation_error("This selection exceeds the 5,000-track batch limit. Narrow the search or library filter.")
            found = [row[0] for row in db.execute(sql + "SELECT DISTINCT key FROM members ORDER BY key", args)]
            if not all_matching and set(found) != set(keys):
                raise self.operation_error("A selected group is no longer indexed in this library. Refresh the list.")
            paths = [row[0] for row in rows]
            single = db.execute("SELECT * FROM tracks WHERE path=?", paths).fetchone() if len(paths) == 1 else None
        return {"keys": found, "paths": paths, "track": self._track_dict(single) if single else None}
