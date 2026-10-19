"""Format-specific metadata access. Unknown tags are never cleared."""

import base64
import binascii
import hashlib
from copy import deepcopy

from mutagen import File
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from .genres import clean_genres, validate_genres


EXTENSIONS = {".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif", ".wma", ".asf"}
FIELDS = ("title", "artist", "album", "album_artist", "track", "disc", "date", "genre", "comment")
ID3_FRAMES = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2,
              "track": TRCK, "disc": TPOS, "date": TDRC, "genre": TCON}
COMMENT_KEYS = {"title": "TITLE", "artist": "ARTIST", "album": "ALBUM",
                "album_artist": "ALBUMARTIST", "track": "TRACKNUMBER", "disc": "DISCNUMBER",
                "date": "DATE", "genre": "GENRE", "comment": "COMMENT"}
MP4_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
            "album_artist": "aART", "track": "trkn", "disc": "disk", "date": "\xa9day",
            "genre": "\xa9gen", "comment": "\xa9cmt"}
ASF_KEYS = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle",
            "album_artist": "WM/AlbumArtist", "track": "WM/TrackNumber", "disc": "WM/PartOfSet",
            "date": "WM/Year", "genre": "WM/Genre", "comment": "Description"}


class UnsupportedFormat(ValueError):
    pass


class _PreserveID3Genres:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, translate=False, **kwargs)
        self.original_genres = deepcopy(self.tags.getall("TCON")) if self.tags is not None else []
        if self.tags is not None:
            self.tags.update_to_v24()


class _GenreMP3(_PreserveID3Genres, MP3):
    pass


class _GenreWAVE(_PreserveID3Genres, WAVE):
    pass


class _GenreAIFF(_PreserveID3Genres, AIFF):
    pass


def open_audio(path):
    audio = File(str(path), easy=False, options=[_GenreMP3, _GenreWAVE, _GenreAIFF, FLAC, MP4, OggVorbis, OggOpus, ASF])
    if not isinstance(audio, (MP3, FLAC, MP4, OggVorbis, OggOpus, WAVE, AIFF, ASF)):
        raise UnsupportedFormat(str(path))
    return audio


def format_name(audio):
    if isinstance(audio, MP3):
        return "MP3"
    if isinstance(audio, FLAC):
        return "FLAC"
    if isinstance(audio, MP4):
        return "M4A/M4B"
    if isinstance(audio, OggVorbis):
        return "Ogg Vorbis"
    if isinstance(audio, OggOpus):
        return "Opus"
    if isinstance(audio, WAVE):
        return "WAV"
    if isinstance(audio, AIFF):
        return "AIFF"
    return "WMA/ASF"


def _first(values):
    if not values:
        return ""
    value = values[0]
    if hasattr(value, "text"):
        value = value.text[0] if value.text else ""
    if isinstance(value, tuple):
        return str(value[0]) if value[0] else ""
    return str(value)


def read_tags(audio):
    result = {field: "" for field in FIELDS}
    result["genre"] = []
    tags = audio.tags
    if tags is None:
        return result
    if isinstance(audio, (MP3, WAVE, AIFF)):
        for field, frame_type in ID3_FRAMES.items():
            result[field] = ([genre for frame in getattr(audio, "original_genres", tags.getall("TCON")) for genre in frame.genres]
                             if field == "genre" else _first(tags.getall(frame_type.__name__)))
        result["comment"] = _first(tags.getall("COMM"))
    elif isinstance(audio, MP4):
        for field, key in MP4_KEYS.items():
            value = tags.get(key)
            if field in ("track", "disc") and value and isinstance(value[0], tuple):
                number, total = value[0]
                result[field] = "{}/{}".format(number, total) if total else str(number)
            elif field == "genre":
                result[field] = [str(genre) for genre in value or []]
            else:
                result[field] = _first(value)
    else:
        keys = ASF_KEYS if isinstance(audio, ASF) else COMMENT_KEYS
        for field, key in keys.items():
            result[field] = ([str(genre) for genre in tags.get(key, [])]
                             if field == "genre" else _first(tags.get(key)))
    return result


def read_file(path):
    audio = open_audio(path)
    return {"path": str(path), "format": format_name(audio), "tags": read_tags(audio),
            "duration": round(getattr(audio.info, "length", 0) or 0),
            "artwork": has_artwork(audio),
            "artwork_signature": artwork_signature(audio),
            "id3_version": getattr(audio.tags, "version", (2, 4, 0))[1]
            if isinstance(audio, (MP3, WAVE, AIFF)) else None}


def artwork_supported(audio):
    return not isinstance(audio, ASF)


def has_artwork(audio):
    if isinstance(audio, FLAC):
        return bool(audio.pictures)
    if audio.tags is None:
        return False
    if isinstance(audio, (MP3, WAVE, AIFF)):
        return bool(audio.tags.getall("APIC"))
    if isinstance(audio, MP4):
        return bool(audio.tags.get("covr"))
    if isinstance(audio, (OggVorbis, OggOpus)):
        return bool(audio.tags.get("metadata_block_picture") or audio.tags.get("coverart"))
    return bool(audio.tags.get("WM/Picture"))


MAX_PREVIEW_ARTWORK_BYTES = 10 * 1024 * 1024


def _image_mime(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def _asf_picture(value):
    try:
        raw = bytes(getattr(value, "value", value))
    except (TypeError, ValueError):
        return None
    if len(raw) < 9:
        return None
    picture_type = raw[0]
    size = int.from_bytes(raw[1:5], "little")
    offset = 5
    for _ in range(2):
        end = next((index for index in range(offset, len(raw) - 1, 2)
                    if raw[index:index + 2] == b"\0\0"), None)
        if end is None:
            return None
        offset = end + 2
    if size < 1 or size > len(raw) - offset:
        return None
    return picture_type, raw[offset:offset + size]


def _artwork_candidates(audio):
    candidates = []
    if isinstance(audio, FLAC):
        candidates = [(picture.type, picture.data) for picture in audio.pictures]
    elif audio.tags is None:
        return []
    elif isinstance(audio, (MP3, WAVE, AIFF)):
        candidates = [(frame.type, frame.data) for frame in audio.tags.getall("APIC")]
    elif isinstance(audio, MP4):
        candidates = [(3, bytes(cover)) for cover in audio.tags.get("covr", [])]
    elif isinstance(audio, (OggVorbis, OggOpus)):
        for value in audio.tags.get("metadata_block_picture", []):
            if len(value) > MAX_PREVIEW_ARTWORK_BYTES * 2:
                continue
            try:
                picture = Picture(base64.b64decode(value))
                candidates.append((picture.type, picture.data))
            except Exception:
                continue
        for value in audio.tags.get("coverart", []):
            if len(value) > MAX_PREVIEW_ARTWORK_BYTES * 2:
                continue
            try:
                candidates.append((3, base64.b64decode(value)))
            except (ValueError, TypeError, binascii.Error):
                continue
    else:
        candidates = [picture for value in audio.tags.get("WM/Picture", [])
                      if (picture := _asf_picture(value)) is not None]
    return sorted(candidates, key=lambda candidate: candidate[0] != 3)


def _artwork_from_audio(audio, candidates=None):
    for _, data in _artwork_candidates(audio) if candidates is None else candidates:
        if 0 < len(data) <= MAX_PREVIEW_ARTWORK_BYTES:
            mime = _image_mime(data)
            if mime:
                return data, mime
    return None


def artwork_signature(audio):
    if not has_artwork(audio):
        return "none"
    candidates = _artwork_candidates(audio)
    preview = _artwork_from_audio(audio, candidates)
    # Compare the displayed front cover; retain comparison for non-previewable images too.
    data = preview[0] if preview else next((data for _, data in candidates if data), None)
    if not data:
        return "unavailable"
    return ("" if preview else "unpreviewable:") + hashlib.sha256(data).hexdigest()


def inspect_artwork(path):
    audio = open_audio(path)
    return has_artwork(audio), _artwork_from_audio(audio)


def id3_genre_frame(values):
    frame = TCON(encoding=3, text=["(" + value if value.startswith("(") else value for value in values])
    if frame.genres != values:
        raise ValueError("ID3 reserves numeric genre codes and RX/CR; use genre names such as Rock, Remix or Cover")
    return frame


def apply_tags(path, changes, artwork=None, upgrade_id3=False):
    audio = open_audio(path)
    if audio.tags is None:
        audio.add_tags()
    version = getattr(audio.tags, "version", (2, 4, 0))[1]
    for field, value in changes.items():
        if field == "genre":
            validate_genres(value)
            values = clean_genres(value)
            if isinstance(audio, (MP3, WAVE, AIFF)):
                if version < 4 and len(values) > 1 and not upgrade_id3:
                    raise ValueError("Multiple genres require permission to upgrade this file to ID3v2.4")
                audio.tags.delall("TCON")
                if values:
                    audio.tags.add(id3_genre_frame(values))
            else:
                key = (MP4_KEYS if isinstance(audio, MP4) else ASF_KEYS if isinstance(audio, ASF) else COMMENT_KEYS)[field]
                if key in audio.tags:
                    del audio.tags[key]
                if values:
                    audio.tags[key] = values
            continue
        if field not in FIELDS or not isinstance(value, str):
            raise ValueError("Invalid tag field or value")
        value = value.strip()
        if isinstance(audio, (MP3, WAVE, AIFF)):
            frame_type = ID3_FRAMES.get(field)
            if field == "comment":
                audio.tags.delall("COMM")
                if value:
                    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
            elif frame_type:
                audio.tags.delall(frame_type.__name__)
                if value:
                    audio.tags.add(frame_type(encoding=3, text=[value]))
        elif isinstance(audio, MP4):
            key = MP4_KEYS[field]
            if not value:
                audio.tags.pop(key, None)
            elif field in ("track", "disc"):
                number = int(value.split("/", 1)[0])
                total = int(value.split("/", 1)[1]) if "/" in value else 0
                audio.tags[key] = [(number, total)]
            else:
                audio.tags[key] = [value]
        else:
            key = (ASF_KEYS if isinstance(audio, ASF) else COMMENT_KEYS)[field]
            if not value:
                if key in audio.tags:
                    del audio.tags[key]
            else:
                audio.tags[key] = [value]
    if artwork is not None:
        if not artwork_supported(audio):
            raise ValueError("Embedded artwork is unavailable for this format")
        data, mime = artwork
        if mime not in ("image/jpeg", "image/png") or len(data) > 10 * 1024 * 1024:
            raise ValueError("Artwork must be JPEG or PNG under 10 MB")
        if (mime == "image/jpeg" and not data.startswith(b"\xff\xd8\xff")) or (mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Artwork data does not match its image type")
        if isinstance(audio, (MP3, WAVE, AIFF)):
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        elif isinstance(audio, FLAC):
            audio.clear_pictures()
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, data
            audio.add_picture(picture)
        elif isinstance(audio, MP4):
            audio.tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG)]
        else:
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, data
            audio.tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    if isinstance(audio, (MP3, WAVE, AIFF)):
        if "genre" not in changes and not upgrade_id3:
            saved_genres = audio.original_genres
        elif "genre" not in changes:
            values = [genre for frame in audio.original_genres for genre in frame.genres]
            saved_genres = [id3_genre_frame(values)] if values else []
        else:
            saved_genres = deepcopy(audio.tags.getall("TCON"))
        if version == 3 and not upgrade_id3:
            audio.tags.update_to_v23()
        audio.tags.setall("TCON", saved_genres)
        audio.save(v2_version=3 if version == 3 and not upgrade_id3 else 4)
    else:
        audio.save()


def artwork_bytes(path):
    return inspect_artwork(path)[1]
