"""
Tests for the Performance tab's Health Check parsers and findings.
Sample output mirrors real Android TV shells (dumpsys formats from Android 12).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import android_tv_tools as atv  # noqa: E402


SCAN = """\
@@meminfo
MemTotal:        3000000 kB
MemAvailable:     420000 kB
SwapTotal:        102400 kB
SwapFree:              0 kB
@@df
Filesystem            1K-blocks    Used Available Use% Mounted on
/dev/block/mmcblk0p50   4000000 3400000    600000  85% /data/user/0
@@settings
window_animation_scale=0.5
transition_animation_scale=0.5
animator_duration_scale=null
cached_apps_freezer=enabled
@@pss
Total PSS by process:
    141,011K: system (pid 3079)
    102,108K: com.example.player (pid 6028 / activities)
     94,549K: com.example.vpn (pid 10272)
     60,000K: tv.samba.ssm (pid 10154)
     30,000K: tv.samba.ssm:worker (pid 10155)
      9,000K: com.example.tiny (pid 1)

@@cpu
Load: 53.08 / 53.06 / 53.15
CPU usage from 62871ms to 14024ms ago:
  25% 3557/aud_cpupost: 11% user + 14% kernel
  6.9% 10154/tv.samba.ssm: 6.1% user + 0.7% kernel / faults: 4432 minor
  4% 7172/com.example.player: 2.5% user + 1.2% kernel
  1% 9/com.example.vpn: 1% user + 0% kernel
28% TOTAL: 12% user + 15% kernel + 0% iowait
@@all_pkgs
package:android
package:tv.samba.ssm
package:com.google.android.katniss
package:com.example.player
package:com.example.vpn
package:com.example.unused
package:com.example.tiny
@@user_pkgs
package:com.example.player
package:com.example.vpn
package:com.example.unused
package:com.example.tiny
@@disabled
package:com.google.android.katniss
@@disk
Data-Free: 600000K / 4000000K total = 15% free
Package Names: ["com.example.player","com.example.vpn"]
App Sizes: [100000000,20000000]
App Data Sizes: [2300000000,1000000]
Cache Sizes: [2200000000,500000]
@@usage
        package=com.example.player totalTimeUsed="35:17:55" lastTimeUsed="2026-10-25 22:56:31"
        package=com.example.vpn totalTimeUsed="07:14" lastTimeUsed="2026-10-25 22:56:50"
        package=com.example.unused totalTimeUsed="00:00" lastTimeUsed="1970-01-01 01:00:00"
@@top
    mResumedActivity: ActivityRecord{d085aae u0 com.example.player/.MainActivity t1452}
"""


def _findings():
    return {f["title"]: f for f in atv._health_findings(atv._split_sections(SCAN))}


def test_parsers():
    sec = atv._split_sections(SCAN)
    pss = atv._parse_pss(sec["pss"])
    assert pss["tv.samba.ssm"] == 90000 * 1024          # sub-processes summed
    total, procs = atv._parse_cpuinfo(sec["cpu"])
    assert total == 28.0
    assert (6.9, "tv.samba.ssm") in procs
    assert atv._usage_secs("35:17:55") == 35 * 3600 + 17 * 60 + 55
    assert atv._usage_secs("07:14") == 434
    assert atv._parse_diskstats(sec["disk"])[0] == (
        "com.example.player", 100000000, 2300000000, 2200000000)
    assert atv._resumed_package(sec["top"]) == "com.example.player"


def test_rss_fallback_and_pss_preferred():
    both = ("Total RSS by process:\n    50,000K: com.a (pid 1)\n\n"
            "Total PSS by process:\n    20,000K: com.a (pid 1)\n")
    assert atv._parse_pss(both) == {"com.a": 20000 * 1024}
    rss_only = "Total RSS by process:\n    50,000K: com.a (pid 1)\n"
    assert atv._parse_pss(rss_only) == {"com.a": 50000 * 1024}


def test_storage_uses_bind_mounted_data_row():
    f = _findings()["Storage"]
    assert f["severity"] == "warn"                     # exactly 15% free; bad is < 15%
    assert "2.0 GB" in f["detail"]                     # the big cache is named
    assert f["actions"] == [("Clear All App Caches", "trim_caches", None)]


def test_memory_lists_stoppable_apps_but_not_foreground():
    f = _findings()["Memory"]
    assert f["severity"] == "bad"                      # 14% available, swap full
    pkgs = [acts[0][2] for _text, acts in f["items"]]
    assert pkgs == ["com.example.vpn", "tv.samba.ssm"]  # player is on screen; tiny < 20 MB


def test_cpu_ignores_load_average_and_names_background_hogs():
    f = _findings()["CPU"]
    assert f["severity"] == "ok"
    assert [acts[0][2] for _t, acts in f["items"]] == ["tv.samba.ssm"]


def test_unused_apps_skip_foreground_and_used():
    f = _findings()["Unused Apps"]
    assert [acts[0][2] for _t, acts in f["items"]] == ["com.example.tiny", "com.example.unused"]


def test_known_services_skip_disabled_ones():
    f = _findings()["Optional Background Services"]
    assert [acts[0][2] for _t, acts in f["items"]] == ["tv.samba.ssm"]   # katniss already off


def test_quick_tweaks_treat_null_as_default_speed():
    f = _findings()["Quick Tweaks"]
    assert ("Speed Up Animations (0.5×)", "animations", "0.5") in f["actions"]
    assert "App freezer: on." in f["detail"]


def test_no_usage_data_skips_unused_finding():
    sec = atv._split_sections(SCAN)
    sec["usage"] = ""
    titles = {f["title"] for f in atv._health_findings(sec)}
    assert "Unused Apps" not in titles
