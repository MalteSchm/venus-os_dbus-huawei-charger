#!/bin/sh
# Installs dbus-huawei-charger to /data/dbus-huawei-charger and sets up the service.
# Run on the GX (as root) from the directory containing the files: sh install.sh
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=/data/dbus-huawei-charger

mkdir -p "$DEST/service/log"
for f in dbus-huawei-charger.py r4850can.py charger_logic.py setv.py; do
    cp "$SRC/$f" "$DEST/$f"
done
cp "$SRC/service/run" "$DEST/service/run"
cp "$SRC/service/log/run" "$DEST/service/log/run"
# Keep an existing configuration
[ -f "$DEST/config.ini" ] || cp "$SRC/config.ini.example" "$DEST/config.ini"
chmod +x "$DEST/dbus-huawei-charger.py" "$DEST/setv.py" "$DEST/service/run" "$DEST/service/log/run"

# Re-link the service after every reboot (/service is not stored on /data)
RC=/data/rc.local
[ -f "$RC" ] || { echo '#!/bin/sh' > "$RC"; chmod +x "$RC"; }
grep -q 'dbus-huawei-charger' "$RC" || \
    echo 'ln -sfn /data/dbus-huawei-charger/service /service/dbus-huawei-charger' >> "$RC"

ln -sfn "$DEST/service" /service/dbus-huawei-charger
echo "Installed. Check with: svstat /service/dbus-huawei-charger"
echo "Configuration: $DEST/config.ini  (afterwards: svc -t /service/dbus-huawei-charger)"
