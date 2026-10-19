# Cadence Music Tagger

Cadence is a DSM 7 music metadata editor that works directly on files stored on the NAS. It provides a browser-based library, single and multi-file tag editing, metadata and artwork lookup, folder organization, a review-before-commit queue, and background jobs.

## Supported audio

| Format | Extensions | Tag editing | Embedded art replacement |
| --- | --- | --- | --- |
| MP3 | `.mp3` | ID3 | Yes |
| FLAC | `.flac` | Vorbis comments | Yes |
| M4A / M4B | `.m4a`, `.m4b` | MP4 atoms | Yes |
| Ogg Vorbis | `.ogg`, `.oga` | Vorbis comments | Yes |
| Opus | `.opus` | Vorbis comments | Yes |
| WAV | `.wav` | ID3 | Yes, but player support varies |
| AIFF | `.aif`, `.aiff` | ID3 | Yes, but player support varies |
| WMA / ASF | `.wma`, `.asf` | ASF | No |

Cadence preserves tag fields it does not edit. It intentionally excludes `.mp4` so a video library cannot be indexed accidentally.

## Library

- Add approved folders at any time; adding one starts a background scan. **Scan folders** refreshes the index after external changes, and Settings can schedule a daily scan at 3:00 a.m. in the NAS's local time zone.
- Sort and page the full indexed library, with embedded cover art beside each track where it can be previewed.
- Browse **Tracks**, **Albums**, **Artists**, **Genres**, **Decades** or **Years**. Grouped views show track counts and total length, and opening a group edits all of its tracks together.
- Library filters find missing or mismatched album artwork, missing fields, invalid characters in tags, unexpected title case, files directly inside artist folders, and empty folders. Filters combine with search and library scope.
- **Tag search** matches saved tag values by field, or across all tags. Queued changes only affect search after they are committed.

## Editing

- Edit one track or select across pages, up to the 5,000-track batch limit. **Select entire list** applies the current search and library filter; **Shift+Click** extends a selection.
- Changes are staged in the Edit pane and previewed in the list. **Add to queue** writes nothing to disk; **Save changes** reviews the current edit; **Process queue** commits every queued file as one background job.
- Review shows before-and-after values and destination paths in pages of 50; files with collisions or unsupported changes are excluded. Committing asks for a final confirmation unless that is disabled in Settings → **Commit safety**.
- **Advanced Track Options** can normalize title capitalization or remove an exact, case-sensitive string from selected titles.
- Single-track editing includes authenticated audio playback from the NAS; browser codec support varies by format.

## Metadata and artwork

- Look up MusicBrainz matches by text, optionally identify audio with AcoustID and `fpcalc`, and stage Cover Art Archive, uploaded, or URL-fetched artwork for review.
- A pasted URL fetches a public HTTP(S) JPEG or PNG up to 10 MB through the NAS; private-network addresses and nonstandard ports are blocked.
- Artwork previews read embedded pictures from ID3, FLAC, MP4, Ogg/Opus and WMA/ASF tags, preferring a front cover. JPEG, PNG, GIF, WebP and BMP images up to 10 MB can be previewed; replacement artwork must be JPEG or PNG.
- A multi-selection shows a shared cover only when every selected track has identical image bytes; otherwise it shows **Mixed artwork**. Folder images such as `cover.jpg` are not treated as embedded artwork.

## Files and folders

- Organize relative to each approved library root as `Artist/file`, `Artist/Album (Year)/file`, or `Existing artist/Album (Year)/file`. The year comes from the track's Date tag; without a valid year the folder uses the album name alone.
- Optionally rename files to `Title`, `Track number - Title`, `Artist - Title`, or `Artist - Album - 00 - Title`, preserving the audio extension.
- **Import tags from filename** reverses the same four patterns. Ambiguous or nonmatching names are rejected, and the whole selection is validated before anything is staged.
- **Delete file(s)** stages permanent removal through the normal queue, review and commit flow; nothing is deleted until **Commit to disk**.
- **Empty folders** finds directories with no entries in a background job and removes them nonrecursively after rechecking each target. Hidden files, artwork, symlinks and empty child directories make a folder nonempty, and library roots cannot be deleted.
- Tag edits write directly to the original file and folder changes use a direct rename. Writes are not atomic, so an interrupted write can leave a file damaged; Cadence keeps no audio backups, holding folder or Undo record. Keep your normal NAS backups or snapshots and test with a small folder first.

## Genres

- The Genres editor uses chips for every supported format, with suggestions drawn from the library and no enforced vocabulary.
- **Overwrite**, **Add** and **Remove** actions work across a selection. Optional cleanup trims whitespace and removes case-insensitive duplicates; it can also split comma or semicolon lists into separate values.
- Writing multiple genres to an older ID3 tag may upgrade it to ID3v2.4. This is allowed by default and can be changed in Settings; the review identifies each upgrade.
- Library filters include **Missing genre** and **Genre needs review**, the latter a heuristic for inconsistent values rather than a declaration that a genre is invalid.

## Background jobs

- Scans and commits run in the background with progress, cancellation and error reporting. Jobs run one at a time in submission order, including after a service restart.
- Activity shows creation, start and stop times and each committed file's completion time. **Clear Completed Jobs** removes only successful jobs; failed, partial, cancelled and interrupted jobs remain until cleared individually.
- Clearing or cancelling a waiting job returns its staged queue edits, but discards unstaged edits submitted through **Save changes**; the UI asks for confirmation first.

## Build an SPK

The package targets DSM 7.0 or later and depends on Web Station. It needs Python 3.8 or newer and selects the newest compatible installed interpreter; Python 3.9 is a compatibility fallback for older models. The generated SPK includes Mutagen 1.47.0 and its license notice.

On a Linux build computer with Python 3 and pip:

```sh
sh build_spk.sh
```

The script creates `Cadence-0.1.0-0056.spk`. Builds are published as release assets rather than committed here; this build is attached to the [0.1.0 build 0056 release](https://github.com/LeroyRich/cadence/releases/tag/v0.1.0-0056) (`Cadence-0.1.0-0056.spk`, 256,000 bytes, SHA-256 `c75295c088720bbd1f2ee2bc013ead37d1214cc4bc3724aed0b625f865ce7bae`). For an offline rebuild, set `MUTAGEN_SOURCE` to a directory containing `mutagen/` and `mutagen-1.47.0.dist-info/`.

## Install on DSM

1. Install **Web Station** from Package Center and ensure a compatible Python interpreter is available.
2. Open Package Center → **Manual Install** and select `Cadence-0.1.0-0056.spk`. When upgrading, install over the existing package so the index and queued changes remain available.
3. Choose an administrator password of at least 12 characters.
4. Open Cadence from Package Center or the DSM application launcher. Its HTTPS portal uses port **8788** and forwards to the app's loopback-only service on port 8787. Your browser must trust the certificate configured for the NAS's DSM portal.
5. In DSM Control Panel → Shared Folder → Edit → Permissions → **System internal user**, grant the `Cadence` package user Read/Write access to each share you intend to edit, including any subfolder with its own restrictive ACL. Cadence does not change DSM permissions itself.
6. In Cadence, choose **Add folder**, browse within a `/volumeN` folder, and let the automatic scan finish.
7. Optionally enter a MusicBrainz contact email and AcoustID application key in Settings. Fingerprint identification also needs a compatible `fpcalc` executable available to the package account.

## Security on your network

Give the package account Read/Write access only to the music shares you approve. In DSM's firewall, allow TCP port 8788 from the computers or trusted subnet that use Cadence and deny it from IoT and guest networks, including IPv6 rules. Avoid router port forwarding; use a VPN for access away from home, and apply firewall changes carefully so you retain DSM access.

The DSM portal uses HTTPS on port 8788 and a loopback-only backend. Logins use browser-session cookies that expire after 12 hours; **Remember me** keeps a browser signed in for 30 days and should be used only on a trusted device. Logout revokes the current session, and changing or resetting the administrator password revokes every session. Login attempts are briefly throttled. The NAS certificate must be trusted by your browser for HTTPS to protect login traffic.

## Local development

```sh
python3 -m pip install -r requirements.txt
python3 -m tagger.server --state-dir ./state --set-password
python3 -m tagger.server --state-dir ./state --host 127.0.0.1 --port 8787 --insecure-local-http
```

Open `http://127.0.0.1:8787` locally. Tests use generated audio fixtures:

```sh
python3 -m unittest discover -s tests -v
node tests/test_library_ui.cjs
```

`ffmpeg` is required only to generate those fixtures; ordinary tag editing needs no FFmpeg. The authenticated `/api` endpoints used by the web UI cover grouped browsing, tag search and library paths; see `tagger/` and `web/` for request shapes.

## Current validation boundary

Build 0056 has been tested on Linux with generated files for every listed format, local HTTP API checks, and browser checks for pending values, queued artwork, queue clearing and artwork filtering. It has not yet been validated on the NAS. Build 0054 was exercised on a DS1817+ running DSM 7, where tag search and the grouped views were verified. The package does not bundle `fpcalc`, so fingerprint lookup is unavailable until a compatible binary is installed.
