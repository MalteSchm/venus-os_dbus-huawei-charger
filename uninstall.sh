#!/bin/sh
# Removes the service. The PSU is switched off in the process (the driver calls safe_off on exit).
svc -d /service/dbus-huawei-charger 2>/dev/null || true
rm -f /service/dbus-huawei-charger
[ -f /data/rc.local ] && sed -i '/dbus-huawei-charger/d' /data/rc.local
echo "Service removed. The files are still in /data/dbus-huawei-charger (delete with: rm -r)."
