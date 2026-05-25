"""
Tests for device-label deduplication in android_tv_tools.py.

Bug: adb() returns (stdout + stderr).strip(), so ADB daemon startup messages
in stderr get concatenated to the manufacturer string.  When that happens,
manufacturer becomes e.g. "Google\n* daemon started successfully" and the
model.startswith(manufacturer) check fails, producing "Google Google TV ...".
"""
import re
import pytest


# ── copy of the CURRENT (unfixed) logic from _refresh_info_async ──────────────
def _make_label_current(manufacturer: str, model: str) -> str:
    if manufacturer and model.lower().startswith(manufacturer.lower()):
        return model.strip()
    return f"{manufacturer} {model}".strip()


# ── copy of the FIXED logic (defined here for reference; tested below) ────────
def _clean_prop(raw: str) -> str:
    """Return first meaningful line from an adb getprop result."""
    for line in raw.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("*"):
            return cleaned
    return raw.strip()


def _make_label_fixed(manufacturer: str, model: str) -> str:
    mfr = _clean_prop(manufacturer)
    mdl = _clean_prop(model)
    if mfr and mdl.lower().startswith(mfr.lower()):
        return mdl
    return f"{mfr} {mdl}".strip()


# ── cases that MUST NOT produce a doubled manufacturer name ───────────────────
CASES = [
    # (manufacturer_as_returned_by_adb, model, expected_label)

    # Happy path — clean values
    ("Google",   "Google TV Streaming Stick", "Google TV Streaming Stick"),
    ("Google",   "Google TV Streamer",        "Google TV Streamer"),
    ("Samsung",  "Samsung Galaxy Tab S9",     "Samsung Galaxy Tab S9"),
    ("Google",   "Chromecast",                "Google Chromecast"),
    ("Samsung",  "Galaxy Tab S9",             "Samsung Galaxy Tab S9"),
    ("",         "Unknown Device",            "Unknown Device"),

    # ADB daemon noise in stderr gets concatenated to stdout value
    (
        "Google\n* daemon not running; starting now at tcp:5037\n* daemon started successfully",
        "Google TV Streaming Stick",
        "Google TV Streaming Stick",
    ),
    # Carriage return appended by Windows ADB
    ("Google\r", "Google TV Streaming Stick", "Google TV Streaming Stick"),
    # Leading ADB warning line before the value
    ("* failed to start daemon\nGoogle", "Google TV Streaming Stick", "Google TV Streaming Stick"),
]


@pytest.mark.parametrize("mfr,mdl,expected", CASES)
def test_label_deduplication(mfr, mdl, expected):
    """All cases must produce a clean label with no doubled manufacturer name."""
    result = _make_label_fixed(mfr, mdl)
    assert result == expected, f"got {result!r}, want {expected!r}"
