#!/usr/bin/env python3
"""
System Diagnostic Tool  v3.0
══════════════════════════════════════════════════════════════════
NEW in v3.0:
  - Live rolling graphs  —  CPU % and RAM % over the last 60 seconds
  - Alert banner         —  instant warning when temps/RAM/disk hit danger zones
  - Report history       —  every scan auto-saved as JSON; last 10 shown in UI
  - All disk partitions  —  every mounted drive, not just root
  - Network interfaces   —  IP, MAC, speed for each adapter
  - Ping latency test    —  round-trip time to Google / Cloudflare DNS
  - CPU details          —  physical cores, logical threads, current frequency
  - Process kill         —  right-click any process to end it
  - Sort processes       —  toggle between CPU % and RAM sort
  - Simple comments      —  plain-English notes on every non-obvious line

Required:   pip install psutil
Optional:   pip install gputil   (richer NVIDIA GPU info)
            pip install wmi      (Windows CPU/GPU temperatures) requires OpenHardwareMonitor open
"""

# ── Standard library imports ──────────────────────────────────────────────────
import platform, socket, psutil, datetime, time, shutil, json, os
import subprocess, threading, math, collections
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, List, Tuple, Dict

# ── Try to import GPUtil (NVIDIA GPU stats) ───────────────────────────────────
try:
    import GPUtil
    GPUTIL_AVAILABLE = True
except ImportError:
    GPUTIL_AVAILABLE = False

# ── Try to import wmi for Windows hardware sensor temperatures ────────────────
WMI_AVAILABLE = False
if platform.system() == "Windows":
    try:
        import wmi as _wmi
        WMI_AVAILABLE = True
    except ImportError:
        pass

# ── Folder where auto-saved JSON scan reports are stored ─────────────────────
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diag_reports")
os.makedirs(HISTORY_DIR, exist_ok=True)   # create the folder if it doesn't exist yet

# ═══════════════════════════════════════════════════════════════════════════════
#  COLOR THEME  (dark purple palette)
# ═══════════════════════════════════════════════════════════════════════════════
BG      = "#12121f"   # main window background
CARD    = "#1c1c30"   # card / panel background
RAISED  = "#252540"   # slightly lighter surface (text boxes, rows)
ACC     = "#7c5cfc"   # accent purple (buttons, headings)
GREEN   = "#4ade80"
YELLOW  = "#fbbf24"
ORANGE  = "#fb923c"
RED     = "#f87171"
BLUE    = "#60a5fa"
TEAL    = "#2dd4bf"
TEXT    = "#e2e8f0"   # primary text (near-white)
SUB     = "#94a3b8"   # secondary / muted text
BORDER  = "#2e2e50"   # subtle divider line color

FN  = "Segoe UI"      # UI font — looks great on Windows, decent fallback elsewhere
FNM = "Consolas"      # monospace font for data readouts

# ── Alert thresholds ──────────────────────────────────────────────────────────
ALERT_CPU_TEMP  = 85    # °C — warn if CPU gets this hot
ALERT_GPU_TEMP  = 85    # °C — warn if GPU gets this hot
ALERT_RAM_PCT   = 88    # %  — warn if RAM usage exceeds this
ALERT_DISK_PCT  = 90    # %  — warn if any partition is this full

# ═══════════════════════════════════════════════════════════════════════════════
#  SMALL HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def score_color(score: int) -> str:
    """Return a color hex string matching how good/bad the health score is."""
    if score >= 80: return GREEN
    if score >= 55: return YELLOW
    if score >= 35: return ORANGE
    return RED

def score_label(score: int) -> str:
    """Return a short human-readable grade for a health score."""
    if score >= 80: return "Excellent"
    if score >= 65: return "Good"
    if score >= 50: return "Fair"
    if score >= 35: return "Poor"
    return "Critical"

def _fmt_bytes(n: float) -> str:
    """Convert a raw byte count into a readable string like '4.2 GB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

# ═══════════════════════════════════════════════════════════════════════════════
#  DATA COLLECTION  —  all functions that read hardware / OS info
# ═══════════════════════════════════════════════════════════════════════════════

def get_system_info() -> dict:
    """Gather basic OS and hardware identity info."""
    uname = platform.uname()
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "Unavailable"
    return {
        "Hostname":   socket.gethostname(),
        "IP Address": ip,
        "OS":         f"{uname.system} {uname.release}",
        "OS Version": uname.version[:80],
        "Machine":    uname.machine,
        "CPU":        get_cpu_name(),
        "GPU":        get_gpu_name(),
        "CPU Cores":  _cpu_core_str(),     # e.g. "6 physical / 12 logical"
        "CPU Freq":   _cpu_freq_str(),     # e.g. "3.60 GHz"
        "Boot Time":  datetime.datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S"),
        "Uptime":     _uptime_str(),
    }

def _uptime_str() -> str:
    """Return how long the PC has been on, e.g. '5h 42m'."""
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}h {m}m"

def _cpu_core_str() -> str:
    """Return physical vs logical core counts."""
    phys = psutil.cpu_count(logical=False) or "?"
    logi = psutil.cpu_count(logical=True)  or "?"
    return f"{phys} physical / {logi} logical"

def _cpu_freq_str() -> str:
    """Return current CPU frequency in GHz, or 'N/A' if unreadable."""
    try:
        freq = psutil.cpu_freq()
        if freq:
            return f"{freq.current / 1000:.2f} GHz"
    except Exception:
        pass
    return "N/A"

# ── Read the CPU model name from the OS ──────────────────────────────────────
def get_cpu_name() -> str:
    sys_ = platform.system()
    try:
        if sys_ == "Windows":
            import winreg
            # The processor name is stored in the Windows registry
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return name.strip()
        elif sys_ == "Darwin":
            # macOS — ask the system control utility
            out = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"],
                                          stderr=subprocess.DEVNULL).decode().strip()
            return out or platform.processor()
        else:
            # Linux — read the CPU info file line by line
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"

# ── NVIDIA GPU info via nvidia-smi (optional, preferred on Windows/Linux) ────
def get_nvidia_smi_info(timeout: float = 2.5) -> dict:
    """
    Query NVIDIA GPU stats using 'nvidia-smi' via subprocess.
    Returns a dict like:
      {
        'available': bool,
        'reason': Optional[str],
        'name': Optional[str],
        'temp': Optional[float],      # °C
        'util': Optional[float],      # %
        'mem_used': Optional[float],  # MiB
        'mem_total': Optional[float], # MiB
      }
    Never raises; on any error, returns available=False with a reason.
    """
    result = {
        "available": False,
        "reason": None,
        "name": None,
        "temp": None,
        "util": None,
        "mem_used": None,
        "mem_total": None,
    }
    # Check if nvidia-smi exists on PATH
    exe = shutil.which("nvidia-smi")
    if not exe:
        result["reason"] = "'nvidia-smi' not found on PATH (NVIDIA drivers not installed or PATH not set)."
        return result
    try:
        # CSV, no header, no units — easy to parse
        cmd = [exe,
               "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
               "--format=csv,noheader,nounits"]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout).decode(errors="ignore").strip()
        if not out:
            result["reason"] = "'nvidia-smi' returned no data."
            return result
        # Take the first GPU line
        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            name, temp, util, mem_used, mem_total = parts[:5]
            result.update({
                "available": True,
                "name": name or None,
                "temp": float(temp) if temp else None,
                "util": float(util) if util else None,
                "mem_used": float(mem_used) if mem_used else None,
                "mem_total": float(mem_total) if mem_total else None,
            })
        else:
            result["reason"] = "Unexpected 'nvidia-smi' output format."
    except FileNotFoundError:
        result["reason"] = "'nvidia-smi' not found (NVIDIA drivers missing)."
    except subprocess.TimeoutExpired:
        result["reason"] = "'nvidia-smi' timed out."
    except subprocess.CalledProcessError as e:
        result["reason"] = f"'nvidia-smi' failed: {e.returncode}"
    except Exception as e:
        result["reason"] = f"Unexpected error: {type(e).__name__}"
    return result

# ── Read the GPU model name from the OS ──────────────────────────────────────
def get_gpu_name() -> str:
    # Prefer NVIDIA via nvidia-smi if available (accurate for Windows/Linux with NVIDIA drivers)
    try:
        info = get_nvidia_smi_info()
        if info.get("available") and info.get("name"):
            return info["name"]
    except Exception:
        pass
    # GPUtil gives a clean name for NVIDIA cards if installed
    if GPUTIL_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                return gpus[0].name
        except Exception:
            pass
    sys_ = platform.system()
    try:
        if sys_ == "Windows":
            # wmic queries the Windows device manager
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                stderr=subprocess.DEVNULL).decode()
            lines = [l.strip() for l in out.splitlines() if l.strip() and "Name" not in l]
            if lines:
                return lines[0]
        elif sys_ == "Darwin":
            out = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType"],
                stderr=subprocess.DEVNULL).decode()
            for line in out.splitlines():
                if "Chipset Model" in line or "GPU Model" in line:
                    return line.split(":")[1].strip()
        else:
            # Linux — lspci lists all PCI devices including the GPU
            out = subprocess.check_output(["lspci"], stderr=subprocess.DEVNULL).decode()
            for line in out.splitlines():
                if "VGA" in line or "3D" in line or "Display" in line:
                    return line.split(":")[-1].strip()
    except Exception:
        pass
    return "Unknown GPU"

# ── Read temperature sensors ──────────────────────────────────────────────────
def get_temperatures() -> dict:
    """
    Returns a dict with structured availability info, e.g.:
      {
        'CPU':  float or None,
        'GPU':  float or None,
        'raw':  {...},
        'reasons': {
            'CPU': 'reason string if unavailable' or None,
            'GPU': 'reason string if unavailable' or None,
        },
        'nvidia': {
            'available': bool,
            'reason': str or None,
            'name': str or None,
            'temp': float or None,
            'util': float or None,
            'mem_used': float or None,
            'mem_total': float or None,
        }
      }
    """
    result = {"CPU": None, "GPU": None, "raw": {}, "reasons": {"CPU": None, "GPU": None}, "nvidia": {}}

    # 1) NVIDIA via nvidia-smi (GPU only) — preferred on Windows/Linux when NVIDIA drivers are installed
    try:
        nv = get_nvidia_smi_info()
    except Exception:
        nv = {"available": False, "reason": "Unexpected error calling nvidia-smi"}
    result["nvidia"] = nv
    if nv.get("available") and isinstance(nv.get("temp"), (int, float)):
        result["GPU"] = float(nv["temp"])

    # 2) psutil sensors — mainly useful on Linux/macOS for CPU temps
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                # Store all raw readings keyed by sensor name
                result["raw"] = {k: [t.current for t in v] for k, v in temps.items()}
                # Known CPU sensor names across different hardware
                for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
                    if key in temps and temps[key]:
                        result["CPU"] = max(t.current for t in temps[key])
                        break
                # If no known sensor matched, just take the overall maximum as CPU proxy
                if result["CPU"] is None:
                    all_vals = [t.current for vals in temps.values() for t in vals]
                    if all_vals:
                        result["CPU"] = max(all_vals)
        except Exception:
            pass

    # 3) Windows WMI sensors via LibreHardwareMonitor/OpenHardwareMonitor if wmi is installed
    if platform.system() == "Windows":
        if not WMI_AVAILABLE:
            if result["CPU"] is None:
                result["reasons"]["CPU"] = "WMI backend requires optional 'wmi' package (pip install wmi)."
        else:
            # Prefer LibreHardwareMonitor first
            if result["CPU"] is None or result["GPU"] is None:
                try:
                    w = _wmi.WMI(namespace=r"root\LibreHardwareMonitor")
                    for sensor in w.Sensor():
                        if sensor.SensorType == "Temperature":
                            lname = sensor.Name.lower()
                            v = float(sensor.Value)
                            if "cpu" in lname:
                                result["CPU"] = max(result["CPU"] or 0, v)
                            elif "gpu" in lname:
                                result["GPU"] = max(result["GPU"] or 0, v)
                except Exception:
                    pass
            # Fallback to OpenHardwareMonitor
            if result["CPU"] is None or result["GPU"] is None:
                try:
                    w = _wmi.WMI(namespace=r"root\OpenHardwareMonitor")
                    for sensor in w.Sensor():
                        if sensor.SensorType == "Temperature":
                            lname = sensor.Name.lower()
                            v = float(sensor.Value)
                            if "cpu" in lname:
                                result["CPU"] = max(result["CPU"] or 0, v)
                            elif "gpu" in lname:
                                result["GPU"] = max(result["GPU"] or 0, v)
                except Exception:
                    pass
            # If still missing CPU temp, provide a Windows-specific reason
            if result["CPU"] is None and result["reasons"]["CPU"] is None:
                result["reasons"]["CPU"] = (
                    "Windows usually does not expose CPU temperature directly. "
                    "Run LibreHardwareMonitor or OpenHardwareMonitor for sensors.")

    # 4) GPUtil fallback for NVIDIA temp if not set yet
    if result["GPU"] is None and GPUTIL_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus and gpus[0].temperature is not None:
                result["GPU"] = gpus[0].temperature
        except Exception:
            pass

    # 5) NVIDIA NVML fallback (pynvml) — optional
    if result["GPU"] is None:
        try:
            import pynvml as nvml
            nvml.nvmlInit()
            h = nvml.nvmlDeviceGetHandleByIndex(0)
            temp = nvml.nvmlDeviceGetTemperature(h, nvml.NVML_TEMPERATURE_GPU)
            if temp not in (None, 0):
                result["GPU"] = float(temp)
        except Exception:
            pass

    # Set GPU reason if still unavailable
    if result["GPU"] is None and not result["reasons"].get("GPU"):
        if nv.get("available") is False:
            reason = nv.get("reason") or "'nvidia-smi' not available or no NVIDIA GPU detected."
        else:
            reason = "No supported GPU temperature source detected."
        result["reasons"]["GPU"] = reason

    # If CPU still None and no prior reason set on non-Windows, add a generic reason
    if result["CPU"] is None and result["reasons"]["CPU"] is None:
        result["reasons"]["CPU"] = "No supported CPU temperature sensors available on this platform."

    return result

# ── RAM info ─────────────────────────────────────────────────────────────────
def get_memory_info() -> dict:
    m = psutil.virtual_memory()
    return {
        "Total":     f"{m.total / 2**30:.2f} GB",
        "Used":      f"{m.used  / 2**30:.2f} GB",
        "Available": f"{m.available / 2**30:.2f} GB",
        "Usage %":   m.percent,
    }

# ── All mounted disk partitions ───────────────────────────────────────────────
def get_all_disk_info() -> List[dict]:
    """
    Return a list of dicts, one per mounted partition.
    Skips unreadable drives (e.g. empty optical drives on Windows).
    """
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue   # some system partitions refuse access — skip them
        partitions.append({
            "device":     part.device,
            "mountpoint": part.mountpoint,
            "fstype":     part.fstype,
            "total":      _fmt_bytes(usage.total),
            "used":       _fmt_bytes(usage.used),
            "free":       _fmt_bytes(usage.free),
            "pct":        usage.percent,
        })
    return partitions

# ── Network I/O counters (cumulative since boot) ──────────────────────────────
def get_network_io() -> dict:
    io = psutil.net_io_counters()
    return {
        "Bytes Sent":     _fmt_bytes(io.bytes_sent),
        "Bytes Received": _fmt_bytes(io.bytes_recv),
        "Packets Sent":   f"{io.packets_sent:,}",
        "Packets Recv":   f"{io.packets_recv:,}",
        "Errors In":      io.errin,
        "Errors Out":     io.errout,
    }

# ── Network interfaces (IP, MAC, speed) ──────────────────────────────────────
def get_network_interfaces() -> List[dict]:
    """Return details for every network adapter found on this machine."""
    results = []
    addrs  = psutil.net_if_addrs()   # dict of interface → list of addresses
    stats  = psutil.net_if_stats()   # dict of interface → speed / is_up
    for iface, addr_list in addrs.items():
        ipv4 = mac = "—"
        for a in addr_list:
            # AF_INET is IPv4; AF_LINK / psutil.AF_LINK is the MAC address family
            if a.family == socket.AF_INET:
                ipv4 = a.address
            elif a.family == psutil.AF_LINK:
                mac = a.address
        st   = stats.get(iface)
        up   = "Up" if (st and st.isup) else "Down"
        spd  = f"{st.speed} Mbps" if (st and st.speed) else "?"
        results.append({"Interface": iface, "IPv4": ipv4,
                         "MAC": mac, "Status": up, "Speed": spd})
    return results

# ── Ping latency to well-known hosts ─────────────────────────────────────────
def ping_hosts() -> List[Tuple[str, str]]:
    """
    Returns a list of (hostname, latency_string) pairs.
    Uses a TCP connect to port 80 rather than ICMP ping,
    so it works without admin rights on all platforms.
    """
    targets = [
        ("Google DNS",      "8.8.8.8"),
        ("Cloudflare DNS",  "1.1.1.1"),
        ("OpenDNS",         "208.67.222.222"),
    ]
    results = []
    for name, host in targets:
        try:
            start = time.perf_counter()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, 53))   # DNS port — almost always open
            s.close()
            ms = (time.perf_counter() - start) * 1000
            results.append((name, f"{ms:.1f} ms"))
        except Exception:
            results.append((name, "Timeout"))
    return results

# ── Internet reachability check ───────────────────────────────────────────────
def check_internet() -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except Exception:
        return False

# ── Battery status ─────────────────────────────────────────────────────────────
def get_battery() -> Optional[dict]:
    batt = psutil.sensors_battery()
    if batt is None:
        return None   # desktop system — no battery
    plug   = "Plugged In" if batt.power_plugged else "On Battery"
    secs   = batt.secsleft
    remain = "Charging" if batt.power_plugged else (
        f"{secs // 3600}h {(secs % 3600) // 60}m"
        if secs != psutil.POWER_TIME_UNLIMITED else "∞")
    return {"Percent": batt.percent, "Status": plug, "Time Left": remain}

# ── Top processes ─────────────────────────────────────────────────────────────
def get_top_processes(n: int = 12, sort_by: str = "cpu") -> List[Tuple]:
    """
    Returns up to n processes as (pid, name, cpu%, ram_mb).
    sort_by can be 'cpu' or 'ram'.
    Two-pass method: first call seeds the CPU counter, sleep, then read.
    """
    for p in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            p.cpu_percent(interval=None)   # seed — first call always returns 0
        except Exception:
            pass
    time.sleep(0.5)   # wait a moment so the next reading is meaningful

    snap = []
    for p in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            cpu = p.cpu_percent(interval=None)
            mem = (p.info['memory_info'].rss / 2**20) if p.info.get('memory_info') else 0
            snap.append((p.info['pid'], p.info['name'] or "?", cpu, round(mem, 1)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass   # process ended between iterations — skip it

    # Sort either by CPU % or RAM usage depending on what the user chose
    key_idx = 2 if sort_by == "cpu" else 3
    snap.sort(key=lambda x: x[key_idx], reverse=True)
    return snap[:n]

# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH SCORE  —  weighted 0-100 rating of overall system condition
# ═══════════════════════════════════════════════════════════════════════════════
def compute_health_score(mem_pct: float, disks: List[dict],
                         cpu_temp: Optional[float], gpu_temp: Optional[float],
                         online: bool, battery: Optional[dict]) -> Tuple[int, List[str]]:
    """
    Weights:
        RAM usage    25 pts
        Disk usage   25 pts  (worst partition counts)
        CPU temp     20 pts
        GPU temp     15 pts
        Internet     10 pts
        Battery       5 pts
    """
    reasons = []
    score   = 0

    # ── RAM ──────────────────────────────────────────────────────────────────
    if mem_pct < 50:
        pts = 25; grade = "Excellent"
    elif mem_pct < 70:
        pts = 20; grade = "Good"
    elif mem_pct < 85:
        pts = 12; grade = "High"
    else:
        pts = 4;  grade = "Critical"
    score += pts
    reasons.append(f"  RAM {mem_pct:.0f}%  →  {grade}  [{pts}/25 pts]")

    # ── Disk  (score based on the most-full partition) ───────────────────────
    worst_pct = max((d["pct"] for d in disks), default=0)
    if worst_pct < 60:
        pts = 25; grade = "Excellent"
    elif worst_pct < 80:
        pts = 18; grade = "Getting full"
    elif worst_pct < 92:
        pts = 9;  grade = "Low space"
    else:
        pts = 2;  grade = "Critical"
    score += pts
    reasons.append(f"  Disk (worst) {worst_pct:.0f}%  →  {grade}  [{pts}/25 pts]")

    # ── CPU temperature ───────────────────────────────────────────────────────
    if cpu_temp is None:
        pts = 20; grade = "Sensor unavailable"  #FIXME
    elif cpu_temp < 60:
        pts = 20; grade = "Cool"
    elif cpu_temp < 75:
        pts = 15; grade = "Warm"
    elif cpu_temp < 90:
        pts = 7;  grade = "Hot"
    else:
        pts = 2;  grade = "Dangerously hot"
    score += pts
    t_str = f"{cpu_temp:.0f}°C" if cpu_temp is not None else "N/A"
    reasons.append(f"  CPU {t_str}  →  {grade}  [{pts}/20 pts]")

    # ── GPU temperature ───────────────────────────────────────────────────────
    if gpu_temp is None:
        pts = 15; grade = "Sensor unavailable"  # Kept temp unavailability score neutral
    elif gpu_temp < 65:
        pts = 15; grade = "Cool"
    elif gpu_temp < 80:
        pts = 11; grade = "Warm"
    elif gpu_temp < 92:
        pts = 5;  grade = "Hot"
    else:
        pts = 1;  grade = "Dangerously hot"
    score += pts
    t_str = f"{gpu_temp:.0f}°C" if gpu_temp is not None else "N/A"
    reasons.append(f"  GPU {t_str}  →  {grade}  [{pts}/15 pts]")

    # ── Internet ───────────────────────────────────────────────────────────────
    pts = 10 if online else 0
    reasons.append(f"  Internet  →  {'Online' if online else 'Offline'}  [{pts}/10 pts]")
    score += pts

    # ── Battery ───────────────────────────────────────────────────────────────
    if battery is not None and battery["Status"] == "On Battery":
        pct = battery["Percent"]
        if pct > 50:
            pts = 5; grade = "Good"
        elif pct > 25:
            pts = 3; grade = "Low"
        else:
            pts = 1; grade = "Critical"
        score += pts
        reasons.append(f"  Battery {pct:.0f}%  →  {grade}  [{pts}/5 pts]")
    elif battery is not None:
        score += 5
        reasons.append("  Battery  →  Plugged in  [5/5 pts]")
    else:
        score += 5   # desktops get full battery marks
        reasons.append("  Battery  →  Desktop / N/A  [5/5 pts]")

    return min(score, 100), reasons

# ── Build alert messages for anything that breached a threshold ───────────────
def build_alerts(cpu_temp, gpu_temp, mem_pct, disks) -> List[str]:
    """Return a list of short warning strings. Empty list means all clear."""
    alerts = []
    if cpu_temp is not None and cpu_temp >= ALERT_CPU_TEMP:
        alerts.append(f"⚠  CPU temp {cpu_temp:.0f}°C  ≥ {ALERT_CPU_TEMP}°C")
    if gpu_temp is not None and gpu_temp >= ALERT_GPU_TEMP:
        alerts.append(f"⚠  GPU temp {gpu_temp:.0f}°C  ≥ {ALERT_GPU_TEMP}°C")
    if mem_pct >= ALERT_RAM_PCT:
        alerts.append(f"⚠  RAM usage {mem_pct:.0f}%  ≥ {ALERT_RAM_PCT}%")
    for d in disks:
        if d["pct"] >= ALERT_DISK_PCT:
            alerts.append(f"⚠  Disk '{d['mountpoint']}'  {d['pct']}%  ≥ {ALERT_DISK_PCT}%")
    return alerts

# ═══════════════════════════════════════════════════════════════════════════════
#  REPORT HISTORY  —  save every scan to disk so you can compare over time
# ═══════════════════════════════════════════════════════════════════════════════
def save_report(data: dict) -> str:
    """Write a JSON report file and return its filename."""
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(HISTORY_DIR, f"scan_{ts}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)   # default=str handles non-serialisable types
    return path

def load_history(limit: int = 10) -> List[dict]:
    """
    Load the most recent 'limit' scan reports from disk.
    Returns a list of dicts sorted newest-first.
    """
    files = sorted(
        [f for f in os.listdir(HISTORY_DIR) if f.endswith(".json")],
        reverse=True   # alphabetical reverse = newest first (timestamp in filename)
    )[:limit]
    records = []
    for fname in files:
        try:
            with open(os.path.join(HISTORY_DIR, fname)) as f:
                records.append(json.load(f))
        except Exception:
            pass
    return records

# ═══════════════════════════════════════════════════════════════════════════════
#  DEAD PIXEL / MONITOR TEST WINDOW
# ═══════════════════════════════════════════════════════════════════════════════
PIXEL_COLORS = [
    ("#000000", "Black  –  look for bright stuck pixels"),
    ("#ffffff", "White  –  look for dark dead pixels"),
    ("#ff0000", "Red  –  stuck red sub-pixel"),
    ("#00ff00", "Green  –  stuck green sub-pixel"),
    ("#0000ff", "Blue  –  stuck blue sub-pixel"),
    ("#ff00ff", "Magenta"),
    ("#ffff00", "Yellow"),
    ("#00ffff", "Cyan"),
    ("#808080", "Gray  –  backlight uniformity / bleed"),
]

def launch_pixel_test():
    """Open a fullscreen window that cycles through solid colors."""
    idx = [0]
    win = tk.Toplevel()
    win.title("Monitor Test")
    win.attributes("-fullscreen", True)
    win.configure(bg=PIXEL_COLORS[0][0])
    win.focus_force()

    lbl  = tk.Label(win, text="", bg=PIXEL_COLORS[0][0],
                    fg="#888888", font=(FN, 14), anchor="s")
    lbl.place(relx=0.5, rely=0.96, anchor="s")
    hint = tk.Label(win, text="← → Arrow keys or Click to change  •  ESC to exit",
                    bg=PIXEL_COLORS[0][0], fg="#555555", font=(FN, 10))
    hint.place(relx=0.5, rely=0.99, anchor="s")

    def update():
        c, desc = PIXEL_COLORS[idx[0]]
        win.configure(bg=c)
        lbl.configure(bg=c, text=f"{idx[0]+1}/{len(PIXEL_COLORS)}  {desc}")
        hint.configure(bg=c)
        # Use dark label text on light backgrounds so it stays readable
        fg = "#333333" if c in ("#ffffff", "#ffff00", "#00ffff", "#00ff00") else "#888888"
        lbl.configure(fg=fg); hint.configure(fg=fg)

    def next_(_e=None): idx[0] = (idx[0] + 1) % len(PIXEL_COLORS); update()
    def prev_(_e=None): idx[0] = (idx[0] - 1) % len(PIXEL_COLORS); update()

    win.bind("<Right>",    next_)
    win.bind("<space>",    next_)
    win.bind("<Button-1>", next_)
    win.bind("<Left>",     prev_)
    win.bind("<Escape>",   lambda _: win.destroy())
    update()

# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE GRAPH CANVAS  —  rolling sparkline for CPU or RAM
# ═══════════════════════════════════════════════════════════════════════════════
class SparkGraph(tk.Canvas):
    """
    A canvas that draws a rolling line chart for one metric.
    It stores the last MAX_PTS readings and redraws on every update() call.
    """
    MAX_PTS = 60   # keep 60 data points (= 60 seconds at 1-second intervals)

    def __init__(self, parent, label: str, color: str, **kw):
        super().__init__(parent, bg=RAISED, highlightthickness=0, **kw)
        self.label  = label
        self.color  = color
        self.values: collections.deque = collections.deque(maxlen=self.MAX_PTS)
        self.bind("<Configure>", lambda _e: self._redraw())   # redraw on resize

    def push(self, value: float):
        """Add a new reading and immediately redraw the graph."""
        self.values.append(max(0.0, min(100.0, value)))   # clamp to 0-100
        self._redraw()

    def _redraw(self):
        self.delete("all")
        W = self.winfo_width()
        H = self.winfo_height()
        if W < 10 or H < 10 or not self.values:
            return

        pad_l, pad_r, pad_t, pad_b = 38, 10, 14, 22

        # ── Background grid lines ─────────────────────────────────────────────
        for pct in (25, 50, 75, 100):
            y = pad_t + (H - pad_t - pad_b) * (1 - pct / 100)
            self.create_line(pad_l, y, W - pad_r, y, fill=BORDER, dash=(3, 4))
            self.create_text(pad_l - 4, y, text=f"{pct}%", fill=SUB,
                             font=(FNM, 7), anchor="e")

        # ── Latest value badge ────────────────────────────────────────────────
        cur   = self.values[-1]
        # Choose badge color based on value
        badge = GREEN if cur < 60 else (YELLOW if cur < 80 else RED)
        self.create_text(W - pad_r, pad_t - 4, text=f"{cur:.0f}%",
                         fill=badge, font=(FNM, 9, "bold"), anchor="ne")

        # ── Chart label ───────────────────────────────────────────────────────
        self.create_text(pad_l, pad_t - 4, text=self.label,
                         fill=SUB, font=(FN, 8), anchor="nw")

        # ── Data line ─────────────────────────────────────────────────────────
        pts    = list(self.values)
        n      = len(pts)
        x_step = (W - pad_l - pad_r) / max(self.MAX_PTS - 1, 1)
        h_plot = H - pad_t - pad_b

        coords = []
        for i, v in enumerate(pts):
            # Offset from the right: most recent point is at the far right
            x = W - pad_r - (n - 1 - i) * x_step
            y = pad_t + h_plot * (1 - v / 100)
            coords += [x, y]

        if len(coords) >= 4:
            self.create_line(*coords, fill=self.color, width=2, smooth=True)

        # ── Filled area under the line ────────────────────────────────────────
        if len(coords) >= 4:
            fill_coords = list(coords)
            fill_coords += [W - pad_r, H - pad_b, pad_l + (0 if n < self.MAX_PTS else 0), H - pad_b]
            # Use a slightly transparent-feeling shade of the line color
            self.create_polygon(*fill_coords, fill=BORDER, outline="")

        # ── Bottom axis ───────────────────────────────────────────────────────
        self.create_text(W // 2, H - 4, text="← 60 s", fill=SUB,
                         font=(FNM, 7), anchor="s")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION CLASS
# ═══════════════════════════════════════════════════════════════════════════════
class DiagApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("System Diagnostic Tool  v3.0")
        self.geometry("960x720")
        self.minsize(780, 580)
        self.configure(bg=BG)

        # ── State variables ───────────────────────────────────────────────────
        self._auto_var    = tk.BooleanVar(value=False)
        self._auto_job    = None         # holds the after() job ID for auto-refresh
        self._sort_by     = tk.StringVar(value="cpu")   # process sort preference
        self._last_report = None         # most recent scan data dict (for copy/save)

        self._build_ui()
        self.after(150, self.run_diagnostic)   # kick off the first scan shortly after startup

    # ── ttk style sheet ───────────────────────────────────────────────────────
    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TNotebook",        background=BG, borderwidth=0)
        s.configure("TNotebook.Tab",    background=RAISED, foreground=SUB,
                    padding=[14, 6], font=(FN, 10))
        s.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", TEXT)])
        s.configure("TFrame",           background=CARD)
        s.configure("TLabel",           background=CARD, foreground=TEXT, font=(FN, 10))
        s.configure("TScrollbar",       background=RAISED, troughcolor=CARD, arrowcolor=SUB)
        s.configure("Treeview",         background=RAISED, fieldbackground=RAISED,
                    foreground=TEXT, font=(FNM, 9), rowheight=22)
        s.configure("Treeview.Heading", background=CARD, foreground=ACC, font=(FN, 9, "bold"))
        s.map("Treeview", background=[("selected", ACC)])
        s.configure("Horizontal.TProgressbar",
                    troughcolor=BORDER, background=ACC, thickness=8)

    # ── Build the entire UI skeleton ──────────────────────────────────────────
    def _build_ui(self):
        self._setup_style()

        # ── Top header bar ────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=CARD, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚙  System Diagnostic Tool",
                 bg=CARD, fg=TEXT, font=(FN, 15, "bold")).pack(side="left", padx=18)
        tk.Label(hdr, text="v3.0", bg=CARD, fg=SUB, font=(FN, 9)).pack(side="left")

        ctrl = tk.Frame(hdr, bg=CARD)
        ctrl.pack(side="right", padx=14)

        self._run_btn = tk.Button(ctrl, text="▶  Run Scan",
                                  command=self.run_diagnostic,
                                  bg=ACC, fg="white", relief="flat",
                                  font=(FN, 10, "bold"), padx=12, pady=4,
                                  activebackground="#6040dd", cursor="hand2")
        self._run_btn.pack(side="left", padx=6)

        tk.Button(ctrl, text="📋  Copy Report", command=self._copy_report,
                  bg=RAISED, fg=TEXT, relief="flat",
                  font=(FN, 10), padx=10, pady=4,
                  cursor="hand2", activebackground=CARD).pack(side="left", padx=6)

        tk.Checkbutton(ctrl, text="Auto 30s", variable=self._auto_var,
                       command=self._toggle_auto,
                       bg=CARD, fg=SUB, selectcolor=RAISED,
                       activebackground=CARD, font=(FN, 9)).pack(side="left", padx=6)

        # ── Alert banner (hidden unless there are warnings) ───────────────────
        self._alert_frame = tk.Frame(self, bg=RED, pady=4)
        # Don't pack it yet — it appears only when alerts fire

        self._alert_lbl = tk.Label(self._alert_frame, text="", bg=RED, fg="white",
                                   font=(FN, 9, "bold"), wraplength=900)
        self._alert_lbl.pack(padx=12)

        # ── Bottom status bar ─────────────────────────────────────────────────
        self._status = tk.Label(self, text="Ready — click Run Scan to begin.",
                                bg=BG, fg=SUB, font=(FN, 9), anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=3)

        # ── Notebook tabs ─────────────────────────────────────────────────────
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=10, pady=(6, 4))

        self._tab_dash    = self._make_tab("🖥  Dashboard")
        self._tab_live    = self._make_tab("📈  Live Monitor")
        self._tab_procs   = self._make_tab("⚡  Processes")
        self._tab_storage = self._make_tab("💾  Storage")
        self._tab_net     = self._make_tab("🌐  Network")
        self._tab_history = self._make_tab("📂  History")
        self._tab_monitor = self._make_tab("🖵  Monitor Test")
        self._tab_help    = self._make_tab("❓  Help")

        self._build_dashboard_tab()
        self._build_live_tab()
        self._build_processes_tab()
        self._build_storage_tab()
        self._build_network_tab()
        self._build_history_tab()
        self._build_monitor_tab()
        self._build_help_tab()

        # Start the live graph ticker right away (independent of full scan)
        self._tick_live()

    def _make_tab(self, label: str) -> ttk.Frame:
        """Create a new notebook tab and return its frame."""
        f = ttk.Frame(self._nb)
        self._nb.add(f, text=label)
        return f

    # ── DASHBOARD TAB ─────────────────────────────────────────────────────────
    def _build_dashboard_tab(self):
        p = self._tab_dash
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)
        p.rowconfigure(0, weight=1)

        # Left column: health score circle + reasoning text
        self._score_frame = tk.Frame(p, bg=CARD)
        self._score_frame.grid(row=0, column=0, sticky="nsew", padx=(8,4), pady=8)
        self._score_canvas = tk.Canvas(self._score_frame, bg=CARD,
                                        highlightthickness=0, width=200, height=170)
        self._score_canvas.pack(pady=(10, 0))
        tk.Label(self._score_frame, text="System Health",
                 bg=CARD, fg=SUB, font=(FN, 9)).pack()
        self._score_reason_box = self._scrolled_text(self._score_frame, height=8)
        self._score_reason_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Right column: system info + mini bars for RAM and each disk
        self._sysinfo_frame = self._card(p, "System Information")
        self._sysinfo_frame.grid(row=0, column=1, sticky="nsew", padx=(4,8), pady=8)
        self._sysinfo_box = self._scrolled_text(self._sysinfo_frame, height=16)
        self._sysinfo_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Temperature row spans both columns
        self._temp_frame = self._card(p, "Temperatures")
        self._temp_frame.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=8, pady=(0, 8))
        self._temp_inner = tk.Frame(self._temp_frame, bg=CARD)
        self._temp_inner.pack(fill="x", padx=12, pady=(0, 10))

    # ── LIVE MONITOR TAB ──────────────────────────────────────────────────────
    def _build_live_tab(self):
        p = self._tab_live
        p.rowconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)
        p.columnconfigure(0, weight=1)

        # CPU graph
        cpu_card = self._card(p, "CPU Usage  (live, 1-second interval)")
        cpu_card.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        cpu_card.rowconfigure(1, weight=1)
        cpu_card.columnconfigure(0, weight=1)
        self._graph_cpu = SparkGraph(cpu_card, "CPU %", ACC, height=140)
        self._graph_cpu.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # RAM graph
        ram_card = self._card(p, "RAM Usage  (live, 1-second interval)")
        ram_card.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 8))
        self._graph_ram = SparkGraph(ram_card, "RAM %", TEAL, height=140)
        self._graph_ram.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _tick_live(self):
        """Push a new data point to both live graphs, then reschedule in 1 second."""
        try:
            self._graph_cpu.push(psutil.cpu_percent(interval=None))
            self._graph_ram.push(psutil.virtual_memory().percent)
        except Exception:
            pass
        # Schedule the next tick — runs forever while the window is open
        self.after(1000, self._tick_live)

    # ── PROCESSES TAB ─────────────────────────────────────────────────────────
    def _build_processes_tab(self):
        p = self._tab_procs
        p.rowconfigure(1, weight=1)
        p.columnconfigure(0, weight=1)

        # Sort-by toolbar
        bar = tk.Frame(p, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        tk.Label(bar, text="Sort by:", bg=BG, fg=SUB, font=(FN, 9)).pack(side="left")
        for val, lbl in (("cpu", "CPU %"), ("ram", "RAM")):
            tk.Radiobutton(bar, text=lbl, variable=self._sort_by, value=val,
                           bg=BG, fg=TEXT, selectcolor=RAISED, activebackground=BG,
                           font=(FN, 9), command=self.run_diagnostic).pack(side="left", padx=6)

        # Process tree
        outer = tk.Frame(p, bg=BG)
        outer.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 2))
        outer.rowconfigure(0, weight=1); outer.columnconfigure(0, weight=1)

        cols = ("PID", "Name", "CPU %", "RAM (MB)")
        self._proc_tree = ttk.Treeview(outer, columns=cols, show="headings",
                                        selectmode="browse")
        for c, w in zip(cols, (65, 280, 85, 110)):
            self._proc_tree.heading(c, text=c)
            self._proc_tree.column(c, width=w, anchor="w" if c == "Name" else "center")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._proc_tree.yview)
        self._proc_tree.configure(yscrollcommand=vsb.set)
        self._proc_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Right-click context menu to kill a process
        self._proc_menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT,
                                   activebackground=ACC, activeforeground="white")
        self._proc_menu.add_command(label="⛔  Kill Process", command=self._kill_selected)
        self._proc_tree.bind("<Button-3>", self._show_proc_menu)   # right-click

        tk.Label(p, text="Right-click a process to kill it  •  refreshed on each scan",
                 bg=BG, fg=SUB, font=(FN, 9)).grid(row=2, column=0, sticky="w", padx=10, pady=2)

    def _show_proc_menu(self, event):
        """Show the right-click kill menu when the user right-clicks a process row."""
        row = self._proc_tree.identify_row(event.y)
        if row:
            self._proc_tree.selection_set(row)
            self._proc_menu.post(event.x_root, event.y_root)

    def _kill_selected(self):
        """Kill whichever process is currently selected in the tree."""
        sel = self._proc_tree.selection()
        if not sel:
            return
        vals = self._proc_tree.item(sel[0], "values")
        pid  = int(vals[0])
        name = vals[1]
        if not messagebox.askyesno("Kill Process",
                                    f"End process '{name}'  (PID {pid})?"):
            return
        try:
            proc = psutil.Process(pid)
            proc.terminate()   # polite SIGTERM first
            self._status.configure(text=f"Sent termination signal to {name} ({pid})", fg=YELLOW)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── STORAGE TAB ───────────────────────────────────────────────────────────
    def _build_storage_tab(self):
        p = self._tab_storage
        p.rowconfigure(0, weight=1)
        p.columnconfigure(0, weight=1)

        card = self._card(p, "All Disk Partitions")
        card.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Inner table container packed into the card; grid used only inside this container
        table = tk.Frame(card, bg=CARD)
        table.pack(fill="both", expand=True, padx=8, pady=(0,8))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        # Treeview with one row per partition
        cols = ("Mount", "Device", "FS", "Total", "Used", "Free", "Usage %")
        self._disk_tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="none")
        widths = [110, 140, 60, 90, 90, 90, 75]
        for c, w in zip(cols, widths):
            self._disk_tree.heading(c, text=c)
            self._disk_tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(table, orient="vertical", command=self._disk_tree.yview)
        self._disk_tree.configure(yscrollcommand=vsb.set)
        self._disk_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns", padx=(6,0))

        # Usage bars drawn on canvas below the table
        self._disk_bars_frame = tk.Frame(p, bg=BG)
        self._disk_bars_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(0,8))

    def _update_disk_tab(self, disks: List[dict]):
        """Refresh the disk partition table and draw usage bars."""
        # Clear old rows
        for row in self._disk_tree.get_children():
            self._disk_tree.delete(row)

        for d in disks:
            tag = "warn" if d["pct"] >= 80 else ("ok" if d["pct"] < 60 else "")
            self._disk_tree.insert("", "end",
                values=(d["mountpoint"], d["device"], d["fstype"],
                        d["total"], d["used"], d["free"], f"{d['pct']}%"),
                tags=(tag,))

        # Color code high-usage partitions in the tree
        self._disk_tree.tag_configure("warn", foreground=ORANGE)

        # Rebuild usage bar widgets
        for w in self._disk_bars_frame.winfo_children():
            w.destroy()
        for d in disks:
            self._disk_bar(self._disk_bars_frame, d["mountpoint"], d["pct"])

    def _disk_bar(self, parent, label: str, pct: float):
        """Draw a labelled horizontal progress bar for one partition."""
        f   = tk.Frame(parent, bg=BG)
        f.pack(fill="x", padx=10, pady=2)
        col = GREEN if pct < 60 else (YELLOW if pct < 80 else RED)
        tk.Label(f, text=f"{label:<20}", bg=BG, fg=SUB,
                 font=(FNM, 8), width=22, anchor="w").pack(side="left")
        bar_bg = tk.Frame(f, bg=BORDER, height=10)
        bar_bg.pack(side="left", fill="x", expand=True)
        bar_bg.update_idletasks()
        fill_w = int(bar_bg.winfo_reqwidth() * pct / 100)
        tk.Frame(bar_bg, bg=col, height=10, width=max(fill_w, 2)).place(x=0, y=0)
        tk.Label(f, text=f"{pct:.0f}%", bg=BG, fg=col,
                 font=(FNM, 8), width=5).pack(side="left", padx=(6, 0))

    # ── NETWORK TAB ───────────────────────────────────────────────────────────
    def _build_network_tab(self):
        p = self._tab_net
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)
        p.rowconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        # Network interfaces card (top-left)
        iface_card = self._card(p, "Network Interfaces")
        iface_card.grid(row=0, column=0, sticky="nsew", padx=(8,4), pady=8)
        cols = ("Interface", "IPv4", "MAC", "Status", "Speed")
        self._iface_tree = ttk.Treeview(iface_card, columns=cols,
                                         show="headings", selectmode="none", height=7)
        for c, w in zip(cols, [120, 120, 140, 60, 80]):
            self._iface_tree.heading(c, text=c)
            self._iface_tree.column(c, width=w, anchor="center")
        self._iface_tree.pack(fill="both", expand=True, padx=8, pady=(0,8))

        # I/O counters card (top-right)
        io_card = self._card(p, "Network I/O  (since boot)")
        io_card.grid(row=0, column=1, sticky="nsew", padx=(4,8), pady=8)
        self._net_io_box = self._scrolled_text(io_card, height=7)
        self._net_io_box.pack(fill="both", expand=True, padx=8, pady=(0,8))

        # Ping latency card (bottom, spans both columns)
        ping_card = self._card(p, "Ping Latency")
        ping_card.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0,8))
        ping_inner = tk.Frame(ping_card, bg=CARD)
        ping_inner.pack(fill="x", padx=12, pady=(0,10))
        self._ping_labels: List[tk.Label] = []
        for _ in range(3):   # three hosts
            lbl = tk.Label(ping_inner, text="—", bg=RAISED, fg=TEXT,
                           font=(FNM, 10), padx=16, pady=6,
                           relief="flat",
                           highlightthickness=1, highlightbackground=BORDER)
            lbl.pack(side="left", expand=True, fill="x", padx=6)
            self._ping_labels.append(lbl)

        tk.Button(ping_card, text="🔄  Run Ping Test", command=self._run_ping,
                  bg=RAISED, fg=TEXT, font=(FN, 9), relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(pady=(0,8))

    def _run_ping(self):
        """Start a background ping test and update the ping labels when done."""
        for lbl in self._ping_labels:
            lbl.configure(text="Testing...", fg=SUB)
        def _do():
            results = ping_hosts()
            self.after(0, lambda: self._show_ping(results))
        threading.Thread(target=_do, daemon=True).start()

    def _show_ping(self, results: List[Tuple[str, str]]):
        """Update the three ping cards with results."""
        for i, (name, latency) in enumerate(results):
            if i >= len(self._ping_labels):
                break
            col = GREEN if "ms" in latency and float(latency.split()[0]) < 50 else (
                  YELLOW if "ms" in latency else RED)
            self._ping_labels[i].configure(
                text=f"{name}\n{latency}", fg=col)

    # ── HISTORY TAB ───────────────────────────────────────────────────────────
    def _build_history_tab(self):
        p = self._tab_history
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        tk.Label(p, text="Last 10 scans are saved automatically to  ./diag_reports/",
                 bg=BG, fg=SUB, font=(FN, 9)).grid(row=0, column=0,
                 sticky="w", padx=12, pady=(8, 2))

        # Top list of past scans
        outer = tk.Frame(p, bg=BG)
        outer.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        outer.columnconfigure(0, weight=1); outer.rowconfigure(0, weight=1)

        cols = ("Timestamp", "Score", "Rating", "CPU Temp", "RAM %", "Worst Disk")
        self._hist_tree = ttk.Treeview(outer, columns=cols, show="headings",
                                        selectmode="browse", height=10)
        for c, w in zip(cols, [170, 65, 85, 90, 75, 100]):
            self._hist_tree.heading(c, text=c)
            self._hist_tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._hist_tree.yview)
        self._hist_tree.configure(yscrollcommand=vsb.set)
        self._hist_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._hist_tree.bind("<<TreeviewSelect>>", self._on_history_select)

        # Detail pane below the list
        detail_card = self._card(p, "Scan Detail")
        detail_card.grid(row=2, column=0, sticky="ew", padx=8, pady=(0,8))
        self._hist_detail = self._scrolled_text(detail_card, height=8)
        self._hist_detail.pack(fill="both", expand=True, padx=8, pady=(0,8))

        tk.Button(p, text="🔄  Refresh history list", command=self._refresh_history,
                  bg=RAISED, fg=TEXT, font=(FN, 9), relief="flat",
                  padx=10, pady=4).grid(row=3, column=0, pady=4)

        self._refresh_history()   # load whatever is on disk right now

    def _refresh_history(self):
        """Re-read the history folder and repopulate the tree."""
        for row in self._hist_tree.get_children():
            self._hist_tree.delete(row)
        self._history_records = load_history(10)
        for rec in self._history_records:
            ts    = rec.get("timestamp", "?")
            score = rec.get("score", "?")
            rating= rec.get("rating", "?")
            ct    = rec.get("cpu_temp", None)
            ct_s  = f"{ct:.0f}°C" if ct else "N/A"
            ram   = rec.get("mem_pct", "?")
            wd    = rec.get("worst_disk_pct", "?")
            self._hist_tree.insert("", "end",
                values=(ts, score, rating, ct_s,
                        f"{ram:.0f}%" if isinstance(ram, float) else ram,
                        f"{wd:.0f}%" if isinstance(wd, float) else wd))

    def _on_history_select(self, _event=None):
        """Show the full detail of the selected historical scan."""
        sel = self._hist_tree.selection()
        if not sel:
            return
        idx = self._hist_tree.index(sel[0])
        if idx >= len(self._history_records):
            return
        rec  = self._history_records[idx]
        text = json.dumps(rec, indent=2, default=str)
        self._write(self._hist_detail, text)

    # ── MONITOR TEST TAB ──────────────────────────────────────────────────────
    def _build_monitor_tab(self):
        p = self._tab_monitor
        p.columnconfigure(0, weight=1)

        card = self._card(p, "Dead Pixel & Monitor Test")
        card.grid(row=0, column=0, sticky="ew", padx=40, pady=30)

        info = (
            "How to use\n"
            "──────────\n"
            "Click Launch to enter fullscreen.  A solid color fills the screen.\n"
            "Scan every corner and edge slowly — any pixel that looks different\n"
            "from its neighbours is stuck or dead.\n\n"
            "  BLACK  →  look for bright / colored stuck pixels\n"
            "  WHITE  →  look for dark dead pixels\n"
            "  RED / GREEN / BLUE  →  single-channel stuck pixel check\n"
            "  GRAY   →  backlight uniformity and bleed check\n\n"
            "  Click  or  → key  →  next color\n"
            "  ← key             →  previous color\n"
            "  ESC               →  exit\n\n"
            "Tip: dim the room lights for best results."
        )
        tk.Label(card, text=info, bg=CARD, fg=TEXT, font=(FN, 11),
                 justify="left", anchor="w").pack(padx=20, pady=(4, 16))
        tk.Button(card, text="🖵  Launch Fullscreen Test",
                  command=launch_pixel_test,
                  bg=ACC, fg="white", font=(FN, 12, "bold"),
                  relief="flat", padx=20, pady=8, cursor="hand2",
                  activebackground="#6040dd").pack(pady=(0, 20))

    # ── HELP TAB ─────────────────────────────────────────────────────────────
    def _build_help_tab(self):
        p = self._tab_help
        p.columnconfigure(0, weight=1)
        p.rowconfigure(0, weight=1)

        txt = self._scrolled_text(p, height=30)
        txt.pack(fill="both", expand=True, padx=10, pady=10)

        content = """\
HOW TO USE THIS TOOL  —  v3.0
══════════════════════════════

-  Run Scan
   Collects all diagnostics and updates every tab at once.
   Runs automatically once at startup.

-  Copy Report
   Copies a plain-text summary of the last scan to your clipboard.

-  Auto 30s
   Ticks the scan automatically every 30 seconds.
   Great for watching temperatures while stress-testing.

-  Dashboard
   Health Score (0–100) with a color arc gauge.
   Scoring breakdown per category (RAM, disk, temps, net, battery).
   System info: CPU model, GPU model, core count, clock speed, uptime.
   Temperature gauges with color-coded severity.

-  Live Monitor
   Rolling 60-second graphs of CPU % and RAM %.
   Updates every second — no manual scan needed.
   Color of the latest-value badge: green < 60%, yellow < 80%, red ≥ 80%.

-  Processes
   Top 12 processes.  Sort by CPU % or RAM with the radio buttons.
   Right-click any row → Kill Process to send a termination signal.

-  Storage
   Every mounted partition: device, filesystem, size, used, free, %.
   Color-coded usage bars below the table.

-  Network
   All network interfaces with IPv4, MAC address, link speed.
   Cumulative I/O counters (bytes / packets / errors) since boot.
   Ping test to Google DNS, Cloudflare, and OpenDNS.

-  History
   Every scan is auto-saved as JSON in ./diag_reports/.
   Click a row to see the full JSON detail of that scan.
   Click "Refresh history list" to pick up new saves.

-  Monitor Test
   Fullscreen dead-pixel tester (9 test colors).

──────────────────────────────────────────────────────────────────

TEMPERATURE NOTES
─────────────────
- Linux / macOS: sensors read automatically via psutil.
- Windows: install OpenHardwareMonitor, then:
      pip install wmi
- For NVIDIA GPU details:
      pip install gputil

──────────────────────────────────────────────────────────────────

ALERT THRESHOLDS  (edit top of source file to change)
──────────────────────────────────────────────────────
  CPU temp  ≥ 85°C   →  red banner
  GPU temp  ≥ 85°C   →  red banner
  RAM       ≥ 88%    →  red banner
  Disk      ≥ 90%    →  red banner

──────────────────────────────────────────────────────────────────

FEATURE IDEAS FOR v4.0
─────────────────────
  1. Real bandwidth speed test (speedtest-cli)
  2. Startup program manager (Windows Registry / systemd)
  3. Temp folder cleaner — show and delete junk files
  4. Speaker / microphone channel test
  5. GPU clock speed display (nvidia-smi / GPUtil)
  6. USB and Bluetooth peripheral inventory
  7. Security check: firewall, AV status, pending updates
  8. Export history to CSV / HTML report
  9. Light / dark theme toggle
 10. AI-powered summary?: one-sentenced plain-English health diagnosis
"""
        txt.insert("1.0", content)
        txt.configure(state="disabled")

    # ── Shared widget helpers ─────────────────────────────────────────────────
    def _card(self, parent, title: str) -> tk.Frame:
        """Create a labelled dark card frame."""
        outer = tk.Frame(parent, bg=CARD, relief="flat")
        tk.Label(outer, text=title, bg=CARD, fg=ACC,
                 font=(FN, 10, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(0, 4))
        return outer

    def _scrolled_text(self, parent, height: int = 8) -> tk.Text:
        """Create a read-only scrollable text box styled to the dark theme."""
        frame = tk.Frame(parent, bg=RAISED, relief="flat")
        frame.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(frame)
        t  = tk.Text(frame, bg=RAISED, fg=TEXT, insertbackground=TEXT,
                     font=(FNM, 9), relief="flat", wrap="word",
                     yscrollcommand=sb.set, height=height,
                     selectbackground=ACC, selectforeground="white",
                     borderwidth=0, padx=6, pady=4)
        sb.configure(command=t.yview)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    def _write(self, widget: tk.Text, text: str):
        """Replace all content in a Text widget (enabling write, then locking again)."""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ── Health score arc drawing ──────────────────────────────────────────────
    def _draw_score(self, score: int):
        c = self._score_canvas
        c.delete("all")
        W, H  = 200, 155
        cx, cy, r = W // 2, H // 2 + 10, 62

        # Grey background arc (the 'track')
        c.create_arc(cx-r, cy-r, cx+r, cy+r,
                     start=220, extent=-260,
                     outline=RAISED, width=12, style="arc")

        # Colored arc proportional to the score
        extent = -260 * (score / 100)
        col    = score_color(score)
        c.create_arc(cx-r, cy-r, cx+r, cy+r,
                     start=220, extent=extent,
                     outline=col, width=12, style="arc")

        # Score number and label inside the arc
        c.create_text(cx, cy - 8,  text=str(score),        fill=col, font=(FN, 28, "bold"))
        c.create_text(cx, cy + 22, text=score_label(score), fill=col, font=(FN, 10, "bold"))
        c.create_text(cx, cy + 38, text="Health Score",     fill=SUB, font=(FN, 8))

    # ── Temperature gauge widget ──────────────────────────────────────────────
    def _draw_temp_gauge(self, parent, label: str, temp: Optional[float], reason: Optional[str] = None, extra: Optional[str] = None):
        """Draw a card showing a temperature value with a color-coded fill bar.
        If temp is None and a reason is provided, show the reason below the value.
        If extra is provided (e.g., NVIDIA util/mem), show it in muted text.
        """
        f = tk.Frame(parent, bg=RAISED, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER)
        f.pack(side="left", expand=True, fill="x", padx=8, pady=6)

        val = f"{temp:.0f}°C" if temp is not None else "Unavailable"
        col = (SUB if temp is None
               else GREEN  if temp < 65
               else YELLOW if temp < 80
               else ORANGE if temp < 90
               else RED)

        tk.Label(f, text=label, bg=RAISED, fg=SUB, font=(FN, 9)).pack(pady=(8, 0))
        tk.Label(f, text=val,   bg=RAISED, fg=col, font=(FN, 20, "bold")).pack()

        # Mini fill bar — temp / 100 gives the fraction (capped at 1.0)
        bar_w = 160
        pct   = min(temp / 100, 1.0) if temp is not None else 0.0
        cnv   = tk.Canvas(f, bg=RAISED, highlightthickness=0, width=bar_w, height=8)
        cnv.pack(pady=(4, 6))
        cnv.create_rectangle(0, 0, bar_w, 8, fill=BORDER, outline="")
        cnv.create_rectangle(0, 0, int(bar_w * pct), 8, fill=col, outline="")

        # Optional extra info or reason text
        if temp is None and reason:
            tk.Label(f, text=f"Reason: {reason}", bg=RAISED, fg=SUB, font=(FNM, 8), wraplength=220, justify="center").pack(padx=6, pady=(2, 8))
        elif extra:
            tk.Label(f, text=extra, bg=RAISED, fg=SUB, font=(FNM, 8), wraplength=220, justify="center").pack(padx=6, pady=(2, 8))

    # ── Main scan runner ──────────────────────────────────────────────────────
    def run_diagnostic(self):
        """Disable the button and kick off data collection in a background thread."""
        self._run_btn.configure(text="...  Scanning", state="disabled")
        self._status.configure(text="Scanning — please wait...", fg=YELLOW)
        self.update_idletasks()   # flush UI so the button text visually changes
        threading.Thread(target=self._collect_and_update, daemon=True).start()

    def _collect_and_update(self):
        """Run all the slow hardware reads in a background thread, then hand off to the UI thread."""
        sysinfo  = get_system_info()
        internet = check_internet()
        mem      = get_memory_info()
        disks    = get_all_disk_info()
        net_io   = get_network_io()
        ifaces   = get_network_interfaces()
        temps    = get_temperatures()
        battery  = get_battery()
        procs    = get_top_processes(sort_by=self._sort_by.get())

        score, reasons = compute_health_score(
            mem["Usage %"], disks, temps["CPU"], temps["GPU"], internet, battery)

        alerts = build_alerts(temps["CPU"], temps["GPU"], mem["Usage %"], disks)

        # after(0, ...) safely moves the UI update back onto the main thread
        self.after(0, lambda: self._update_ui(
            sysinfo, internet, mem, disks, net_io, ifaces,
            temps, battery, procs, score, reasons, alerts))

    def _update_ui(self, sysinfo, internet, mem, disks, net_io, ifaces,
                   temps, battery, procs, score, reasons, alerts):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Alert banner ──────────────────────────────────────────────────────
        if alerts:
            self._alert_lbl.configure(text="  ".join(alerts))
            self._alert_frame.pack(fill="x", after=self.winfo_children()[0])
        else:
            self._alert_frame.pack_forget()   # hide banner when all clear

        # ── Dashboard ─────────────────────────────────────────────────────────
        self._draw_score(score)

        reason_txt  = f"Score: {score}/100 — {score_label(score)}\n"
        reason_txt += "─" * 36 + "\n" + "\n".join(reasons)
        self._write(self._score_reason_box, reason_txt)

        si_txt = ""
        for k, v in sysinfo.items():
            si_txt += f"{k:<14} {v}\n"
        si_txt += f"\n{'Internet':<14} {'Online' if internet else 'Offline'}\n"
        si_txt += f"{'RAM':<14} {mem['Used']} / {mem['Total']}  ({mem['Usage %']}%)\n"
        self._write(self._sysinfo_box, si_txt)

        # Rebuild temp gauges each scan
        for w in self._temp_inner.winfo_children():
            w.destroy()
        cpu_reason = None
        gpu_reason = None
        try:
            cpu_reason = temps.get("reasons", {}).get("CPU")
            gpu_reason = temps.get("reasons", {}).get("GPU")
        except Exception:
            pass
        # Optional NVIDIA extra line
        extra_gpu = None
        nv = temps.get("nvidia", {}) if isinstance(temps, dict) else {}
        if nv.get("available"):
            util = nv.get("util")
            mu = nv.get("mem_used")
            mt = nv.get("mem_total")
            name = nv.get("name") or "NVIDIA GPU"
            parts = [name]
            if isinstance(util, (int, float)):
                parts.append(f"Util {util:.0f}%")
            if isinstance(mu, (int, float)) and isinstance(mt, (int, float)) and mt > 0:
                parts.append(f"Mem {mu:.0f}/{mt:.0f} MiB")
            extra_gpu = " • ".join(parts)
        elif nv.get("available") is False and not gpu_reason:
            # If nvidia-smi explicitly unavailable, include its reason
            gpu_reason = nv.get("reason")

        self._draw_temp_gauge(self._temp_inner, "CPU Temperature", temps["CPU"], reason=cpu_reason)
        self._draw_temp_gauge(self._temp_inner, "GPU Temperature", temps["GPU"], reason=gpu_reason, extra=extra_gpu)
        if temps["raw"]:
            extra = {k: max(t for t in v) for k, v in temps["raw"].items() if v}
            for sensor, val in list(extra.items())[:2]:
                if sensor.lower() not in ("coretemp", "k10temp", "cpu_thermal"):
                    self._draw_temp_gauge(self._temp_inner, sensor.capitalize(), val)

        # ── Processes ─────────────────────────────────────────────────────────
        for row in self._proc_tree.get_children():
            self._proc_tree.delete(row)
        for pid, name, cpu, mem_mb in procs:
            self._proc_tree.insert("", "end",
                values=(pid, name, f"{cpu:.1f}", f"{mem_mb:.0f}"))

        # ── Storage ───────────────────────────────────────────────────────────
        self._update_disk_tab(disks)

        # ── Network ───────────────────────────────────────────────────────────
        for row in self._iface_tree.get_children():
            self._iface_tree.delete(row)
        for iface in ifaces:
            self._iface_tree.insert("", "end",
                values=(iface["Interface"], iface["IPv4"],
                        iface["MAC"], iface["Status"], iface["Speed"]))

        net_txt = "\n".join(f"{k:<18} {v}" for k, v in net_io.items())
        net_txt += f"\n\n{'Internet':<18} {'Online ✓' if internet else 'Offline ✗'}"
        self._write(self._net_io_box, net_txt)

        # ── Auto-run ping on first load and every scan ─────────────────────
        self._run_ping()

        # ── Save report to disk and refresh history list ───────────────────
        worst_disk = max((d["pct"] for d in disks), default=0)
        report_data = {
            "timestamp":    now,
            "score":        score,
            "rating":       score_label(score),
            "cpu_temp":     temps["CPU"],
            "gpu_temp":     temps["GPU"],
            "mem_pct":      mem["Usage %"],
            "worst_disk_pct": worst_disk,
            "sysinfo":      sysinfo,
            "memory":       mem,
            "disks":        disks,
            "net_io":       net_io,
            "battery":      battery,
            "reasons":      reasons,
            "alerts":       alerts,
        }
        save_report(report_data)
        self._refresh_history()

        # Keep a copy in memory for the clipboard button
        self._last_report = self._build_report_text(
            sysinfo, internet, mem, disks, net_io, temps,
            battery, procs, score, reasons, now)

        # ── Restore button and status bar ────────────────────────────────────
        self._run_btn.configure(text="▶  Run Scan", state="normal")
        self._status.configure(
            text=f"Last scan: {now}  •  Health Score: {score}/100 — {score_label(score)}",
            fg=score_color(score))

    # ── Build a plain-text report string for the clipboard ───────────────────
    def _build_report_text(self, sysinfo, internet, mem, disks, net_io,
                            temps, battery, procs, score, reasons, timestamp) -> str:
        lines = [
            "=" * 54,
            "  SYSTEM DIAGNOSTIC REPORT",
            f"  {timestamp}",
            "=" * 54,
            "",
            "[System Info]",
        ] + [f"  {k:<16} {v}" for k, v in sysinfo.items()] + [
            "",
            "[Health Score]",
            f"  {score}/100 — {score_label(score)}",
        ] + reasons + [
            "",
            "[Memory]",
        ] + [f"  {k:<14} {v}" for k, v in mem.items()] + [
            "",
            "[Disk Partitions]",
        ]
        for d in disks:
            lines.append(f"  {d['mountpoint']:<16} {d['used']} / {d['total']}  ({d['pct']}%)")
        lines += [
            "",
            "[Network I/O]",
            f"  Internet       {'Online' if internet else 'Offline'}",
        ] + [f"  {k:<16} {v}" for k, v in net_io.items()] + [
            "",
            "[Temperatures]",
            "  CPU: " + (f"{temps['CPU']:.1f}°C" if temps['CPU'] is not None else "N/A"),
            "  GPU: " + (f"{temps['GPU']:.1f}°C" if temps['GPU'] is not None else "N/A"),
        ]
        if battery:
            lines += ["", "[Battery]"] + [f"  {k:<14} {v}" for k, v in battery.items()]
        lines += [
            "",
            "[Top Processes]",
            f"  {'PID':<7} {'Name':<28} {'CPU%':>6}  {'RAM MB':>8}",
            "  " + "─" * 54,
        ] + [f"  {pid:<7} {name:<28} {cpu:>5.1f}%  {mem_mb:>7.0f} MB"
             for pid, name, cpu, mem_mb in procs]
        return "\n".join(lines)

    def _copy_report(self):
        """Copy the last scan report to the system clipboard."""
        if not self._last_report:
            messagebox.showinfo("No report", "Run a scan first.")
            return
        self.clipboard_clear()
        self.clipboard_append(self._last_report)
        self._status.configure(text="Report copied to clipboard!", fg=GREEN)

    # ── Auto-refresh logic ────────────────────────────────────────────────────
    def _toggle_auto(self):
        """Start or stop the 30-second auto-refresh timer."""
        if self._auto_var.get():
            self._schedule_auto()
        else:
            if self._auto_job:
                self.after_cancel(self._auto_job)
                self._auto_job = None

    def _schedule_auto(self):
        if self._auto_var.get():
            self.run_diagnostic()
            self._auto_job = self.after(30_000, self._schedule_auto)

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = DiagApp()
    app.mainloop()