# Xenticorp Media Import

Headless USB to Jellyfin importer for a Linux media server. Plug in a USB with a `Compressed Media` folder at its root (or `Ripped Media`, or a whole library drive; see [Settings](#settings)):

| On the USB | Goes to |
|---|---|
| `Compressed Media/Movie.mkv` (or `.mp4`) | `/srv/jellyfin/media/movies/` |
| `Compressed Media/<Show>/Season N/...` | `/srv/jellyfin/media/shows/<Show>/Season N/...` |

The library paths above are examples. Set yours with `LIBRARY_ROOT` in `media_import.py` and `LIB` in `install.sh`.

- Show names match existing library folders ignoring case, punctuation and a trailing `(YYYY)`, so `The Big Bang Theory` goes into `The Big Bang Theory (2007)`.
- Loose files at the root whose name contains an episode code (`S01E01`, `s1e1`, `1x01`) are not imported as movies: they're skipped, left on the USB, and the run exits non-zero so it shows as needing attention. Put them in a show folder.
- Each file is copied, SHA-256 verified, then deleted from the USB. Existing library files are overwritten.
- Folders the import empties are removed. Folders that were already empty stay on the USB.
- The library drive is never touched (UUID ignore list). The importer refuses to run if the library isn't mounted.

## Settings

Three switches on the Xenticorp dashboard's Importer card, saved in `/var/lib/media-import/settings.json`
(e.g. `{"source": "ripped", "library_drive": false, "skip_existing": true}`). The importer reads them
when a drive is plugged in; a missing or broken file means all defaults.

- **Source folder** (`"source"`: `"compressed"`, the default, or `"ripped"`): which folder at the drive's root
  is imported, `Compressed Media` or `Ripped Media`. Both work exactly as described above.
- **Library drive mode** (`"library_drive"`, off by default): for a drive that's already laid out like a
  library. Everything in its `Movies/` and `Shows/` folders (any case) is copied, videos, subtitles,
  artwork and `.nfo` files alike, keeping the folders: `Movies/Heat (1995)/...` goes to
  `movies/Heat (1995)/...`, and a show folder is matched to an existing library show as above. Nothing on
  the drive is changed or deleted, so it's only mounted read-only. The source folder switch doesn't apply.
- **Skip files already in the library** (`"skip_existing"`, off by default): a file whose name is already
  at its spot in the library isn't copied (in the normal modes it stays on the USB). Works in every mode.

## Install

```bash
git clone <repo-url> media-import
cd media-import
sudo ./install.sh
```

## Update

```bash
cd /path/to/media-import && git pull
# re-run sudo ./install.sh only if media-import@.service or 99-media-import.rules changed
```

## Status file

While it runs, the importer keeps `/var/lib/media-import/status.json` up to date: `plugged_in`
(mounting, scanning), `running` (file N of M, current file, bytes copied) and `finished` (with a
`result` of `ok`, `problems`, `no_folder`, `nothing`, `aborted`, `mount_failed`, `interrupted` or
`error`, plus counts, including `existing` for files skip-existing mode didn't copy, the `mode` (`compressed`,
`ripped` or `library`) and a message). It's rewritten every 5 seconds during a run, so a stale
`updated` time means the importer died. The Xenticorp dashboard reads it to show the import status.
Drives on the ignore list and the library drive never touch it. On a drive with several partitions,
partitions with no media don't overwrite a sibling partition's result while that drive is plugged in.

## Logs / manual run

```bash
journalctl -u 'media-import@*' -n 50
sudo python3 media_import.py --dir /path/to/mounted/usb
```
