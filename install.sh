#!/usr/bin/env bash
# Xenticorp Media Import installer. Run from the repo clone on X4, with the media
# drive mounted:  sudo ./install.sh
#
# /opt/media-import is a symlink to this clone, so `git pull` updates the script
# immediately. Re-run this installer only if a .service, .path or .rules file changed.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
LIB=/mnt/media/jellyfin/media

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
[[ -d $LIB/movies && -d $LIB/shows ]] || { echo "Media drive not mounted ($LIB/movies or /shows missing)."; exit 1; }

chmod +x "$REPO/media_import.py"
if [[ -e /opt/media-import && ! -L /opt/media-import ]]; then
  mv /opt/media-import "/opt/media-import.old.$(date +%s)"   # old copy-based install
fi
ln -sfn "$REPO" /opt/media-import
install -Dm644 "$REPO/media-import@.service" /etc/systemd/system/media-import@.service
install -Dm644 "$REPO/99-media-import.rules" /etc/udev/rules.d/99-media-import.rules
# Dashboard "Tidy library" button: a path unit watching for its request file
install -Dm644 "$REPO/media-import-tidy.service" /etc/systemd/system/media-import-tidy.service
install -Dm644 "$REPO/media-import-tidy.path" /etc/systemd/system/media-import-tidy.path
mkdir -p /var/lib/media-import

# Never let the importer touch the library drive itself
mkdir -p /etc/media-import
touch /etc/media-import/ignore-uuids
UUID=$(findmnt -n -o UUID --target "$LIB" || true)
if [[ -n $UUID ]] && ! grep -qix "$UUID" /etc/media-import/ignore-uuids; then
  echo "$UUID" >> /etc/media-import/ignore-uuids
  echo "Added library drive UUID $UUID to ignore list."
fi

systemctl daemon-reload
systemctl enable --now media-import-tidy.path
udevadm control --reload-rules
echo "Installed (/opt/media-import -> $REPO)."
echo "Logs:  journalctl -fu 'media-import@*'"
