# Xenticorp Media Import

Headless USB to Jellyfin importer for X4. Plug in a USB with a `Compressed Media` folder at its root:

| On the USB | Goes to |
|---|---|
| `Compressed Media/Movie.mkv` (or `.mp4`) | `/mnt/media/jellyfin/media/movies/` |
| `Compressed Media/<Show>/Season N/...` | `/mnt/media/jellyfin/media/shows/<Show>/Season N/...` |

- Show names match existing library folders ignoring case, punctuation and a trailing `(YYYY)`, so `The Big Bang Theory` goes into `The Big Bang Theory (2007)`.
- Loose files at the root whose name contains an episode code (`S01E01`, `s1e1`, `1x01`) are not imported as movies: they're skipped, left on the USB, and the run exits non-zero so it shows as needing attention. Put them in a show folder.
- Each file is copied, SHA-256 verified, then deleted from the USB. Existing library files are overwritten.
- Folders the import empties are removed. Folders that were already empty stay on the USB.
- The library drive is never touched (UUID ignore list). The importer refuses to run if the library isn't mounted.

## Install (X4)

```bash
git clone <repo-url> ~/media-import
cd ~/media-import
sudo ./install.sh
```

## Update

```bash
cd ~/media-import && git pull
# re-run sudo ./install.sh only if media-import@.service or 99-media-import.rules changed
```

## Logs / manual run

```bash
journalctl -u 'media-import@*' -n 50
sudo python3 media_import.py --dir /path/to/mounted/usb
```
