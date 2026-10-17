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
  Sub-switch "copy the library onto the drive": X4's library also goes the other
  way, into the drive's Movies/ and Shows/ folders (anything not on it yet; the
  library itself is never changed). That needs the drive read-write.
- Skip files already in the library (works in every mode; see below).
- Movie folders (on unless switched off): each movie goes into a folder of its
  own, the way Jellyfin likes it, with the loose files that share its name
  (subtitles, artwork, .nfo): Heat (1995).mkv + Heat (1995).en.srt ->
  <library>/movies/Heat (1995)/. Also used when copying onto a library drive.

Sort incoming (the dashboard's "Sort incoming" button, or --incoming by hand):
everything inside INCOMING_DIR's movies/ and shows/ folders goes into the library
the same way as a library drive (movie folders, show matching), then is removed
from incoming; the movies/ and shows/ folders themselves stay. Files already in
the library are never copied again (they stay in incoming), and a file changed in
the last couple of minutes is left for next time in case it's still being written.

Tidy (the dashboard's "Tidy library" button, or by hand): moves movies sitting
loose in <library>/movies into a folder each, the same way. --tidy only plans
it; --tidy --run moves. Nothing is ever overwritten.

- Loose .mkv / .mp4 files at the root count as movies; other loose files are ignored.
- A show folder is matched to an existing library show by name. Case, punctuation
  and a trailing "(YYYY)" / "[tmdbid-...]" tag are ignored, so "Breaking Bad"
  lands in "Breaking Bad (2008)" if that's the existing one. If nothing matches,
  a new show folder is created.
- Every file is copied, verified by SHA-256, and only then deleted from the USB
  (unless the dashboard's "delete from the drive" switch is off, and never in
  library drive mode, which keeps everything on the drive). With deleting off the
  drive is only read, so it stays mounted read-only.
  Folders emptied by the import are removed; folders that were already empty
  (e.g. a blank "Season 2") stay on the USB, and so does their show folder.
- If the file already exists in the library, it is overwritten. With skip-existing
  mode on (a switch on the Xenticorp dashboard, saved in
  /var/lib/media-import/settings.json), it's left on the USB instead.
- Progress is written to /var/lib/media-import/status.json (plugged in, importing
  file N of M, finished) for the Xenticorp dashboard to show. Drives on the ignore
  list and the library drive never touch it.

Manual use:  sudo python3 media_import.py --dir /path/to/mounted/usb
             sudo python3 media_import.py --tidy [--run]
             sudo python3 media_import.py --incoming
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
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
INCOMING_DIR = Path("/home/greygamer14/incoming")  # Sort incoming: holds movies/ and shows/
INCOMING_SETTLE = 120  # seconds a file must have been left alone before Sort incoming takes it
SOURCE_FOLDERS = {"compressed": "Compressed Media", "ripped": "Ripped Media"}  # picked on the dashboard
LIBRARY_FOLDERS = {"movies": "Movies", "shows": "Shows"}  # library drive mode; case doesn't matter
DRIVE_SPARE = 512 * 1024 * 1024  # copying the library onto a drive leaves at least this much free
MOVIE_EXTS = {".mkv", ".mp4"}
# Any video, for working out which loose files belong to one movie (copy is still MOVIE_EXTS-only in
# Compressed / Ripped Media; library drives and the library itself can hold anything).
VIDEO_EXTS = MOVIE_EXTS | {".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg"}
IGNORE_UUIDS_FILE = Path("/etc/media-import/ignore-uuids")  # drives never touched
MOUNT_BASE = Path("/run/media-import")
LOCK_FILE = "/run/media-import.lock"
CHUNK = 8 * 1024 * 1024
STATUS_FILE = Path("/var/lib/media-import/status.json")  # read by the dashboard
SETTINGS_FILE = Path("/var/lib/media-import/settings.json")  # written by the dashboard
REVIEW_FILE = Path("/var/lib/media-import/review.json")  # library drive mode: the dashboard's approval
TIDY_REQUEST = Path("/var/lib/media-import/tidy-request.json")  # the dashboard's Tidy button
TIDY_STATUS = Path("/var/lib/media-import/tidy.json")  # read by the dashboard
SYS_BLOCK = Path("/sys/class/block")
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
# Episode codes like S01E01 / s1e1 / 1x01; loose files matching this aren't movies.
# A season folder ("Season 3", "Specials", "S03") sitting where a show folder should be.
SEASON_DIR_RE = re.compile(r"(?i)^(season\s*\d+|specials|s\d{1,2})$")
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
                     "mode": None, "items": [], "run_id": None, "rejected": 0, "exported": 0,
                     "no_space": 0, "drive_free": None, "deletes": None, "note": None, "result": None, "message": None}
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
        if self.data.get("note"):  # e.g. why the library wasn't copied onto the drive
            message = f"{message}. {self.data['note']}" if message else self.data["note"]
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
    settings = {"source": "compressed", "library_drive": False, "export_library": False, "skip_existing": False,
                "movie_folders": True, "delete_after": True}
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
    # A sub-switch of library drive mode: means nothing without it.
    settings["export_library"] = settings["library_drive"] and raw.get("export_library") is True
    settings["skip_existing"] = raw.get("skip_existing") is True
    settings["movie_folders"] = raw.get("movie_folders") is not False  # on unless switched off
    settings["delete_after"] = raw.get("delete_after") is not False  # likewise; never in library drive mode
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


def add_item(items, kind, name, direction="in", **extra):
    """One movie or show in the dashboard's list (items: id -> item). direction "in" = drive -> library,
    "out" = library -> drive (library drive mode's copy onto the drive). Returns its id."""
    iid = f"out:{kind}:{name}" if direction == "out" else f"{kind}:{name}"
    if iid not in items:
        items[iid] = {"id": iid, "kind": kind, "name": name, "dir": direction, "files": 0, "bytes": 0,
                      "existing": 0, "copy": 0, "done": 0, "failed": 0, "state": "waiting", **extra}
    return iid


def movie_name(rel, stems):
    """Which movie a file under a Movies folder belongs to: its top folder, or for a loose file the
    loose video its name starts with (Heat.en.srt -> Heat). stems: loose video names, longest first."""
    if len(rel.parts) > 1:
        return rel.parts[0]
    return next((st for st in stems if rel.name.startswith(st)), Path(rel.name).stem)


def loose_video_stems(folder, exts=VIDEO_EXTS):
    return sorted((e.stem for e in folder.iterdir() if e.is_file() and e.suffix.lower() in exts),
                  key=len, reverse=True)  # longest first, so "Heat 2" wins over "Heat"


def movie_dest(movies, rel, name, folders):
    """Where a file from a Movies folder goes under `movies`: a loose file moves into its movie's
    own folder when folders is on (Heat.en.srt -> Heat/Heat.en.srt); anything else keeps its path."""
    return movies / name / rel.name if folders and len(rel.parts) == 1 else movies / rel


def already_there(d, movies):
    """d exists, or it's movies/<Movie>/<file> and that file still sits loose in movies/ (not tidied)."""
    return d.exists() or (d.parent.parent == movies and (movies / d.name).exists())


def add_show(items, entry):
    show_dir, exists = find_show_dir(entry.name)
    log(f"Show '{entry.name}' -> {show_dir} ({'existing' if exists else 'new'})")
    return show_dir, add_item(items, "show", entry.name, target=show_dir.name, new=not exists)


def scan_media_folder(src_dir, items, folders=False):
    """Compressed / Ripped Media: loose videos are movies, folders are shows. With movie folders on,
    each movie gets its own folder and takes the loose files sharing its name (subtitles, artwork).
    Returns (jobs as (src, dst, item id), skipped misplaced episodes)."""
    jobs, skipped = [], 0
    movies = [st for st in loose_video_stems(src_dir, MOVIE_EXTS) if not EPISODE_RE.search(st)]
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if is_junk(entry.name):
            continue
        if entry.is_file():
            if entry.suffix.lower() in MOVIE_EXTS:
                if EPISODE_RE.search(entry.name):
                    log(f"Skipping '{entry.name}': looks like a TV episode, put it in a show folder")
                    skipped += 1
                    continue
                dst = MOVIES_DIR / entry.stem / entry.name if folders else MOVIES_DIR / entry.name
                jobs.append((entry, dst, add_item(items, "movie", entry.stem)))
            elif folders and (name := next((st for st in movies if entry.name.startswith(st)), None)):
                jobs.append((entry, MOVIES_DIR / name / entry.name, add_item(items, "movie", name)))
            else:
                log(f"Skipping non-video file: {entry.name}"
                    + ("" if folders else " (turn on movie folders to bring a movie's subtitles and artwork along)"))
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


def library_folder_clashes(root):
    """Names of Movies/Shows folders that exist more than once in different capitals ("Shows" and
    "shows"): only one would be read, so the run stops instead of quietly skipping the other."""
    clashes = []
    for name in LIBRARY_FOLDERS.values():
        same = sorted(e.name for e in root.iterdir() if e.is_dir() and e.name.lower() == name.lower())
        if len(same) > 1:
            clashes.append(same)
    return clashes


def scan_library_drive(found, items, folders=False, orphans=None):
    """Library drive mode: every file, folders kept. Movies/X/... -> movies/X/...; a show folder
    is matched to an existing library show like in the other modes. Each folder in Movies/ is one
    movie; a loose file there belongs to the loose video its name starts with (Heat.en.srt -> Heat)."""
    jobs = []
    if "movies" in found:
        movies = found["movies"]
        stems = loose_video_stems(movies)
        for s in walk_files(movies):
            rel = s.relative_to(movies)
            name = movie_name(rel, stems)
            jobs.append((s, movie_dest(MOVIES_DIR, rel, name, folders), add_item(items, "movie", name)))
    if "shows" in found:
        for entry in sorted(found["shows"].iterdir(), key=lambda p: p.name.lower()):
            if is_junk(entry.name):
                continue
            if not entry.is_dir():
                log(f"Skipping '{found['shows'].name}/{entry.name}': not inside a show folder")
                if orphans is not None and entry.suffix.lower() in VIDEO_EXTS:  # a stray folder.jpg is fine
                    orphans.append(f"{found['shows'].name}/{entry.name}")
                continue
            if SEASON_DIR_RE.match(entry.name):  # would otherwise become a show called "Season 3"
                log(f"Skipping '{found['shows'].name}/{entry.name}': a season folder needs to be inside its show's folder")
                if orphans is not None:
                    orphans.append(f"{found['shows'].name}/{entry.name}")
                continue
            show_dir, iid = add_show(items, entry)
            for s in walk_files(entry):
                jobs.append((s, show_dir / s.relative_to(entry), iid))
    return jobs


def scan_library_export(root, found, items, folders=False):
    """Library drive mode's copy onto the drive: every library file that isn't on the drive yet, into
    its Movies/ and Shows/ folders (made if missing). A library show goes into the drive's folder for
    the same show when there is one ("Breaking Bad (2008)" -> the drive's "Breaking Bad")."""
    jobs = []
    drive_movies = found.get("movies") or root / LIBRARY_FOLDERS["movies"]
    stems = loose_video_stems(MOVIES_DIR)
    for s in walk_files(MOVIES_DIR):
        rel = s.relative_to(MOVIES_DIR)
        name = movie_name(rel, stems)
        d = movie_dest(drive_movies, rel, name, folders)
        # Already on the drive, in its folder or still loose there (a drive that isn't tidied yet).
        if s.name.startswith(".") or already_there(d, drive_movies) or (drive_movies / rel).exists():
            continue
        jobs.append((s, d, add_item(items, "movie", name, "out")))
    drive_shows = found.get("shows") or root / LIBRARY_FOLDERS["shows"]
    on_drive = {norm_title(e.name): e for e in drive_shows.iterdir() if e.is_dir()} if drive_shows.is_dir() else {}
    for show in sorted(SHOWS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not show.is_dir() or is_junk(show.name) or show.name.startswith("."):
            continue
        target = on_drive.get(norm_title(show.name), drive_shows / show.name)
        for s in walk_files(show):
            d = target / s.relative_to(show)
            if s.name.startswith(".") or d.exists():
                continue
            jobs.append((s, d, add_item(items, "show", show.name, "out", target=target.name,
                                        new=not target.exists())))
    return jobs


def clear_incoming(found, dupes):
    """After Sort incoming: delete the files that were already in the library (dupes), junk files, and
    loose non-video files in shows/, and every folder left empty under incoming's movies/ and shows/
    (those two stay). Runs even when nothing was copied. Anything else still there is media that isn't
    in the library (failed, still being written, not inside a show folder) and is kept."""
    STATUS.update(step="cleaning up", file=None, force=True)
    for f in dupes:
        try:
            f.unlink()
            log(f"Removed from incoming (already in the library): {f}")
        except OSError as e:
            log(f"  Could not remove {f}: {e}")
    shows = found.get("shows")
    if shows:  # loose non-video files in shows/ (a stray folder.jpg) belong to no show: they go too
        for e in shows.iterdir():
            if e.is_file() and e.suffix.lower() not in VIDEO_EXTS:
                try:
                    e.unlink()
                    log(f"Removed from incoming (not part of any show): {e.name}")
                except OSError:
                    pass
    for top in found.values():
        for dirpath, dirnames, filenames in os.walk(top, topdown=False):
            d = Path(dirpath)
            for fn in filenames:
                if is_junk(fn):
                    try:
                        (d / fn).unlink()
                    except OSError:
                        pass
            if d != top:
                try:
                    d.rmdir()  # only succeeds when it's empty
                    log(f"Removed empty folder from incoming: {d.relative_to(top.parent)}")
                except OSError:
                    pass


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
        rejected = {r for r in review.get("rejected") or []
                    if isinstance(r, str) and r in items and items[r]["dir"] == "in"}
        for iid in rejected:
            log(f"Rejected on the dashboard: {items[iid]['name']}")
        return rejected


def clean_up_emptied(src_dir, jobs, stops=None):
    STATUS.update(step="cleaning up", file=None, force=True)
    # Remove USB folders that this run emptied (deepest first). Folders that were
    # already empty (e.g. a blank "Season 2") are never in this set, so they stay,
    # and so do src_dir and the stops (incoming's movies/ and shows/).
    stops = {src_dir, *(stops or ())}
    emptied = set()
    for s, *_ in jobs:
        p = s.parent
        while p not in stops and p != p.parent:
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


def import_media(root: Path, settings=None, incoming=False):
    """incoming: Sort incoming (root = INCOMING_DIR): laid out like a library drive, but everything is
    moved (copied, verified, removed), files already in the library are always skipped, and there's no
    review and no copy back."""
    settings = settings or load_settings()
    library = settings["library_drive"] or incoming
    folder = SOURCE_FOLDERS[settings["source"]]
    export = library and settings["export_library"] and not incoming
    delete = incoming or (not library and settings["delete_after"])  # delete each file once it's verified
    skip_existing = incoming or settings["skip_existing"]
    place = "in incoming" if incoming else "on the drive"  # where a file that isn't moved stays
    mode = "incoming" if incoming else "library" if library else settings["source"]
    STATUS.update(step="scanning", mode=mode, deletes=delete, force=True)
    clashes = library_folder_clashes(root) if library else []
    if clashes:
        names = "; ".join(" and ".join(f"'{n}'" for n in c) for c in clashes)
        log(f"Stopping: {names} are the same folder in different capitals; only one would be read.")
        STATUS.finish("problems", f"Found {names}: move everything into one of them and delete the other. "
                                  "Nothing was copied.")
        return 1
    if incoming:
        found = library_folders(root)
        if not found:
            log(f"No movies or shows folder in {root}, nothing to do.")
            STATUS.finish("no_folder", f"No movies or shows folder in {root}")
            return 0
        base = root
        where = " and ".join(f"'{p.name}'" for p in found.values())
        log(f"Sort incoming: moving what's in {where} into the library.")
    elif library:
        found = library_folders(root)
        if not found and not export:
            log(f"Library drive mode: no Movies or Shows folder on {root}, nothing to do.")
            STATUS.finish("no_folder", "Library drive mode is on, but this drive has no Movies or Shows folder")
            return 0
        base = root  # for the paths in the log
        where = " and ".join(f"'{p.name}'" for p in found.values()) or "Movies and Shows"
        log(f"Library drive mode: copying {where}; nothing on the drive is deleted."
            + (" The library is copied onto the drive too." if export else ""))
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
    orphans = []  # season folders sitting directly in Shows/ (library drive / incoming)
    if library:
        folders = settings["movie_folders"]
        jobs, skipped = scan_library_drive(found, items, folders, orphans), 0  # (src, dst, item id); skipped = misplaced episodes
        if export:  # copied after the imports; never reviewed, everything new goes onto the drive
            jobs += scan_library_export(root, found, items, folders)
    else:
        jobs, skipped = scan_media_folder(src_dir, items, settings["movie_folders"])

    existing = 0  # already in the library, left on the USB (skip-existing mode)
    dupes = []  # Sort incoming: those files, deleted from incoming at the end
    recent = 0  # Sort incoming: changed too recently, maybe still being written
    if jobs and skip_existing:
        log("Skip-existing mode is on: files already in the library aren't copied.")
    keep = []
    for s, d, iid in jobs:
        item = items[iid]
        if incoming:
            try:
                fresh = time.time() - s.stat().st_mtime < INCOMING_SETTLE
            except OSError:
                fresh = True
            if fresh:
                log(f"Leaving '{s.relative_to(base)}' for next time: it changed in the last {INCOMING_SETTLE // 60} minutes")
                recent += 1
                item["recent"] = item.get("recent", 0) + 1
                continue
        item["files"] += 1
        if item["dir"] == "in" and skip_existing and already_there(d, MOVIES_DIR):
            log(f"Skipping '{s.relative_to(base)}': already in the library")
            existing += 1
            if incoming:
                dupes.append(s)
            item["existing"] += 1
            continue
        try:
            item["bytes"] += s.stat().st_size
        except OSError:
            pass
        item["copy"] += 1
        keep.append((s, d, iid))
    jobs = keep
    items = dict(sorted(items.items(), key=lambda kv: (kv[1]["dir"] == "out", kv[1]["kind"] != "movie",
                                                      kv[1]["name"].lower())))
    for item in items.values():
        if not item["copy"]:
            item["state"] = "in_library" if item["existing"] or not item.get("recent") else "recent"
    drive_free = None
    if export:
        try:
            drive_free = shutil.disk_usage(root).free
        except OSError:
            pass
    STATUS.update(items=list(items.values()), existing=existing, drive_free=drive_free, force=True)

    rejected = set()
    # Only what comes onto X4 is reviewed; the copy onto the drive always goes ahead.
    if library and not incoming and any(items[iid]["dir"] == "in" for _, _, iid in jobs):
        rejected = wait_for_review(items)
        if rejected is None:
            for item in items.values():
                if item["state"] == "waiting":
                    item["state"] = "rejected"
            STATUS.finish("cancelled", "Cancelled on the dashboard; nothing was copied either way")
            return 0
        for iid in rejected:
            items[iid]["state"] = "rejected"
        jobs = [j for j in jobs if j[2] not in rejected]
        STATUS.update(state="plugged_in", step="scanning", rejected=len(rejected), force=True)
        if not jobs:
            log("Everything was rejected; nothing to copy.")
            STATUS.finish("cancelled", "Everything was rejected on the dashboard; nothing was copied")
            return 0
    left_note = "; ".join(n for n in (
        existing and f"{existing} file{'s' if existing != 1 else ''} already in the library"
                     + (", removed from incoming" if incoming else f", left {place}" if delete else ", not copied"),
        recent and f"{recent} file{'s' if recent != 1 else ''} changed in the last {INCOMING_SETTLE // 60} minutes"
                   " and stayed in incoming for next time") if n) or None

    orphan_note = (f"{len(orphans)} item{'s' if len(orphans) != 1 else ''} in Shows not inside a show's folder "
                   f"stayed where {'they were' if len(orphans) != 1 else 'it was'} ({', '.join(orphans[:5])}): "
                   "put episodes inside a folder named after their show" if orphans else None)

    if not jobs:
        log("Nothing to import.")
        if incoming:
            clear_incoming(found, dupes)
        if orphans:
            STATUS.finish("problems", "; ".join(n for n in (orphan_note, left_note) if n))
            return 1
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
        if existing or recent:
            STATUS.finish("ok", left_note)
            return 0
        if incoming:
            STATUS.finish("nothing", f"Nothing in incoming's {where} folder{'s' if len(found) > 1 else ''}")
            return 0
        STATUS.finish("nothing", ("Nothing new either way: the drive and the library have the same files"
                                  if export else f"Nothing in this drive's {where} folder{'s' if len(found) > 1 else ''}")
                      if library else f"The '{folder}' folder is empty")
        return 0

    total_bytes = sum(item["bytes"] for item in items.values() if item["id"] not in rejected)
    STATUS.update(state="running", step="copying", total=len(jobs), skipped=skipped, existing=existing,
                  bytes_total=total_bytes, force=True)

    ok, exported, failed, no_space = 0, 0, [], []
    for i, (s, d, iid) in enumerate(jobs, 1):
        item = items[iid]
        out = item["dir"] == "out"
        rel = Path("To the drive") / s.relative_to(LIBRARY_ROOT) if out else s.relative_to(base)
        if item["state"] == "no_space":
            continue
        if out and item["state"] == "waiting":
            # Each title only goes onto the drive if all of it fits; never a half-copied movie or show.
            try:
                free = shutil.disk_usage(root).free
            except OSError:
                free = None
            if free is not None and item["bytes"] + DRIVE_SPARE > free:
                log(f"Not enough space on the drive for '{item['name']}' ({item['bytes']} bytes, {free} free)")
                item["state"] = "no_space"
                no_space.append(item["name"])
                STATUS.update(no_space=len(no_space), force=True)
                continue
        item["state"] = "copying"
        STATUS.update(index=i, file=str(rel), step="copying", force=True)
        try:
            action = "overwrite" if d.exists() else "copy"
            log(f"[{i}/{len(jobs)}] {action}: {rel} -> {d}")
            copy_verify_delete(s, d, delete=delete)
            item["done"] += 1
            flat = MOVIES_DIR / d.name
            if not out and d.parent.parent == MOVIES_DIR and flat.is_file():
                flat.unlink()  # the same file still loose in movies/ was overwritten by this one
                log(f"  replaced the loose copy {flat}")
            if out:
                exported += 1
                STATUS.update(exported=exported)
            else:
                ok += 1
                STATUS.update(imported=ok)
        except Exception as e:
            log(f"  FAILED: {rel}: {e}" + (f" (left {place})" if delete else ""))
            failed.append(rel)
            item["failed"] += 1
            STATUS.update(failed=len(failed), failed_files=[str(f) for f in failed[:10]], force=True)
        if item["done"] + item["failed"] == item["copy"]:
            item["state"] = "failed" if item["failed"] else "done"
            STATUS.update(force=True)

    if incoming:
        clear_incoming(found, dupes)
    elif delete:  # only folders this run emptied; nothing to tidy up when nothing was deleted
        clean_up_emptied(base, jobs, found.values() if library else None)

    os.sync()
    log(f"Done: {ok} imported, {len(failed)} failed, {skipped} skipped (misplaced episodes)"
        + (f", {existing} already in the library" if existing else "")
        + (f", {exported} copied onto the drive, {len(no_space)} didn't fit" if export else "") + ".")
    if failed or skipped or no_space or orphans:
        why = [orphan_note] if orphans else []
        if failed:
            why.append(f"{len(failed)} file{'s' if len(failed) != 1 else ''} failed"
                       + (f" and stayed {place}" if delete else ""))
        if no_space:
            why.append(f"{len(no_space)} title{'s' if len(no_space) != 1 else ''} didn't fit on the drive: "
                       + ", ".join(no_space[:5]) + (", ..." if len(no_space) > 5 else ""))
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
    # Library drive mode only reads the drive (stays read-only) unless the library is copied onto it.
    if settings["library_drive"]:
        needs_write = settings["export_library"]
    else:
        # Only deleting from the drive writes to it.
        needs_write = settings["delete_after"] and (Path(mnt) / SOURCE_FOLDERS[settings["source"]]).is_dir()
    try:
        if we_mounted and needs_write:
            # Unmount and mount again read-write. (An in-place "remount,rw" isn't supported
            # by every filesystem driver, e.g. FUSE exFAT.)
            run(["umount", mnt])
            r = run(["mount", "-o", "rw,noatime", dev, mnt])
            if r.returncode != 0:
                log(f"Could not remount {dev} read-write: {r.stderr.strip()}")
                if not settings["library_drive"]:
                    STATUS.finish("mount_failed", "Couldn't mount the drive read-write")
                    return 1
                # Library drive: still import, read-only; only the copy onto the drive is lost.
                if run(["mount", "-o", "ro,noatime", dev, mnt]).returncode != 0:
                    STATUS.finish("mount_failed", "Couldn't mount the drive")
                    return 1
                settings = {**settings, "export_library": False}
                STATUS.update(note="Couldn't write to this drive (Windows may have left it hibernated: shut "
                                   "Windows down fully, or run chkdsk), so the library wasn't copied onto it")
        return import_media(Path(mnt), settings)
    finally:
        if we_mounted:  # put things back the way we found them
            run(["umount", mnt])
            try:
                os.rmdir(mnt)
            except OSError:
                pass


# ---- Tidy: loose movies in the library -> a folder each ----------------------------------------

def tidy_write(data):
    data["updated"] = int(time.time())
    try:
        TIDY_STATUS.parent.mkdir(parents=True, exist_ok=True)
        tmp = TIDY_STATUS.with_name(TIDY_STATUS.name + ".tmp")
        tmp.write_text(json.dumps(data) + "\n")
        tmp.chmod(0o644)
        os.replace(tmp, TIDY_STATUS)
    except OSError as e:
        print(f"(couldn't write {TIDY_STATUS}: {e})", flush=True)


def tidy_plan():
    """Loose files in movies/, grouped by the movie folder they'd move into. A file only belongs to a
    movie if its name starts with that movie's video name; anything else (a stray poster.jpg) stays.
    Returns ([{"name", "files", "conflicts"}], [names left where they are])."""
    stems = loose_video_stems(MOVIES_DIR)
    plan, left = {}, []
    for e in sorted(MOVIES_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not e.is_file() or e.name.startswith(".") or is_junk(e.name):
            continue
        name = next((st for st in stems if e.name.startswith(st)), None)
        if name is None:
            left.append(e.name)
            continue
        movie = plan.setdefault(name, {"name": name, "files": [], "conflicts": []})
        # Never overwrite: a file already in the movie's folder under the same name stays loose.
        movie["conflicts" if (MOVIES_DIR / name / e.name).exists() else "files"].append(e.name)
    return [m for m in plan.values() if m["files"] or m["conflicts"]], left


def tidy(action, request_id=None):
    """action "plan": write what would move to TIDY_STATUS. "run": move it. Library moves are renames on
    the same drive, so they're instant and nothing is copied."""
    data = {"v": 1, "id": request_id, "action": action, "state": "planning" if action == "plan" else "running",
            "started": int(time.time()), "finished": None, "movies": [], "left": [], "moved": 0,
            "movies_moved": 0, "conflicts": 0, "message": None}
    tidy_write(data)
    if not MOVIES_DIR.is_dir() or fs_of(LIBRARY_ROOT, "TARGET") == "/":
        data.update(state="failed", finished=int(time.time()),
                    message="The library drive isn't mounted, so nothing was moved")
        tidy_write(data)
        return 2
    movies, left = tidy_plan()
    data.update(movies=movies, left=left, conflicts=sum(len(m["conflicts"]) for m in movies))
    if action == "plan":
        n = sum(1 for m in movies if m["files"])
        log(f"Tidy plan: {n} movie{'s' if n != 1 else ''} to move into folders, {len(left)} loose file(s) stay")
        data.update(state="ready", finished=int(time.time()))
        tidy_write(data)
        return 0
    failed = []
    for m in movies:
        folder = MOVIES_DIR / m["name"]
        moved_any = False
        for name in m["files"]:
            src, dst = MOVIES_DIR / name, folder / name
            try:
                folder.mkdir(exist_ok=True)
                if dst.exists():  # appeared since the plan: leave it
                    m["conflicts"].append(name)
                    continue
                os.rename(src, dst)
                log(f"Tidy: {name} -> {folder.name}/")
                data["moved"] += 1
                moved_any = True
            except OSError as e:
                log(f"Tidy: couldn't move {name}: {e}")
                failed.append(name)
        data["movies_moved"] += moved_any
        tidy_write(data)
    data["conflicts"] = sum(len(m["conflicts"]) for m in movies)
    os.sync()
    msg = [f"Moved {data['movies_moved']} movie{'s' if data['movies_moved'] != 1 else ''} into their own folders"]
    if data["conflicts"]:
        msg.append(f"{data['conflicts']} file(s) stayed loose because the folder already has a file with that name")
    if failed:
        msg.append(f"{len(failed)} file(s) couldn't be moved: " + ", ".join(failed[:5]))
    data.update(state="failed" if failed else "done", finished=int(time.time()), message="; ".join(msg))
    tidy_write(data)
    log("Tidy: " + data["message"])
    return 1 if failed else 0


def tidy_request():
    """The dashboard's request (TIDY_REQUEST), removed as soon as it's read so systemd's path unit
    doesn't start this again. Returns (action, id)."""
    try:
        req = json.loads(TIDY_REQUEST.read_text())
    except (OSError, ValueError):
        req = {}
    TIDY_REQUEST.unlink(missing_ok=True)
    req = req if isinstance(req, dict) else {}
    action = req.get("action") if req.get("action") in ("run", "incoming") else "plan"
    return action, req.get("id") if isinstance(req.get("id"), str) else None


def sort_incoming():
    STATUS.start(None, "Incoming folder")
    if not INCOMING_DIR.is_dir():
        log(f"{INCOMING_DIR} doesn't exist, nothing to do.")
        STATUS.finish("no_folder", f"{INCOMING_DIR} doesn't exist")
        return 0
    return import_media(INCOMING_DIR, load_settings(), incoming=True)


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
    elif sys.argv[1:2] == ["--tidy"] and len(sys.argv) <= 3:
        if sys.argv[2:] == ["--run"]:
            action, request_id = "run", None
        elif len(sys.argv) == 2:
            action, request_id = tidy_request()
        else:
            print(__doc__)
            return 64
        # The same path unit also carries the dashboard's Sort incoming button.
        target = sort_incoming if action == "incoming" else lambda: tidy(action, request_id)
    elif sys.argv[1:] == ["--incoming"]:
        target = sort_incoming
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
