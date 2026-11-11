# venus-os_dbus-huawei-charger

Use a **Huawei R4850G2** rectifier module as a battery charger on **Victron Venus OS**.
The driver talks to the PSU over CAN (USB-CAN adapter) and publishes it on the dbus as a
`com.victronenergy.charger` service. Charge current and voltage are set through dbus paths
(for example from Node-RED), and the BMS always has the last word.

> **Status: v0.1, experimental.** Tested on a Raspberry Pi Zero 2 W (Venus OS v3.85) and on a
> Multiplus-II GX (NanoPi, Venus OS Large) with a JK BMS and a 48 V LiFePO4 battery. You are
> connecting a mains-powered charger to a battery: use it at your own risk, test with a
> current-limited setup first, and read the safety notes below.

## Features

- Presents the PSU as a charger: `/Dc/0/Voltage`, `/Dc/0/Current`, `/State`, `/Mode`.
  `systemcalc` includes it in `/Dc/Charger/Power` and `/Dc/System/Power`.
- Control via `/Mode`, `/Link/ChargeCurrent` and `/Link/ChargeVoltage`.
- **BMS veto:** the requested values are limited to the battery service's
  `/Info/MaxChargeCurrent`, `/Info/MaxChargeVoltage` and `/Io/AllowToCharge`. Current from other
  sources (e.g. the Multiplus) is subtracted, so both chargers together never exceed the BMS
  limit; the other charger has priority. No battery service, missing values or a limit of 0
  switch the PSU off.
- Starts **off** (`/Mode = 4`) after every start; nothing is persisted.
- Switches the PSU off when the driver exits. If the driver dies, the PSU falls back to its
  offline values after about 60 s without CAN frames.
- Extra measurements as dbus paths under `/Huawei/*` (input/output power, efficiency, AC voltage,
  current and frequency, temperatures; not shown by the Victron GUI, but readable via dbus,
  Node-RED and MQTT) and a periodic status line in the log. The standard paths `/Ac/In/L1/I` and
  `/Ac/In/L1/P` are filled too; the GUI's charger page shows the AC current.
- Optional: PSU offline voltage/current and fan full-speed flags from `config.ini`.
- Optional heartbeat watchdog.

## Requirements

- Venus OS with root access (SSH), e.g. Cerbo/Multiplus-II GX/NanoPi/Raspberry Pi.
- A USB-CAN adapter that appears as a SocketCAN interface (tested: candleLight/CANable with the
  `gs_usb` driver). It must **not** be the CAN port used by the BMS.
- Huawei R4850G2 on the CAN bus at **125 kbit/s** (extended IDs).
- A battery service on the dbus that provides `/Info/MaxChargeCurrent`, `/Info/MaxChargeVoltage`,
  `/Io/AllowToCharge` and `/Dc/0/Current` (e.g. dbus-serialbattery, a CAN BMS).

## Safety notes

- **Offline voltage.** When the PSU receives no CAN frames it uses its *offline* voltage. The
  default of 48 V is below a 48 V LiFePO4 battery's voltage, so the PSU stays passive. This is a
  safety feature: **do not raise it** above your battery voltage. The driver never writes it
  unless you configure it explicitly.
- The driver never writes the PSU's input current limit.
- The driver limits are hard caps (`vmin`, `vmax`, `imax`); the BMS limits apply on top.
- A fuse and a battery disconnect belong in the DC path as for any charger.
- This driver does not replace a BMS. Test BMS alarms with your own hardware.

## Installation

1. Find the CAN interface of the adapter. Venus numbers interfaces by driver registration
   order, so on some devices the adapter and the BMS port may swap between boots:

   ```sh
   for i in /sys/class/net/can*; do echo $i $(basename $(readlink $i/device/driver)); done
   ```

   Example from a NanoPi: `can0` = `mcp251xfd` (onboard, BMS), `can1` = `gs_usb` (adapter).

2. If the numbering is not stable, make the onboard controller register first:

   ```sh
   echo 'softdep gs_usb pre: mcp251xfd' > /etc/modprobe.d/can-order.conf
   ```

   A firmware update may remove this file.

3. Download the package to the device and run the installer (Venus OS has no `git`):

   ```sh
   cd /tmp
   wget -O huawei.zip https://github.com/MalteSchm/venus-os_dbus-huawei-charger/archive/refs/heads/main.zip
   unzip huawei.zip
   cd venus-os_dbus-huawei-charger-main
   sh install.sh
   ```

   Or copy the files to the device with `scp`. The installer installs to
   `/data/dbus-huawei-charger`, keeps an existing `config.ini`, links the service
   to `/service/dbus-huawei-charger` and adds a re-link line to `/data/rc.local`.

4. Edit `/data/dbus-huawei-charger/config.ini` (see below), then restart the service:

   ```sh
   svc -t /service/dbus-huawei-charger
   svstat /service/dbus-huawei-charger
   ```

The service start script reads `interface` and `driver` from `config.ini`, refuses to start if
the interface is served by another driver, stops `vecan-dbus.<interface>` (so it does not occupy
the adapter), sets the bitrate to 125000 and brings the interface up.

To remove it: `sh uninstall.sh`.

## Configuration (`config.ini`)

| Key | Default | Meaning |
| --- | --- | --- |
| `interface` | `can1` | CAN interface of the adapter (explicit name; `auto` = first interface of `driver`). |
| `driver` | `gs_usb` | Kernel driver the interface must use; mismatch = no start. Empty = no check. |
| `vmin` / `vmax` | 48 / 55.2 | Voltage range in V. A BMS limit below `vmin` switches the PSU off. |
| `imax` | 50 | Hard maximum current in A (the R4850G2 delivers up to about 52 A). |
| `deviceinstance` | 290 | VRM/dbus device instance. |
| `heartbeat_timeout` | 0 | Seconds without a change of `/Huawei/Heartbeat` before switching off; 0 = off. |
| `status_log_interval` | 60 | Seconds between status lines in the log; 0 = off. |

### Optional PSU settings

| Key | Range | Meaning |
| --- | --- | --- |
| `offline_voltage` | 41.5 .. `vmax` V | Voltage the PSU uses without CAN frames. |
| `offline_current` | 0 .. `imax` A | Current limit the PSU uses without CAN frames. |
| `fan_online_full_speed` | true/false | Fan at full speed while controlled. |
| `fan_offline_full_speed` | true/false | Fan at full speed without control. |

A key that is missing, commented out or empty is **never written**. A configured key is written
every time the PSU becomes reachable (driver start, PSU power-up, CAN reconnect), with up to 5
attempts per setting; the PSU must acknowledge the value. Invalid values stop the driver at
start with an error. Restart the service after editing.

## dbus interface

Service name: `com.victronenergy.charger.huawei_r4850_<interface>`.

| Path | Access | Meaning |
| --- | --- | --- |
| `/Mode` | read/write | 1 = on, 4 = off. Always 4 after a start. |
| `/Link/ChargeCurrent` | read/write | Requested charge current in A (0 after start). |
| `/Link/ChargeVoltage` | read/write | Requested voltage limit in V (0 after start = nothing). |
| `/Dc/0/Voltage`, `/Dc/0/Current` | read | Output values. |
| `/Ac/In/L1/I`, `/Ac/In/L1/P` | read | AC input current and power (the GUI charger page shows the current). |
| `/State` | read | 252 (external control) while charging, otherwise 0. |
| `/Huawei/Status` | read | Reason text, e.g. `setpoint: 20.0 A / 54.0 V`, `BMS limit: 37.8 A / 54.0 V`, `Mode off`. |
| `/Huawei/Reachable` | read | 1 if the PSU answers on CAN. |
| `/Huawei/OutputPower`, `InputPower`, `Efficiency` | read | W, W, %. |
| `/Huawei/AcVoltage`, `AcCurrent`, `AcFrequency` | read | Input side. |
| `/Huawei/InputTemperature`, `OutputTemperature` | read | Degrees C. |
| `/Huawei/CurrentLimit` | read | Output current limit reported by the PSU. |
| `/Huawei/Heartbeat` | read/write | Optional watchdog (see `heartbeat_timeout`). |

Charging starts when `/Mode` is 1 and the effective current is at least 1 A (0.5 A to stay on).
Write **numbers**, not strings: the `dbus` command line tool writes text, which can crash other
services. `setv.py` (installed next to the driver) writes typed values:

```sh
cd /data/dbus-huawei-charger
S=com.victronenergy.charger.huawei_r4850_can1
python3 setv.py $S /Link/ChargeVoltage 54
python3 setv.py $S /Link/ChargeCurrent 20
python3 setv.py $S /Mode 1
```

In Node-RED, use a dbus-victron output node with the same service and paths.

## Node-RED

With the `node-red-contrib-victron` nodes (included in Venus OS Large) the same service can be
used for control and for monitoring, e.g. in a Node-RED dashboard:

- Service: `com.victronenergy.charger.huawei_r4850_can1` (`_<interface>` is your CAN interface).
- Control (output nodes): `/Mode`, `/Link/ChargeVoltage`, `/Link/ChargeCurrent`. The driver always
  starts with `/Mode = 4` (off), so a flow has to set voltage and current first and then `/Mode = 1`.
  The BMS limits still apply, see above.
- Monitoring (input nodes): `/Huawei/Efficiency` (%), `/Huawei/OutputTemperature` and
  `/Huawei/InputTemperature` (degrees C), `/Huawei/OutputPower`, `/Huawei/InputPower`,
  `/Huawei/AcVoltage`, `/Huawei/AcCurrent`, `/Huawei/AcFrequency`, `/Huawei/CurrentLimit` and
  `/Huawei/Status` (text with the reason for the current limit).

The Victron GUI only shows standard paths (switch, state, output voltage/current, AC input
current); the `/Huawei/*` values are not displayed there.

## Monitoring

```sh
tail -f /var/log/dbus-huawei-charger/current | tai64nlocal
```

The status line shows output voltage/current/power, input voltage/current/frequency/power,
efficiency, temperatures and the status reason.

## Troubleshooting

- **Refuses to start, driver mismatch:** the interface numbering changed; fix the order (see
  installation step 2) or correct `interface` in `config.ini`. Nothing was touched.
- **BMS disappeared after installing:** the adapter and BMS interfaces were swapped, see above.
- **PSU does not charge after a reboot:** check that the bitrate is 125000
  (`ip -details link show can1`).
- **Status `no battery service` / `BMS limits missing`:** the battery service is absent or does
  not publish the required paths.
- **Status `no current headroom`:** other chargers already use the BMS charge current limit.

## Development

```sh
python3 -m unittest discover -s tests
```

The CAN layer (`r4850can.py`) and the decision logic (`charger_logic.py`) are unit-tested; the dbus
glue layer is not. The tests are not needed on the device.

## License and credits

GPL-2.0-or-later, see the SPDX headers. The CAN protocol knowledge comes from
[OpenDTU-OnBattery](https://github.com/hoylabs/OpenDTU-OnBattery) (GPL-2.0-or-later),
the beyondlogic R4850 write-up and the Peacock/Huawei reverse-engineering community.
