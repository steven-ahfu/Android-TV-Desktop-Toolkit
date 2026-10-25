"""
Tests for the Info tab's system-info parsers in android_tv_tools.py.
Sample output is trimmed from real Android TV devices (toybox df, /proc files).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import android_tv_tools as atv  # noqa: E402


GETPROP = """\
* daemon not running; starting now at tcp:5037
* daemon started successfully
[ro.product.manufacturer]: [Google]
[ro.product.model]: [Chromecast]
[ro.build.version.sdk]: [34]
[ro.empty]: []
"""

SYSINFO = """\
@@meminfo
MemTotal:        1999324 kB
MemFree:          123456 kB
MemAvailable:     999662 kB
Cached:           400000 kB
SwapTotal:        999660 kB
SwapFree:         499830 kB
@@df
Filesystem                 1K-blocks    Used Available Use% Mounted on
/dev/root                    1548636 1530000     2000  99% /
tmpfs                         999662    1200    998462   1% /dev
/dev/block/dm-5              4194304 1048576   3145728  25% /data
/dev/fuse                    4194304 1048576   3145728  25% /storage/emulated
/dev/block/vold/public:8,1  30000000 10000000 20000000  34% /mnt/media_rw/1A2B-3C4D
/dev/fuse                   30000000 10000000 20000000  34% /storage/1A2B-3C4D
@@cpuinfo
processor\t: 0
processor\t: 1
processor\t: 2
processor\t: 3
Hardware\t: Amlogic
@@maxfreq
1908000
1908000
1908000
1908000
@@uptime
273845.12 1000000.00
@@kernel
5.4.210-android12
@@density
Physical density: 320
@@ip
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
9: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0
@@gles
GLES: ARM, Mali-G31, OpenGL ES 3.2 v1.r26p0
"""


def test_getprop_ignores_daemon_noise():
    props = atv._parse_getprop(GETPROP)
    assert props["ro.product.manufacturer"] == "Google"
    assert props["ro.build.version.sdk"] == "34"
    assert props["ro.empty"] == ""
    assert len(props) == 4


def test_sections_and_meminfo():
    sec = atv._split_sections(SYSINFO)
    assert set(sec) == {"meminfo", "df", "cpuinfo", "maxfreq", "uptime",
                        "kernel", "density", "ip", "gles"}
    mem = atv._parse_meminfo(sec["meminfo"])
    assert mem["MemTotal"] == 1999324 * 1024
    assert mem["MemAvailable"] == 999662 * 1024


def test_storage_internal_and_external_deduped():
    sec = atv._split_sections(SYSINFO)
    internal, external = atv._storage_volumes(atv._parse_df(sec["df"]))
    assert internal == (4194304 * 1024, 1048576 * 1024, 3145728 * 1024)
    assert [v[0] for v in external] == ["1A2B-3C4D"]
    assert atv._fmt_usage(*internal) == "3.0 GB free of 4.0 GB  (25% used)"


def test_cpu_summary():
    sec = atv._split_sections(SYSINFO)
    assert atv._parse_cpu(sec["cpuinfo"], sec["maxfreq"]) == "4 cores @ 1.9 GHz"
    assert atv._cpuinfo_hardware(sec["cpuinfo"]) == "Amlogic"
    big_little = atv._parse_cpu("", "2000000\n2000000\n1500000\n1500000\n1500000\n1500000")
    assert big_little == "6 cores (2× 2.0 GHz + 4× 1.5 GHz)"
    assert atv._parse_cpu("", "") == "—"


def test_misc_fields():
    sec = atv._split_sections(SYSINFO)
    assert atv._fmt_uptime(sec["uptime"]) == "3d 4h 4m"
    assert atv._parse_density(sec["density"]) == "320 dpi"
    assert atv._parse_density("Physical density: 320\nOverride density: 240") == \
        "240 dpi (physical 320)"
    assert atv._parse_ip_addrs(sec["ip"]) == "wlan0 192.168.1.20"
    assert atv._fmt_bytes(512) == "512 B"
    assert atv._fmt_bytes(1536 * 1024 ** 2) == "1.5 GB"
