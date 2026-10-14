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
- Progress is written to /var/lib/media-import/status.json (plugged in, importing
  file N of M, finished) for the Xenticorp dashboard to show. Drives on the ignore
  list and the library drive never touch it.

Manual use:  sudo python3 media_import.py --dir /path/to/mounted/usb
"""
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
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
STATUS_FILE = Path("/var/lib/media-import/status.json")  # read by the dashboard
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
# Episode codes like S01E01 / s1e1 / 1x01; loose files matching this aren't movies.
EPISODE_RE = re.compile(r"(?i)(?<![a-z0-9])(s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{2,3})(?![0-9])")
# -----------------------------------------------------------------------------


def log(msg):
    print(msg, flush=True)  # stdout goes to journald: journalctl -u 'media-import@*'


class Status:
    """The current run, as JSON in STATUS_FILE. state: plugged_in -> running -> finished.
    A heartbeat thread rewrites it every few seconds while a run is going, so a reader can
    tell a run that died (stale "updated") from one that's just copying a big file."""

    def __init__(self):
        self.data = None
        self.lock = threading.Lock()
        self.last_write = 0

    def start(self, device, label):
        now = int(time.time())
        self.data = {"v": 1, "state": "plugged_in", "step": "mounting", "device": device,
                     "label": label or None, "started": now, "updated": now, "finished": None,
                     "total": 0, "index": 0, "file": None, "bytes_total": 0, "bytes_done": 0,
                     "imported": 0, "failed": 0, "skipped": 0, "failed_files": [],
                     "result": None, "message": None}
        self.write(force=True)
        threading.Thread(target=self._beat, daemon=True).start()

    def update(self, force=False, **fields):
        if self.data is None:
            return
        with self.lock:
            self.data.update(fields)
        self.write(force)

    def finish(self, result, message=None):
        if self.data is None or self.data["state"] == "finished":
            return
        self.update(force=True, state="finished", step=None, file=None, result=result,
                    message=message, finished=int(time.time()))

    def write(self, force=False):
        if self.data is None or (not force and time.monotonic() - self.last_write < 1):
            return
        with self.lock:
            self.data["updated"] = int(time.time())
            text = json.dumps(self.data)
            self.last_write = time.monotonic()
            try:
                STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
                tmp.write_text(text + "\n")
                tmp.chmod(0o644)
                os.replace(tmp, STATUS_FILE)  # readers never see a half-written file
            except OSError as e:
                print(f"(couldn't write {STATUS_FILE}: {e})", flush=True)

    def _beat(self):
        while self.data["state"] != "finished":
            time.sleep(5)
            self.write(force=True)


STATUS = Status()


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
                if STATUS.data:
                    STATUS.update(bytes_done=STATUS.data["bytes_done"] + len(chunk))
            fo.flush()
            STATUS.update(step="verifying", force=True)
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
    STATUS.update(step="scanning", force=True)
    src_dir = root / SOURCE_FOLDER
    if not src_dir.is_dir():
        log(f"No '{SOURCE_FOLDER}' folder on {root}, nothing to do.")
        STATUS.finish("no_folder", f"No '{SOURCE_FOLDER}' folder on this drive")
        return 0

    for d in (MOVIES_DIR, SHOWS_DIR):
        if not d.is_dir():
            log(f"ABORT: library folder missing: {d} (is the media drive mounted?)")
            STATUS.finish("aborted", "The library drive isn't mounted, so nothing was imported")
            return 2
    if fs_of(LIBRARY_ROOT, "TARGET") == "/":
        log(f"ABORT: {LIBRARY_ROOT} is on the root filesystem; media drive not mounted?")
        STATUS.finish("aborted", "The library drive isn't mounted, so nothing was imported")
        return 2

    jobs = []  # (src, dst)
    skipped = 0  # misplaced episodes left on the USB
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if is_junk(entry.name):
            continue
        if entry.is_file():
            if entry.suffix.lower() in MOVIE_EXTS:
                if EPISODE_RE.search(entry.name):
                    log(f"Skipping '{entry.name}': looks like a TV episode, put it in a show folder")
                    skipped += 1
                    continue
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
        if skipped:
            log(f"Done: 0 imported, 0 failed, {skipped} skipped (misplaced episodes).")
            STATUS.update(skipped=skipped)
            STATUS.finish("problems", f"{skipped} loose episode file{'s' if skipped != 1 else ''} skipped; put them in a show folder")
            return 1
        STATUS.finish("nothing", f"The '{SOURCE_FOLDER}' folder is empty")
        return 0

    total_bytes = 0
    for s, _ in jobs:
        try:
            total_bytes += s.stat().st_size
        except OSError:
            pass
    STATUS.update(state="running", step="copying", total=len(jobs), skipped=skipped,
                  bytes_total=total_bytes, force=True)

    ok, failed = 0, []
    for i, (s, d) in enumerate(jobs, 1):
        rel = s.relative_to(src_dir)
        STATUS.update(index=i, file=str(rel), step="copying", force=True)
        try:
            action = "overwrite" if d.exists() else "copy"
            log(f"[{i}/{len(jobs)}] {action}: {rel} -> {d}")
            copy_verify_delete(s, d)
            ok += 1
            STATUS.update(imported=ok)
        except Exception as e:
            log(f"  FAILED: {rel}: {e} (left on USB)")
            failed.append(rel)
            STATUS.update(failed=len(failed), failed_files=[str(f) for f in failed[:10]], force=True)

    STATUS.update(step="cleaning up", file=None, force=True)
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
    log(f"Done: {ok} imported, {len(failed)} failed, {skipped} skipped (misplaced episodes).")
    if failed or skipped:
        why = []
        if failed:
            why.append(f"{len(failed)} file{'s' if len(failed) != 1 else ''} failed and stayed on the drive")
        if skipped:
            why.append(f"{skipped} loose episode file{'s' if skipped != 1 else ''} skipped; put them in a show folder")
        STATUS.finish("problems", "; ".join(why))
        return 1
    STATUS.finish("ok")
    return 0


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

    label = run(["blkid", "-s", "LABEL", "-o", "value", dev]).stdout.strip()
    STATUS.start(os.path.basename(dev), label)
    mnt, we_mounted = acquire_mount(dev)
    if not mnt:
        STATUS.finish("mount_failed", "Couldn't mount the drive")
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


def stopped(signum, frame):
    # systemd stops the service (SIGTERM) when the drive is pulled mid-run (BindsTo=) or X4 shuts
    # down. Unwind normally so the half-written copy is removed and the status says what happened.
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, stopped)
    if len(sys.argv) == 3 and sys.argv[1] == "--dir":
        def target():
            STATUS.start(None, sys.argv[2])
            return import_media(Path(sys.argv[2]))
    elif len(sys.argv) == 2:
        target = lambda: handle_device(sys.argv[1])
    else:
        print(__doc__)
        return 64
    with open(LOCK_FILE, "w") as lock:  # one import at a time
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return target()
        except (SystemExit, KeyboardInterrupt):
            STATUS.finish("interrupted", "Stopped before finishing (drive removed or X4 shutting down). "
                                         "Anything not yet verified is still on the drive.")
            raise
        except BaseException as e:
            STATUS.finish("error", f"Importer crashed: {e}")
            raise


if __name__ == "__main__":
    sys.exit(main())
