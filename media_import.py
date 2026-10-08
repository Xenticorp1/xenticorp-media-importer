#!/usr/bin/env python3
"""
Xenticorp Media Import (X4)

Runs when a USB drive is plugged in (via udev -> systemd). If the drive has a
"Compressed Media" folder at its root, its contents are imported into Jellyfin:

  Compressed Media/Movie.mkv              -> <library>/movies/Movie.mkv
  Compressed Media/<Show>/Season 01/...   -> <library>/shows/<Show>/Season 01/...

- Loose .mkv / .mp4 files at the root count as movies; other loose files are ignored.
- A show folder is matched to an existing library show by name. Case, punctuation
  and a trailing "(YYYY)" / "[tmdbid-...]" tag are ignored, so "Breaking Bad"
  lands in "Breaking Bad (2008)" if that's the existing one. If nothing matches,
  a new show folder is created.
- Every file is copied, verified by SHA-256, and only then deleted from the USB.
  Folders emptied by the import are removed; folders that were already empty
  (e.g. a blank "Season 2") stay on the USB, and so does their show folder.
- If the file already exists in the library, it is overwritten.

Manual use:  sudo python3 media_import.py --dir /path/to/mounted/usb
"""
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---- Config -----------------------------------------------------------------
LIBRARY_ROOT = Path("/mnt/media/jellyfin/media")
MOVIES_DIR = LIBRARY_ROOT / "movies"
SHOWS_DIR = LIBRARY_ROOT / "shows"
SOURCE_FOLDER = "Compressed Media"
MOVIE_EXTS = {".mkv", ".mp4"}
IGNORE_UUIDS_FILE = Path("/etc/media-import/ignore-uuids")  # drives never touched
MOUNT_BASE = Path("/run/media-import")
LOCK_FILE = "/run/media-import.lock"
CHUNK = 8 * 1024 * 1024
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
# -----------------------------------------------------------------------------


def log(msg):
    print(msg, flush=True)  # stdout goes to journald: journalctl -u 'media-import@*'


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def mount_target_of(dev):
    out = run(["findmnt", "-n", "-o", "TARGET", "--source", dev]).stdout.splitlines()
    return out[0] if out else None


def fs_of(path, field):
    out = run(["findmnt", "-n", "-o", field, "--target", str(path)]).stdout.splitlines()
    return out[0] if out else None


def is_junk(name):
    return name.lower() in JUNK_NAMES or name.startswith("._")


def norm_title(name):
    name = re.sub(r"\s*[\[{][^\]}]*[\]}]", "", name)   # [tmdbid-123], {imdb-tt..}
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)        # trailing (2008)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_show_dir(show_name):
    target = norm_title(show_name)
    if SHOWS_DIR.is_dir():
        for d in SHOWS_DIR.iterdir():
            if d.is_dir() and norm_title(d.name) == target:
                return d, True
    return SHOWS_DIR / show_name, False


def sha256_file(path, drop_cache=False):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if drop_cache:  # make sure verification reads the disk, not RAM
            try:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def copy_verify_delete(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.importing")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(CHUNK):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if tmp.stat().st_size != src.stat().st_size or sha256_file(tmp, True) != h.hexdigest():
            raise IOError("verification failed (hash/size mismatch)")
        os.replace(tmp, dst)  # atomic; overwrites an existing file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    src.unlink()


def import_media(root: Path):
    src_dir = root / SOURCE_FOLDER
    if not src_dir.is_dir():
        log(f"No '{SOURCE_FOLDER}' folder on {root}, nothing to do.")
        return 0

    for d in (MOVIES_DIR, SHOWS_DIR):
        if not d.is_dir():
            log(f"ABORT: library folder missing: {d} (is the media drive mounted?)")
            return 2
    if fs_of(LIBRARY_ROOT, "TARGET") == "/":
        log(f"ABORT: {LIBRARY_ROOT} is on the root filesystem; media drive not mounted?")
        return 2

    jobs = []  # (src, dst)
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if is_junk(entry.name):
            continue
        if entry.is_file():
            if entry.suffix.lower() in MOVIE_EXTS:
                jobs.append((entry, MOVIES_DIR / entry.name))
            else:
                log(f"Skipping non-video file: {entry.name}")
        elif entry.is_dir():
            show_dir, exists = find_show_dir(entry.name)
            log(f"Show '{entry.name}' -> {show_dir} ({'existing' if exists else 'new'})")
            for dirpath, dirnames, filenames in os.walk(entry):
                dirnames[:] = sorted(n for n in dirnames if not is_junk(n))
                for fn in sorted(filenames):
                    if is_junk(fn):
                        continue
                    s = Path(dirpath) / fn
                    jobs.append((s, show_dir / s.relative_to(entry)))

    if not jobs:
        log("Nothing to import.")
        return 0

    ok, failed = 0, []
    for i, (s, d) in enumerate(jobs, 1):
        rel = s.relative_to(src_dir)
        try:
            action = "overwrite" if d.exists() else "copy"
            log(f"[{i}/{len(jobs)}] {action}: {rel} -> {d}")
            copy_verify_delete(s, d)
            ok += 1
        except Exception as e:
            log(f"  FAILED: {rel}: {e} (left on USB)")
            failed.append(rel)

    # Remove USB folders that this run emptied (deepest first). Folders that were
    # already empty (e.g. a blank "Season 2") are never in this set, so they stay.
    emptied = set()
    for s, _ in jobs:
        p = s.parent
        while p != src_dir:
            emptied.add(p)
            p = p.parent
    for d in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        try:
            leftovers = list(d.iterdir())
            if all(is_junk(x.name) and x.is_file() for x in leftovers):
                for x in leftovers:
                    x.unlink()
                d.rmdir()
                log(f"Removed emptied folder: {d.relative_to(src_dir)}")
        except OSError as e:
            log(f"  Could not remove {d}: {e}")

    os.sync()
    log(f"Done: {ok} imported, {len(failed)} failed.")
    return 1 if failed else 0


def ignored_uuids():
    try:
        return {l.strip().lower() for l in IGNORE_UUIDS_FILE.read_text().splitlines()
                if l.strip() and not l.startswith("#")}
    except FileNotFoundError:
        return set()


def wait_for_mount(dev, seconds):
    for _ in range(seconds):
        mnt = mount_target_of(dev)
        if mnt:
            return mnt
        time.sleep(1)
    return None


def acquire_mount(dev):
    """Use the desktop automounter's mount if it shows up; otherwise mount it
    ourselves. Returns (mountpoint, we_mounted) or (None, False)."""
    mnt = wait_for_mount(dev, 10)  # give GNOME/udisks first shot
    if mnt:
        return mnt, False
    target = str(MOUNT_BASE / os.path.basename(dev))
    os.makedirs(target, exist_ok=True)
    for attempt in range(1, 4):
        r = run(["mount", "-o", "rw,noatime", dev, target])
        if r.returncode == 0:
            return target, True
        # Lost a race with the automounter? Then just use its mount.
        mnt = wait_for_mount(dev, 5)
        if mnt:
            try:
                os.rmdir(target)
            except OSError:
                pass
            return mnt, False
        log(f"Mount attempt {attempt} failed: {r.stderr.strip()}")
    try:
        os.rmdir(target)
    except OSError:
        pass
    log(f"Could not mount {dev}, giving up.")
    return None, False


def handle_device(dev):
    dev = os.path.realpath(dev)
    uuid = run(["blkid", "-s", "UUID", "-o", "value", dev]).stdout.strip().lower()
    if uuid and uuid in ignored_uuids():
        log(f"{dev} (UUID {uuid}) is on the ignore list, skipping.")
        return 0
    lib_src = fs_of(LIBRARY_ROOT, "SOURCE")
    if lib_src and os.path.realpath(lib_src) == dev:
        log(f"{dev} is the media library drive, skipping.")
        return 0

    mnt, we_mounted = acquire_mount(dev)
    if not mnt:
        return 1
    log(f"{dev} mounted at {mnt}")

    try:
        return import_media(Path(mnt))
    finally:
        if we_mounted:  # put things back the way we found them
            run(["umount", mnt])
            try:
                os.rmdir(mnt)
            except OSError:
                pass


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--dir":
        target = lambda: import_media(Path(sys.argv[2]))
    elif len(sys.argv) == 2:
        target = lambda: handle_device(sys.argv[1])
    else:
        print(__doc__)
        return 64
    with open(LOCK_FILE, "w") as lock:  # one import at a time
        fcntl.flock(lock, fcntl.LOCK_EX)
        return target()


if __name__ == "__main__":
    sys.exit(main())
