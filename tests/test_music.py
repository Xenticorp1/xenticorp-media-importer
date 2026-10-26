"""Music import (a drive's Music folder and the dashboard's uploads), on temporary folders.
python3 -m unittest discover tests"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import media_import as mi  # noqa: E402


class MusicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        lib = self.tmp / "library"
        for d in ("movies", "shows", "music"):
            (lib / d).mkdir(parents=True)
        self.patch(LIBRARY_ROOT=lib, MOVIES_DIR=lib / "movies", SHOWS_DIR=lib / "shows", MUSIC_DIR=lib / "music",
                   STATUS_FILE=self.tmp / "status.json", SETTINGS_FILE=self.tmp / "settings.json",
                   UPLOAD_DIR=self.tmp / "upload")
        self.patch(fs_of=lambda path, field: "/mnt/media")  # "the library drive is mounted"
        self.drive = self.tmp / "drive"
        self.music = lib / "music"
        mi.STATUS.data = None

    def patch(self, **values):
        for k, v in values.items():
            old = getattr(mi, k)
            setattr(mi, k, v)
            self.addCleanup(setattr, mi, k, old)

    def settings(self, **s):
        (self.tmp / "settings.json").write_text(json.dumps(s))

    def put(self, rel, data=b"x" * 100, top=None):
        p = (top or self.drive) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def run_drive(self):
        mi.STATUS.start(None, "TEST")
        code = mi.import_media(self.drive)
        return code, json.loads((self.tmp / "status.json").read_text())

    def test_music_folder_alone_is_imported(self):
        self.put("Music/Album A/01 - One.mp3")
        self.put("Music/Album A/cover.jpg")
        self.put("Music/Album A/notes.txt")
        self.put("Music/loose.flac")
        code, st = self.run_drive()
        self.assertEqual((code, st["result"]), (0, "ok"), st.get("message"))
        self.assertTrue((self.music / "Album A" / "01 - One.mp3").is_file())
        self.assertTrue((self.music / "Album A" / "cover.jpg").is_file())
        self.assertFalse((self.music / "Album A" / "notes.txt").exists(), "only audio, art and lyrics")
        self.assertTrue((self.music / "loose.flac").is_file())
        names = {(i["kind"], i["name"]) for i in st["items"]}
        self.assertEqual(names, {("music", "Album A"), ("music", "Loose tracks")})
        # delete-after is on by default: imported files go, what wasn't taken (notes.txt) stays on the drive
        self.assertEqual([p.name for p in (self.drive / "Music" / "Album A").iterdir()], ["notes.txt"])
        self.assertTrue((self.drive / "Music").is_dir())

    def test_emptied_album_folder_is_removed_music_folder_stays(self):
        self.put("Music/Album E/01 - Six.mp3")
        self.run_drive()
        self.assertFalse((self.drive / "Music" / "Album E").exists())
        self.assertTrue((self.drive / "Music").is_dir())

    def test_music_next_to_movies_any_capitals(self):
        self.put("Compressed Media/Heat (1995).mkv")
        self.put("music/Album B/01 - Two.mp3")
        code, st = self.run_drive()
        self.assertEqual(st["result"], "ok", st.get("message"))
        self.assertTrue((self.music / "Album B" / "01 - Two.mp3").is_file())
        self.assertTrue(any(p.name == "Heat (1995).mkv" for p in (self.tmp / "library" / "movies").rglob("*.mkv")))

    def test_skip_existing_skips_tracks_already_there(self):
        self.settings(skip_existing=True, delete_after=False)
        self.put("Album C/01 - Three.mp3", b"old", top=self.music)
        self.put("Music/Album C/01 - Three.mp3", b"new")
        self.put("Music/Album C/02 - Four.mp3", b"new")
        code, st = self.run_drive()
        self.assertEqual((self.music / "Album C" / "01 - Three.mp3").read_bytes(), b"old")
        self.assertTrue((self.music / "Album C" / "02 - Four.mp3").is_file())
        self.assertEqual(st["existing"], 1)
        self.assertTrue((self.drive / "Music" / "Album C" / "02 - Four.mp3").exists(), "delete-after off keeps the drive")

    def test_uploads_are_moved_in(self):
        up = self.tmp / "upload"
        self.put("Music/Album D/01 - Five.mp3", top=up)
        mi.STATUS.start(None, "Uploaded music")
        code = mi.import_media(up, mi.load_settings(), uploads=True)
        st = json.loads((self.tmp / "status.json").read_text())
        self.assertEqual((code, st["result"], st["mode"]), (0, "ok", "upload"), st.get("message"))
        self.assertTrue((self.music / "Album D" / "01 - Five.mp3").is_file())
        self.assertEqual(list((up / "Music").iterdir()), [], "the upload folder is emptied, Music/ stays")

    def test_no_uploads_waiting(self):
        mi.STATUS.start(None, "Uploaded music")
        mi.import_media(self.tmp / "upload", mi.load_settings(), uploads=True)
        st = json.loads((self.tmp / "status.json").read_text())
        self.assertEqual(st["result"], "nothing")

    def test_drive_without_any_folder_still_says_so(self):
        self.put("random.txt")
        code, st = self.run_drive()
        self.assertEqual(st["result"], "no_folder")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
