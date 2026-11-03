#!/usr/bin/env bash
# Xenticorp Media Import installer. Run from the repo clone on X4, with the media
# drive mounted:  sudo ./install.sh
#
# /opt/media-import is a symlink to this clone, so `git pull` updates the script
# immediately. Re-run this installer only if a .service, .path or .rules file changed.
#
# A manual run also arms automatic installs: it installs media-import-install.path, which
# runs `install.sh --auto` whenever the dashboard drops the install-needed flag file after
# an update that changed an install file. --auto refuses unless the clone is a git
# repository on branch main with no uncommitted changes to tracked files.
#
# The paths below can be overridden (for tests); the defaults are the real ones.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd -P)"
LIB=${LIB:-/mnt/media/jellyfin/media}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
UDEV_DIR=${UDEV_DIR:-/etc/udev/rules.d}
ETC_DIR=${ETC_DIR:-/etc/media-import}
OPT_LINK=${OPT_LINK:-/opt/media-import}
STATE_DIR=${STATE_DIR:-/var/lib/media-import}

AUTO=0
[[ ${1:-} == --auto ]] && AUTO=1

git_in_repo() { git -c safe.directory="$REPO" -C "$REPO" "$@"; }
now() { date +%s; }
# write_json FILE python-expression-for-the-dict (atomic: temp file then rename)
write_json() {
  mkdir -p "$STATE_DIR"
  python3 -c 'import json,sys; sys.stdout.write(json.dumps(eval(sys.argv[1]))+"\n")' "$2" > "$1.tmp.$$"
  mv -f "$1.tmp.$$" "$1"
}
# fail MESSAGE REASON: plain mode keeps the old message and exit code 1; --auto also
# records install-failed (the install-needed flag stays in place) and exits non-zero.
fail() {
  echo "$1"
  if [[ $AUTO == 1 ]]; then
    write_json "$STATE_DIR/install-failed" "{'reason': '$2', 'at': $(now)}" || true
    echo "media-import-install: refused ($2); install-needed left in place."
  fi
  exit 1
}

if [[ ${XENTICORP_INSTALL_TEST:-} != 1 ]]; then
  [[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
fi

if [[ $AUTO == 1 ]]; then
  git_in_repo rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "Not a git repository: $REPO" "the clone is not a git repository"
  [[ "$(git_in_repo symbolic-ref --short -q HEAD || true)" == main ]] \
    || fail "Clone is not on branch main." "the clone is not on branch main"
  [[ -z "$(git_in_repo status --porcelain --untracked-files=no)" ]] \
    || fail "Clone has uncommitted changes to tracked files." "the clone has uncommitted changes"
fi

if [[ ! ( -d $LIB/movies && -d $LIB/shows ) ]]; then
  fail "Media drive not mounted ($LIB/movies or /shows missing)." "the media drive is not mounted"
fi

# Files the dashboard listed in the flag (auto mode only); read before anything is removed.
FILES_JSON='[]'
if [[ $AUTO == 1 && -f $STATE_DIR/install-needed ]]; then
  FILES_JSON=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps([str(f) for f in d.get("files",[])]))' \
    "$STATE_DIR/install-needed" 2>/dev/null || echo '[]')
fi

chmod +x "$REPO/media_import.py"
if [[ -e $OPT_LINK && ! -L $OPT_LINK ]]; then
  mv "$OPT_LINK" "$OPT_LINK.old.$(date +%s)"   # old copy-based install
fi
ln -sfn "$REPO" "$OPT_LINK"
install -Dm644 "$REPO/media-import@.service" "$SYSTEMD_DIR/media-import@.service"
install -Dm644 "$REPO/99-media-import.rules" "$UDEV_DIR/99-media-import.rules"
# Dashboard "Tidy library" button: a path unit watching for its request file
install -Dm644 "$REPO/media-import-tidy.service" "$SYSTEMD_DIR/media-import-tidy.service"
install -Dm644 "$REPO/media-import-tidy.path" "$SYSTEMD_DIR/media-import-tidy.path"
# Automatic installs: a path unit watching for the install-needed flag
install -Dm644 "$REPO/media-import-install.service" "$SYSTEMD_DIR/media-import-install.service"
install -Dm644 "$REPO/media-import-install.path" "$SYSTEMD_DIR/media-import-install.path"
mkdir -p "$STATE_DIR"

# Never let the importer touch the library drive itself
mkdir -p "$ETC_DIR"
touch "$ETC_DIR/ignore-uuids"
UUID=$(findmnt -n -o UUID --target "$LIB" || true)
if [[ -n $UUID ]] && ! grep -qix "$UUID" "$ETC_DIR/ignore-uuids"; then
  echo "$UUID" >> "$ETC_DIR/ignore-uuids"
  echo "Added library drive UUID $UUID to ignore list."
fi

# daemon-reload only: never stop/restart anything here (an import may be running, and in
# --auto mode the install path unit is what started this script).
systemctl daemon-reload
systemctl enable --now media-import-tidy.path
systemctl enable --now media-import-install.path
udevadm control --reload-rules

SHA=$(git_in_repo rev-parse HEAD 2>/dev/null || echo unknown)
if [[ $AUTO == 1 ]]; then
  rm -f "$STATE_DIR/install-needed" "$STATE_DIR/install-failed"
  write_json "$STATE_DIR/install-done" "{'at': $(now), 'commit': '$SHA', 'files': $FILES_JSON}"
  echo "media-import-install: automatic install done at commit $SHA (/opt/media-import -> $REPO)."
else
  write_json "$STATE_DIR/install-armed" "{'commit': '$SHA', 'at': $(now)}"
  echo "Installed (/opt/media-import -> $REPO)."
  echo "Automatic installs armed (media-import-install.path)."
  echo "Logs:  journalctl -fu 'media-import@*'"
fi
