import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["install.sh", "media_import.py", "media-import@.service", "99-media-import.rules",
         "media-import-tidy.service", "media-import-tidy.path",
         "media-import-install.service", "media-import-install.path"]


def sh(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.repo = os.path.join(t, "clone")
        os.mkdir(self.repo)
        for f in FILES:
            shutil.copy(os.path.join(ROOT, f), self.repo)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        sh(["git", "add", "-A"], self.repo)
        sh(git + ["commit", "-qm", "init"], self.repo)
        self.git = git
        self.d = {k: os.path.join(t, k) for k in
                  ("systemd", "udev", "etc", "state", "lib", "bin")}
        for k in ("state", "bin"):
            os.mkdir(self.d[k])
        os.makedirs(os.path.join(self.d["lib"], "movies"))
        os.makedirs(os.path.join(self.d["lib"], "shows"))
        self.opt = os.path.join(t, "opt-link")
        self.calls = os.path.join(t, "calls.log")
        for name in ("systemctl", "udevadm"):
            self._fake(name, 'echo "%s $*" >> "%s"' % (name, self.calls))
        self._fake("findmnt", "echo 1234-ABCD")

    def _fake(self, name, body):
        p = os.path.join(self.d["bin"], name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n%s\n" % body)
        os.chmod(p, 0o755)

    def run_install(self, *args, script=None):
        env = dict(os.environ,
                   PATH=self.d["bin"] + os.pathsep + os.environ["PATH"],
                   XENTICORP_INSTALL_TEST="1", LIB=self.d["lib"],
                   SYSTEMD_DIR=self.d["systemd"], UDEV_DIR=self.d["udev"],
                   ETC_DIR=self.d["etc"], OPT_LINK=self.opt, STATE_DIR=self.d["state"])
        return sh(["bash", script or os.path.join(self.repo, "install.sh"), *args],
                  self.repo, env)

    def state(self, name):
        return os.path.join(self.d["state"], name)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def jread(self, name):
        return json.loads(self.read(self.state(name)))

    def calls_text(self):
        return self.read(self.calls) if os.path.exists(self.calls) else ""

    def flag(self, files=("media-import@.service",)):
        with open(self.state("install-needed"), "w") as fh:
            json.dump({"files": list(files), "at": 1, "from": "a", "to": "b"}, fh)

    def assertRefused(self, r, reason_part):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(reason_part, self.jread("install-failed")["reason"])
        self.assertTrue(os.path.exists(self.state("install-needed")))
        self.assertFalse(os.path.exists(self.state("install-done")))
        self.assertEqual(self.calls_text(), "")  # nothing installed or reloaded

    # manual run
    def test_manual_installs_and_arms(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("media-import@.service", "media-import-tidy.service", "media-import-tidy.path",
                  "media-import-install.service", "media-import-install.path"):
            self.assertTrue(os.path.exists(os.path.join(self.d["systemd"], f)), f)
        self.assertTrue(os.path.exists(os.path.join(self.d["udev"], "99-media-import.rules")))
        self.assertEqual(os.path.realpath(self.opt), os.path.realpath(self.repo))
        c = self.calls_text()
        self.assertIn("systemctl daemon-reload", c)
        self.assertIn("systemctl enable --now media-import-install.path", c)
        self.assertIn("udevadm control --reload-rules", c)
        sha = sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()
        armed = self.jread("install-armed")
        self.assertEqual(armed["commit"], sha)
        self.assertIn("at", armed)
        self.assertIn("1234-ABCD", self.read(os.path.join(self.d["etc"], "ignore-uuids")))

    def test_manual_missing_drive_keeps_old_behaviour(self):
        shutil.rmtree(os.path.join(self.d["lib"], "shows"))
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Media drive not mounted", r.stdout)
        self.assertFalse(os.path.exists(self.state("install-failed")))

    def test_root_check_without_test_flag(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        env = dict(os.environ, XENTICORP_INSTALL_TEST="")
        r = sh(["bash", os.path.join(self.repo, "install.sh")], self.repo, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Run with sudo.", r.stdout)

    def test_ignore_list_not_duplicated(self):
        self.run_install()
        self.run_install()
        lines = self.read(os.path.join(self.d["etc"], "ignore-uuids")).split()
        self.assertEqual(lines.count("1234-ABCD"), 1)

    # --auto refusals
    def test_auto_refuses_wrong_branch(self):
        sh(["git", "checkout", "-q", "-b", "testing"], self.repo)
        self.flag()
        self.assertRefused(self.run_install("--auto"), "main")

    def test_auto_refuses_dirty_tree(self):
        self.flag()
        with open(os.path.join(self.repo, "install.sh"), "a") as fh:
            fh.write("\n# local edit\n")
        self.assertRefused(self.run_install("--auto"), "uncommitted")

    def test_auto_ignores_untracked_files(self):
        self.flag()
        open(os.path.join(self.repo, "stray.txt"), "w").close()
        r = self.run_install("--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_auto_refuses_non_git_folder(self):
        plain = os.path.join(self.tmp, "plain")
        os.mkdir(plain)
        for f in FILES:
            shutil.copy(os.path.join(ROOT, f), plain)
        self.flag()
        r = self.run_install("--auto", script=os.path.join(plain, "install.sh"))
        self.assertRefused(r, "git")

    def test_auto_refuses_missing_drive(self):
        shutil.rmtree(os.path.join(self.d["lib"], "movies"))
        self.flag()
        self.assertRefused(self.run_install("--auto"), "drive")

    # --auto success
    def test_auto_success_and_twice(self):
        self.flag(["media-import@.service", "99-media-import.rules"])
        with open(self.state("install-failed"), "w") as fh:
            fh.write('{"reason": "old", "at": 1}')
        sha = sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()
        r = self.run_install("--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("automatic install done", r.stdout)
        self.assertFalse(os.path.exists(self.state("install-needed")))
        self.assertFalse(os.path.exists(self.state("install-failed")))
        done = self.jread("install-done")
        self.assertEqual(done["commit"], sha)
        self.assertEqual(done["files"], ["media-import@.service", "99-media-import.rules"])
        self.assertTrue(os.path.exists(os.path.join(self.d["systemd"], "media-import@.service")))
        c = self.calls_text()
        self.assertIn("systemctl daemon-reload", c)
        self.assertIn("udevadm control --reload-rules", c)
        self.assertNotRegex(c, r"\b(stop|restart|try-restart|kill)\b")
        self.assertNotIn("disable", c)
        # second run, no flag: still fine
        r = self.run_install("--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.jread("install-done")["files"], [])
        self.assertNotRegex(self.calls_text(), r"\b(stop|restart)\b")

    def test_auto_after_refusal_then_fixed(self):
        sh(["git", "checkout", "-q", "-b", "testing"], self.repo)
        self.flag()
        self.run_install("--auto")
        sh(["git", "checkout", "-q", "main"], self.repo)
        r = self.run_install("--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.state("install-failed")))

    # unit files
    def test_unit_lines(self):
        path = self.read(os.path.join(ROOT, "media-import-install.path")).splitlines()
        svc = self.read(os.path.join(ROOT, "media-import-install.service")).splitlines()
        self.assertIn("PathExists=/var/lib/media-import/install-needed", path)
        self.assertIn("Unit=media-import-install.service", path)
        self.assertIn("ExecStart=/opt/media-import/install.sh --auto", svc)
        self.assertIn("Type=oneshot", svc)


if __name__ == "__main__":
    unittest.main()
