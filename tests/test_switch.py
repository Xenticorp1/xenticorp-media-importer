"""The dashboard's on/off switch for the importer. python3 -m unittest discover tests"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import media_import as mi  # noqa: E402


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.patch(STATUS_FILE=self.tmp / "status.json", SETTINGS_FILE=self.tmp / "settings.json",
                   LIBRARY_ROOT=self.tmp / "library")
        self.patch(ignored_uuids=lambda: set(), fs_of=lambda path, field: "/dev/other",
                   run=lambda cmd: type("R", (), {"stdout": "", "returncode": 0})())
        self.mounted = []
        self.patch(acquire_mount=lambda dev: self.mounted.append(dev) or (None, False))
        mi.STATUS.data = None

    def patch(self, **values):
        for k, v in values.items():
            old = getattr(mi, k)
            setattr(mi, k, v)
            self.addCleanup(setattr, mi, k, old)

    def test_on_unless_switched_off(self):
        self.assertTrue(mi.load_settings()["enabled"], "no settings file: on")
        (self.tmp / "settings.json").write_text(json.dumps({"enabled": "no"}))
        self.assertTrue(mi.load_settings()["enabled"], "only false switches it off")
        (self.tmp / "settings.json").write_text(json.dumps({"enabled": False}))
        self.assertFalse(mi.load_settings()["enabled"])

    def test_off_leaves_the_drive_alone(self):
        (self.tmp / "settings.json").write_text(json.dumps({"enabled": False}))
        self.assertEqual(mi.handle_device("/dev/sdz1"), 0)
        self.assertEqual(self.mounted, [], "not even mounted")
        st = json.loads((self.tmp / "status.json").read_text())
        self.assertEqual((st["state"], st["result"]), ("finished", "off"))
        self.assertIn("switched off", st["message"])

    def test_on_goes_ahead(self):
        (self.tmp / "settings.json").write_text(json.dumps({"enabled": True}))
        mi.handle_device("/dev/sdz1")
        self.assertEqual(len(self.mounted), 1, "it tries to mount the drive as usual")


if __name__ == "__main__":
    unittest.main()
