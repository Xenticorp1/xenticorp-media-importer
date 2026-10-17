# Xenticorp Media Import

Headless USB to Jellyfin importer for a Linux media server. Plug in a USB with a `Compressed Media` folder at its root (or `Ripped Media`, or a whole library drive; see [Settings](#settings)):

| On the USB | Goes to |
|---|---|
| `Compressed Media/Movie.mkv` (or `.mp4`) | `/srv/jellyfin/media/movies/Movie/Movie.mkv` |
| `Compressed Media/Movie.en.srt`, `Movie-poster.jpg`... | `/srv/jellyfin/media/movies/Movie/` (with its movie) |
| `Compressed Media/<Show>/Season N/...` | `/srv/jellyfin/media/shows/<Show>/Season N/...` |

The library paths above are examples. Set yours with `LIBRARY_ROOT` in `media_import.py` and `LIB` in `install.sh`.

- Show names match existing library folders ignoring case, punctuation and a trailing `(YYYY)`, so `The Big Bang Theory` goes into `The Big Bang Theory (2007)`.
- Loose files at the root whose name contains an episode code (`S01E01`, `s1e1`, `1x01`) are not imported as movies: they're skipped, left on the USB, and the run exits non-zero so it shows as needing attention. Put them in a show folder.
- Each file is copied, SHA-256 verified, then deleted from the USB (switchable, see [Settings](#settings)). Existing library files are overwritten.
- Folders the import empties are removed. Folders that were already empty stay on the USB.
- The library drive is never touched (UUID ignore list). The importer refuses to run if the library isn't mounted.

## Settings

Three switches on the Xenticorp dashboard's Importer card, saved in `/var/lib/media-import/settings.json`
(e.g. `{"source": "ripped", "library_drive": true, "export_library": true, "skip_existing": true}`). The importer reads them
when a drive is plugged in; a missing or broken file means all defaults.

- **Source folder** (`"source"`: `"compressed"`, the default, or `"ripped"`): which folder at the drive's root
  is imported, `Compressed Media` or `Ripped Media`. Both work exactly as described above.
- **Library drive mode** (`"library_drive"`, off by default): for a drive that's already laid out like a
  library. Everything in its `Movies/` and `Shows/` folders (any case) is copied, videos, subtitles,
  artwork and `.nfo` files alike, keeping the folders: `Movies/Heat (1995)/...` goes to
  `movies/Heat (1995)/...`, and a show folder is matched to an existing library show as above. Nothing on
  the drive is changed or deleted, so it's only mounted read-only. The source folder switch doesn't apply.
  Before copying anything it waits for you on the dashboard: the card lists the movies and shows it found,
  you reject the ones you don't want and press approve (or cancel), and only the rest is copied. The
  answer comes back as `/var/lib/media-import/review.json` (`{"run_id": ..., "rejected": [ids]}` or
  `"cancel": true`). Unplugging the drive while it waits stops the run, as always.
  - **Copy the library onto the drive** (`"export_library"`, a sub-switch, off by default; ignored unless
    library drive mode is on): for swapping media with someone. After the imports, everything in X4's
    library that isn't on the drive yet is copied into the drive's `Movies/` and `Shows/` (made if
    missing; a library show goes into the drive's folder for the same show, e.g. `Breaking Bad (2008)` into
    `Breaking Bad`). Files already on the drive are never overwritten and the library is never changed.
    These titles aren't part of the review: they're listed, but always copied (a cancel stops both ways).
    Each title is only copied if all of it fits, leaving 512 MB free; ones that don't fit are reported.
    This needs the drive read-write; if it can't be (e.g. Windows left it hibernated), the import still
    runs read-only and the run says the library wasn't copied.
- **Skip files already in the library** (`"skip_existing"`, off by default): a file whose name is already
  at its spot in the library isn't copied (in the normal modes it stays on the USB). Works in every mode.
  A movie still sitting loose in `movies/` counts too, so nothing is duplicated before you tidy.
- **Movie folders** (`"movie_folders"`, **on** by default): each movie goes into a folder of its own, the
  way Jellyfin likes it, along with the loose files whose names start with the movie's name (subtitles,
  `-poster.jpg`, `.nfo`): `Heat (1995).mkv` + `Heat (1995).en.srt` -> `movies/Heat (1995)/`. Loose files
  that don't belong to a movie are left on the USB. It applies to library drives too, both ways: a
  drive with loose movies is sorted into folders in the library, and the library goes onto a drive in
  folders. Off, movies land loose in `movies/` and other loose files are skipped, as before.

- **Delete from the drive after copying** (`"delete_after"`, **on** by default): each file is deleted from
  the USB once its copy is verified, and folders that empties are removed. Off, the import only copies:
  everything stays on the USB, which is then only ever mounted read-only (so a drive Windows left
  hibernated still imports). Library drive mode never deletes either way.

## Sort incoming

The dashboard's **Sort incoming** button (or `sudo python3 media_import.py --incoming`) moves everything
inside `INCOMING_DIR`'s `movies/` and `shows/` folders into the library (set `INCOMING_DIR` at the top
of `media_import.py`). The `movies/` and `shows/` folders themselves always stay; folders inside them
that the run empties are removed.

- Sorted the same way as a library drive: movie folders (if that switch is on) and show matching.
- Each file is copied, SHA-256 verified, and only then removed from incoming (it's usually another drive).
- A movie or show that's already in the library only gets its new files: anything already there under
  the same name isn't copied and stays in incoming, whatever the skip-existing switch says.
- A file changed in the last 2 minutes is left for next time, in case it's still being written.
- Two `movies` or two `shows` folders that differ only in capitals (`Shows` and `shows`) stop the run with
  a message, since only one would be read; the same goes for a library drive. A season folder sitting
  straight in `shows/` (`shows/Season 3/...`) is left where it is and reported: it needs to go inside a
  folder named after its show (`shows/Metalocalypse/Season 3/...`).
- Progress and the list of what moved show on the dashboard's Importer card, like an import. It runs
  through the same path unit as Tidy, so there's nothing extra to install.

## Tidy library

The dashboard's **Tidy library** button sorts movies already sitting loose in `movies/` into a folder
each, by the same rule. It first shows what would move; nothing moves until you confirm. Files are
renamed in place (same drive, instant), never overwritten: if the folder already has a file with the same
name, that one stays loose and is reported. Loose files that don't belong to a movie stay put. It waits
for any running import (same lock).

The dashboard asks for it by writing `/var/lib/media-import/tidy-request.json` (`"action"`: `"plan"`,
`"run"`, or `"incoming"` for Sort incoming); `media-import-tidy.path` (installed by `install.sh`) runs
`media_import.py --tidy`, which writes the plan or result to
`/var/lib/media-import/tidy.json`. By hand: `sudo python3 media_import.py --tidy` (plan only) or
`--tidy --run`. Jellyfin notices the moves on its own if real-time monitoring is on; otherwise run a
library scan.

## Install

```bash
git clone <repo-url> media-import
cd media-import
sudo ./install.sh
```

## Update

```bash
cd /path/to/media-import && git pull
# re-run sudo ./install.sh only if a .service, .path or .rules file changed
```

## Status file

While it runs, the importer keeps `/var/lib/media-import/status.json` up to date: `plugged_in`
(mounting, scanning), `running` (file N of M, current file, bytes copied) and `finished` (with a
`result` of `ok`, `problems`, `no_folder`, `nothing`, `aborted`, `mount_failed`, `interrupted` or
`error`, plus counts, including `existing` for files skip-existing mode didn't copy, the `mode` (`compressed`,
`ripped` or `library`), the movies and shows found (`items`, one per movie or show, with
`dir` `in` (drive -> library) or `out` (library -> drive), file counts, sizes and how far each got;
plus `exported`, `no_space` and `drive_free` when the library is copied onto the drive) and a message). In library drive mode the state goes `plugged_in` ->
`review` (waiting for approval) -> `running` -> `finished`; a cancel or rejecting everything finishes
with `cancelled`. It's rewritten every 5 seconds during a run, so a stale
`updated` time means the importer died. The Xenticorp dashboard reads it to show the import status.
Drives on the ignore list and the library drive never touch it. On a drive with several partitions,
partitions with no media don't overwrite a sibling partition's result while that drive is plugged in.

## Logs / manual run

```bash
journalctl -u 'media-import@*' -n 50
sudo python3 media_import.py --dir /path/to/mounted/usb
```
