import sys
import shutil
import webbrowser
import customtkinter as ctk
import subprocess
import threading
import os
import socket
import queue
from pathlib import Path
from tkinter import filedialog, StringVar
import urllib.request
import json as _json

# ── paths ──────────────────────────────────────────────────────────────────────
def _base_dir() -> Path:
    """Resolves app root whether running as a script or a PyInstaller frozen exe."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

VERSION      = "4.1.6"
_NO_WINDOW   = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SCRIPT_DIR   = _base_dir()
BIN_DIR      = SCRIPT_DIR / "bin"
DATA_DIR     = SCRIPT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

def _find_adb() -> str:
    # 1. User-saved preference from a previous session
    pref_file = DATA_DIR / "adb_path.txt"
    if pref_file.exists():
        saved = pref_file.read_text().strip()
        if saved and Path(saved).exists():
            return saved
    # 2. Bundled in bin/
    adb_name = "adb.exe" if sys.platform == "win32" else "adb"
    bundled = BIN_DIR / adb_name
    if bundled.exists():
        return str(bundled)
    # 3. Common default install locations
    if sys.platform == "win32":
        lappdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(lappdata) / "Android/Sdk/platform-tools/adb.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Android/android-sdk/platform-tools/adb.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Android/android-sdk/platform-tools/adb.exe",
            Path("C:/platform-tools/adb.exe"),
        ]
    else:
        home = Path.home()
        candidates = [
            home / "Library/Android/sdk/platform-tools/adb",
            home / "Android/Sdk/platform-tools/adb",
            Path("/usr/local/bin/adb"),
            Path("/opt/homebrew/bin/adb"),
        ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # 4. PATH
    return shutil.which("adb") or ""

ADB = _find_adb()
HISTORY_FILE   = DATA_DIR / "Device_history.txt"   # legacy flat-text (read-only compat)
DEVICES_FILE   = DATA_DIR / "devices.json"          # new JSON store
SHIZUKU_APK    = DATA_DIR / "shizuku.apk"
SCREENSHOT_TMP = DATA_DIR / "_screenshot_tmp.png"

# ── adb helpers ────────────────────────────────────────────────────────────────
_LOG_FILE = DATA_DIR / "adb_calls.log"

def _log_adb(args):
    try:
        from datetime import datetime
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] adb {' '.join(args)}\n"
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def adb(*args, serial=None, timeout=10):
    if not ADB:
        return "ERROR: ADB not configured"
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    _log_adb(cmd[1:])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"

def adb_out(*args, serial=None, timeout=10):
    return adb(*args, serial=serial, timeout=timeout)

def _clean_prop(raw: str) -> str:
    """Return the first meaningful line from an adb getprop result.
    Guards against ADB daemon startup messages in stderr being concatenated
    to the actual property value by adb() which joins stdout+stderr."""
    for line in raw.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("*"):
            return cleaned
    return raw.strip()

def _dev_quote(path: str) -> str:
    """Single-quote a path for the on-device shell.
    `adb shell` joins its argv with spaces and re-parses on the device, so a
    path containing spaces must arrive already quoted."""
    return "'" + path.replace("'", "'\\''") + "'"

def _parse_ls(out: str):
    """Parse `ls -1ap` output into ([(name, is_dir), ...], error).
    Directories carry a trailing '/' from the -p flag. Returns a non-None error
    string when the listing failed (timeout, ADB error, or all-`ls:`-error lines)."""
    if out == "TIMEOUT":
        return [], "Timed out listing directory (device slow or path very large)."
    if out.startswith("ERROR:"):
        return [], out
    lines = [l.rstrip("\r") for l in out.splitlines()]
    nonempty = [l for l in lines if l.strip()]
    err_lines = [l for l in nonempty if l.strip().startswith("ls:")]
    if nonempty and len(err_lines) == len(nonempty):
        return [], "\n".join(err_lines)
    entries = []
    for name in lines:
        if not name or name in ("./", "../", ".", ".."):
            continue
        if name.strip().startswith("ls:"):
            continue
        if name.startswith("* daemon") or name.startswith("adb:"):
            continue
        is_dir = name.endswith("/")
        disp = name[:-1] if is_dir else name
        if not disp:
            continue
        entries.append((disp, is_dir))
    # directories first, then case-insensitive name order
    entries.sort(key=lambda e: (not e[1], e[0].lower()))
    return entries, None

# ── device history (JSON) ──────────────────────────────────────────────────────
import re as _re
import json as _json_hist
from datetime import datetime as _dt

_IP_RE = _re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b')

def load_history():
    """Return dict keyed by IP: {label, port, last_seen}.
    Reads devices.json if present, otherwise migrates legacy Device_history.txt."""
    devices = {}
    if DEVICES_FILE.exists():
        try:
            raw = _json_hist.loads(DEVICES_FILE.read_text(encoding="utf-8"))
            # normalise older entries that may be plain strings
            for ip, v in raw.items():
                if isinstance(v, str):
                    devices[ip] = {"label": v, "port": 5555, "last_seen": ""}
                else:
                    devices[ip] = v
            return devices
        except Exception:
            pass
    # migrate legacy flat-text file
    if HISTORY_FILE.exists():
        try:
            for line in HISTORY_FILE.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("=") or ("Date" in line and "Time" in line):
                    continue
                m = _IP_RE.search(line)
                if not m:
                    continue
                ip = m.group(1)
                after = line[m.end():].strip()
                devices[ip] = {"label": after or ip, "port": 5555, "last_seen": ""}
        except Exception:
            pass
    return devices

def save_history(ip, label, port=5555, touch_seen=True):
    try:
        devices = load_history()
        entry = devices.get(ip, {"label": label, "port": port, "last_seen": ""})
        entry["label"] = label or entry.get("label", ip)
        entry["port"] = port
        if touch_seen:
            entry["last_seen"] = _dt.now().strftime("%Y-%m-%d %H:%M")
        devices[ip] = entry
        DEVICES_FILE.write_text(
            _json_hist.dumps(devices, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass

def delete_history(ip):
    try:
        devices = load_history()
        devices.pop(ip, None)
        DEVICES_FILE.write_text(
            _json_hist.dumps(devices, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass

def _last_seen_str(ts: str) -> str:
    """Convert ISO timestamp to human-readable age string."""
    if not ts:
        return "never seen"
    try:
        delta = _dt.now() - _dt.strptime(ts, "%Y-%m-%d %H:%M")
        minutes = int(delta.total_seconds() / 60)
        if minutes < 2:    return "just now"
        if minutes < 60:   return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:     return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return ""

# ── network scan ───────────────────────────────────────────────────────────────
def check_host(ip, port, result_queue):
    try:
        with socket.create_connection((ip, port), timeout=0.5):
            result_queue.put((ip, port))
    except Exception:
        pass

def scan_subnet(base="192.168.1", result_callback=None, done_callback=None):
    """Scan subnet for port 5555 hits, then verify each is Android via ADB."""
    q = queue.Queue()
    threads = []
    for i in range(1, 256):
        ip = f"{base}.{i}"
        t = threading.Thread(target=check_host, args=(ip, 5555, q), daemon=True)
        t.start()
        threads.append(t)

    def collector():
        for t in threads:
            t.join()
        hits = []
        while not q.empty():
            hits.append(q.get())

        # verify each hit is actually an Android device
        verified = []
        for ip, port in hits:
            serial = f"{ip}:{port}"
            out = adb("connect", serial)
            if "connected" in out.lower() or "already" in out.lower():
                model = adb("shell", "getprop", "ro.product.model", serial=serial)
                if model and not model.startswith("ERROR"):
                    verified.append({"ip": ip, "port": port, "label": model.strip(), "verified": True})
                    continue
            verified.append({"ip": ip, "port": port, "label": "", "verified": False})

        # also check mdns for wireless-debugging devices (Android 11+)
        mdns = scan_mdns()
        for entry in mdns:
            if not any(v["ip"] == entry["ip"] for v in verified):
                verified.append(entry)

        if result_callback:
            verified.sort(key=lambda x: [int(p) if p.isdigit() else 0 for p in x["ip"].split(".")])
            result_callback(verified)
        if done_callback:
            done_callback()

    threading.Thread(target=collector, daemon=True).start()

def scan_mdns():
    """Use 'adb mdns services' to find wireless-debugging devices on the LAN."""
    results = []
    if not ADB:
        return results
    try:
        r = subprocess.run([ADB, "mdns", "services"], capture_output=True, text=True, timeout=5,
                           creationflags=_NO_WINDOW)
        for line in (r.stdout + r.stderr).splitlines():
            # format: <name>  _adb-tls-connect._tcp  <ip>:<port>
            if "_adb-tls-connect" in line:
                parts = line.split()
                addr = parts[-1] if parts else ""
                if ":" in addr:
                    ip, port_s = addr.rsplit(":", 1)
                    try:
                        results.append({"ip": ip, "port": int(port_s),
                                        "label": "(wireless debug)", "verified": True,
                                        "wireless": True})
                    except ValueError:
                        pass
    except Exception:
        pass
    return results

# ── One Dark design tokens ─────────────────────────────────────────────────────
APP_BG          = "#282c34"
SIDEBAR_BG      = "#21252b"
SURFACE_1       = "#2c313c"
SURFACE_2       = "#333842"
SURFACE_3       = "#3e4452"
BORDER          = "#4b5263"

TEXT            = "#abb2bf"
TEXT_MUTED      = "#7f848e"
TEXT_DISABLED   = "#5c6370"

PRIMARY         = "#61afef"
PRIMARY_HOVER   = "#528bff"
PRIMARY_MUTED   = "#2d425a"

SECONDARY       = "#3e4452"
SECONDARY_HOVER = "#4b5263"

DANGER          = "#e06c75"
DANGER_BG       = "#3f2a2e"
DANGER_HOVER    = "#4a3035"

SUCCESS         = "#98c379"
SUCCESS_BG      = "#2f3f32"
SUCCESS_HOVER   = "#3a5040"

WARNING         = "#e5c07b"
INFO            = "#56b6c2"
PURPLE          = "#c678dd"

RADIUS_SM   = 8
RADIUS_MD   = 12
RADIUS_LG   = 18
RADIUS_XL   = 24
RADIUS_PILL = 999

PAD_SM = 8
PAD_MD = 12
PAD_LG = 16
PAD_XL = 24

_FONT = "Helvetica"


class Panel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=kwargs.pop("fg_color", SURFACE_1),
            corner_radius=kwargs.pop("corner_radius", RADIUS_XL),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", BORDER),
            **kwargs,
        )


class PrimaryButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 40),
            corner_radius=kwargs.pop("corner_radius", RADIUS_MD),
            fg_color=kwargs.pop("fg_color", PRIMARY),
            hover_color=kwargs.pop("hover_color", PRIMARY_HOVER),
            text_color=kwargs.pop("text_color", APP_BG),
            font=kwargs.pop("font", ctk.CTkFont(family=_FONT, size=13, weight="bold")),
            **kwargs,
        )


class SecondaryButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 40),
            corner_radius=kwargs.pop("corner_radius", RADIUS_MD),
            fg_color=kwargs.pop("fg_color", SURFACE_2),
            hover_color=kwargs.pop("hover_color", SURFACE_3),
            text_color=kwargs.pop("text_color", TEXT),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", BORDER),
            font=kwargs.pop("font", ctk.CTkFont(family=_FONT, size=13)),
            **kwargs,
        )


class SuccessButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 40),
            corner_radius=kwargs.pop("corner_radius", RADIUS_MD),
            fg_color=kwargs.pop("fg_color", SUCCESS_BG),
            hover_color=kwargs.pop("hover_color", SUCCESS_HOVER),
            text_color=kwargs.pop("text_color", SUCCESS),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", "#3d5a3f"),
            font=kwargs.pop("font", ctk.CTkFont(family=_FONT, size=13, weight="bold")),
            **kwargs,
        )


class DangerButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 40),
            corner_radius=kwargs.pop("corner_radius", RADIUS_MD),
            fg_color=kwargs.pop("fg_color", DANGER_BG),
            hover_color=kwargs.pop("hover_color", DANGER_HOVER),
            text_color=kwargs.pop("text_color", DANGER),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", "#5a3439"),
            font=kwargs.pop("font", ctk.CTkFont(family=_FONT, size=13, weight="bold")),
            **kwargs,
        )


class ChipButton(ctk.CTkButton):
    def __init__(self, master, selected: bool = False, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 34),
            corner_radius=kwargs.pop("corner_radius", RADIUS_PILL),
            fg_color=kwargs.pop("fg_color", PRIMARY_MUTED if selected else SURFACE_1),
            hover_color=kwargs.pop("hover_color", SURFACE_3),
            text_color=kwargs.pop("text_color", PRIMARY if selected else TEXT_MUTED),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", PRIMARY if selected else BORDER),
            font=kwargs.pop("font", ctk.CTkFont(family=_FONT, size=13)),
            **kwargs,
        )


class EmptyState(ctk.CTkFrame):
    def __init__(self, master, title="No data", subtitle="", **kwargs):
        super().__init__(master, fg_color=kwargs.pop("fg_color", "transparent"), **kwargs)
        ctk.CTkLabel(
            self, text=title,
            font=ctk.CTkFont(family=_FONT, size=14, weight="bold"),
            text_color=TEXT_DISABLED,
        ).pack(pady=(0, 4))
        if subtitle:
            ctk.CTkLabel(
                self, text=subtitle,
                font=ctk.CTkFont(family=_FONT, size=12),
                text_color=TEXT_DISABLED,
            ).pack()


class _RoundEntry(ctk.CTkEntry):
    def __init__(self, master, **kw):
        super().__init__(master,
            corner_radius=kw.pop("corner_radius", RADIUS_SM),
            height=kw.pop("height", 36),
            fg_color=kw.pop("fg_color", SIDEBAR_BG),
            border_color=kw.pop("border_color", BORDER),
            border_width=kw.pop("border_width", 1),
            text_color=kw.pop("text_color", TEXT),
            placeholder_text_color=kw.pop("placeholder_text_color", TEXT_DISABLED), **kw)


class TabBar(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=kwargs.pop("fg_color", SURFACE_1),
            corner_radius=kwargs.pop("corner_radius", 18),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", BORDER),
            **kwargs,
        )


class TabButton(ctk.CTkButton):
    def __init__(self, master, selected: bool = False, **kwargs):
        super().__init__(
            master,
            height=kwargs.pop("height", 38),
            corner_radius=kwargs.pop("corner_radius", 14),
            fg_color=kwargs.pop("fg_color", SURFACE_3 if selected else "transparent"),
            hover_color=kwargs.pop("hover_color", SURFACE_2),
            text_color=kwargs.pop("text_color", PRIMARY if selected else TEXT),
            text_color_disabled=kwargs.pop("text_color_disabled", TEXT_DISABLED),
            border_width=kwargs.pop("border_width", 1 if selected else 0),
            border_color=kwargs.pop("border_color", BORDER),
            font=kwargs.pop(
                "font",
                ctk.CTkFont(family=_FONT, size=13, weight="bold" if selected else "normal"),
            ),
            **kwargs,
        )


class StatusDot(ctk.CTkFrame):
    def __init__(self, master, online: bool = False, **kwargs):
        super().__init__(
            master,
            width=kwargs.pop("width", 18),
            height=kwargs.pop("height", 18),
            corner_radius=kwargs.pop("corner_radius", 9),
            fg_color=kwargs.pop("fg_color", PRIMARY_MUTED if online else "transparent"),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", PRIMARY if online else TEXT_DISABLED),
            **kwargs,
        )
        self.pack_propagate(False)
        inner = ctk.CTkFrame(
            self,
            width=7,
            height=7,
            corner_radius=4,
            fg_color=PRIMARY if online else TEXT_DISABLED,
        )
        inner.place(relx=0.5, rely=0.5, anchor="center")


def _scrollable(parent, **kwargs):
    """Return a CTkScrollableFrame filling parent, themed for One Dark."""
    sf = ctk.CTkScrollableFrame(
        parent,
        fg_color=kwargs.pop("fg_color", "transparent"),
        corner_radius=kwargs.pop("corner_radius", 0),
        scrollbar_button_color=kwargs.pop("scrollbar_button_color", SURFACE_3),
        scrollbar_button_hover_color=kwargs.pop("scrollbar_button_hover_color", SECONDARY_HOVER),
        **kwargs,
    )
    sf.pack(fill="both", expand=True)
    return sf


# ── main app ───────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=APP_BG)

        self.title(f"Android TV Desktop Toolkit v{VERSION}")
        self.geometry("1100x700")
        self.minsize(900, 600)
        _icon = SCRIPT_DIR / "assets" / "icon.ico"
        if _icon.exists():
            self.iconbitmap(str(_icon))

        self.serial = None
        self.history = load_history()

        self._build_ui()
        self._populate_device_list()
        self.after(200, self._apply_mica)
        self._start_adb_server()

    def _apply_mica(self):
        try:
            import pywinstyles
            pywinstyles.apply_style(self, "mica")
            pywinstyles.change_header_color(self, APP_BG)
            pywinstyles.change_title_color(self, TEXT)
        except Exception:
            pass

    # ── UI build ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── left panel ─────────────────────────────────────────────────────────
        left = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=SIDEBAR_BG)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(left, text="Devices",
                     font=ctk.CTkFont(family=_FONT, size=16, weight="bold"),
                     text_color=TEXT).grid(
            row=0, column=0, padx=14, pady=(16, 6), sticky="w")

        # subnet scan row
        subnet_row = ctk.CTkFrame(left, fg_color="transparent")
        subnet_row.grid(row=1, column=0, padx=10, pady=(0, 4), sticky="ew")
        subnet_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(subnet_row, text="Subnet:", font=ctk.CTkFont(family=_FONT, size=11),
                     text_color=TEXT_DISABLED).grid(row=0, column=0, padx=(0, 6))
        self._subnet_var = StringVar(value="192.168.1")
        _RoundEntry(subnet_row, textvariable=self._subnet_var,
                    font=ctk.CTkFont(family=_FONT, size=11), height=30).grid(row=0, column=1, sticky="ew")

        scan_btn_row = ctk.CTkFrame(left, fg_color="transparent")
        scan_btn_row.grid(row=2, column=0, padx=10, pady=4, sticky="ew")
        scan_btn_row.grid_columnconfigure(0, weight=1)
        self.scan_btn = SecondaryButton(scan_btn_row, text="Scan Network", height=34,
                                        command=self._scan)
        self.scan_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        SecondaryButton(scan_btn_row, text="mDNS", width=56, height=34,
                        command=self._scan_mdns).grid(row=0, column=1)

        self.scan_status = ctk.CTkLabel(left, text="", text_color=TEXT_DISABLED,
                                         font=ctk.CTkFont(family=_FONT, size=10))
        self.scan_status.grid(row=3, column=0, padx=12, pady=0, sticky="w")

        self.device_list = ctk.CTkScrollableFrame(left, label_text="",
                                                   fg_color="transparent")
        self.device_list.grid(row=4, column=0, padx=6, pady=4, sticky="nsew")
        self.device_list.grid_columnconfigure(0, weight=1)

        # connect / disconnect
        conn_row = ctk.CTkFrame(left, fg_color="transparent")
        conn_row.grid(row=5, column=0, padx=10, pady=6, sticky="ew")
        conn_row.grid_columnconfigure(0, weight=1)
        self.connect_btn = PrimaryButton(conn_row, text="Connect", height=38,
                                          corner_radius=RADIUS_PILL,
                                          command=self._connect)
        self.connect_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.disconnect_btn = SecondaryButton(conn_row, text="⏏", width=38, height=38,
                                               command=self._disconnect)
        self.disconnect_btn.grid(row=0, column=1)

        # divider
        ctk.CTkFrame(left, height=1, fg_color=BORDER).grid(
            row=6, column=0, padx=10, pady=(4, 6), sticky="ew")

        ctk.CTkLabel(left, text="Manual / Direct Connect",
                     font=ctk.CTkFont(family=_FONT, size=10), text_color=TEXT_DISABLED).grid(
                     row=7, column=0, padx=12, pady=(0, 4), sticky="w")
        manual_row = ctk.CTkFrame(left, fg_color="transparent")
        manual_row.grid(row=8, column=0, padx=10, pady=(0, 4), sticky="ew")
        manual_row.grid_columnconfigure(0, weight=1)
        self._manual_ip_var = StringVar()
        _RoundEntry(manual_row, textvariable=self._manual_ip_var,
                    placeholder_text="192.168.1.x", height=30,
                    font=ctk.CTkFont(family=_FONT, size=11)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._manual_port_var = StringVar(value="5555")
        _RoundEntry(manual_row, textvariable=self._manual_port_var,
                    width=50, height=30, font=ctk.CTkFont(family=_FONT, size=11)).grid(row=0, column=1)
        SecondaryButton(left, text="Connect to IP", height=32,
                        command=self._connect_manual).grid(
                        row=9, column=0, padx=10, pady=(2, 4), sticky="ew")
        SecondaryButton(left, text="Pair (Android 11+ Wireless)", height=32,
                        text_color=PRIMARY, border_color=PRIMARY_MUTED,
                        command=self._open_pair_dialog).grid(
                        row=10, column=0, padx=10, pady=(0, 12), sticky="ew")

        self.selected_ip = StringVar(value="")
        self._device_radio_buttons = []

        # ── right panel ────────────────────────────────────────────────────────
        right = ctk.CTkFrame(self, corner_radius=0, fg_color=APP_BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        # ── custom tab bar ──────────────────────────────────────────────────────
        tab_bar_row = ctk.CTkFrame(right, fg_color="transparent")
        tab_bar_row.grid(row=0, column=0, padx=0, pady=(6, 0), sticky="ew")
        tab_bar_row.grid_columnconfigure(0, weight=1)

        self._tab_bar = TabBar(tab_bar_row)
        self._tab_bar.grid(row=0, column=0, padx=(8, 22), sticky="ew")

        # tab content area
        self._tab_content_area = ctk.CTkFrame(right, fg_color=APP_BG, corner_radius=0)
        self._tab_content_area.grid(row=1, column=0, padx=0, pady=0, sticky="nsew")
        self._tab_content_area.grid_columnconfigure(0, weight=1)
        self._tab_content_area.grid_rowconfigure(0, weight=1)

        self._tab_frames: dict = {}
        self._tab_buttons: dict = {}
        self._active_tab: str | None = None

        _TAB_NAMES = ("Info", "Packages", "Performance", "Install", "Settings",
                      "Tools", "Media", "Remote", "Screenshot")
        for name in _TAB_NAMES:
            btn = TabButton(
                self._tab_bar,
                text=name,
                selected=False,
                command=lambda n=name: self._switch_tab(n),
                width=max(80, len(name) * 9 + 24),
            )
            btn.pack(side="left", padx=4, pady=6)
            self._tab_buttons[name] = btn

            frame = ctk.CTkFrame(self._tab_content_area, fg_color=APP_BG, corner_radius=0)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_remove()
            frame.grid_columnconfigure(0, weight=1)
            self._tab_frames[name] = frame

        self._build_info_tab()
        self._build_packages_tab()
        self._build_performance_tab()
        self._build_install_tab()
        self._build_settings_tab()
        self._build_tools_tab()
        self._build_media_tab()
        self._build_remote_tab()
        self._build_screenshot_tab()
        self._switch_tab("Info")
        self._set_tabs_enabled(False)

        # log area — border turns green when a device is connected
        self.log_frame = Panel(right, fg_color=SURFACE_1, corner_radius=RADIUS_LG)
        self.log_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")
        self.log_frame.grid_columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, padx=10, pady=(8, 0), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_header, text="Output Log",
                     font=ctk.CTkFont(family=_FONT, size=11, weight="bold"),
                     text_color=TEXT_MUTED).grid(row=0, column=0, sticky="w")
        self.copy_btn = SecondaryButton(log_header, text="Copy", width=60, height=26,
                                        font=ctk.CTkFont(family=_FONT, size=11),
                                        command=self._copy_log)
        self.copy_btn.grid(row=0, column=1, padx=(0, 2))

        self.log_box = ctk.CTkTextbox(self.log_frame, height=110,
                                       font=ctk.CTkFont(family="Consolas", size=11),
                                       fg_color=SIDEBAR_BG,
                                       text_color=TEXT_MUTED,
                                       state="disabled")
        self.log_box.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="ew")

    # ── tab switching ──────────────────────────────────────────────────────────
    def _switch_tab(self, name: str):
        if self._active_tab and self._active_tab != name:
            self._tab_frames[self._active_tab].grid_remove()
            b = self._tab_buttons[self._active_tab]
            b.configure(
                fg_color="transparent",
                text_color=TEXT,
                border_width=0,
                font=ctk.CTkFont(family=_FONT, size=13, weight="normal"),
            )
        if self._active_tab != name:
            self._tab_frames[name].grid()
            self._tab_buttons[name].configure(
                fg_color=SURFACE_3,
                text_color=PRIMARY,
                border_width=1,
                font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
            )
            self._active_tab = name

    def _set_tabs_enabled(self, enabled: bool):
        """Enable or disable all tabs except Info. Switches to Info when disabling."""
        for name, btn in self._tab_buttons.items():
            if name == "Info":
                continue
            if enabled:
                is_active = name == self._active_tab
                btn.configure(
                    state="normal",
                    text_color=PRIMARY if is_active else TEXT,
                    fg_color=SURFACE_3 if is_active else "transparent",
                    border_width=1 if is_active else 0,
                    font=ctk.CTkFont(family=_FONT, size=13,
                                     weight="bold" if is_active else "normal"),
                )
            else:
                btn.configure(state="disabled")
        if not enabled and self._active_tab != "Info":
            self._switch_tab("Info")

    def _build_info_tab(self):
        tab = _scrollable(self._tab_frames["Info"])
        tab.grid_columnconfigure(0, weight=1)

        card = Panel(tab)
        card.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        fields = [
            ("Manufacturer", "info_manufacturer"),
            ("Model", "info_model"),
            ("Android Version", "info_android"),
            ("Build / Firmware", "info_build"),
            ("Serial Number", "info_serial"),
            ("Resolution", "info_resolution"),
            ("Battery", "info_battery"),
            ("CPU ABI", "info_abi"),
            ("DNS Mode", "info_dns"),
            ("Package Verifier", "info_pkgverifier"),
            ("Input Method", "info_ime"),
        ]
        for i, (label, attr) in enumerate(fields):
            ctk.CTkLabel(card, text=label + ":", anchor="e", width=150,
                         text_color=TEXT_MUTED,
                         font=ctk.CTkFont(family=_FONT, size=13, weight="bold")).grid(
                row=i, column=0, padx=(14, 6), pady=6, sticky="e")
            var = ctk.CTkLabel(card, text="—", anchor="w",
                               text_color=TEXT, font=ctk.CTkFont(family=_FONT, size=13))
            var.grid(row=i, column=1, padx=6, pady=6, sticky="w")
            setattr(self, attr, var)

        SecondaryButton(card, text="Refresh Info", command=self._refresh_info, width=140).grid(
            row=len(fields), column=0, columnspan=2, padx=14, pady=14)

    def _build_packages_tab(self):
        import tkinter as tk
        import tkinter.ttk as ttk
        outer = self._tab_frames["Packages"]

        chip_row = ctk.CTkFrame(outer, fg_color="transparent")
        chip_row.pack(side="top", fill="x", padx=4, pady=(8, 4))

        ChipButton(chip_row, text="List System",
                   command=lambda: self._list_packages("-s")).pack(side="left", padx=(0, 4))
        ChipButton(chip_row, text="List All",
                   command=lambda: self._list_packages("")).pack(side="left", padx=(0, 4))
        ChipButton(chip_row, text="Uninstalled",
                   command=lambda: self._list_packages("-u")).pack(side="left", padx=(0, 4))
        ChipButton(chip_row, text="Play Store",
                   command=self._list_playstore_pkgs).pack(side="left", padx=(0, 4))
        ChipButton(chip_row, text="Sideloaded",
                   command=self._list_sideloaded_pkgs).pack(side="left")

        # pack bottom row before the expanding list so it anchors to the bottom
        action_row = ctk.CTkFrame(outer, fg_color="transparent")
        action_row.pack(side="bottom", fill="x", padx=4, pady=(0, 6))
        DangerButton(action_row, text="Uninstall", height=36, width=100,
                     command=self._pkg_uninstall).pack(side="left", padx=(0, 4))
        SecondaryButton(action_row, text="Disable", height=36, width=90,
                        command=self._pkg_disable).pack(side="left", padx=(0, 4))
        SecondaryButton(action_row, text="Enable", height=36, width=90,
                        command=self._pkg_enable).pack(side="left", padx=(0, 4))
        SecondaryButton(action_row, text="Get Version", height=36, width=100,
                        command=self._pkg_version).pack(side="left", padx=(0, 4))
        SuccessButton(action_row, text="Save APK", height=36, width=95,
                      command=self._pkg_save_apk).pack(side="left", padx=(0, 4))
        SecondaryButton(action_row, text="Clear Cache", height=36, width=100,
                        command=self._pkg_clear_cache).pack(side="left", padx=(0, 4))
        DangerButton(action_row, text="Clear Data", height=36, width=95,
                     command=self._pkg_clear_data).pack(side="left")

        list_frame = Panel(outer)
        list_frame.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 4))
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        # Dark-styled scrollbar via ttk
        _sb_style = ttk.Style()
        _sb_style.theme_use("default")
        _sb_style.configure("Dark.Vertical.TScrollbar",
            background=SURFACE_2, troughcolor=SIDEBAR_BG,
            bordercolor=BORDER, arrowcolor=TEXT_MUTED,
            darkcolor=SURFACE_1, lightcolor=SURFACE_2, relief="flat")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", style="Dark.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=8)

        self.pkg_listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set, selectmode="single",
            bg=SIDEBAR_BG, fg=TEXT, selectbackground=PRIMARY_MUTED,
            selectforeground=PRIMARY,
            font=("Consolas", 11), relief="flat", borderwidth=0,
            activestyle="none", highlightthickness=0)
        self.pkg_listbox.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=8)
        scrollbar.config(command=self.pkg_listbox.yview)

        # Empty state — shown by default, hidden once packages load
        self._pkg_empty_state = EmptyState(
            list_frame,
            title="No package data loaded",
            subtitle="Connect to a device, then choose List All.",
        )
        self._pkg_empty_state.place(relx=0.5, rely=0.5, anchor="center")

    def _build_performance_tab(self):
        tab = _scrollable(self._tab_frames["Performance"])
        tab.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(tab, text="Performance Optimizations",
                     font=ctk.CTkFont(family=_FONT, size=15, weight="bold"),
                     text_color=TEXT).grid(
                     row=0, column=0, columnspan=2, pady=(12, 2))
        ctk.CTkLabel(tab, text="Note: Compile Speed Profile can take several minutes.",
                     text_color=WARNING, font=ctk.CTkFont(family=_FONT, size=11)).grid(
                     row=1, column=0, columnspan=2, pady=(0, 10))

        left_card = Panel(tab)
        left_card.grid(row=2, column=0, padx=(8, 4), pady=4, sticky="ew")

        ctk.CTkLabel(left_card, text="Optimizations",
                     font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                     text_color=TEXT_MUTED).pack(padx=14, pady=(12, 6), anchor="w")
        PrimaryButton(left_card, text="Compile Speed Profile",
                      command=self._compile_speed_profile).pack(fill="x", padx=12, pady=4)
        SecondaryButton(left_card, text="Enable App Freezer",
                        command=self._enable_freezer).pack(fill="x", padx=12, pady=4)
        SecondaryButton(left_card, text="Optimize Touch Response",
                        command=self._optimize_touch).pack(fill="x", padx=12, pady=4)
        SuccessButton(left_card, text="All Optimizations",
                      command=self._all_optimizations).pack(fill="x", padx=12, pady=(4, 12))

        right_card = Panel(tab)
        right_card.grid(row=2, column=1, padx=(4, 8), pady=4, sticky="ew")

        ctk.CTkLabel(right_card, text="Animations & Memory",
                     font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                     text_color=TEXT_MUTED).pack(padx=14, pady=(12, 6), anchor="w")
        SecondaryButton(right_card, text="Speed Up Animations (0.5×)",
                        command=lambda: self._set_animations("0.5")).pack(fill="x", padx=12, pady=4)
        SecondaryButton(right_card, text="Disable Animations",
                        command=lambda: self._set_animations("0")).pack(fill="x", padx=12, pady=4)
        SecondaryButton(right_card, text="Reset Animations (1×)",
                        command=lambda: self._set_animations("1")).pack(fill="x", padx=12, pady=4)
        SecondaryButton(right_card, text="Clear All App Caches",
                        command=self._clear_all_caches).pack(fill="x", padx=12, pady=4)
        DangerButton(right_card, text="Kill Background Apps",
                     command=self._kill_background_apps).pack(fill="x", padx=12, pady=(4, 12))

    def _build_install_tab(self):
        outer = self._tab_frames["Install"]
        scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent",
            scrollbar_button_color=SURFACE_3, scrollbar_button_hover_color=SECONDARY_HOVER)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)

        # APK install card
        apk_frame = Panel(scroll)
        apk_frame.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        apk_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(apk_frame, text="Install APK",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).grid(
            row=0, column=0, columnspan=3, padx=14, pady=(12, 4), sticky="w")

        self.apk_path_var = StringVar(value="No file selected")
        ctk.CTkLabel(apk_frame, textvariable=self.apk_path_var,
                     text_color=TEXT_DISABLED,
                     font=ctk.CTkFont(family=_FONT, size=11)).grid(
            row=1, column=0, columnspan=3, padx=14, pady=2, sticky="w")

        SecondaryButton(apk_frame, text="Browse APK", command=self._browse_apk, width=120).grid(
            row=2, column=0, padx=12, pady=(4, 12), sticky="w")
        SuccessButton(apk_frame, text="Install", command=self._install_apk, width=120).grid(
            row=2, column=1, padx=4, pady=(4, 12))
        SecondaryButton(apk_frame, text="Bulk Install (Folder)", command=self._browse_bulk_folder, width=160).grid(
            row=2, column=2, padx=12, pady=(4, 12), sticky="e")

        # Shizuku card
        shiz_frame = Panel(scroll)
        shiz_frame.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(shiz_frame, text="Shizuku",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).grid(
            row=0, column=0, columnspan=3, padx=14, pady=(12, 4), sticky="w")

        self._shizuku_status_label = ctk.CTkLabel(shiz_frame, text="",
                                                   text_color=TEXT_DISABLED,
                                                   font=ctk.CTkFont(family=_FONT, size=11))
        self._shizuku_status_label.grid(row=1, column=0, columnspan=3, padx=14, pady=2, sticky="w")

        apk_present = SHIZUKU_APK.exists()
        self._shizuku_dl_btn = SecondaryButton(shiz_frame, text="Download Shizuku", width=160,
                                               command=self._download_shizuku,
                                               state="disabled" if apk_present else "normal")
        self._shizuku_dl_btn.grid(row=2, column=0, padx=12, pady=(4, 12))
        self._shizuku_install_btn = SuccessButton(shiz_frame, text="Install Shizuku", width=160,
                                                   command=self._install_shizuku,
                                                   state="normal" if apk_present else "disabled")
        self._shizuku_install_btn.grid(row=2, column=1, padx=4, pady=(4, 12))
        SecondaryButton(shiz_frame, text="Launch Shizuku", command=self._launch_shizuku, width=160).grid(
            row=2, column=2, padx=12, pady=(4, 12))

        self._refresh_shizuku_status()

        # Quick-install popular apps card
        qi_frame = Panel(scroll)
        qi_frame.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(qi_frame, text="Quick Install Popular Apps",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 8))

        self._qi_apps = {
            "AdGuard TV":       {"file": DATA_DIR / "adguard_tv.apk",
                                 "direct_url": "https://agrd.io/tvapk",
                                 "homepage": "https://adguard.com/en/adguard-android-tv/overview.html",
                                 "description": "Ad blocker and content filter for Android TV"},
            "Aurora Store":     {"file": DATA_DIR / "aurora_store.apk",
                                 "repo": "AuroraOSS/AuroraStore",
                                 "asset_suffix": ".apk",
                                 "gitlab": True,
                                 "description": "An unofficial FOSS client to Google Play."},
            "Flicky":           {"file": DATA_DIR / "flicky.apk",
                                 "repo": "mlm-games/flicky",
                                 "asset_suffix": ".apk",
                                 "description": "Yet Another FDroid Client (wide screen / TV friendly)"},
            "SmartTube Stable": {"file": DATA_DIR / "smarttube_stable.apk",
                                 "repo": "yuliskov/SmartTube",
                                 "asset_contains": "smarttube_stable",
                                 "description": "Browse media content with your own rules on Android TV"},
            "TizenTube":        {"file": DATA_DIR / "tizentube.apk",
                                 "repo": "reisxd/TizenTubeCobalt",
                                 "asset_suffix": ".apk",
                                 "description": "Experience TizenTube on other devices that are not Tizen."},
        }

        qi_grid = ctk.CTkFrame(qi_frame, fg_color="transparent")
        qi_grid.pack(fill="x", padx=8, pady=(0, 10))
        qi_grid.grid_columnconfigure(0, weight=1)
        qi_grid.grid_columnconfigure(1, weight=1)

        for i, (name, meta) in enumerate(self._qi_apps.items()):
            g_col = i % 2
            g_row = i // 2
            repo_url = (meta.get("homepage")
                        or (f"https://gitlab.com/{meta['repo']}" if meta.get("gitlab")
                            else f"https://github.com/{meta['repo']}"))

            cell = ctk.CTkFrame(qi_grid, fg_color=SURFACE_2, corner_radius=RADIUS_MD,
                                border_width=1, border_color=BORDER)
            cell.grid(row=g_row, column=g_col, padx=4, pady=4, sticky="ew")

            name_lbl = ctk.CTkLabel(cell, text=name,
                                    font=ctk.CTkFont(family=_FONT, size=12, weight="bold", underline=True),
                                    text_color=PRIMARY, anchor="w")
            name_lbl.pack(fill="x", padx=10, pady=(8, 2))
            name_lbl.bind("<Button-1>", lambda e, u=repo_url: webbrowser.open(u))
            name_lbl.bind("<Enter>", lambda e: e.widget.configure(cursor="hand2"))
            name_lbl.bind("<Leave>", lambda e: e.widget.configure(cursor=""))

            if meta.get("description"):
                ctk.CTkLabel(cell, text=meta["description"], text_color=TEXT_DISABLED,
                             font=ctk.CTkFont(family=_FONT, size=9),
                             wraplength=220, anchor="w", justify="left").pack(fill="x", padx=10, pady=(0, 6))

            btn_row = ctk.CTkFrame(cell, fg_color="transparent")
            btn_row.pack(fill="x", padx=8, pady=(0, 8))

            present = meta["file"].exists()
            dl_btn = SecondaryButton(btn_row, text="Download", width=100, height=32,
                                     state="disabled" if present else "normal",
                                     command=lambda n=name: self._qi_download(n))
            dl_btn.pack(side="left", padx=(0, 4))
            inst_btn = SuccessButton(btn_row, text="Install", width=90, height=32,
                                     state="normal" if present else "disabled",
                                     command=lambda n=name: self._qi_install(n))
            inst_btn.pack(side="left")
            meta["dl_btn"] = dl_btn
            meta["inst_btn"] = inst_btn

    def _build_settings_tab(self):
        tab = _scrollable(self._tab_frames["Settings"])
        tab.grid_columnconfigure((0, 1), weight=1)

        def section(text, row, col=0, span=1):
            ctk.CTkLabel(tab, text=text, font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                         text_color=TEXT_MUTED).grid(
                row=row, column=col, columnspan=span,
                padx=12, pady=(14, 4), sticky="w")

        def btn(text, cmd, row, col, w=200):
            SecondaryButton(tab, text=text, width=w, height=36, command=cmd).grid(
                row=row, column=col, padx=10, pady=3, sticky="w")

        # ── Display section ─────────────────────────────────────────────────────
        section("Display", 0, col=0, span=1)

        disp = ctk.CTkFrame(tab, fg_color="transparent")
        disp.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 4))
        disp.grid_columnconfigure(0, minsize=148)
        disp.grid_columnconfigure(1, weight=1, minsize=260)
        disp.grid_columnconfigure(2, minsize=96)

        def _lbl(parent, text, row):
            ctk.CTkLabel(parent, text=text, anchor="e", text_color=TEXT,
                         font=ctk.CTkFont(family=_FONT, size=11)).grid(
                row=row, column=0, sticky="e", padx=(0, 10), pady=7)

        rotation_opts = {"Portrait (0°)": "0", "Landscape (90°)": "1",
                         "Reverse Portrait (180°)": "2", "Reverse Landscape (270°)": "3"}
        self._rotation_var = ctk.StringVar(value="Portrait (0°)")
        _lbl(disp, "Rotate Screen:", 0)
        ctk.CTkOptionMenu(disp, values=list(rotation_opts.keys()),
                          variable=self._rotation_var, width=280,
                          fg_color=SURFACE_2, button_color=PRIMARY,
                          button_hover_color=PRIMARY_HOVER,
                          text_color=TEXT, corner_radius=RADIUS_SM).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=7)
        SecondaryButton(disp, text="Apply", width=92, height=34,
                        command=lambda: self._set_rotation(
                            rotation_opts[self._rotation_var.get()])).grid(
            row=0, column=2, sticky="w", pady=7)

        timeout_opts = {"30 seconds": "30000", "1 minute": "60000", "2 minutes": "120000",
                        "5 minutes": "300000", "10 minutes": "600000", "Never": "2147483647"}
        self._timeout_var = ctk.StringVar(value="5 minutes")
        _lbl(disp, "Screen Timeout:", 1)
        ctk.CTkOptionMenu(disp, values=list(timeout_opts.keys()),
                          variable=self._timeout_var, width=280,
                          fg_color=SURFACE_2, button_color=PRIMARY,
                          button_hover_color=PRIMARY_HOVER,
                          text_color=TEXT, corner_radius=RADIUS_SM).grid(
            row=1, column=1, sticky="w", padx=(0, 8), pady=7)
        SecondaryButton(disp, text="Apply", width=92, height=34,
                        command=lambda: self._set_screen_timeout(
                            timeout_opts[self._timeout_var.get()])).grid(
            row=1, column=2, sticky="w", pady=7)

        _lbl(disp, "Screen Density:", 2)
        density_inner = ctk.CTkFrame(disp, fg_color="transparent")
        density_inner.grid(row=2, column=1, sticky="w", padx=(0, 8), pady=7)
        self._density_entry = _RoundEntry(density_inner, width=120, placeholder_text="320", height=34)
        self._density_entry.pack(side="left", padx=(0, 8))
        SecondaryButton(density_inner, text="Apply", width=88, height=34,
                        command=self._set_density).pack(side="left", padx=(0, 6))
        SecondaryButton(density_inner, text="Reset", width=88, height=34,
                        command=self._reset_density).pack(side="left")

        # ── Other sections (paired columns, same rows) ──────────────────────────
        section("Location",          2, col=0)
        section("Developer Options", 2, col=1)
        btn("Enable GPS",              self._gps_on,           3, 0)
        btn("Show Developer Options",  self._dev_options_show, 3, 1)
        btn("Disable GPS",             self._gps_off,          4, 0)
        btn("Hide Developer Options",  self._dev_options_hide, 4, 1)

        section("Ambient Display", 5, col=0)
        section("Play Protect",    5, col=1)
        btn("Enable Ambient Display",  self._ambient_on,        6, 0)
        btn("Enable Play Protect",     self._play_protect_on,   6, 1)
        btn("Disable Ambient Display", self._ambient_off,       7, 0)
        btn("Disable Play Protect",    self._play_protect_off,  7, 1)

        section("Date & Time", 8, col=1)
        btn("Repair NTP Server", self._repair_ntp, 9, 1)

    def _set_rotation(self, val):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "system", "user_rotation", val, serial=self.serial)
            self._log(f"Screen rotation set to {val}.")
        threading.Thread(target=run, daemon=True).start()

    def _set_screen_timeout(self, ms):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "system", "screen_off_timeout", ms, serial=self.serial)
            self._log(f"Screen timeout set to {ms}ms.")
        threading.Thread(target=run, daemon=True).start()

    def _set_density(self):
        if not self._require_connection():
            return
        val = self._density_entry.get().strip()
        if not val.isdigit():
            self._log("Enter a numeric density value (e.g. 320).")
            return
        def run():
            result = adb("shell", "wm", "density", val, serial=self.serial)
            self._log(result or f"Density set to {val}.")
        threading.Thread(target=run, daemon=True).start()

    def _reset_density(self):
        if not self._require_connection():
            return
        def run():
            result = adb("shell", "wm", "density", "reset", serial=self.serial)
            self._log(result or "Density reset to default.")
        threading.Thread(target=run, daemon=True).start()

    def _dev_options_show(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "global", "development_settings_enabled", "1", serial=self.serial)
            self._log("Developer options visible.")
        threading.Thread(target=run, daemon=True).start()

    def _dev_options_hide(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "global", "development_settings_enabled", "0", serial=self.serial)
            self._log("Developer options hidden.")
        threading.Thread(target=run, daemon=True).start()

    def _gps_on(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "secure", "location_mode", "3", serial=self.serial)
            self._log("GPS enabled.")
        threading.Thread(target=run, daemon=True).start()

    def _gps_off(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "secure", "location_mode", "0", serial=self.serial)
            self._log("GPS disabled.")
        threading.Thread(target=run, daemon=True).start()

    def _play_protect_on(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "global", "package_verifier_enable", "1", serial=self.serial)
            self._log("Play Protect enabled.")
        threading.Thread(target=run, daemon=True).start()

    def _play_protect_off(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "global", "package_verifier_enable", "0", serial=self.serial)
            self._log("Play Protect disabled.")
        threading.Thread(target=run, daemon=True).start()

    def _ambient_on(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "secure", "doze_enabled", "1", serial=self.serial)
            self._log("Ambient display enabled.")
        threading.Thread(target=run, daemon=True).start()

    def _ambient_off(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "settings", "put", "secure", "doze_enabled", "0", serial=self.serial)
            self._log("Ambient display disabled.")
        threading.Thread(target=run, daemon=True).start()

    def _repair_ntp(self):
        if not self._require_connection():
            return
        def run():
            self._log("Setting NTP server to pool.ntp.org...")
            adb("shell", "settings", "put", "global", "ntp_server", "pool.ntp.org", serial=self.serial)
            adb("shell", "am", "broadcast", "-a", "android.intent.action.TIME_TICK", serial=self.serial)
            self._log("NTP server updated.")
        threading.Thread(target=run, daemon=True).start()

    def _build_tools_tab(self):
        tab = _scrollable(self._tab_frames["Tools"])
        tab.grid_columnconfigure((0, 1, 2), weight=1)

        def section(text, row, col):
            ctk.CTkLabel(tab, text=text, font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                         text_color=TEXT_MUTED).grid(
                row=row, column=col, padx=10, pady=(12, 4), sticky="w")

        section("Power", 0, 0)
        SecondaryButton(tab, text="Wake Device", width=190, height=36,
                        command=lambda: self._keyevent(224)).grid(row=1, column=0, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Sleep / Stand-by", width=190, height=36,
                        command=lambda: self._keyevent(223)).grid(row=2, column=0, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Soft Reboot", width=190, height=36,
                        command=self._reboot_soft).grid(row=3, column=0, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Reboot to Recovery", width=190, height=36,
                        command=self._reboot_recovery).grid(row=4, column=0, padx=8, pady=3, sticky="w")

        section("Navigation", 0, 1)
        SecondaryButton(tab, text="Notification Curtain", width=190, height=36,
                        command=self._notification_curtain).grid(row=1, column=1, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Open Google Search (Weather)", width=190, height=36,
                        command=self._google_search_weather).grid(row=2, column=1, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Open System Updates", width=190, height=36,
                        command=self._system_updates).grid(row=3, column=1, padx=8, pady=3, sticky="w")
        SecondaryButton(tab, text="Disconnect All ADB", width=190, height=36,
                        command=self._adb_disconnect_all).grid(row=4, column=1, padx=8, pady=3, sticky="w")

        section("Settings Shortcuts", 0, 2)
        for i, (label, action) in enumerate([
            ("Display Settings",    "android.settings.DISPLAY_SETTINGS"),
            ("Wi-Fi Settings",      "android.settings.WIFI_SETTINGS"),
            ("Bluetooth Settings",  "android.settings.BLUETOOTH_SETTINGS"),
            ("App Settings",        "android.settings.MANAGE_APPLICATIONS_SETTINGS"),
            ("Developer Options",   "com.android.settings.APPLICATION_DEVELOPMENT_SETTINGS"),
        ]):
            a = action
            SecondaryButton(tab, text=label, width=190, height=36,
                            command=lambda act=a: self._open_settings(act)).grid(
                            row=i + 1, column=2, padx=8, pady=3, sticky="w")

        ctk.CTkLabel(tab, text="Launch App (package name)",
                     font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                     text_color=TEXT_MUTED).grid(
            row=5, column=0, columnspan=2, padx=10, pady=(14, 4), sticky="w")
        launch_row = ctk.CTkFrame(tab, fg_color="transparent")
        launch_row.grid(row=6, column=0, columnspan=2, padx=10, pady=2, sticky="ew")
        launch_row.grid_columnconfigure(0, weight=1)
        self._launch_pkg_entry = _RoundEntry(launch_row, placeholder_text="com.example.app")
        self._launch_pkg_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._launch_pkg_entry.bind("<Return>", lambda e: self._launch_app())
        SecondaryButton(launch_row, text="Launch", width=90, height=36,
                        command=self._launch_app).grid(row=0, column=1)

        ctk.CTkLabel(tab, text="Send Text to Device",
                     font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                     text_color=TEXT_MUTED).grid(
            row=7, column=0, columnspan=2, padx=10, pady=(12, 4), sticky="w")
        send_row = ctk.CTkFrame(tab, fg_color="transparent")
        send_row.grid(row=8, column=0, columnspan=2, padx=10, pady=2, sticky="ew")
        send_row.grid_columnconfigure(0, weight=1)
        self._send_text_entry = _RoundEntry(send_row, placeholder_text="Type text to send...")
        self._send_text_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._send_text_entry.bind("<Return>", lambda e: self._send_text())
        SecondaryButton(send_row, text="Send", width=80, height=36,
                        command=self._send_text).grid(row=0, column=1)

        ctk.CTkLabel(tab, text="ADB Console",
                     font=ctk.CTkFont(family=_FONT, size=12, weight="bold"),
                     text_color=TEXT_MUTED).grid(
            row=9, column=0, columnspan=3, padx=10, pady=(12, 4), sticky="w")
        adb_row = ctk.CTkFrame(tab, fg_color="transparent")
        adb_row.grid(row=10, column=0, columnspan=3, padx=10, pady=(2, 10), sticky="ew")
        adb_row.grid_columnconfigure(0, weight=1)
        self._adb_cmd_entry = _RoundEntry(adb_row, placeholder_text="shell input keyevent 3   (omit 'adb')")
        self._adb_cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._adb_cmd_entry.bind("<Return>", lambda e: self._run_adb_console())
        SecondaryButton(adb_row, text="Run", width=70, height=36,
                        command=self._run_adb_console).grid(row=0, column=1)

    def _build_media_tab(self):
        tab = _scrollable(self._tab_frames["Media"])
        tab.grid_columnconfigure(0, weight=1)

        scrcpy_frame = Panel(tab)
        scrcpy_frame.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(scrcpy_frame, text="ScrCpy — View & Control",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).pack(padx=14, pady=(12, 2), anchor="w")
        ctk.CTkLabel(scrcpy_frame, text="Place scrcpy.exe in bin/ or install to PATH.",
                     text_color=TEXT_DISABLED, font=ctk.CTkFont(family=_FONT, size=11)).pack(padx=14, pady=(0, 6), anchor="w")
        btn_row = ctk.CTkFrame(scrcpy_frame, fg_color="transparent")
        btn_row.pack(padx=12, pady=(0, 12), anchor="w")
        PrimaryButton(btn_row, text="Launch ScrCpy", width=150,
                      command=self._launch_scrcpy).pack(side="left", padx=(0, 8))
        SecondaryButton(btn_row, text="Download ScrCpy", width=150,
                        command=lambda: webbrowser.open("https://github.com/Genymobile/scrcpy/releases/latest")).pack(side="left")

        rec_frame = Panel(tab)
        rec_frame.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(rec_frame, text="Screen Recording",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).pack(padx=14, pady=(12, 2), anchor="w")
        self._rec_status = ctk.CTkLabel(rec_frame, text="Not recording.",
                                         text_color=TEXT_DISABLED, font=ctk.CTkFont(family=_FONT, size=11))
        self._rec_status.pack(padx=14, pady=(0, 6), anchor="w")
        rec_btn_row = ctk.CTkFrame(rec_frame, fg_color="transparent")
        rec_btn_row.pack(padx=12, pady=(0, 12), anchor="w")
        self._rec_start_btn = SuccessButton(rec_btn_row, text="Start Recording", width=150,
                                             command=self._start_recording)
        self._rec_start_btn.pack(side="left", padx=(0, 8))
        self._rec_stop_btn = SecondaryButton(rec_btn_row, text="Stop & Pull", width=150,
                                              state="disabled", command=self._stop_recording)
        self._rec_stop_btn.pack(side="left")
        self._recording = False

        ft_frame = Panel(tab)
        ft_frame.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")
        ft_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ft_frame, text="File Transfer",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                     text_color=TEXT).grid(
                     row=0, column=0, columnspan=3, padx=14, pady=(12, 4), sticky="w")

        ctk.CTkLabel(ft_frame, text="Send file to /sdcard/:",
                     font=ctk.CTkFont(family=_FONT, size=11), text_color=TEXT_MUTED).grid(
            row=1, column=0, padx=14, pady=(0, 2), sticky="w")
        push_row = ctk.CTkFrame(ft_frame, fg_color="transparent")
        push_row.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")
        push_row.grid_columnconfigure(0, weight=1)
        self._push_path_var = ctk.StringVar(value="No file selected")
        ctk.CTkLabel(push_row, textvariable=self._push_path_var,
                     text_color=TEXT_DISABLED, font=ctk.CTkFont(family=_FONT, size=11)).grid(
            row=0, column=0, sticky="w")
        SecondaryButton(push_row, text="Browse", width=80, height=36,
                        command=self._browse_push_file).grid(row=0, column=1, padx=(6, 4))
        SuccessButton(push_row, text="Send", width=80, height=36,
                      command=self._push_file).grid(row=0, column=2)

        ctk.CTkLabel(ft_frame, text="Download file from device:",
                     font=ctk.CTkFont(family=_FONT, size=11), text_color=TEXT_MUTED).grid(
            row=3, column=0, padx=14, pady=(4, 2), sticky="w")
        pull_row = ctk.CTkFrame(ft_frame, fg_color="transparent")
        pull_row.grid(row=4, column=0, padx=12, pady=(0, 12), sticky="ew")
        pull_row.grid_columnconfigure(0, weight=1)
        self._pull_path_entry = _RoundEntry(pull_row, placeholder_text="/sdcard/Download/file.mp4")
        self._pull_path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        SecondaryButton(pull_row, text="Browse device", width=120, height=36,
                        command=self._open_device_browser).grid(row=0, column=1, padx=(0, 6))
        SecondaryButton(pull_row, text="Download", width=100, height=36,
                        command=self._pull_file).grid(row=0, column=2)

    def _launch_scrcpy(self):
        if not self._require_connection():
            return
        scrcpy = shutil.which("scrcpy") or str(BIN_DIR / "scrcpy.exe")
        if not Path(scrcpy).exists():
            self._log("scrcpy not found. Download it and place scrcpy.exe in bin/.")
            webbrowser.open("https://github.com/Genymobile/scrcpy/releases/latest")
            return
        try:
            subprocess.Popen([scrcpy, "--serial", self.serial],
                             creationflags=_NO_WINDOW)
            self._log("ScrCpy launched.")
        except Exception as e:
            self._log(f"Failed to launch scrcpy: {e}")

    def _start_recording(self):
        if not self._require_connection():
            return
        self._recording = True
        self._rec_start_btn.configure(state="disabled")
        self._rec_stop_btn.configure(state="normal")
        self._rec_status.configure(text="Recording...", text_color=DANGER)
        def run():
            adb("shell", "screenrecord", "--bit-rate", "4000000",
                "/sdcard/_tv_tools_rec.mp4", serial=self.serial, timeout=600)
        threading.Thread(target=run, daemon=True).start()

    def _stop_recording(self):
        if not self._require_connection():
            return
        self._recording = False
        self._rec_start_btn.configure(state="normal")
        self._rec_stop_btn.configure(state="disabled")
        self._rec_status.configure(text="Stopping...", text_color=WARNING)
        def run():
            adb("shell", "pkill", "-INT", "screenrecord", serial=self.serial)
            import time; time.sleep(1)
            dest = filedialog.asksaveasfilename(
                defaultextension=".mp4",
                filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
                initialfile="recording.mp4")
            if dest:
                self._log("Pulling recording...")
                adb("pull", "/sdcard/_tv_tools_rec.mp4", dest, serial=self.serial, timeout=120)
                self._log(f"Saved to {dest}")
            adb("shell", "rm", "-f", "/sdcard/_tv_tools_rec.mp4", serial=self.serial)
            self.after(0, lambda: self._rec_status.configure(text="Not recording.", text_color=TEXT_DISABLED))
        threading.Thread(target=run, daemon=True).start()

    def _browse_push_file(self):
        path = filedialog.askopenfilename(title="Select file to send")
        if path:
            self._push_path_var.set(path)
            self._push_local_path = path

    def _push_file(self):
        if not self._require_connection():
            return
        path = getattr(self, "_push_local_path", None)
        if not path or not os.path.exists(path):
            self._log("No file selected.")
            return
        def run():
            fname = os.path.basename(path)
            self._log(f"Sending {fname} to /sdcard/...")
            result = adb("push", path, f"/sdcard/{fname}", serial=self.serial, timeout=120)
            self._log(result or "File sent.")
        threading.Thread(target=run, daemon=True).start()

    def _pull_file(self):
        if not self._require_connection():
            return
        device_path = self._pull_path_entry.get().strip()
        if not device_path:
            self._log("Enter a device file path.")
            return
        dest_dir = filedialog.askdirectory(title="Select destination folder")
        if not dest_dir:
            return
        def run():
            self._log(f"Downloading {device_path}...")
            result = adb("pull", device_path, dest_dir, serial=self.serial, timeout=120)
            self._log(result or "File downloaded.")
        threading.Thread(target=run, daemon=True).start()

    def _open_device_browser(self):
        """Navigable file browser over the device's storage. Tap a folder to
        enter it, tap a file to select it, then pull the selection to the PC."""
        if not self._require_connection():
            return

        win = ctk.CTkToplevel(self)
        win.title("Browse Device Files")
        win.geometry("560x580")
        win.configure(fg_color=APP_BG)
        win.lift()
        win.after(50, win.focus)

        state = {"cwd": "/sdcard/", "selected": "/sdcard/", "sel_btn": None}

        top = ctk.CTkFrame(win, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 2))
        top.grid_columnconfigure(0, weight=1)
        path_var = ctk.StringVar(value=state["cwd"])
        path_entry = _RoundEntry(top, textvariable=path_var)
        path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        SecondaryButton(top, text="Go", width=56, height=36,
                        command=lambda: go_to(path_var.get())).grid(row=0, column=1, padx=(0, 6))
        SecondaryButton(top, text="Up", width=56, height=36,
                        command=lambda: go_up()).grid(row=0, column=2, padx=(0, 6))
        SecondaryButton(top, text="↻", width=44, height=36,
                        command=lambda: load()).grid(row=0, column=3)

        ctk.CTkLabel(win, text="Folders (blue, end in /) open · files are selectable.",
                     text_color=TEXT_DISABLED, anchor="w",
                     font=ctk.CTkFont(family=_FONT, size=11)).pack(fill="x", padx=14, pady=(0, 2))

        list_frame = ctk.CTkScrollableFrame(
            win, fg_color=SURFACE_1, corner_radius=RADIUS_MD,
            border_width=1, border_color=BORDER,
            scrollbar_button_color=SURFACE_3,
            scrollbar_button_hover_color=SECONDARY_HOVER)
        list_frame.pack(fill="both", expand=True, padx=12, pady=4)

        sel_var = ctk.StringVar(value=f"Selected: {state['selected']}")
        ctk.CTkLabel(win, textvariable=sel_var, anchor="w", text_color=TEXT_MUTED,
                     font=ctk.CTkFont(family=_FONT, size=11)).pack(fill="x", padx=14, pady=(2, 0))
        status_var = ctk.StringVar(value="")
        ctk.CTkLabel(win, textvariable=status_var, anchor="w", text_color=TEXT_DISABLED,
                     font=ctk.CTkFont(family=_FONT, size=11)).pack(fill="x", padx=14, pady=(0, 2))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 12))
        SecondaryButton(btn_row, text="Use Path", width=100, height=36,
                        command=lambda: use_path()).pack(side="left")
        SecondaryButton(btn_row, text="Close", width=88, height=36,
                        command=win.destroy).pack(side="right")
        dl_btn = SuccessButton(btn_row, text="Download to PC", width=150, height=36,
                               command=lambda: download())
        dl_btn.pack(side="right", padx=(0, 8))

        def set_selected(path, btn=None):
            state["selected"] = path
            sel_var.set(f"Selected: {path}")
            if state["sel_btn"] is not None:
                try:
                    state["sel_btn"].configure(fg_color=SURFACE_1, border_color=BORDER)
                except Exception:
                    pass
            state["sel_btn"] = btn
            if btn is not None:
                btn.configure(fg_color=PRIMARY_MUTED, border_color=PRIMARY)

        def render(entries):
            for w in list_frame.winfo_children():
                w.destroy()
            state["sel_btn"] = None
            if not entries:
                ctk.CTkLabel(list_frame, text="(empty folder)", text_color=TEXT_DISABLED,
                             font=ctk.CTkFont(family=_FONT, size=12)).pack(pady=20)
                return
            for name, is_dir in entries:
                text = (name + "/") if is_dir else name
                b = ctk.CTkButton(
                    list_frame, text=text, anchor="w", height=32,
                    corner_radius=RADIUS_SM, fg_color=SURFACE_1,
                    hover_color=SURFACE_2,
                    text_color=(PRIMARY if is_dir else TEXT),
                    border_width=1, border_color=BORDER,
                    font=ctk.CTkFont(family=_FONT, size=12))
                if is_dir:
                    b.configure(command=lambda n=name: enter_dir(n))
                else:
                    b.configure(command=lambda n=name, bt=b: set_selected(state["cwd"] + n, bt))
                b.pack(fill="x", padx=6, pady=2)

        def load():
            cwd = state["cwd"]
            path_var.set(cwd)
            set_selected(cwd)  # default: download the current folder if no file is picked
            for w in list_frame.winfo_children():
                w.destroy()
            ctk.CTkLabel(list_frame, text="Loading…", text_color=TEXT_MUTED,
                         font=ctk.CTkFont(family=_FONT, size=12)).pack(pady=20)

            def run():
                out = adb("shell", "ls", "-1ap", _dev_quote(cwd), serial=self.serial, timeout=20)
                entries, err = _parse_ls(out)

                def apply():
                    if err:
                        for w in list_frame.winfo_children():
                            w.destroy()
                        ctk.CTkLabel(list_frame, text=err, text_color=DANGER,
                                     font=ctk.CTkFont(family=_FONT, size=12),
                                     wraplength=480, justify="left").pack(pady=20, padx=10)
                    else:
                        render(entries)
                self.after(0, apply)
            threading.Thread(target=run, daemon=True).start()

        def enter_dir(name):
            state["cwd"] = state["cwd"] + name + "/"
            load()

        def go_up():
            cwd = state["cwd"].rstrip("/")
            if not cwd:
                return
            parent = cwd.rsplit("/", 1)[0]
            state["cwd"] = (parent + "/") if parent else "/"
            load()

        def go_to(path):
            path = path.strip()
            if not path:
                return
            if not path.endswith("/"):
                path += "/"
            state["cwd"] = path
            load()

        def use_path():
            self._pull_path_entry.delete(0, "end")
            self._pull_path_entry.insert(0, state["selected"])
            win.destroy()

        def download():
            sel = state["selected"]
            dest = filedialog.askdirectory(title="Select destination folder")
            if not dest:
                return
            dl_btn.configure(state="disabled", text="Downloading…")
            status_var.set(f"Pulling {sel} …")

            def run():
                self._log(f"Pulling {sel} from device...")
                result = adb("pull", sel, dest, serial=self.serial, timeout=300)
                self._log(result or "Download complete.")

                def done():
                    last = result.splitlines()[-1] if result else "Done."
                    status_var.set(last)
                    try:
                        dl_btn.configure(state="normal", text="Download to PC")
                    except Exception:
                        pass
                self.after(0, done)
            threading.Thread(target=run, daemon=True).start()

        load()

    def _build_remote_tab(self):
        outer = _scrollable(self._tab_frames["Remote"])
        outer.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(outer, text="Remote Control",
                     font=ctk.CTkFont(family=_FONT, size=15, weight="bold"),
                     text_color=TEXT).grid(row=0, column=0, pady=(12, 8))

        remote_panel = Panel(outer)
        remote_panel.grid(row=1, column=0, pady=(0, 16), padx=16)

        for c in range(4):
            remote_panel.grid_columnconfigure(c, weight=1)

        def _sb(text, code, row, col, w=112, h=52):
            SecondaryButton(remote_panel, text=text, width=w, height=h,
                            command=lambda c=code: self._keyevent(c)).grid(
                row=row, column=col, padx=6, pady=6)

        # D-pad (columns 0-2, rows 1-3)
        _sb("▲", 19, 1, 1)
        _sb("◀", 21, 2, 0)
        PrimaryButton(remote_panel, text="OK", width=112, height=52,
                      command=lambda: self._keyevent(23)).grid(row=2, column=1, padx=6, pady=6)
        _sb("▶", 22, 2, 2)
        _sb("▼", 20, 3, 1)

        # Volume group (column 3, rows 1-3)
        _sb("Vol+", 24, 1, 3, w=104, h=48)
        _sb("Mute", 164, 2, 3, w=104, h=48)
        _sb("Vol-", 25, 3, 3, w=104, h=48)

        # Nav row (row 4)
        _sb("Back",  4, 4, 0, w=112, h=48)
        _sb("Home",  3, 4, 1, w=112, h=48)
        _sb("Menu", 82, 4, 2, w=112, h=48)

        # Media row (row 5)
        _sb("⏮", 88, 5, 0, w=112, h=48)
        _sb("⏯", 85, 5, 1, w=112, h=48)
        _sb("⏭", 87, 5, 2, w=112, h=48)

        # Power (row 6, separated)
        DangerButton(remote_panel, text="Power", width=112, height=48,
                     command=lambda: self._keyevent(26)).grid(
            row=6, column=1, padx=6, pady=(18, 12))

    def _build_screenshot_tab(self):
        tab = self._tab_frames["Screenshot"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=0)
        tab.grid_rowconfigure(1, weight=1)

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")

        PrimaryButton(btn_row, text="Take Screenshot", width=160,
                      command=self._take_screenshot).pack(side="left", padx=(0, 8))
        SecondaryButton(btn_row, text="Save As...", width=110,
                        command=self._save_screenshot).pack(side="left", padx=(0, 8))
        self._screenshot_label = ctk.CTkLabel(btn_row, text="No screenshot yet.",
                                              text_color=TEXT_DISABLED,
                                              font=ctk.CTkFont(family=_FONT, size=11))
        self._screenshot_label.pack(side="left")

        self._screenshot_canvas = ctk.CTkLabel(tab, text="",
                                                fg_color=SURFACE_1, corner_radius=RADIUS_XL)
        self._screenshot_canvas.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self._screenshot_image = None

    # ── device list ────────────────────────────────────────────────────────────
    def _populate_device_list(self):
        for w in self.device_list.winfo_children():
            w.destroy()
        self._device_radio_buttons.clear()

        if not self.history:
            ctk.CTkLabel(self.device_list, text="No devices.\nScan or add manually.",
                         text_color=TEXT_DISABLED, font=ctk.CTkFont(family=_FONT, size=10)).pack(pady=10)
            return

        for ip in sorted(self.history.keys(),
                         key=lambda x: [int(p) if p.isdigit() else 0 for p in x.split(".")]):
            meta     = self.history[ip]
            label    = meta.get("label", ip) if isinstance(meta, dict) else str(meta)
            port     = meta.get("port", 5555) if isinstance(meta, dict) else 5555
            seen     = _last_seen_str(meta.get("last_seen", "") if isinstance(meta, dict) else "")
            verified = meta.get("verified", False) if isinstance(meta, dict) else False
            wireless = meta.get("wireless", False) if isinstance(meta, dict) else False

            port_txt = f":{port}" if port != 5555 else ""
            suffix   = " ↝" if wireless else ""
            is_sel   = self.selected_ip.get() == ip

            card = Panel(
                self.device_list,
                fg_color=SURFACE_2 if is_sel else SURFACE_1,
                corner_radius=RADIUS_MD,
                border_width=1,
                border_color=PRIMARY if is_sel else BORDER,
            )
            card.pack(fill="x", padx=4, pady=3)
            card.grid_columnconfigure(0, weight=1)
            card.grid_columnconfigure(1, minsize=24)

            # Row 0: device name + × button on same line
            name_row = ctk.CTkFrame(card, fg_color="transparent")
            name_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(4, 0), padx=(10, 0))
            name_row.grid_columnconfigure(0, weight=1)

            name_lbl = ctk.CTkLabel(name_row,
                         text=label,
                         text_color=TEXT,
                         font=ctk.CTkFont(family=_FONT, size=13, weight="bold"),
                         anchor="w",
                         justify="left",
                         wraplength=155)
            name_lbl.grid(row=0, column=0, sticky="ew", padx=(0, 2))

            del_btn = ctk.CTkButton(
                name_row, text="×", width=18, height=18,
                corner_radius=6,
                fg_color="transparent",
                hover_color=SURFACE_3,
                text_color=TEXT_DISABLED,
                font=ctk.CTkFont(family=_FONT, size=13),
                command=lambda i=ip: self._delete_device(i),
            )
            del_btn.grid(row=0, column=1, padx=(0, 6), pady=(2, 0), sticky="ne")

            # Row 1: IP (subtitle)
            ip_lbl = ctk.CTkLabel(card,
                         text=f"{ip}{port_txt}{suffix}",
                         text_color=TEXT_MUTED,
                         font=ctk.CTkFont(family=_FONT, size=11),
                         height=16, anchor="w")
            ip_lbl.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=0)

            # Row 2: last seen
            seen_lbl = ctk.CTkLabel(card,
                         text=seen,
                         text_color=TEXT_DISABLED,
                         font=ctk.CTkFont(family=_FONT, size=10),
                         height=16, anchor="w")
            seen_lbl.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=(0, 4))

            # Select on click anywhere on card except the delete button
            def _select(event=None, i=ip):
                self.selected_ip.set(i)
                self._populate_device_list()
            for w in (card, name_row, name_lbl, ip_lbl, seen_lbl):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", _select)

            self._device_radio_buttons.append(card)

    def _delete_device(self, ip):
        delete_history(ip)
        self.history.pop(ip, None)
        self._populate_device_list()

    def _add_discovered(self, devices):
        """devices is a list of {ip, port, label, verified} dicts from scan_subnet."""
        for d in devices:
            ip = d["ip"]
            existing = self.history.get(ip, {})
            if not isinstance(existing, dict):
                existing = {"label": str(existing), "port": 5555, "last_seen": ""}
            label = d.get("label") or existing.get("label", ip)
            port  = d.get("port", 5555)
            # update history in memory and on disk
            existing.update({"label": label, "port": port,
                              "verified": d.get("verified", False),
                              "wireless": d.get("wireless", False)})
            self.history[ip] = existing
            save_history(ip, label, port=port, touch_seen=False)
        self.after(0, self._populate_device_list)

    # ── actions ────────────────────────────────────────────────────────────────
    def _start_adb_server(self):
        def run():
            if not ADB or adb("version").startswith("ERROR:"):
                self.after(0, self._prompt_adb_path)
                return
            self._log("Starting ADB server...")
            adb("kill-server")
            adb("start-server")
            ver = adb("version")
            self._log(f"ADB ready: {ver.splitlines()[0]}")
            self._probe_known_devices()
        threading.Thread(target=run, daemon=True).start()

    def _probe_known_devices(self):
        """Silently check which history devices are reachable; update verified flag."""
        devices = list(self.history.items())
        if not devices:
            return
        self._log(f"Probing {len(devices)} known device(s)...")
        reachable = []
        probe_threads = []

        def probe(ip, meta):
            port = meta.get("port", 5555) if isinstance(meta, dict) else 5555
            try:
                with socket.create_connection((ip, port), timeout=1):
                    serial = f"{ip}:{port}"
                    out = adb("connect", serial)
                    if "connected" in out.lower() or "already" in out.lower():
                        reachable.append((ip, port))
            except Exception:
                pass

        for ip, meta in devices:
            t = threading.Thread(target=probe, args=(ip, meta), daemon=True)
            t.start()
            probe_threads.append(t)
        for t in probe_threads:
            t.join()

        if reachable:
            self._log(f"  Reachable: {', '.join(f'{ip}:{p}' for ip, p in reachable)}")
            for ip, port in reachable:
                meta = self.history.get(ip, {})
                if isinstance(meta, dict):
                    meta["verified"] = True
            self.after(0, self._populate_device_list)

    def _prompt_adb_path(self):
        global ADB
        dialog = ctk.CTkToplevel(self)
        dialog.title("ADB Not Found")
        dialog.geometry("500x230")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.lift()

        ctk.CTkLabel(dialog, text="Android Debug Bridge (ADB) could not be found.",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold")).pack(pady=(20, 2), padx=20, anchor="w")
        ctk.CTkLabel(dialog, text="ADB is required to communicate with your Android TV device.",
                     text_color="gray").pack(padx=20, anchor="w")

        link = ctk.CTkLabel(dialog, text="  → Download Android SDK Platform Tools",
                            text_color="#4a9eff", cursor="hand2",
                            font=ctk.CTkFont(family=_FONT, size=12, underline=True))
        link.pack(padx=20, pady=(6, 14), anchor="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://developer.android.com/tools/releases/platform-tools"))

        path_row = ctk.CTkFrame(dialog, fg_color="transparent")
        path_row.pack(fill="x", padx=20, pady=(0, 14))
        path_row.grid_columnconfigure(0, weight=1)
        path_var = StringVar()
        _placeholder = ("Path to adb.exe  (e.g. C:\\platform-tools\\adb.exe)"
                        if sys.platform == "win32"
                        else "Path to adb  (e.g. /usr/local/bin/adb)")
        path_entry = ctk.CTkEntry(path_row, textvariable=path_var,
                                  placeholder_text=_placeholder)
        path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        def browse():
            _filetypes = ([("ADB executable", "adb.exe"), ("All files", "*.*")]
                          if sys.platform == "win32"
                          else [("All files", "*")])
            p = filedialog.askopenfilename(
                title="Locate adb",
                filetypes=_filetypes)
            if p:
                path_var.set(p)

        ctk.CTkButton(path_row, text="Browse…", width=90, command=browse).grid(row=0, column=1)

        def confirm():
            global ADB
            p = path_var.get().strip()
            if p and Path(p).exists():
                ADB = p
                try:
                    (DATA_DIR / "adb_path.txt").write_text(p)
                except Exception:
                    pass
                dialog.destroy()
                self._start_adb_server()
            else:
                path_entry.configure(border_color="red")

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=20)
        ctk.CTkButton(btn_row, text="Cancel", width=90, fg_color="gray40",
                      command=dialog.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_row, text="OK", width=90, command=confirm).pack(side="right")

    def _scan(self):
        subnet = self._subnet_var.get().strip().rstrip(".")
        if not subnet:
            self._log("Enter a subnet prefix (e.g. 192.168.1) before scanning.")
            return
        self.scan_btn.configure(state="disabled")
        self.scan_status.configure(text="Scanning + verifying…")
        self._log(f"Scanning {subnet}.0/24 — verifying Android devices...")

        def done():
            self.after(0, lambda: self.scan_btn.configure(state="normal"))
            self.after(0, lambda: self.scan_status.configure(text="Scan complete"))
            self._log("Scan complete.")

        scan_subnet(base=subnet, result_callback=self._add_discovered, done_callback=done)

    def _scan_mdns(self):
        self.scan_status.configure(text="mDNS scan…")
        self._log("Running mDNS scan for wireless debugging devices...")
        def run():
            results = scan_mdns()
            if results:
                self._log(f"mDNS: found {len(results)} wireless debug device(s).")
                self._add_discovered(results)
            else:
                self._log("mDNS: no wireless debugging devices found.")
            self.after(0, lambda: self.scan_status.configure(text=""))
        threading.Thread(target=run, daemon=True).start()

    def _connect(self):
        ip = self.selected_ip.get()
        if not ip:
            self._log("No device selected.")
            return
        meta = self.history.get(ip, {})
        port = meta.get("port", 5555) if isinstance(meta, dict) else 5555
        self._connect_to(ip, port)

    def _connect_manual(self):
        ip = self._manual_ip_var.get().strip()
        if not ip:
            self._log("Enter an IP address.")
            return
        try:
            port = int(self._manual_port_var.get().strip() or "5555")
        except ValueError:
            self._log("Port must be a number.")
            return
        self._connect_to(ip, port)

    def _connect_to(self, ip, port=5555):
        serial = f"{ip}:{port}"
        def run():
            self._log(f"Connecting to {serial}...")
            result = adb("connect", serial)
            self._log(result)
            if "connected" in result.lower() or "already" in result.lower():
                self.serial = serial
                self._refresh_info_async()
            else:
                self._log(f"Failed to connect to {serial}")
                self.after(0, lambda: self.log_frame.configure(border_color=DANGER))
        threading.Thread(target=run, daemon=True).start()

    def _disconnect(self):
        def run():
            self._log("Disconnecting...")
            if self.serial:
                adb("disconnect", self.serial)
            self.serial = None
            self.after(0, self._reset_connect_btn)
            self._log("Disconnected.")
        threading.Thread(target=run, daemon=True).start()

    def _reset_connect_btn(self):
        self.log_frame.configure(border_color=BORDER)
        self._set_tabs_enabled(False)
        self.connect_btn.configure(
            text="Connect", fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER, text_color=APP_BG,
            command=self._connect,
        )
        self.disconnect_btn.grid()

    def _open_pair_dialog(self):
        """Wireless debugging pairing dialog for Android 11+ (adb pair)."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Wireless Debugging Pair (Android 11+)")
        dlg.geometry("420x310")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.lift()

        ctk.CTkLabel(dlg, text="Pair via Wireless Debugging",
                     font=ctk.CTkFont(family=_FONT, size=13, weight="bold")).pack(padx=20, pady=(16, 4), anchor="w")
        ctk.CTkLabel(dlg, text=(
            "On the TV: Settings → Developer Options → Wireless Debugging\n"
            "Tap 'Pair device with pairing code' to get the pairing port + code."),
            text_color="gray", font=ctk.CTkFont(family=_FONT, size=11), justify="left").pack(padx=20, pady=(0, 10), anchor="w")

        form = ctk.CTkFrame(dlg, fg_color="transparent")
        form.pack(fill="x", padx=20)
        form.grid_columnconfigure(1, weight=1)

        fields = [("IP Address", "pair_ip"), ("Pairing Port", "pair_port"),
                  ("6-digit Code", "pair_code"), ("Connect Port", "pair_conn_port")]
        self._pair_vars = {}
        hints = ["192.168.1.x", "e.g. 42389", "e.g. 123456", "shown on Wireless Debug screen"]
        for i, ((lbl, key), hint) in enumerate(zip(fields, hints)):
            ctk.CTkLabel(form, text=lbl + ":", width=110, anchor="e",
                         font=ctk.CTkFont(family=_FONT, size=11)).grid(row=i, column=0, padx=(0, 8), pady=4, sticky="e")
            var = StringVar()
            self._pair_vars[key] = var
            ctk.CTkEntry(form, textvariable=var, placeholder_text=hint,
                         height=28, font=ctk.CTkFont(family=_FONT, size=11)).grid(row=i, column=1, sticky="ew", pady=4)

        status = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(family=_FONT, size=11), text_color="gray")
        status.pack(padx=20, pady=(8, 0), anchor="w")

        def do_pair():
            ip   = self._pair_vars["pair_ip"].get().strip()
            port = self._pair_vars["pair_port"].get().strip()
            code = self._pair_vars["pair_code"].get().strip()
            conn = self._pair_vars["pair_conn_port"].get().strip()
            if not ip or not port or not code:
                status.configure(text="IP, pairing port, and code are required.", text_color="red")
                return
            pair_btn.configure(state="disabled", text="Pairing…")
            def run():
                self._log(f"Pairing with {ip}:{port} code={code}...")
                result = adb("pair", f"{ip}:{port}", code)
                self._log(result)
                if "successfully" in result.lower() or "paired" in result.lower():
                    conn_port = conn or port
                    self._log(f"Pair OK. Connecting to {ip}:{conn_port}...")
                    r2 = adb("connect", f"{ip}:{conn_port}")
                    self._log(r2)
                    if "connected" in r2.lower() or "already" in r2.lower():
                        self.serial = f"{ip}:{conn_port}"
                        save_history(ip, "(wireless debug)", port=int(conn_port))
                        self.history[ip] = {"label": "(wireless debug)",
                                            "port": int(conn_port),
                                            "last_seen": "", "verified": True, "wireless": True}
                        self.after(0, self._populate_device_list)
                        self.after(0, lambda: status.configure(text="Connected!", text_color="green"))
                        self._refresh_info_async()
                        return
                self.after(0, lambda: status.configure(text="Pairing failed — check code/port.", text_color="red"))
                self.after(0, lambda: pair_btn.configure(state="normal", text="Pair & Connect"))
            threading.Thread(target=run, daemon=True).start()

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(8, 16))
        ctk.CTkButton(btn_row, text="Cancel", width=90, fg_color="gray40",
                      command=dlg.destroy).pack(side="right", padx=(8, 0))
        pair_btn = ctk.CTkButton(btn_row, text="Pair & Connect", width=120, command=do_pair)
        pair_btn.pack(side="right")

    def _refresh_info(self):
        if not self.serial:
            self._log("Not connected.")
            return
        threading.Thread(target=self._refresh_info_async, daemon=True).start()

    def _refresh_info_async(self):
        s = self.serial
        manufacturer = _clean_prop(adb_out("shell", "getprop", "ro.product.manufacturer", serial=s))
        model        = _clean_prop(adb_out("shell", "getprop", "ro.product.model", serial=s))
        android_ver  = _clean_prop(adb_out("shell", "getprop", "ro.build.version.release", serial=s))
        build_id     = _clean_prop(adb_out("shell", "getprop", "ro.build.display.id", serial=s))
        serial_no    = _clean_prop(adb_out("shell", "getprop", "ro.serialno", serial=s))
        abi          = _clean_prop(adb_out("shell", "getprop", "ro.product.cpu.abi", serial=s))
        dns          = adb_out("shell", "settings", "get", "global", "private_dns_mode", serial=s)
        pkgv         = adb_out("shell", "settings", "get", "global", "package_verifier_user_consent", serial=s)

        wm_raw = adb_out("shell", "wm", "size", serial=s)
        resolution = "—"
        for line in wm_raw.splitlines():
            if "Physical size" in line or "Override size" in line:
                resolution = line.split(":")[-1].strip()
                break
            if "x" in line.lower():
                resolution = line.strip()
                break

        batt_raw = adb_out("shell", "dumpsys", "battery", serial=s, timeout=10)
        battery = "—"
        for line in batt_raw.splitlines():
            if "level:" in line:
                battery = line.split(":")[-1].strip() + "%"
                break

        ime_raw = adb_out("shell", "dumpsys", "input_method", serial=s, timeout=15)
        ime = "—"
        for line in ime_raw.splitlines():
            if "mCurMethodId" in line:
                ime = line.split("=")[-1].strip()
                break

        if manufacturer and model.lower().startswith(manufacturer.lower()):
            label = model.strip()
        else:
            label = f"{manufacturer} {model}".strip()
        ip    = s.split(":")[0]
        port  = int(s.split(":")[-1]) if ":" in s else 5555
        if label:
            save_history(ip, label, port=port, touch_seen=True)
            now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
            existing = self.history.get(ip, {})
            if isinstance(existing, dict):
                existing.update({"label": label, "port": port, "last_seen": now_str, "verified": True})
            else:
                existing = {"label": label, "port": port, "last_seen": now_str, "verified": True}
            self.history[ip] = existing

        def update():
            self.info_manufacturer.configure(text=manufacturer or "—")
            self.info_model.configure(text=model or "—")
            self.info_android.configure(text=android_ver or "—")
            self.info_build.configure(text=build_id or "—")
            self.info_serial.configure(text=serial_no or "—")
            self.info_resolution.configure(text=resolution)
            self.info_battery.configure(text=battery)
            self.info_abi.configure(text=abi or "—")
            self.info_dns.configure(text=dns or "—")
            self.info_pkgverifier.configure(text=pkgv or "—")
            self.info_ime.configure(text=ime)
            self.log_frame.configure(border_color=SUCCESS)
            self._set_tabs_enabled(True)
            self._populate_device_list()
            self.connect_btn.configure(
                text="Disconnect", fg_color=DANGER_BG,
                hover_color=DANGER_HOVER, text_color=DANGER,
                command=self._disconnect,
            )
            self.disconnect_btn.grid_remove()

        self.after(0, update)
        self._log(f"Device: {label}  |  Android: {android_ver}  |  ABI: {abi}  |  Battery: {battery}")

    def _require_connection(self):
        if not self.serial:
            self._log("Not connected to any device.")
            return False
        return True

    def _list_packages(self, flag):
        if not self._require_connection():
            return
        def run():
            args = ["shell", "pm", "list", "packages"]
            if flag:
                args.append(flag)
            self._log(f"Listing packages {flag or '(all)'}...")
            result = adb_out(*args, serial=self.serial, timeout=30)
            self.after(0, lambda: self._show_packages(result))
        threading.Thread(target=run, daemon=True).start()

    def _show_packages(self, text):
        self.pkg_listbox.delete(0, "end")
        for line in text.splitlines():
            pkg = line.strip()
            if pkg.startswith("package:"):
                pkg = pkg[len("package:"):]
            if pkg:
                self.pkg_listbox.insert("end", pkg)
        if self.pkg_listbox.size() == 0:
            self._pkg_empty_state.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self._pkg_empty_state.place_forget()

    def _selected_package(self):
        sel = self.pkg_listbox.curselection()
        if not sel:
            self._log("No package selected.")
            return None
        return self.pkg_listbox.get(sel[0])

    def _pkg_uninstall(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            self._log(f"Uninstalling {pkg}...")
            result = adb("shell", "pm", "uninstall", "--user", "0", pkg, serial=self.serial)
            self._log(result or "Done.")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_disable(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            self._log(f"Disabling {pkg}...")
            result = adb("shell", "pm", "disable-user", "--user", "0", pkg, serial=self.serial)
            self._log(result or "Done.")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_enable(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            self._log(f"Enabling {pkg}...")
            result = adb("shell", "pm", "enable", pkg, serial=self.serial)
            self._log(result or "Done.")
        threading.Thread(target=run, daemon=True).start()

    def _list_playstore_pkgs(self):
        if not self._require_connection():
            return
        def run():
            self._log("Listing Play Store packages...")
            raw = adb_out("shell", "pm", "list", "packages", "-i", serial=self.serial, timeout=30)
            lines = [l for l in raw.splitlines() if "installer=com.android.vending" in l]
            pkgs = [l.split("package:")[-1].split(" ")[0] for l in lines]
            self.after(0, lambda: self._show_packages("\n".join(f"package:{p}" for p in pkgs)))
            self._log(f"Found {len(pkgs)} Play Store packages.")
        threading.Thread(target=run, daemon=True).start()

    def _list_sideloaded_pkgs(self):
        if not self._require_connection():
            return
        def run():
            self._log("Listing sideloaded packages...")
            raw = adb_out("shell", "pm", "list", "packages", "-i", "-3", serial=self.serial, timeout=30)
            lines = [l for l in raw.splitlines()
                     if "installer=com.android.vending" not in l and l.startswith("package:")]
            pkgs = [l.split("package:")[-1].split(" ")[0] for l in lines]
            self.after(0, lambda: self._show_packages("\n".join(f"package:{p}" for p in pkgs)))
            self._log(f"Found {len(pkgs)} sideloaded packages.")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_version(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            raw = adb_out("shell", "dumpsys", "package", pkg, serial=self.serial, timeout=15)
            version = "unknown"
            for line in raw.splitlines():
                if "versionName=" in line:
                    version = line.strip().split("versionName=")[-1].split(" ")[0]
                    break
            self._log(f"{pkg}  version: {version}")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_save_apk(self):
        """Pull the installed APK(s) for the selected package to the PC.
        Resolves the on-device paths via `pm path`, which returns the base APK
        plus any split APKs for app-bundle installs."""
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        dest_dir = filedialog.askdirectory(title=f"Save APK for {pkg} — choose folder")
        if not dest_dir:
            return

        def run():
            self._log(f"Resolving APK path for {pkg}...")
            raw = adb_out("shell", "pm", "path", pkg, serial=self.serial, timeout=15)
            paths = [l.strip()[len("package:"):] for l in raw.splitlines()
                     if l.strip().startswith("package:")]
            if not paths:
                self._log(f"Could not resolve APK path for {pkg}: {raw.strip() or 'no path returned'}")
                return

            if len(paths) == 1:
                # Single APK — save directly as <package>.apk
                targets = [(paths[0], os.path.join(dest_dir, f"{pkg}.apk"))]
            else:
                # Split APKs (app bundle) — keep the set together in <package>/
                sub = os.path.join(dest_dir, pkg)
                try:
                    os.makedirs(sub, exist_ok=True)
                except Exception as e:
                    self._log(f"Could not create folder {sub}: {e}")
                    return
                targets = [(p, os.path.join(sub, os.path.basename(p))) for p in paths]

            self._log(f"Pulling {len(targets)} APK file(s) for {pkg}...")
            ok = 0
            for remote, local in targets:
                result = adb("pull", remote, local, serial=self.serial, timeout=300)
                if os.path.exists(local):
                    ok += 1
                    self._log(f"  Saved {os.path.basename(local)}")
                else:
                    self._log(f"  Failed: {remote} — {result.strip()}")
            self._log(f"Saved {ok}/{len(targets)} APK file(s) to {dest_dir}")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_clear_cache(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            self._log(f"Clearing cache for {pkg}...")
            result = adb("shell", "pm", "clear-cache", pkg, serial=self.serial)
            self._log(result or "Cache cleared.")
        threading.Thread(target=run, daemon=True).start()

    def _pkg_clear_data(self):
        if not self._require_connection():
            return
        pkg = self._selected_package()
        if not pkg:
            return
        def run():
            self._log(f"Clearing data for {pkg}...")
            result = adb("shell", "pm", "clear", pkg, serial=self.serial)
            self._log(result or "Data cleared.")
        threading.Thread(target=run, daemon=True).start()

    def _compile_speed_profile(self):
        if not self._require_connection():
            return
        def run():
            self._log("Compiling speed profile (this takes a few minutes)...")
            result = adb("shell", "cmd", "package", "compile", "-m", "speed-profile", "-a",
                        serial=self.serial, timeout=300)
            self._log(result or "Done.")
        threading.Thread(target=run, daemon=True).start()

    def _enable_freezer(self):
        if not self._require_connection():
            return
        def run():
            self._log("Enabling app freezer...")
            result = adb("shell", "settings", "put", "global", "cached_apps_freezer", "enabled",
                        serial=self.serial)
            self._log(result or "App freezer enabled.")
        threading.Thread(target=run, daemon=True).start()

    def _optimize_touch(self):
        if not self._require_connection():
            return
        def run():
            self._log("Optimizing touch response...")
            adb("shell", "settings", "put", "secure", "tap_duration_threshold", "0.0", serial=self.serial)
            adb("shell", "settings", "put", "secure", "touch_blocking_period", "0.0", serial=self.serial)
            self._log("Touch optimized.")
        threading.Thread(target=run, daemon=True).start()

    def _all_optimizations(self):
        if not self._require_connection():
            return
        def run():
            self._log("Running all optimizations...")
            self._log("  [1/3] Enabling app freezer...")
            adb("shell", "settings", "put", "global", "cached_apps_freezer", "enabled", serial=self.serial)
            self._log("  [2/3] Optimizing touch...")
            adb("shell", "settings", "put", "secure", "tap_duration_threshold", "0.0", serial=self.serial)
            adb("shell", "settings", "put", "secure", "touch_blocking_period", "0.0", serial=self.serial)
            self._log("  [3/3] Compiling speed profile (may take minutes)...")
            result = adb("shell", "cmd", "package", "compile", "-m", "speed-profile", "-a",
                        serial=self.serial, timeout=300)
            self._log(result or "All optimizations complete.")
        threading.Thread(target=run, daemon=True).start()

    def _set_animations(self, scale):
        if not self._require_connection():
            return
        def run():
            self._log(f"Setting animation scale to {scale}×...")
            for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
                adb("shell", "settings", "put", "global", key, scale, serial=self.serial)
            self._log("Animation scale updated.")
        threading.Thread(target=run, daemon=True).start()

    def _clear_all_caches(self):
        if not self._require_connection():
            return
        def run():
            self._log("Trimming all app caches...")
            result = adb("shell", "pm", "trim-caches", "0", serial=self.serial, timeout=30)
            self._log(result or "Caches trimmed.")
        threading.Thread(target=run, daemon=True).start()

    def _kill_background_apps(self):
        if not self._require_connection():
            return
        def run():
            self._log("Killing background apps...")
            result = adb("shell", "am", "kill-all", serial=self.serial)
            self._log(result or "Background apps killed.")
        threading.Thread(target=run, daemon=True).start()

    def _browse_apk(self):
        path = filedialog.askopenfilename(filetypes=[("APK files", "*.apk"), ("All files", "*.*")])
        if path:
            self.apk_path_var.set(path)
            self._selected_apk = path

    def _install_apk(self):
        if not self._require_connection():
            return
        apk = getattr(self, "_selected_apk", None)
        if not apk or not os.path.exists(apk):
            self._log("No APK selected or file not found.")
            return
        def run():
            self._log(f"Installing {os.path.basename(apk)}...")
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "-1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "0", serial=self.serial)
            result = adb("install", "-r", "-g", apk, serial=self.serial, timeout=120)
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "1", serial=self.serial)
            self._log(result or "Install complete.")
        threading.Thread(target=run, daemon=True).start()

    def _refresh_shizuku_status(self):
        apk_present = SHIZUKU_APK.exists()
        if apk_present:
            self._shizuku_status_label.configure(text="shizuku.apk ready.", text_color=TEXT_MUTED)
            self._shizuku_dl_btn.configure(state="disabled")
            self._shizuku_install_btn.configure(state="normal")
        else:
            self._shizuku_status_label.configure(text="shizuku.apk not downloaded yet.", text_color=TEXT_DISABLED)
            self._shizuku_dl_btn.configure(state="normal")
            self._shizuku_install_btn.configure(state="disabled")

    def _download_shizuku(self):
        self._shizuku_dl_btn.configure(state="disabled", text="Downloading...")
        def run():
            try:
                self._log("Fetching latest Shizuku release from GitHub...")
                req = urllib.request.Request(
                    "https://api.github.com/repos/RikkaApps/Shizuku/releases/latest",
                    headers={"User-Agent": f"AndroidTVDesktopToolkit/{VERSION}", "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = _json.loads(resp.read())
                apk_url = next(
                    (a["browser_download_url"] for a in data.get("assets", [])
                     if a["name"].endswith(".apk")), None)
                if not apk_url:
                    self._log("Could not find APK in latest release.")
                    self.after(0, lambda: self._shizuku_dl_btn.configure(state="normal", text="Download Shizuku"))
                    return
                version = data.get("tag_name", "")
                self._log(f"Downloading Shizuku {version}...")
                dest = str(SHIZUKU_APK)
                urllib.request.urlretrieve(apk_url, dest)
                self._log(f"Shizuku {version} saved to tool folder.")
                self.after(0, self._refresh_shizuku_status)
            except Exception as e:
                self._log(f"Download failed: {e}")
                self.after(0, lambda: self._shizuku_dl_btn.configure(state="normal", text="Download Shizuku"))
        threading.Thread(target=run, daemon=True).start()

    def _install_shizuku(self):
        if not self._require_connection():
            return
        shizuku_apk = str(SHIZUKU_APK)
        if not os.path.exists(shizuku_apk):
            self._log("shizuku.apk not found — use Download Shizuku first.")
            return
        def run():
            self._log("Checking Shizuku...")
            check = adb("shell", "pm", "list", "packages", "moe.shizuku.privileged.api", serial=self.serial)
            if "moe.shizuku" in check:
                self._log("Shizuku already installed.")
            else:
                self._log("Installing Shizuku...")
                adb("shell", "settings", "put", "global", "package_verifier_user_consent", "-1", serial=self.serial)
                result = adb("install", "-r", "-g", shizuku_apk, serial=self.serial, timeout=60)
                adb("shell", "settings", "put", "global", "package_verifier_user_consent", "1", serial=self.serial)
                self._log(result or "Shizuku installed.")
            # Clicking Install should leave Shizuku running, not just installed.
            self._start_shizuku()
        threading.Thread(target=run, daemon=True).start()

    def _start_shizuku(self):
        """Open Shizuku (so it writes start.sh), wait for that file, then start
        the service over ADB. Synchronous — call from a background thread."""
        import time
        start_sh = "/sdcard/Android/data/moe.shizuku.privileged.api/start.sh"
        self._log("Opening Shizuku...")
        adb("shell", "monkey", "-p", "moe.shizuku.privileged.api", "1", serial=self.serial)
        # First launch writes start.sh lazily; wait briefly for it to appear so
        # the very first start doesn't fail with "No such file".
        for _ in range(6):
            if "No such file" not in adb("shell", "ls", start_sh, serial=self.serial):
                break
            time.sleep(1)
        self._log("Starting Shizuku service...")
        result = adb("shell", "sh", start_sh, serial=self.serial)
        self._log(result or "Shizuku service started.")

    def _launch_shizuku(self):
        if not self._require_connection():
            return
        threading.Thread(target=self._start_shizuku, daemon=True).start()

    def _browse_bulk_folder(self):
        folder = filedialog.askdirectory(title="Select folder with APK files")
        if not folder:
            return
        apks = list(Path(folder).glob("*.apk"))
        if not apks:
            self._log("No APK files found in selected folder.")
            return
        if not self._require_connection():
            return
        def run():
            self._log(f"Bulk installing {len(apks)} APK(s)...")
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "-1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "0", serial=self.serial)
            for i, apk in enumerate(apks, 1):
                self._log(f"  [{i}/{len(apks)}] {apk.name}")
                result = adb("install", "-r", "-g", str(apk), serial=self.serial, timeout=120)
                self._log(f"    → {result or 'OK'}")
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "1", serial=self.serial)
            self._log("Bulk install complete.")
        threading.Thread(target=run, daemon=True).start()

    def _qi_download(self, name):
        meta = self._qi_apps[name]
        meta["dl_btn"].configure(state="disabled", text="Downloading...")
        def run():
            try:
                self._log(f"Fetching latest {name} release...")
                if meta.get("direct_url"):
                    # Closed-source app distributed from its own site (e.g. AdGuard TV).
                    apk_url = meta["direct_url"]
                elif meta.get("gitlab"):
                    # Aurora Store on GitLab
                    api = "https://gitlab.com/api/v4/projects/AuroraOSS%2FAuroraStore/releases"
                    req = urllib.request.Request(api, headers={"User-Agent": f"AndroidTVDesktopToolkit/{VERSION}"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = _json.loads(resp.read())
                    # find first .apk asset link
                    apk_url = None
                    for release in data[:1]:
                        for link in release.get("assets", {}).get("links", []):
                            if link["url"].endswith(".apk"):
                                apk_url = link["url"]
                                break
                        # Recent Aurora releases no longer attach .apk asset links;
                        # fall back to the stable download URL built from the tag.
                        if not apk_url and release.get("tag_name"):
                            apk_url = ("https://auroraoss.com/downloads/AuroraStore/"
                                       f"Release/AuroraStore-{release['tag_name']}.apk")
                else:
                    repo = meta["repo"]
                    api = f"https://api.github.com/repos/{repo}/releases/latest"
                    req = urllib.request.Request(api, headers={
                        "User-Agent": f"AndroidTVDesktopToolkit/{VERSION}",
                        "Accept": "application/vnd.github+json"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = _json.loads(resp.read())
                    assets = data.get("assets", [])
                    contains = meta.get("asset_contains", "")
                    suffix = meta.get("asset_suffix", ".apk")
                    if contains:
                        apk_url = next((a["browser_download_url"] for a in assets
                                        if contains in a["name"].lower() and a["name"].endswith(".apk")), None)
                    else:
                        apk_url = next((a["browser_download_url"] for a in assets
                                        if a["name"].endswith(suffix)), None)

                if not apk_url:
                    self._log(f"Could not find APK for {name}.")
                    self.after(0, lambda: meta["dl_btn"].configure(state="normal", text="Download"))
                    return
                self._log(f"Downloading {name}...")
                # Stream with a browser User-Agent — some hosts (e.g. auroraoss.com)
                # reject the default urllib agent with HTTP 403.
                dl_req = urllib.request.Request(apk_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(dl_req, timeout=60) as resp, \
                        open(meta["file"], "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                self._log(f"{name} downloaded.")
                def refresh():
                    meta["dl_btn"].configure(state="disabled", text="Download")
                    meta["inst_btn"].configure(state="normal")
                self.after(0, refresh)
            except Exception as e:
                self._log(f"Download failed for {name}: {e}")
                self.after(0, lambda: meta["dl_btn"].configure(state="normal", text="Download"))
        threading.Thread(target=run, daemon=True).start()

    def _qi_install(self, name):
        if not self._require_connection():
            return
        meta = self._qi_apps[name]
        apk = str(meta["file"])
        if not os.path.exists(apk):
            self._log(f"{name} APK not found — download it first.")
            return
        def run():
            self._log(f"Installing {name}...")
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "-1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "0", serial=self.serial)
            result = adb("install", "-r", "-g", apk, serial=self.serial, timeout=120)
            adb("shell", "settings", "put", "global", "package_verifier_user_consent", "1", serial=self.serial)
            adb("shell", "settings", "put", "global", "package_verifier_enable", "1", serial=self.serial)
            self._log(result or f"{name} installed.")
        threading.Thread(target=run, daemon=True).start()

    def _google_search_weather(self):
        if not self._require_connection():
            return
        def run():
            result = adb("shell", "am", "start", "-a", "android.search.action.GLOBAL_SEARCH",
                        "--es", "query", "Tell me what the weather is like today", serial=self.serial)
            self._log(result or "Opened Google Search.")
        threading.Thread(target=run, daemon=True).start()

    def _system_updates(self):
        if not self._require_connection():
            return
        def run():
            result = adb("shell", "am", "start", "-a", "android.settings.SYSTEM_UPDATE_SETTINGS", serial=self.serial)
            self._log(result or "Opened System Update Settings.")
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _escape_adb_text(text):
        _SHELL_SPECIAL = set(r'\"`$&|;()<>!#*?[]{}^~\'')
        result = []
        for ch in text:
            if ch == ' ':
                result.append('%s')
            elif ch in _SHELL_SPECIAL:
                result.append('\\' + ch)
            else:
                result.append(ch)
        return ''.join(result)

    def _send_text(self):
        if not self._require_connection():
            return
        text = self._send_text_entry.get()
        if not text:
            return
        def run():
            adb("shell", "input", "text", self._escape_adb_text(text), serial=self.serial)
            self._log(f"Sent text: {text}")
        self._send_text_entry.delete(0, "end")
        threading.Thread(target=run, daemon=True).start()

    def _adb_disconnect_all(self):
        def run():
            result = adb("disconnect")
            self.serial = None
            self.after(0, self._reset_connect_btn)
            self._log(result or "Disconnected all.")
        threading.Thread(target=run, daemon=True).start()

    def _keyevent(self, code):
        if not self._require_connection():
            return
        threading.Thread(target=lambda: adb("shell", "input", "keyevent", str(code), serial=self.serial), daemon=True).start()

    def _reboot_soft(self):
        if not self._require_connection():
            return
        def run():
            self._log("Rebooting device...")
            adb("shell", "reboot", serial=self.serial)
        threading.Thread(target=run, daemon=True).start()

    def _reboot_recovery(self):
        if not self._require_connection():
            return
        def run():
            self._log("Rebooting to recovery...")
            adb("shell", "reboot", "recovery", serial=self.serial)
        threading.Thread(target=run, daemon=True).start()

    def _notification_curtain(self):
        if not self._require_connection():
            return
        def run():
            adb("shell", "cmd", "statusbar", "expand-notifications", serial=self.serial)
            self._log("Notification curtain expanded.")
        threading.Thread(target=run, daemon=True).start()

    def _launch_app(self):
        if not self._require_connection():
            return
        pkg = self._launch_pkg_entry.get().strip()
        if not pkg:
            self._log("Enter a package name.")
            return
        def run():
            result = adb("shell", "monkey", "-p", pkg, "1", serial=self.serial)
            self._log(result or f"Launched {pkg}.")
        threading.Thread(target=run, daemon=True).start()

    def _open_settings(self, action):
        if not self._require_connection():
            return
        def run():
            result = adb("shell", "am", "start", "-a", action, serial=self.serial)
            self._log(result or f"Opened {action}.")
        threading.Thread(target=run, daemon=True).start()

    def _run_adb_console(self):
        if not self._require_connection():
            return
        raw = self._adb_cmd_entry.get().strip()
        if not raw:
            return
        # strip leading "adb shell" or "adb" if user typed it
        cmd = raw
        for prefix in ("adb shell ", "adb "):
            if cmd.lower().startswith(prefix):
                cmd = cmd[len(prefix):]
                break
        self._adb_cmd_entry.delete(0, "end")
        def run():
            self._log(f"$ {raw}")
            import shlex
            parts = shlex.split(cmd)
            result = adb("shell", *parts, serial=self.serial, timeout=30)
            self._log(result or "(no output)")
        threading.Thread(target=run, daemon=True).start()

    # ── screenshot ─────────────────────────────────────────────────────────────
    def _take_screenshot(self):
        if not self._require_connection():
            return
        def run():
            self._log("Taking screenshot...")
            self._screenshot_label_set("Capturing...")
            adb("shell", "screencap", "-p", "/sdcard/_tv_tools_cap.png", serial=self.serial)
            tmp = str(SCREENSHOT_TMP)
            adb("pull", "/sdcard/_tv_tools_cap.png", tmp, serial=self.serial)
            adb("shell", "rm", "/sdcard/_tv_tools_cap.png", serial=self.serial)
            if not os.path.exists(tmp):
                self._log("Screenshot failed — file not pulled.")
                self._screenshot_label_set("Failed.")
                return
            self._log("Screenshot captured.")
            self.after(0, lambda: self._display_screenshot(tmp))
        threading.Thread(target=run, daemon=True).start()

    def _display_screenshot(self, path):
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            # scale to fit the canvas widget (max 800×500)
            img.thumbnail((800, 500), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._screenshot_image = photo  # keep reference
            self._screenshot_canvas.configure(image=photo, text="")
            self._screenshot_label_set(f"Captured  {img.width}×{img.height}px  —  {os.path.basename(path)}")
        except ImportError:
            self._log("Pillow not installed — run: uv pip install pillow")
            self._screenshot_label_set("Install Pillow to preview.")
        except Exception as e:
            self._log(f"Preview error: {e}")

    def _save_screenshot(self):
        tmp = str(SCREENSHOT_TMP)
        if not os.path.exists(tmp):
            self._log("No screenshot to save — take one first.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
            initialfile="screenshot.png")
        if dest:
            import shutil
            shutil.copy2(tmp, dest)
            self._log(f"Saved to {dest}")

    def _screenshot_label_set(self, text):
        self.after(0, lambda: self._screenshot_label.configure(text=text))

    # ── log ────────────────────────────────────────────────────────────────────
    def _copy_log(self):
        self.log_box.configure(state="normal")
        text = self.log_box.get("1.0", "end").strip()
        self.log_box.configure(state="disabled")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.copy_btn.configure(text="Copied!")
            self.after(1500, lambda: self.copy_btn.configure(text="Copy"))

    def _log(self, msg):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg.rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, append)


def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
