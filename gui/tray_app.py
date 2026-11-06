"""Local tray/status GUI for one Community AI Grid worker device (Windows prototype).

A thin wrapper around scripts/grid_join.py: starts/stops its "run" subprocess (tunnel +
worker.client) and displays state. Never duplicates tunnel/worker logic, never opens a
network port, never enrolls devices or downloads models. State comes only from the local
status file the worker writes (CAG_STATUS_PATH, wired up by grid_join.py) and the
non-secret fields of the device bundle; the worker token is never read or displayed.

Run from source (same venv as the worker, after `pip install -e '.[gui]'`):
    python gui\\tray_app.py

Or the packaged dist\\CommunityAIGridWorker.exe (PyInstaller --onefile --windowed). The
packaged build still needs the project's own .venv to run grid_join.py/worker.client
(it is not bundled into the exe), so it assumes the exe stays at <project>/dist/ next to
that .venv - see venv_python() below.
"""
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

from PIL import Image, ImageDraw
import pystray

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    # PyInstaller: __file__ resolves into a temp extraction dir (useless here), and
    # sys.executable is this very .exe, not a Python interpreter - see venv_python().
    ROOT = Path(sys.executable).resolve().parent.parent
else:
    ROOT = Path(__file__).resolve().parent.parent

CONFIG = Path.home() / ".config" / "community-grid"
STATUS_PATH = CONFIG / "status.json"
BUNDLE_PATH = CONFIG / "worker.env"
LOG_PATH = CONFIG / "gui-run.log"
LIMITS_PATH = CONFIG / "limits.json"

# (json key, worker.env bundle key, label, low, high, hardcoded default). Ranges match
# worker/client.py's own Worker.__init__ validation for these same four Ollama settings - this
# file only ever narrows/widens those same numbers, never adds a new capability. Precedence
# resolved by resolve_effective_limits(): limits.json > bundle field > hardcoded default.
LIMIT_FIELDS = (
    ("context_tokens", "CAG_OLLAMA_NUM_CTX", "Context tokens", 256, 8192, 1024),
    ("cpu_threads", "CAG_OLLAMA_NUM_THREAD", "CPU threads", 1, 8, 2),
    ("gpu_layers", "CAG_OLLAMA_NUM_GPU", "GPU layers", 0, 999, 0),
    ("max_output_tokens", "CAG_OLLAMA_MAX_OUTPUT_TOKENS", "Max output tokens", 1, 512, 32),
)

# Defensive filter only: worker.client never prints prompt content or tokens, but a
# locally added log line could; never render anything that looks like one.
SECRET_LINE = re.compile(r"token|bearer|authorization", re.IGNORECASE)

# Shown in Normal mode; EXTRA_BUNDLE_FIELDS adds more detail in Extended mode. Never
# CAG_WORKER_TOKEN (the actual credential) or CAG_TUNNEL_HOST_KEY, even there.
DISPLAY_BUNDLE_FIELDS = ("CAG_DEVICE_NAME", "CAG_MODEL", "CAG_LLM_BACKEND")
EXTRA_BUNDLE_FIELDS = ("CAG_TUNNEL_HOST", "CAG_TUNNEL_LOCAL_PORT", "CAG_LLM_URL",
                       "CAG_OLLAMA_NUM_CTX", "CAG_OLLAMA_NUM_THREAD", "CAG_OLLAMA_NUM_GPU",
                       "CAG_OLLAMA_MAX_OUTPUT_TOKENS")

# Scheduled Task creation needs elevation on some managed/policy-restricted devices (observed
# here: "Access denied" from both Register-ScheduledTask and schtasks.exe for an ordinary,
# non-elevated per-user task) - CLAUDE.md forbids elevating without separate approval, so this
# uses the per-user HKCU Run key instead (no admin rights needed, no Task Scheduler API).
AUTOSTART_REGISTRY_NAME = "CommunityAIGridWorkerGUI"
AUTOSTART_REGISTRY_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

# Every helper here launches a console-subsystem executable (powershell.exe, python.exe)
# from this windowed (no-console) GUI. Without this flag Windows allocates a new, briefly
# visible console for each one - CREATE_NO_WINDOW suppresses that entirely.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def venv_python() -> str:
    """The real Python interpreter that can run scripts/grid_join.py. Never just
    sys.executable: under a frozen (PyInstaller) build that IS this GUI .exe, not an
    interpreter - using it would relaunch the GUI itself instead of the worker."""
    if FROZEN:
        candidate = ROOT / ".venv" / "Scripts" / "python.exe"
        if not candidate.exists():
            raise FileNotFoundError(
                f"Python interpreter not found at {candidate} - the packaged .exe "
                "expects to stay in <project>/dist/, next to the project's own .venv")
        return str(candidate)
    return sys.executable


def _autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    script = str(Path(__file__).resolve())
    return f'"{pythonw}" "{script}"'


def is_autostart_installed() -> bool:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-ItemProperty -Path '{AUTOSTART_REGISTRY_KEY}' "
             f"-Name '{AUTOSTART_REGISTRY_NAME}' -ErrorAction SilentlyContinue) -ne $null"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        return result.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def install_autostart() -> None:
    """Starts this GUI (not the worker itself) at login via the current user's own
    HKCU Run key - no admin rights, no system-wide change. The worker still only
    starts when "Start worker" is clicked; autostart only makes the status
    window/tray icon available after login."""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"New-ItemProperty -Path '{AUTOSTART_REGISTRY_KEY}' -Name '{AUTOSTART_REGISTRY_NAME}' "
         f"-Value '{_autostart_command()}' -PropertyType String -Force | Out-Null"],
        check=True, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)


def remove_autostart() -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"Remove-ItemProperty -Path '{AUTOSTART_REGISTRY_KEY}' -Name '{AUTOSTART_REGISTRY_NAME}' "
         "-ErrorAction SilentlyContinue"],
        capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def read_bundle_fields(extra: bool = False) -> dict:
    wanted = DISPLAY_BUNDLE_FIELDS + EXTRA_BUNDLE_FIELDS if extra else DISPLAY_BUNDLE_FIELDS
    fields = {}
    try:
        for line in BUNDLE_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in wanted:
                fields[key] = value
    except OSError:
        pass
    return fields


def tail_log(n: int) -> list[str]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if not SECRET_LINE.search(line)][-n:]


def read_limits() -> dict:
    """Current local resource-limit overrides, or {} if missing/unreadable/not a JSON object.
    Same file worker/client.py's read_limits_override() reads (CAG_LIMITS_PATH) - this GUI only
    ever writes it, never the worker token or anything network-reachable."""
    try:
        data = json.loads(LIMITS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_limits(values: dict) -> None:
    """Atomically writes `values` as JSON to LIMITS_PATH (same tmp-then-replace pattern as
    worker/client.py's write_status, so a reader never sees a half-written file)."""
    LIMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LIMITS_PATH.with_suffix(LIMITS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(values))
    tmp.replace(LIMITS_PATH)


def resolve_effective_limits(bundle_fields: dict, limits: dict) -> dict:
    """The four currently-effective limit values, for pre-filling the GUI's entry fields."""
    resolved = {}
    for key, bundle_key, _label, _low, _high, default in LIMIT_FIELDS:
        if key in limits and type(limits[key]) is int:
            resolved[key] = limits[key]
        elif bundle_key in bundle_fields:
            try:
                resolved[key] = int(bundle_fields[bundle_key])
            except ValueError:
                resolved[key] = default
        else:
            resolved[key] = default
    return resolved


def log_event(message: str) -> None:
    """Appends a timestamped GUI-side line to the same log file grid_join.py/worker.client
    write to, so Extended mode shows one combined timeline of everything the GUI, tunnel and
    worker did - never prompt content or tokens, same as the rest of this file."""
    try:
        CONFIG.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} [gui] {message}\n")
    except OSError:
        pass


def stop_run_process(proc: subprocess.Popen | None) -> None:
    """Forceful by necessity: Windows does not deliver SIGINT/SIGTERM to a
    background process the way Ctrl+C does in an interactive console, so the
    graceful shutdown grid_join.py relies on cannot be triggered from here.
    Stop each child (ssh tunnel, worker.client) explicitly instead."""
    if proc is None or proc.poll() is not None:
        return
    try:
        import psutil
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except ImportError:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def make_icon_image(color: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=color)
    return image


# Anthracite-led palette: dark graphite chrome throughout, a single blue accent reserved
# for the primary action, status colors tuned for contrast against the dark surfaces.
BG = "#232629"           # window background (anthracite)
PANEL_BG = "#2b2e32"      # status card surface, one step lighter than BG
BORDER = "#3c4046"
TEXT_PRIMARY = "#e8e9eb"
TEXT_SECONDARY = "#9aa0a6"
ACCENT = "#4c8bf5"
ACCENT_ACTIVE = "#6ba0f7"
BTN_BG = "#3a3d42"
BTN_ACTIVE = "#45484e"
GOOD = "#3fb950"
BAD = "#f85149"
LOG_BG = "#17181b"
LOG_FG = "#d7dce1"

CONNECTED_COLOR = (63, 185, 80)
DISCONNECTED_COLOR = (154, 160, 166)
ERROR_COLOR = (248, 81, 73)

NORMAL_LOG_LINES = 25
EXTENDED_LOG_LINES = 1000


class App:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.log_file = None
        self.icon: pystray.Icon | None = None
        self.start_error: str | None = None
        self._last_connected = "unset"
        self._last_job_status = "unset"

        self.root = tk.Tk()
        self.root.title("Community AI Grid - Worker")
        self.root.geometry("620x560")
        self.root.minsize(500, 440)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        style = ttk.Style(self.root)
        style.theme_use("clam")  # fully themeable; the native 'vista' theme ignores
                                  # custom colors on buttons/checkbuttons, breaking a dark theme

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Header.TLabel", background=BG, foreground=TEXT_PRIMARY, font=("Segoe UI", 15, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=TEXT_SECONDARY, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=BG, foreground=TEXT_SECONDARY, font=("Segoe UI", 8, "bold"))
        style.configure("Field.TLabel", background=PANEL_BG, foreground=TEXT_PRIMARY, font=("Segoe UI", 9, "bold"))
        style.configure("Value.TLabel", background=PANEL_BG, foreground=TEXT_PRIMARY, font=("Segoe UI", 9))
        style.configure("Good.TLabel", background=PANEL_BG, foreground=GOOD, font=("Segoe UI", 9, "bold"))
        style.configure("Bad.TLabel", background=PANEL_BG, foreground=BAD, font=("Segoe UI", 9, "bold"))
        style.configure("Neutral.TLabel", background=PANEL_BG, foreground=TEXT_SECONDARY, font=("Segoe UI", 9, "bold"))

        style.configure("TButton", background=BTN_BG, foreground=TEXT_PRIMARY,
                        bordercolor=BORDER, focuscolor=BTN_BG, padding=(12, 7))
        style.map("TButton", background=[("active", BTN_ACTIVE), ("disabled", BG)],
                 foreground=[("disabled", TEXT_SECONDARY)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, padding=(12, 7), font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", ACCENT_ACTIVE), ("disabled", BORDER)],
                 foreground=[("disabled", TEXT_SECONDARY)])

        style.configure("TCheckbutton", background=BG, foreground=TEXT_PRIMARY, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT_PRIMARY)])

        header = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Community AI Grid", style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text="Local worker status and control", style="Sub.TLabel").pack(anchor="w")

        status_wrap = ttk.Frame(self.root, padding=(16, 4, 16, 8))
        status_wrap.pack(fill=tk.X)
        ttk.Label(status_wrap, text="STATUS", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        panel = tk.Frame(status_wrap, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        panel.pack(fill=tk.X)
        grid = ttk.Frame(panel, style="Panel.TFrame", padding=12)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        self.device_val = tk.StringVar(value="-")
        self.model_val = tk.StringVar(value="-")
        self.conn_val = tk.StringVar(value="unknown")
        self.job_val = tk.StringVar(value="idle")
        self.last_val = tk.StringVar(value="-")

        rows = [
            ("Device", self.device_val, None),
            ("Model", self.model_val, None),
            ("Coordinator", self.conn_val, "conn"),
            ("Job", self.job_val, None),
            ("Last result", self.last_val, None),
        ]
        for r, (label, var, kind) in enumerate(rows):
            ttk.Label(grid, text=label, style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            if kind == "conn":
                self.conn_label = ttk.Label(grid, textvariable=var, style="Neutral.TLabel")
                self.conn_label.grid(row=r, column=1, sticky="w", pady=3)
            else:
                ttk.Label(grid, textvariable=var, style="Value.TLabel", wraplength=440,
                         justify="left").grid(row=r, column=1, sticky="w", pady=3)

        limits_wrap = ttk.Frame(self.root, padding=(16, 4, 16, 8))
        limits_wrap.pack(fill=tk.X)
        ttk.Label(limits_wrap, text="STATS & LIMITS", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        limits_card = tk.Frame(limits_wrap, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        limits_card.pack(fill=tk.X)
        limits_grid = ttk.Frame(limits_card, style="Panel.TFrame", padding=12)
        limits_grid.pack(fill=tk.X)
        limits_grid.columnconfigure(1, weight=1)

        self.jobs_completed_val = tk.StringVar(value="0")
        self.provisional_credits_val = tk.StringVar(value="-")
        self.trust_level_val = tk.StringVar(value="-")
        for r, (label, var) in enumerate([
            ("Jobs completed", self.jobs_completed_val),
            ("Provisional credits", self.provisional_credits_val),
            ("Trust level", self.trust_level_val),
        ]):
            ttk.Label(limits_grid, text=label, style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            ttk.Label(limits_grid, textvariable=var, style="Value.TLabel").grid(row=r, column=1, sticky="w", pady=3)

        ttk.Separator(limits_grid).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 8))

        effective = resolve_effective_limits(read_bundle_fields(extra=True), read_limits())
        self.limit_vars: dict[str, tk.StringVar] = {}
        for i, (key, _bundle_key, label, low, high, _default) in enumerate(LIMIT_FIELDS):
            row = 4 + i
            ttk.Label(limits_grid, text=f"{label} ({low}-{high})", style="Field.TLabel").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            var = tk.StringVar(value=str(effective[key]))
            self.limit_vars[key] = var
            ttk.Entry(limits_grid, textvariable=var, width=10).grid(row=row, column=1, sticky="w", pady=3)

        status_row = 4 + len(LIMIT_FIELDS)
        self.limits_status_val = tk.StringVar(value="")
        self.limits_status_label = ttk.Label(limits_grid, textvariable=self.limits_status_val,
                                             style="Neutral.TLabel", wraplength=440, justify="left")
        self.limits_status_label.grid(row=status_row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(limits_grid, text="Apply limits", command=self.apply_limits).grid(
            row=status_row + 1, column=0, sticky="w", pady=(8, 0))

        controls = ttk.Frame(self.root, padding=(16, 2, 16, 8))
        controls.pack(fill=tk.X)
        self.start_btn = ttk.Button(controls, text="Start worker", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(controls, text="Stop worker", command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        autostart_row = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        autostart_row.pack(fill=tk.X)
        self.autostart_var = tk.BooleanVar(value=is_autostart_installed())
        ttk.Checkbutton(autostart_row, text="Start this window at login", variable=self.autostart_var,
                       command=self.toggle_autostart).pack(side=tk.LEFT)

        log_wrap = ttk.Frame(self.root, padding=(16, 0, 16, 16))
        log_wrap.pack(fill=tk.BOTH, expand=True)
        log_header = ttk.Frame(log_wrap, style="TFrame")
        log_header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(log_header, text="LOG", style="Section.TLabel").pack(side=tk.LEFT)
        self.extended_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(log_header, text="Extended (status.json + settings + full history)",
                       variable=self.extended_var, command=self.render_log).pack(side=tk.LEFT, padx=12)
        self.log_box = scrolledtext.ScrolledText(
            log_wrap, height=16, state=tk.DISABLED, bg=LOG_BG, fg=LOG_FG,
            insertbackground=LOG_FG, relief=tk.FLAT, font=("Consolas", 9), padx=8, pady=8)
        self.log_box.pack(fill=tk.BOTH, expand=True)

        log_event("GUI started")
        self.refresh()

    # -- worker process lifecycle (wraps scripts/grid_join.py, never reimplements it) --

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        self.start_error = None
        try:
            python = venv_python()
        except FileNotFoundError as exc:
            self.start_error = str(exc)
            log_event(f"Start worker failed: {exc}")
            return
        CONFIG.mkdir(parents=True, exist_ok=True)
        log_event("Start worker clicked")
        self.log_file = open(LOG_PATH, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [python, str(ROOT / "scripts" / "grid_join.py"), "run"],
            cwd=ROOT, stdout=self.log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def stop(self):
        log_event("Stop worker clicked")
        stop_run_process(self.proc)
        self.proc = None
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def toggle_autostart(self):
        desired = self.autostart_var.get()

        def work():
            try:
                (install_autostart if desired else remove_autostart)()
                log_event(f"Autostart {'enabled' if desired else 'disabled'}")
            except (OSError, subprocess.SubprocessError) as exc:
                log_event(f"Autostart change failed: {exc}")
            actual = is_autostart_installed()
            self.root.after(0, lambda: self.autostart_var.set(actual))

        threading.Thread(target=work, daemon=True).start()

    def apply_limits(self):
        """Validate all four fields, then write limits.json - or write nothing at all on any
        failure. Takes effect on the worker's next start, not live (no hot reload)."""
        values = {}
        for key, _bundle_key, label, low, high, _default in LIMIT_FIELDS:
            raw = self.limit_vars[key].get().strip()
            try:
                value = int(raw)
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                self.limits_status_val.set(f"Invalid {label}: must be an integer between {low} and {high}.")
                self.limits_status_label.configure(style="Bad.TLabel")
                return
            values[key] = value
        write_limits(values)
        log_event(f"Resource limits applied: {values}")
        self.limits_status_val.set("Applied - takes effect the next time the worker starts.")
        self.limits_status_label.configure(style="Neutral.TLabel")

    # -- display --

    def render_log(self):
        extended = self.extended_var.get()
        self.log_box.config(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        if extended:
            status = read_status()
            fields = read_bundle_fields(extra=True)
            self.log_box.insert(tk.END, "--- status.json ---\n")
            self.log_box.insert(tk.END, (json.dumps(status, indent=2) if status else "(no status yet)") + "\n\n")
            self.log_box.insert(tk.END, "--- device settings (worker token never shown) ---\n")
            if fields:
                for key, value in fields.items():
                    self.log_box.insert(tk.END, f"{key} = {value}\n")
            else:
                self.log_box.insert(tk.END, "(no bundle found)\n")
            self.log_box.insert(tk.END, "\n--- full log ---\n")
        lines = tail_log(EXTENDED_LOG_LINES if extended else NORMAL_LOG_LINES)
        self.log_box.insert(tk.END, "\n".join(lines) if lines else "(no log output yet)")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    def refresh(self):
        status = read_status()
        fields = read_bundle_fields()
        self.device_val.set(fields.get("CAG_DEVICE_NAME", "-"))
        self.model_val.set(f"{status.get('model', fields.get('CAG_MODEL', '-'))} "
                           f"(backend: {status.get('backend', fields.get('CAG_LLM_BACKEND', '-'))})")
        connected = status.get("connected")
        if connected is True:
            self.conn_val.set(f"Connected  -  last heartbeat {status.get('last_heartbeat', '-')}")
            self.conn_label.configure(style="Good.TLabel")
        elif connected is False:
            self.conn_val.set("Disconnected")
            self.conn_label.configure(style="Bad.TLabel")
        else:
            self.conn_val.set("Unknown (worker not running / no status yet)")
            self.conn_label.configure(style="Neutral.TLabel")
        self.jobs_completed_val.set(str(status.get("jobs_completed", 0)))
        credits = status.get("provisional_credits")
        self.provisional_credits_val.set("-" if credits is None else str(credits))
        self.trust_level_val.set(status.get("trust_level", "-"))

        job_status = status.get("job_status", "idle")
        if job_status == "running":
            self.job_val.set(f"Running  -  {status.get('job_model', '-')}  (id {status.get('job_id', '-')})")
        else:
            self.job_val.set("Idle")
        if self.start_error:
            self.last_val.set(f"Could not start worker: {self.start_error}")
        elif status.get("last_error"):
            self.last_val.set(f"Error ({status.get('last_error_at', '-')}): {status['last_error']}")
        elif status.get("last_success"):
            self.last_val.set(f"Success at {status['last_success']} (job {status.get('last_job_id', '-')})")
        else:
            self.last_val.set("-")

        if connected != self._last_connected:
            log_event(f"Connection state: {self._last_connected} -> {connected}")
            self._last_connected = connected
        if job_status != self._last_job_status:
            log_event(f"Job state: {self._last_job_status} -> {job_status}")
            self._last_job_status = job_status

        self.render_log()

        if self.icon:
            if (status.get("last_error") or self.start_error) and job_status != "running":
                color = ERROR_COLOR
            elif connected:
                color = CONNECTED_COLOR
            else:
                color = DISCONNECTED_COLOR
            self.icon.icon = make_icon_image(color)

        self.root.after(2000, self.refresh)

    # -- window / tray --

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def quit_all(self):
        self.stop()
        if self.icon:
            self.icon.stop()
        self.root.quit()


def run_tray(app: App):
    menu = pystray.Menu(
        pystray.MenuItem("Show window", lambda: app.root.after(0, app.show), default=True),
        pystray.MenuItem("Start worker", lambda: app.root.after(0, app.start)),
        pystray.MenuItem("Stop worker", lambda: app.root.after(0, app.stop)),
        pystray.MenuItem("Quit", lambda: app.root.after(0, app.quit_all)))
    app.icon = pystray.Icon("community-grid", make_icon_image(DISCONNECTED_COLOR), "Community AI Grid", menu)
    app.icon.run()


def main():
    app = App()
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    app.root.mainloop()


if __name__ == "__main__":
    main()
