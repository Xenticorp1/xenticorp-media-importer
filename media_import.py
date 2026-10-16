#!/usr/bin/env python3
"""
Xenticorp Media Import (X4)

Runs when a USB drive is plugged in (via udev -> systemd). If the drive has a
"Compressed Media" folder at its root, its contents are imported into Jellyfin:

  Compressed Media/Movie.mkv              -> <library>/movies/Movie.mkv
  Compressed Media/<Show>/Season 01/...   -> <library>/shows/<Show>/Season 01/...

Switches on the Xenticorp dashboard (saved in /var/lib/media-import/settings.json):
- Ripped Media: import the "Ripped Media" folder instead, the same way.
- Library drive mode: the drive is laid out like a library. Everything in its
  Movies/ and Shows/ folders (videos, subtitles, artwork, .nfo...) is copied:
    Movies/<Movie (Year)>/...             -> <library>/movies/<Movie (Year)>/...
    Shows/<Show>/Season 01/...            -> <library>/shows/<Show>/Season 01/...
  Nothing on the drive is changed or deleted; it's only ever mounted read-only.
  The run then waits for the dashboard: you reject the movies/shows you don't
  want and approve, and only the rest is copied.
- Skip files already in the library (works in every mode; see below).

- Loose .mkv / .mp4 files at the root count as movies; other loose files are ignored.
- A show folder is matched to an existing library show by name. Case, punctuation
  and a trailing "(YYYY)" / "[tmdbid-...]" tag are ignored, so "Breaking Bad"
  lands in "Breaking Bad (2008)" if that's the existing one. If nothing matches,
  a new show folder is created.
- Every file is copied, verified by SHA-256, and only then deleted from the USB
  (except in library drive mode, which keeps everything on the drive).
  Folders emptied by the import are removed; folders that were already empty
  (e.g. a blank "Season 2") stay on the USB, and so does their show folder.
- If the file already exists in the library, it is overwritten. With skip-existing
  mode on (a switch on the Xenticorp dashboard, saved in
  /var/lib/media-import/settings.json), it's left on the USB instead.
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
import secrets
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
SOURCE_FOLDERS = {"compressed": "Compressed Media", "ripped": "Ripped Media"}  # picked on the dashboard
LIBRARY_FOLDERS = {"movies": "Movies", "shows": "Shows"}  # library drive mode; case doesn't matter
MOVIE_EXTS = {".mkv", ".mp4"}
IGNORE_UUIDS_FILE = Path("/etc/media-import/ignore-uuids")  # drives never touched
MOUNT_BASE = Path("/run/media-import")
LOCK_FILE = "/run/media-import.lock"
CHUNK = 8 * 1024 * 1024
STATUS_FILE = Path("/var/lib/media-import/status.json")  # read by the dashboard
SETTINGS_FILE = Path("/var/lib/media-import/settings.json")  # written by the dashboard
REVIEW_FILE = Path("/var/lib/media-import/review.json")  # library drive mode: the dashboard's approval
SYS_BLOCK = Path("/sys/class/block")
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

    # A drive with several partitions runs the importer once per partition, one after another.
    # Results that just mean "no media on this partition" stay quiet when another partition of the
    # same drive, still plugged in, already reported something real (e.g. its import finished).
    QUIET_RESULTS = (None, "no_folder", "nothing", "mount_failed")

    def __init__(self):
        self.data = None
        self.quiet = False
        self.lock = threading.Lock()
        self.last_write = 0

    def start(self, device, label):
        now = int(time.time())
        disk = device
        if device:
            parent = os.path.basename(os.path.realpath(SYS_BLOCK / device / ".."))
            if parent != "block":  # a partition: its parent is the whole disk (sdb4 -> sdb)
                disk = parent
            try:
                prev = json.loads(STATUS_FILE.read_text())
                self.quiet = (prev.get("disk") == disk and prev.get("device") != device
                              and prev.get("result") not in self.QUIET_RESULTS
                              and (SYS_BLOCK / str(prev.get("device"))).exists())
            except (OSError, ValueError, AttributeError):
                pass
        self.data = {"v": 1, "state": "plugged_in", "step": "mounting", "device": device, "disk": disk,
                     "label": label or None, "started": now, "updated": now, "finished": None,
                     "total": 0, "index": 0, "file": None, "bytes_total": 0, "bytes_done": 0,
                     "imported": 0, "failed": 0, "skipped": 0, "existing": 0, "failed_files": [],
                     "mode": None, "items": [], "run_id": None, "rejected": 0, "result": None, "message": None}
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
        if (self.quiet and self.data["state"] not in ("running", "review")
                and self.data["result"] in self.QUIET_RESULTS):
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


def load_settings():
    """The dashboard's switches. Anything missing or unreadable gets the default:
    Compressed Media, library drive mode off, existing library files overwritten."""
    settings = {"source": "compressed", "library_drive": False, "skip_existing": False}
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return settings
    except (OSError, ValueError) as e:
        log(f"(couldn't read {SETTINGS_FILE}, using the defaults: {e})")
        return settings
    if raw.get("source") in SOURCE_FOLDERS:
        settings["source"] = raw["source"]
    settings["library_drive"] = raw.get("library_drive") is True
    settings["skip_existing"] = raw.get("skip_existing") is True
    return settings


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


def walk_files(top):
    """Every file under top (sorted, junk left out)."""
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(n for n in dirnames if not is_junk(n))
        for fn in sorted(filenames):
            if not is_junk(fn):
                yield Path(dirpath) / fn


def copy_verify_delete(src: Path, dst: Path, delete=True):
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
    if delete:
        src.unlink()


def add_item(items, kind, name, **extra):
    """One movie or show in the dashboard's list (items: id -> item, in drive order). Returns its id."""
    iid = f"{kind}:{name}"
    if iid not in items:
        items[iid] = {"id": iid, "kind": kind, "name": name, "files": 0, "bytes": 0, "existing": 0,
                      "copy": 0, "done": 0, "failed": 0, "state": "waiting", **extra}
    return iid


def add_show(items, entry):
    show_dir, exists = find_show_dir(entry.name)
    log(f"Show '{entry.name}' -> {show_dir} ({'existing' if exists else 'new'})")
    return show_dir, add_item(items, "show", entry.name, target=show_dir.name, new=not exists)


def scan_media_folder(src_dir, items):
    """Compressed / Ripped Media: loose videos are movies, folders are shows.
    Returns (jobs as (src, dst, item id), skipped misplaced episodes)."""
    jobs, skipped = [], 0
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if is_junk(entry.name):
            continue
        if entry.is_file():
            if entry.suffix.lower() in MOVIE_EXTS:
                if EPISODE_RE.search(entry.name):
                    log(f"Skipping '{entry.name}': looks like a TV episode, put it in a show folder")
                    skipped += 1
                    continue
                jobs.append((entry, MOVIES_DIR / entry.name, add_item(items, "movie", entry.stem)))
            else:
                log(f"Skipping non-video file: {entry.name}")
        elif entry.is_dir():
            show_dir, iid = add_show(items, entry)
            for s in walk_files(entry):
                jobs.append((s, show_dir / s.relative_to(entry), iid))
    return jobs, skipped


def library_folders(root):
    """Library drive mode: the drive's Movies/ and Shows/ folders, as {"movies": path, "shows": path}."""
    found = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        for kind, name in LIBRARY_FOLDERS.items():
            if kind not in found and entry.name.lower() == name.lower() and entry.is_dir():
                found[kind] = entry
    return found


def scan_library_drive(found, items):
    """Library drive mode: every file, folders kept. Movies/X/... -> movies/X/...; a show folder
    is matched to an existing library show like in the other modes. Each folder in Movies/ is one
    movie; a loose file there belongs to the loose video its name starts with (Heat.en.srt -> Heat)."""
    jobs = []
    if "movies" in found:
        movies = found["movies"]
        stems = sorted((e.stem for e in movies.iterdir() if e.is_file() and e.suffix.lower() in MOVIE_EXTS),
                       key=len, reverse=True)  # longest first, so "Heat 2" wins over "Heat"
        for s in walk_files(movies):
            rel = s.relative_to(movies)
            name = rel.parts[0] if len(rel.parts) > 1 else next(
                (st for st in stems if s.name.startswith(st)), s.stem)
            jobs.append((s, MOVIES_DIR / rel, add_item(items, "movie", name)))
    if "shows" in found:
        for entry in sorted(found["shows"].iterdir(), key=lambda p: p.name.lower()):
            if is_junk(entry.name):
                continue
            if not entry.is_dir():
                log(f"Skipping '{found['shows'].name}/{entry.name}': not inside a show folder")
                continue
            show_dir, iid = add_show(items, entry)
            for s in walk_files(entry):
                jobs.append((s, show_dir / s.relative_to(entry), iid))
    return jobs


def wait_for_review(items):
    """Library drive mode: show the list on the dashboard and wait until it's approved there.
    Returns the rejected item ids, or None if the run was cancelled."""
    run_id = secrets.token_hex(8)
    REVIEW_FILE.unlink(missing_ok=True)
    STATUS.update(state="review", step="waiting for approval", run_id=run_id, force=True)
    log("Library drive mode: waiting for approval on the dashboard...")
    while True:
        time.sleep(1)
        try:
            review = json.loads(REVIEW_FILE.read_text())
        except (OSError, ValueError):
            continue  # not answered yet (the dashboard writes the file in one go)
        if not isinstance(review, dict) or review.get("run_id") != run_id:
            continue  # left over from another run
        REVIEW_FILE.unlink(missing_ok=True)
        if review.get("cancel") is True:
            log("Cancelled on the dashboard.")
            return None
        rejected = {r for r in review.get("rejected") or [] if isinstance(r, str) and r in items}
        for iid in rejected:
            log(f"Rejected on the dashboard: {items[iid]['name']}")
        return rejected


def clean_up_emptied(src_dir, jobs):
    STATUS.update(step="cleaning up", file=None, force=True)
    # Remove USB folders that this run emptied (deepest first). Folders that were
    # already empty (e.g. a blank "Season 2") are never in this set, so they stay.
    emptied = set()
    for s, *_ in jobs:
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


def import_media(root: Path, settings=None):
    settings = settings or load_settings()
    library = settings["library_drive"]
    folder = SOURCE_FOLDERS[settings["source"]]
    STATUS.update(step="scanning", mode="library" if library else settings["source"], force=True)
    if library:
        found = library_folders(root)
        if not found:
            log(f"Library drive mode: no Movies or Shows folder on {root}, nothing to do.")
            STATUS.finish("no_folder", "Library drive mode is on, but this drive has no Movies or Shows folder")
            return 0
        base = root  # for the paths in the log
        where = " and ".join(f"'{p.name}'" for p in found.values())
        log(f"Library drive mode: copying {where}; nothing on the drive is deleted.")
    else:
        base = src_dir = root / folder
        if not src_dir.is_dir():
            log(f"No '{folder}' folder on {root}, nothing to do.")
            STATUS.finish("no_folder", f"No '{folder}' folder on this drive")
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

    items = {}  # the movies and shows found, for the dashboard
    if library:
        jobs, skipped = scan_library_drive(found, items), 0  # (src, dst, item id); skipped = misplaced episodes
    else:
        jobs, skipped = scan_media_folder(src_dir, items)

    existing = 0  # already in the library, left on the USB (skip-existing mode)
    if jobs and settings["skip_existing"]:
        log("Skip-existing mode is on: files already in the library aren't copied.")
    keep = []
    for s, d, iid in jobs:
        item = items[iid]
        item["files"] += 1
        if settings["skip_existing"] and d.exists():
            log(f"Skipping '{s.relative_to(base)}': already in the library")
            existing += 1
            item["existing"] += 1
            continue
        try:
            item["bytes"] += s.stat().st_size
        except OSError:
            pass
        item["copy"] += 1
        keep.append((s, d, iid))
    jobs = keep
    items = dict(sorted(items.items(), key=lambda kv: (kv[1]["kind"] != "movie", kv[1]["name"].lower())))
    for item in items.values():
        if not item["copy"]:
            item["state"] = "in_library"
    STATUS.update(items=list(items.values()), existing=existing, force=True)

    rejected = set()
    if library and jobs:
        rejected = wait_for_review(items)
        if rejected is None:
            for item in items.values():
                if item["state"] == "waiting":
                    item["state"] = "rejected"
            STATUS.finish("cancelled", "Cancelled on the dashboard; nothing was copied")
            return 0
        for iid in rejected:
            items[iid]["state"] = "rejected"
        jobs = [j for j in jobs if j[2] not in rejected]
        STATUS.update(state="plugged_in", step="scanning", rejected=len(rejected), force=True)
        if not jobs:
            log("Everything was rejected; nothing to copy.")
            STATUS.finish("cancelled", "Everything was rejected on the dashboard; nothing was copied")
            return 0
    left_note = (f"{existing} file{'s' if existing != 1 else ''} already in the library"
                 + (", not copied" if library else ", left on the drive") if existing else None)

    if not jobs:
        log("Nothing to import.")
        if skipped or existing:
            log(f"Done: 0 imported, 0 failed, {skipped} skipped (misplaced episodes)"
                + (f", {existing} already in the library." if existing else "."))
            STATUS.update(skipped=skipped, existing=existing)
        if skipped:
            why = [f"{skipped} loose episode file{'s' if skipped != 1 else ''} skipped; put them in a show folder"]
            if left_note:
                why.append(left_note)
            STATUS.finish("problems", "; ".join(why))
            return 1
        if existing:
            STATUS.finish("ok", left_note)
            return 0
        STATUS.finish("nothing", f"Nothing in this drive's {where} folder{'s' if len(found) > 1 else ''}"
                      if library else f"The '{folder}' folder is empty")
        return 0

    total_bytes = sum(item["bytes"] for item in items.values() if item["id"] not in rejected)
    STATUS.update(state="running", step="copying", total=len(jobs), skipped=skipped, existing=existing,
                  bytes_total=total_bytes, force=True)

    ok, failed = 0, []
    for i, (s, d, iid) in enumerate(jobs, 1):
        rel = s.relative_to(base)
        item = items[iid]
        item["state"] = "copying"
        STATUS.update(index=i, file=str(rel), step="copying", force=True)
        try:
            action = "overwrite" if d.exists() else "copy"
            log(f"[{i}/{len(jobs)}] {action}: {rel} -> {d}")
            copy_verify_delete(s, d, delete=not library)
            ok += 1
            item["done"] += 1
            STATUS.update(imported=ok)
        except Exception as e:
            log(f"  FAILED: {rel}: {e} (left on USB)")
            failed.append(rel)
            item["failed"] += 1
            STATUS.update(failed=len(failed), failed_files=[str(f) for f in failed[:10]], force=True)
        if item["done"] + item["failed"] == item["copy"]:
            item["state"] = "failed" if item["failed"] else "done"
            STATUS.update(force=True)

    if not library:  # library drive mode deletes nothing, so there's nothing to tidy up
        clean_up_emptied(src_dir, jobs)

    os.sync()
    log(f"Done: {ok} imported, {len(failed)} failed, {skipped} skipped (misplaced episodes)"
        + (f", {existing} already in the library." if existing else "."))
    if failed or skipped:
        why = []
        if failed:
            why.append(f"{len(failed)} file{'s' if len(failed) != 1 else ''} failed and stayed on the drive")
        if skipped:
            why.append(f"{skipped} loose episode file{'s' if skipped != 1 else ''} skipped; put them in a show folder")
        if left_note:
            why.append(left_note)
        STATUS.finish("problems", "; ".join(why))
        return 1
    STATUS.finish("ok", left_note)
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
    ourselves, read-only (handle_device switches it to read-write only if it has
    media, so other partitions, e.g. Windows ones on an SSD, are never written).
    Returns (mountpoint, we_mounted) or (None, False)."""
    mnt = wait_for_mount(dev, 10)  # give GNOME/udisks first shot
    if mnt:
        return mnt, False
    target = str(MOUNT_BASE / os.path.basename(dev))
    os.makedirs(target, exist_ok=True)
    for attempt in range(1, 4):
        r = run(["mount", "-o", "ro,noatime", dev, target])
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

    settings = load_settings()
    # Library drive mode only reads the drive, so it stays read-only.
    needs_write = not settings["library_drive"] and (Path(mnt) / SOURCE_FOLDERS[settings["source"]]).is_dir()
    try:
        if we_mounted and needs_write:
            # Unmount and mount again read-write. (An in-place "remount,rw" isn't supported
            # by every filesystem driver, e.g. FUSE exFAT.)
            run(["umount", mnt])
            r = run(["mount", "-o", "rw,noatime", dev, mnt])
            if r.returncode != 0:
                log(f"Could not remount {dev} read-write: {r.stderr.strip()}")
                STATUS.finish("mount_failed", "Couldn't mount the drive read-write")
                return 1
        return import_media(Path(mnt), settings)
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
