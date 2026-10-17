"""Non-AI metadata suggestions from MusicBrainz and AcoustID."""

import json
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlencode

from .artwork import MAX_ARTWORK_BYTES, fetch_public_response


MBID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class MetadataClient:
    def __init__(self, library):
        self.library = library
        self.lock = threading.Lock()
        self.last_musicbrainz = 0.0
        self.last_acoustid = 0.0

    def _get_json(self, url, rate="mb", data=None):
        with self.lock:
            last = self.last_musicbrainz if rate == "mb" else self.last_acoustid
            wait = (1.1 if rate == "mb" else 0.4) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            if rate == "mb":
                self.last_musicbrainz = time.monotonic()
            else:
                self.last_acoustid = time.monotonic()
        contact = self.library.get_setting("contact_email")
        agent = "NASMusicTagger/0.1 ({})".format(contact if contact else "local-user")
        host = "api.acoustid.org" if rate == "acoustid" else "musicbrainz.org"
        payload, mime = fetch_public_response(url, 4 * 1024 * 1024,
                                              {"User-Agent": agent, "Accept": "application/json",
                                               "Content-Type": "application/x-www-form-urlencoded"},
                                              data=data, allowed_hosts={host})
        if mime.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Metadata service did not return JSON")
        return json.loads(payload)

    def search(self, path, fingerprint=False):
        track = self.library.track(path)
        tags = track["tags"]
        if not self.library.get_setting("contact_email"):
            raise ValueError("Set a contact email in Settings before MusicBrainz lookup")
        candidates = []
        if fingerprint:
            key = self.library.get_setting("acoustid_key")
            if not key:
                raise ValueError("Set an AcoustID application key in Settings")
            executable = shutil.which("fpcalc")
            if not executable:
                raise ValueError("fpcalc is not installed on this NAS")
            output = subprocess.check_output([executable, "-json", path], timeout=120)
            result = json.loads(output)
            params = urlencode({"client": key, "duration": result["duration"],
                                "fingerprint": result["fingerprint"], "meta": "recordingids"}).encode("ascii")
            data = self._get_json("https://api.acoustid.org/v2/lookup", "acoustid", params)
            for item in data.get("results", [])[:5]:
                for recording in item.get("recordings", [])[:3]:
                    identifier = recording.get("id", "")
                    if MBID.match(identifier):
                        details = self._get_json("https://musicbrainz.org/ws/2/recording/{}?inc=artists+releases&fmt=json".format(identifier))
                        candidates.append(self._candidate(details, float(item.get("score", 0)), "AcoustID"))
        else:
            title = tags.get("title") or path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            terms = ['recording:"{}"'.format(title.replace('"', ""))]
            if tags.get("artist"):
                terms.append('artist:"{}"'.format(tags["artist"].replace('"', "")))
            params = urlencode({"query": " AND ".join(terms), "limit": 10, "fmt": "json"})
            data = self._get_json("https://musicbrainz.org/ws/2/recording/?" + params)
            candidates = [self._candidate(item, float(item.get("score", 0)) / 100, "MusicBrainz")
                          for item in data.get("recordings", [])]
        return candidates

    @staticmethod
    def _candidate(item, confidence, source):
        artists = item.get("artist-credit", [])
        artist = "".join(part if isinstance(part, str) else part.get("name", "") for part in artists)
        releases = item.get("releases", [])
        release = releases[0] if releases else {}
        return {"source": source, "confidence": round(confidence, 3),
                "recording_id": item.get("id", ""), "release_id": release.get("id", ""),
                "tags": {"title": item.get("title", ""), "artist": artist,
                         "album": release.get("title", ""), "date": release.get("date", "")}}

    def cover(self, release_id):
        if not MBID.match(release_id):
            raise ValueError("Invalid MusicBrainz release ID")
        url = "https://coverartarchive.org/release/{}/front-500".format(release_id)
        data, content_type = fetch_public_response(url, MAX_ARTWORK_BYTES,
                                                   {"Accept": "image/jpeg,image/png"})
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime not in ("image/jpeg", "image/png") or not (
                (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff")) or
                (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))):
            raise ValueError("Cover Art Archive did not return a JPEG or PNG image")
        return data, mime
