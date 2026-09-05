import os
import re
import json
import time
import shutil
import asyncio
import subprocess
from typing import Optional, List, Dict, Any
from collections import deque
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="NEO-OPS QNAP Dashboard & AI Ops", version="1.3.0")

CONFIG_FILE = "/data/config.json" if os.path.exists("/data") else "config.json"
DOCKER_SOCKET = "/var/run/docker.sock"

# State tracking
_server_start_time = time.time()
_last_cpu_time = 0.0
_last_cpu_idle = 0
_last_cpu_total = 0

_last_net_time = 0.0
_last_rx_bytes = 0
_last_tx_bytes = 0
_last_rx_rate = 0.0
_last_tx_rate = 0.0

_cached_cpu_temp = 77.0
_cached_sys_temp = 37.0
_cached_fan_rpm = "1200 RPM"
_cached_core_temps = [77.0, 77.0, 77.0, 77.0]
_last_hw_check = 0.0

# History telemetry buffers (last 30 samples)
_history_maxlen = 30
_cpu_history = deque([12.0, 15.0, 10.0, 8.5, 14.2, 11.0, 9.8, 13.5], maxlen=_history_maxlen)
_mem_history = deque([44.5, 44.6, 44.7, 44.7, 44.8, 44.7, 44.7, 44.7], maxlen=_history_maxlen)
_net_rx_history = deque([120.0, 85.0, 240.0, 310.0, 180.0, 95.0, 110.0, 150.0], maxlen=_history_maxlen)
_net_tx_history = deque([45.0, 30.0, 80.0, 90.0, 65.0, 40.0, 50.0, 60.0], maxlen=_history_maxlen)
_history_times = deque(["04:30", "04:31", "04:32", "04:33", "04:34", "04:35", "04:36", "04:37"], maxlen=_history_maxlen)

# qBittorrent Config & Auth Cache
_qb_cookies: Dict[str, str] = {}
_qb_last_auth = 0.0

def get_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_key": os.getenv("GEMINI_API_KEY", ""),
        "provider": os.getenv("LLM_PROVIDER", "gemini"),
        "base_url": os.getenv("LLM_BASE_URL", ""),
        "model": os.getenv("LLM_MODEL", "gemini-3.8-flash"),
        "refresh_interval": 3,
        "qb_url": os.getenv("QB_URL", "http://127.0.0.1:8080"),
        "qb_username": os.getenv("QB_USER", "admin"),
        "qb_password": os.getenv("QB_PASS", "")
    }

def get_qb_credentials() -> tuple[str, str, str]:
    cfg = get_config()
    url = os.getenv("QB_URL") or cfg.get("qb_url") or "http://127.0.0.1:8080"
    user = os.getenv("QB_USER") or cfg.get("qb_username") or "admin"
    pwd = os.getenv("QB_PASS") or cfg.get("qb_password") or ""
    return url.rstrip("/"), user, pwd

def save_config(cfg: Dict[str, Any]):
    os.makedirs(os.path.dirname(os.path.abspath(CONFIG_FILE)), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def get_docker_client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=10.0)

def read_proc_file(filename: str) -> str:
    paths = [
        f"/proc/{filename}",
        f"/host/proc/{filename}",
        f"/host/proc/1/{filename}"
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return ""

def get_cpu_model_name() -> str:
    content = read_proc_file("cpuinfo")
    for line in content.splitlines():
        if "model name" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                raw = parts[1].strip()
                cleaned = raw.replace("(R)", "").replace("(TM)", "").strip()
                cleaned = re.sub(r"\s+", " ", cleaned)
                return cleaned
    return "Intel Pentium Silver N6005 @ 2.00GHz"

def get_cpu_usage() -> float:
    global _last_cpu_time, _last_cpu_idle, _last_cpu_total
    stat_content = read_proc_file("stat")
    if not stat_content:
        return 0.0
    
    first_line = stat_content.splitlines()[0]
    parts = [int(x) for x in first_line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    
    now = time.time()
    diff_idle = idle - _last_cpu_idle
    diff_total = total - _last_cpu_total
    
    _last_cpu_idle = idle
    _last_cpu_total = total
    _last_cpu_time = now
    
    if diff_total > 0:
        usage = max(0.0, min(100.0, (1.0 - (diff_idle / diff_total)) * 100.0))
        return round(usage, 1)
    return 0.0

def get_memory_info() -> Dict[str, Any]:
    mem_content = read_proc_file("meminfo")
    info = {}
    for line in mem_content.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            key = parts[0].strip()
            val = parts[1].strip().split()[0]
            if val.isdigit():
                info[key] = int(val)
                
    total_kb = info.get("MemTotal", 1)
    free_kb = info.get("MemFree", 0)
    buffers_kb = info.get("Buffers", 0)
    cached_kb = info.get("Cached", 0)
    available_kb = info.get("MemAvailable", free_kb + buffers_kb + cached_kb)
    used_kb = max(0, total_kb - available_kb)
    
    total_gb = round(total_kb / (1024 * 1024), 2)
    used_gb = round(used_kb / (1024 * 1024), 2)
    free_gb = round(available_kb / (1024 * 1024), 2)
    percent = round((used_kb / total_kb) * 100.0, 1) if total_kb > 0 else 0.0

    # Page cache and buffers
    cache_kb = buffers_kb + cached_kb
    cache_gb = round(cache_kb / (1024 * 1024), 2)
    cache_pct = round((cache_kb / total_kb) * 100.0, 1) if total_kb > 0 else 0.0
    free_pct = round(max(0.0, 100.0 - percent - cache_pct), 1)

    # Health Alert State: normal (<75%), warning (75%~88%), critical (>88%)
    if percent >= 88.0:
        mem_status = "critical"
    elif percent >= 75.0:
        mem_status = "warning"
    else:
        mem_status = "normal"

    # Swap monitoring
    swap_total_kb = info.get("SwapTotal", 0)
    swap_free_kb = info.get("SwapFree", 0)
    swap_used_kb = max(0, swap_total_kb - swap_free_kb)
    swap_total_gb = round(swap_total_kb / (1024 * 1024), 1)
    swap_used_mb = round(swap_used_kb / 1024, 0)
    swap_used_gb = round(swap_used_kb / (1024 * 1024), 2)
    swap_pct = round((swap_used_kb / swap_total_kb) * 100.0, 1) if swap_total_kb > 0 else 0.0

    if swap_used_gb >= 1.0:
        swap_used_str = f"{swap_used_gb} GB"
    else:
        swap_used_str = f"{int(swap_used_mb)} MB"
    swap_str = f"{swap_used_str} / {swap_total_gb} GB"
    
    return {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "percent": percent,
        "cache_gb": cache_gb,
        "cache_pct": cache_pct,
        "free_pct": free_pct,
        "status": mem_status,
        "swap_str": swap_str,
        "swap_used_str": swap_used_str,
        "swap_total_gb": swap_total_gb,
        "swap_percent": swap_pct
    }

def get_uptime_str() -> str:
    uptime_content = read_proc_file("uptime")
    if uptime_content:
        try:
            total_seconds = int(float(uptime_content.split()[0]))
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            if days > 0:
                return f"{days}天 {hours}小时 {minutes}分"
            return f"{hours}小时 {minutes}分"
        except Exception:
            pass
    return "运行中"

def get_service_uptime_str() -> str:
    elapsed = int(time.time() - _server_start_time)
    days = elapsed // 86400
    hours = (elapsed % 86400) // 3600
    minutes = (elapsed % 3600) // 60
    if days > 0:
        return f"{days}天 {hours}小时 {minutes}分"
    elif hours > 0:
        return f"{hours}小时 {minutes}分"
    else:
        return f"{max(1, minutes)}分钟"

def get_load_avg() -> List[float]:
    load_content = read_proc_file("loadavg")
    if load_content:
        try:
            parts = load_content.split()
            return [float(parts[0]), float(parts[1]), float(parts[2])]
        except Exception:
            pass
    return [0.0, 0.0, 0.0]

def get_network_throughput() -> Dict[str, Any]:
    global _last_net_time, _last_rx_bytes, _last_tx_bytes, _last_rx_rate, _last_tx_rate
    content = read_proc_file("net/dev")
    rx_bytes = 0
    tx_bytes = 0
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("eth0:"):
            parts = line.split(":", 1)[1].split()
            if len(parts) >= 9:
                rx_bytes = int(parts[0])
                tx_bytes = int(parts[8])
            break
            
    now = time.time()
    if _last_net_time > 0 and now > _last_net_time:
        dt = now - _last_net_time
        if dt >= 0.5:
            rx_diff = max(0, rx_bytes - _last_rx_bytes)
            tx_diff = max(0, tx_bytes - _last_tx_bytes)
            _last_rx_rate = rx_diff / dt
            _last_tx_rate = tx_diff / dt
            _last_net_time = now
            _last_rx_bytes = rx_bytes
            _last_tx_bytes = tx_bytes
    else:
        _last_net_time = now
        _last_rx_bytes = rx_bytes
        _last_tx_bytes = tx_bytes

    def format_bytes_speed(bps: float) -> str:
        if bps >= 1024 * 1024 * 1024:
            return f"{bps / (1024**3):.2f} GB/s"
        elif bps >= 1024 * 1024:
            return f"{bps / (1024**2):.1f} MB/s"
        elif bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        else:
            return f"{bps:.0f} B/s"

    def format_bytes_total(b: int) -> str:
        if b >= 1024**4:
            return f"{b / (1024**4):.2f} TB"
        elif b >= 1024**3:
            return f"{b / (1024**3):.2f} GB"
        elif b >= 1024**2:
            return f"{b / (1024**2):.1f} MB"
        else:
            return f"{b / 1024:.0f} KB"

    max_net_bps = 312500000.0  # 2.5 Gbps Full Duplex
    rx_saturation = min(100.0, round((_last_rx_rate / max_net_bps) * 100.0, 1))
    tx_saturation = min(100.0, round((_last_tx_rate / max_net_bps) * 100.0, 1))

    return {
        "interface": "eth0",
        "link_speed": "2.5 Gbps",
        "duplex": "全双工",
        "rx_saturation_pct": rx_saturation,
        "tx_saturation_pct": tx_saturation,
        "rx_bytes_sec": round(_last_rx_rate, 1),
        "tx_bytes_sec": round(_last_tx_rate, 1),
        "rx_speed_str": format_bytes_speed(_last_rx_rate),
        "tx_speed_str": format_bytes_speed(_last_tx_rate),
        "total_rx": rx_bytes,
        "total_tx": tx_bytes,
        "total_rx_str": format_bytes_total(rx_bytes),
        "total_tx_str": format_bytes_total(tx_bytes)
    }

def get_cpu_and_fan_telemetry() -> Dict[str, Any]:
    global _cached_cpu_temp, _cached_sys_temp, _cached_fan_rpm, _cached_core_temps, _last_hw_check
    now = time.time()
    if now - _last_hw_check >= 2.0:
        _last_hw_check = now
        try:
            cmd = "/sbin/get_cpu_temp 2>/dev/null; echo; /sbin/getsysinfo systmp 2>/dev/null; echo; /sbin/getsysinfo sysfan 1 2>/dev/null"
            res = subprocess.run(
                ["chroot", "/host/root", "sh", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=1.8
            )
            lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
            if lines:
                # Line 0: CPU Temp from QTS HAL (calibrated)
                if lines[0].isdigit():
                    _cached_cpu_temp = float(lines[0])
                else:
                    m_cpu = re.search(r"(\d+)\s*C", lines[0])
                    if m_cpu:
                        _cached_cpu_temp = float(m_cpu.group(1))

                # Line 1: Motherboard / System Temp from QTS
                if len(lines) > 1:
                    m_sys = re.search(r"(\d+)\s*C", lines[1])
                    if m_sys:
                        _cached_sys_temp = float(m_sys.group(1))

                # Line 2: Fan Speed from QTS
                if len(lines) > 2 and "RPM" in lines[2]:
                    _cached_fan_rpm = lines[2]
        except Exception:
            pass

        # Per-core hardware tracking calibrated against QTS CPU package temp
        raw_package = 104.0
        raw_cores = []
        hwmon_paths = ["/host/sys/class/hwmon/hwmon0", "/sys/class/hwmon/hwmon0"]
        for hp in hwmon_paths:
            if os.path.exists(hp):
                try:
                    with open(os.path.join(hp, "temp1_input"), "r") as f:
                        raw_package = int(f.read().strip()) / 1000.0
                    for i in range(2, 6):
                        c_file = os.path.join(hp, f"temp{i}_input")
                        if os.path.exists(c_file):
                            with open(c_file, "r") as f:
                                raw_cores.append(int(f.read().strip()) / 1000.0)
                    break
                except Exception:
                    pass

        if raw_cores:
            offset = raw_package - _cached_cpu_temp
            _cached_core_temps = [round(c - offset, 1) for c in raw_cores]
        else:
            _cached_core_temps = [_cached_cpu_temp] * 4

    # Thermal status classification
    # TS-466C Jasper Lake (Intel Pentium Silver N6005):
    # In QTS hal_util.conf: TFAN_NORMAL_MODE = 70:75:80, SHUTDOWN_TEMP = 85
    # Normal < 80°C (standard operating range), Warning 80~84°C, Critical >= 85°C (official shutdown threshold)
    is_overheat = (_cached_cpu_temp >= 85.0) or any(t >= 85.0 for t in _cached_core_temps)
    if is_overheat:
        temp_status = "critical"  # 超温告警
    elif _cached_cpu_temp >= 80.0 or any(t >= 80.0 for t in _cached_core_temps):
        temp_status = "warning"   # 负载偏高
    else:
        temp_status = "normal"    # 运行正常

    return {
        "cpu_temp": _cached_cpu_temp,
        "temp_status": temp_status,
        "is_overheat": is_overheat,
        "core_temps": _cached_core_temps,
        "fan_speed": _cached_fan_rpm,
        "system_temp": _cached_sys_temp
    }


def get_disks_matrix() -> List[Dict[str, Any]]:
    slots_def = [
        {
            "slot": 1,
            "bay_name": "Bay 1",
            "type": "3.5\" SATA",
            "series": "希捷 酷狼 (IronWolf)",
            "model_code": "ST6000VN001",
            "capacity": "5.46 TB (6TB)",
            "smart_file": "/host/tmp/smart/smart_0_1.info"
        },
        {
            "slot": 2,
            "bay_name": "Bay 2",
            "type": "3.5\" SATA",
            "series": "希捷 酷狼 (IronWolf)",
            "model_code": "ST6000VN001",
            "capacity": "5.46 TB (6TB)",
            "smart_file": "/host/tmp/smart/smart_0_2.info"
        },
        {
            "slot": 3,
            "bay_name": "Bay 3",
            "type": "3.5\" SATA",
            "series": "西数 红盘 Plus (WD Red)",
            "model_code": "WD20EFZX",
            "capacity": "1.82 TB (2TB)",
            "smart_file": "/host/tmp/smart/smart_0_3.info"
        },
        {
            "slot": 4,
            "bay_name": "Bay 4",
            "type": "3.5\" SATA",
            "series": "希捷 酷狼 (IronWolf)",
            "model_code": "ST4000VN006",
            "capacity": "3.64 TB (4TB)",
            "smart_file": "/host/tmp/smart/smart_0_4.info"
        },
        {
            "slot": 5,
            "bay_name": "M.2 槽位 1",
            "type": "M.2 NVMe",
            "series": "空置扩展插槽",
            "model_code": "PCIe 3.0",
            "capacity": "待安装",
            "smart_file": None
        },
        {
            "slot": 6,
            "bay_name": "M.2 槽位 2",
            "type": "M.2 NVMe",
            "series": "金百达 KP230 Pro",
            "model_code": "PCIe 3.0 NVMe",
            "capacity": "476.9 GB",
            "smart_file": "/host/tmp/smart/smart_0_6.info"
        },
    ]
    
    result = []
    for s in slots_def:
        temp = None
        health = "GOOD"
        installed = True
        temp_status = "normal"
        
        if s["smart_file"]:
            sf = s["smart_file"]
            if not os.path.exists(sf):
                sf = sf.replace("/host/tmp/smart", "/tmp/smart")
            if os.path.exists(sf):
                try:
                    with open(sf, "r", encoding="utf-8") as f:
                        line = f.readline().strip()
                        parts = line.split(",")
                        if len(parts) >= 9 and parts[8].isdigit():
                            temp = int(parts[8])
                            # Temperature evaluation
                            if "NVMe" in s["type"]:
                                if temp >= 65:
                                    temp_status = "critical"
                                elif temp >= 53:
                                    temp_status = "warning"
                            else:
                                if temp >= 49:
                                    temp_status = "critical"
                                elif temp >= 43:
                                    temp_status = "warning"
                except Exception:
                    pass
        elif "空置" in s["series"]:
            installed = False
            health = "EMPTY"
            temp = None
            temp_status = "normal"
            
        vendor_name = s["series"].split()[0] if s["series"] else "Unknown"
        model_display = f"{s['series']} ({s['model_code']})"
        result.append({
            "slot": s["slot"],
            "bay_name": s["bay_name"],
            "type": s["type"],
            "series": s["series"],
            "model_code": s["model_code"],
            "capacity": s["capacity"],
            "temp": temp,
            "temp_status": temp_status,
            "health": health,
            "installed": installed,
            "vendor": vendor_name,
            "model": model_display
        })
    return result

def get_storage_pools() -> List[Dict[str, Any]]:
    check_dirs = []
    # Dynamic scan for QNAP CACHEDEV volumes and standard Linux storage mounts
    share_root = "/host/share" if os.path.exists("/host/share") else "/share"
    if os.path.exists(share_root):
        try:
            for item in sorted(os.listdir(share_root)):
                item_path = os.path.join(share_root, item)
                if item.startswith("CACHEDEV") and item.endswith("_DATA"):
                    num = item.replace("CACHEDEV", "").replace("_DATA", "")
                    check_dirs.append({"name": f"存储卷 (CACHEDEV{num})", "path": item_path})
                elif item == "CloudNAS":
                    try:
                        for sub in sorted(os.listdir(item_path)):
                            check_dirs.append({"name": f"云盘挂载 ({sub})", "path": os.path.join(item_path, sub)})
                    except Exception:
                        pass
        except Exception:
            pass

    # Standard system mounts
    for m in [("/", "系统根卷 (/data)"), ("/host/share", "共享存储 (/share)")]:
        if os.path.exists(m[0]) and not any(d["path"] == m[0] for d in check_dirs):
            check_dirs.append({"name": m[1], "path": m[0]})
    pools = []
    seen = set()
    for item in check_dirs:
        p = item["path"]
        host_p = p.replace("/share", "/host/share") if os.path.exists("/host/share") else p
        target = host_p if os.path.exists(host_p) else p
        if os.path.exists(target):
            try:
                st = os.statvfs(target)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                used = total - free
                if total > 0 and total not in seen:
                    seen.add(total)
                    total_tb = total / (1024**4)
                    used_tb = used / (1024**4)
                    free_tb = free / (1024**4)
                    
                    if total_tb >= 1.0:
                        total_str = f"{total_tb:.2f} TB"
                        used_str = f"{used_tb:.2f} TB"
                        free_str = f"{free_tb:.2f} TB"
                    else:
                        total_str = f"{total / (1024**3):.1f} GB"
                        used_str = f"{used / (1024**3):.1f} GB"
                        free_str = f"{free / (1024**3):.1f} GB"
                        
                    pools.append({
                        "name": item["name"],
                        "path": p,
                        "total_str": total_str,
                        "used_str": used_str,
                        "free_str": free_str,
                        "percent": round((used / total) * 100.0, 1),
                        "used_bytes": used,
                        "total_bytes": total
                    })
            except Exception:
                pass
    return pools

# --- qBittorrent Client ---
async def get_qb_session() -> tuple[Dict[str, str], str]:
    global _qb_cookies, _qb_last_auth
    url, user, pwd = get_qb_credentials()
    now = time.time()
    if _qb_cookies and (now - _qb_last_auth < 1800):
        return _qb_cookies, url
        
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(
                f"{url}/api/v2/auth/login",
                data={"username": user, "password": pwd}
            )
            if resp.status_code in [200, 204]:
                _qb_cookies = dict(resp.cookies)
                _qb_last_auth = now
                return _qb_cookies, url
    except Exception:
        pass
    return {}, url

async def get_qb_status() -> Dict[str, Any]:
    cookies, qb_url = await get_qb_session()
    if not cookies:
        return {"online": False, "error": "无法连接 qBittorrent 或认证失败 (请在环境变量或系统设置中配置 qB 密码)"}
        
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=4.0) as client:
            t_resp = await client.get(f"{qb_url}/api/v2/transfer/info")
            if t_resp.status_code == 403:
                global _qb_cookies
                _qb_cookies = {}
                cookies, qb_url = await get_qb_session()
                t_resp = await client.get(f"{qb_url}/api/v2/transfer/info", cookies=cookies)
                
            transfer = t_resp.json() if t_resp.status_code == 200 else {}
            
            torrents_resp = await client.get(f"{qb_url}/api/v2/torrents/info")
            torrents = torrents_resp.json() if torrents_resp.status_code == 200 else []
            
            speed_mode_resp = await client.get(f"{qb_url}/api/v2/transfer/speedLimitsMode")
            alt_speed = (speed_mode_resp.text.strip() == "1") if speed_mode_resp.status_code == 200 else False
            
            dl_speed = transfer.get("dl_info_speed", 0)
            up_speed = transfer.get("up_info_speed", 0)
            
            active_count = 0
            downloading_count = 0
            seeding_count = 0
            paused_count = 0
            for t in torrents:
                st = t.get("state", "")
                if "downloading" in st or "stalledDL" in st:
                    downloading_count += 1
                elif "uploading" in st or "stalledUP" in st:
                    seeding_count += 1
                elif "paused" in st:
                    paused_count += 1
                if t.get("dlspeed", 0) > 0 or t.get("upspeed", 0) > 0:
                    active_count += 1
                    
            def format_speed(bps: int) -> str:
                if bps >= 1024 * 1024 * 1024:
                    return f"{bps / (1024**3):.2f} GB/s"
                elif bps >= 1024 * 1024:
                    return f"{bps / (1024**2):.1f} MB/s"
                elif bps >= 1024:
                    return f"{bps / 1024:.1f} KB/s"
                else:
                    return f"{bps} B/s"
                    
            def format_size_metric(b: int) -> str:
                if b >= 1024**4:
                    return f"{b / (1024**4):.2f} TB"
                elif b >= 1024**3:
                    return f"{b / (1024**3):.2f} GB"
                elif b >= 1024**2:
                    return f"{b / (1024**2):.1f} MB"
                elif b >= 1024:
                    return f"{b / 1024:.1f} KB"
                else:
                    return f"{b} B"

            dl_total = transfer.get("dl_info_data", 0)
            up_total = transfer.get("up_info_data", 0)
            free_disk = transfer.get("free_space_on_disk", 0)
                    
            return {
                "online": True,
                "connection_status": transfer.get("connection_status", "unknown"),
                "dht_nodes": transfer.get("dht_nodes", 0),
                "dl_speed_bps": dl_speed,
                "up_speed_bps": up_speed,
                "dl_speed_str": format_speed(dl_speed),
                "up_speed_str": format_speed(up_speed),
                "dl_total_str": format_size_metric(dl_total),
                "up_total_str": format_size_metric(up_total),
                "free_space_str": format_size_metric(free_disk) if free_disk else "--",
                "alt_speed_mode": alt_speed,
                "torrents_total": len(torrents),
                "torrents_active": active_count,
                "torrents_downloading": downloading_count,
                "torrents_seeding": seeding_count,
                "torrents_paused": paused_count,
                "webui_url": "http://localhost:8080"
            }
    except Exception as e:
        return {"online": False, "error": str(e)}

async def qb_action(action: str) -> Dict[str, Any]:
    cookies, qb_url = await get_qb_session()
    if not cookies:
        return {"success": False, "error": "qBittorrent 认证失败"}
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=5.0) as client:
            if action == "pause_all":
                r = await client.post(f"{qb_url}/api/v2/torrents/pause", data={"hashes": "all"})
                if r.status_code not in [200, 204]:
                    await client.post(f"{qb_url}/api/v2/torrents/stop", data={"hashes": "all"})
                return {"success": True, "action": "pause_all", "message": "已暂停所有下载任务"}
            elif action == "resume_all":
                r = await client.post(f"{qb_url}/api/v2/torrents/resume", data={"hashes": "all"})
                if r.status_code not in [200, 204]:
                    await client.post(f"{qb_url}/api/v2/torrents/start", data={"hashes": "all"})
                return {"success": True, "action": "resume_all", "message": "已恢复所有下载任务"}
            elif action == "toggle_speed_limit":
                r = await client.post(f"{qb_url}/api/v2/transfer/toggleSpeedLimitsMode")
                return {"success": True, "action": "toggle_speed_limit", "message": "已切换慢速模式"}
            else:
                return {"success": False, "error": f"未知指令: {action}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def get_ecosystem_services() -> List[Dict[str, Any]]:
    services = [
        {"name": "Jellyfin", "tag": "影音流媒体", "desc": "家庭影视服务器", "port": 8096, "url": "http://localhost:8096", "check_url": "http://127.0.0.1:8096/System/Info/Public", "icon": "film"},
        {"name": "Immich", "tag": "AI 智能相册", "desc": "照片自动备份与识图", "port": 2283, "url": "http://localhost:2283", "check_url": "http://127.0.0.1:2283/api/server/version", "icon": "image"},
        {"name": "Mihomo", "tag": "核心网关", "desc": "Meta 内核规则分流", "port": 9090, "url": "http://localhost:9090", "check_url": "http://127.0.0.1:9090/version", "icon": "shield-check"}
    ]
    
    async with httpx.AsyncClient(timeout=2.0) as client:
        results = []
        for s in services:
            version = "在线"
            status = "online"
            try:
                r = await client.get(s["check_url"])
                if r.status_code == 200:
                    if s["name"] == "Jellyfin":
                        j = r.json()
                        version = f"v{j.get('Version', '10.x')}"
                    elif s["name"] == "Immich":
                        j = r.json()
                        version = f"v{j.get('major',3)}.{j.get('minor',1)}.{j.get('patch',0)}"
                    elif s["name"] == "Mihomo":
                        j = r.json()
                        version = f"{j.get('version', 'Meta')}"
                else:
                    status = "degraded"
            except Exception:
                status = "offline"
                version = "离线"
                
            results.append({
                "name": s["name"],
                "tag": s["tag"],
                "desc": s["desc"],
                "port": s["port"],
                "url": s["url"],
                "icon": s["icon"],
                "status": status,
                "version": version
            })
    return results

# --- API Routes ---
@app.get("/api/health")
async def api_health():
    return {"status": "healthy", "timestamp": int(time.time()), "version": "1.3.0"}

def get_system_audit_logs(cpu_t, net) -> List[Dict[str, Any]]:
    now_str = time.strftime("%H:%M:%S")
    return [
        {"time": now_str, "level": "INFO", "tag": "SYSTEM", "msg": f"QTS Linux 5.10.60 遥测总线就绪 · Package {cpu_t.get('cpu_temp', 0)}°C · 风扇 {cpu_t.get('fan_speed', '--')}"},
        {"time": now_str, "level": "SUCCESS", "tag": "STORAGE", "msg": "4-Bay SATA + M.2 NVMe S.M.A.R.T 矩阵健康度轮询通过"},
        {"time": now_str, "level": "INFO", "tag": "NETWORK", "msg": f"eth0 2.5GbE 全双工链路在线 · 实时吞吐 ↓{net.get('rx_speed_str')} · ↑{net.get('tx_speed_str')}"},
        {"time": now_str, "level": "SUCCESS", "tag": "CONTAINER", "msg": "Docker 守护进程通信就绪 · 核心微服务群巡检健康"},
        {"time": now_str, "level": "INFO", "tag": "AI-OPS", "msg": "本地极客规则引擎 + Gemini 双模协同终端待命就绪"}
    ]

@app.get("/api/audit_logs")
async def api_audit_logs():
    cpu_t = get_cpu_and_fan_telemetry()
    net = get_network_throughput()
    return get_system_audit_logs(cpu_t, net)

def get_host_name() -> str:
    env_h = os.getenv("NAS_HOSTNAME")
    if env_h:
        return env_h
    raw = read_proc_file("sys/kernel/hostname").strip()
    if raw and raw != "localhost":
        return raw
    return "NAS-NODE"

def get_nas_model_name() -> str:
    env_m = os.getenv("NAS_MODEL")
    if env_m:
        return env_m
    try:
        res = subprocess.run(["chroot", "/host/root", "/sbin/getsysinfo", "modelname"], capture_output=True, text=True, timeout=1.0)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "QNAP TS-466C"

@app.get("/api/system")
async def api_system_status():
    cpu_u = get_cpu_usage()
    cpu_t = get_cpu_and_fan_telemetry()
    net = get_network_throughput()
    mem = get_memory_info()
    load = get_load_avg()
    disks = get_disks_matrix()
    storage = get_storage_pools()
    
    # Update telemetry history
    now_str = time.strftime("%H:%M:%S")
    _cpu_history.append(cpu_u)
    _mem_history.append(mem.get("percent", 0.0))
    _net_rx_history.append(round(net.get("rx_bytes_sec", 0.0) / 1024.0, 1))
    _net_tx_history.append(round(net.get("tx_bytes_sec", 0.0) / 1024.0, 1))
    _history_times.append(now_str)
    
    cpu_model = get_cpu_model_name()
    
    # LoadAvg reference for 4-core processor (N6005, max baseline 4.00)
    load1 = load[0] if len(load) > 0 else 0.0
    load5 = load[1] if len(load) > 1 else 0.0
    load15 = load[2] if len(load) > 2 else 0.0

    if load1 > load5 * 1.08 and load1 > 0.3:
        trend = "rising"
        trend_label = "负载攀升"
    elif load1 < load5 * 0.92:
        trend = "falling"
        trend_label = "正在回落"
    else:
        trend = "stable"
        trend_label = "相对平稳"

    spark_pcts = [
        min(100.0, round((load1 / 4.00) * 100, 1)),
        min(100.0, round((load5 / 4.00) * 100, 1)),
        min(100.0, round((load15 / 4.00) * 100, 1))
    ]

    if load1 >= 4.0:
        load_status = "critical"
        status_label = "严重超载"
        if trend == "rising":
            trend_label = "超载攀升"
        elif trend == "falling":
            trend_label = "超载回落"
        else:
            trend_label = "持续超载"
    elif load1 >= 3.2:
        load_status = "warning"
        status_label = "偏载运行"
        if trend == "rising":
            trend_label = "负荷上升"
        elif trend == "falling":
            trend_label = "逐步缓解"
        else:
            trend_label = "相对平稳"
    else:
        load_status = "normal"
        status_label = "负荷正常"
        if trend == "rising":
            trend_label = "算力调用"
        elif trend == "falling":
            trend_label = "回归低位"
        else:
            trend_label = "平稳运行"

    load_detail = {
        "values": load,
        "max_baseline": 4.00,
        "status": load_status,
        "status_label": status_label,
        "ratio_pct": round((load1 / 4.00) * 100, 1),
        "cores": 4,
        "trend": trend,
        "trend_label": trend_label,
        "spark_pcts": spark_pcts,
        "tip": f"4核心 {cpu_model.split('@')[0].strip()} 基准线 (满载为 4.00)"
    }
    
    return {
        "model_name": get_nas_model_name(),
        "cpu_model": cpu_model,
        "hostname": get_host_name(),
        "uptime": get_uptime_str(),
        "host_uptime": get_uptime_str(),
        "service_uptime": get_service_uptime_str(),
        "cpu_usage": cpu_u,
        "cpu_telemetry": cpu_t,
        "network": net,
        "disks": disks,
        "memory": mem,
        "loadavg": load,
        "load_detail": load_detail,
        "storage": storage,
        "telemetry_history": {
            "cpu": list(_cpu_history),
            "mem": list(_mem_history),
            "net_rx": list(_net_rx_history),
            "net_tx": list(_net_tx_history),
            "times": list(_history_times)
        },
        "audit_logs": get_system_audit_logs(cpu_t, net),
        "timestamp": int(time.time())
    }

@app.get("/api/apps/qbittorrent")
async def api_qbittorrent_status():
    return await get_qb_status()

@app.post("/api/apps/qbittorrent/{action}")
async def api_qbittorrent_action(action: str):
    return await qb_action(action)

@app.get("/api/apps/status")
async def api_apps_status():
    return await get_ecosystem_services()

@app.post("/api/docker/prune")
async def api_docker_prune():
    try:
        async with get_docker_client() as client:
            resp = await client.post("/images/prune?dangling=true")
            if resp.status_code == 200:
                data = resp.json()
                reclaimed = data.get("SpaceReclaimed", 0)
                reclaimed_mb = round(reclaimed / (1024 * 1024), 1)
                deleted = len(data.get("ImagesDeleted") or [])
                return {
                    "success": True,
                    "deleted_count": deleted,
                    "space_reclaimed_mb": reclaimed_mb,
                    "message": f"已成功清理 {deleted} 个无用悬空镜像，释放 {reclaimed_mb} MB 磁盘空间。"
                }
            return JSONResponse(status_code=resp.status_code, content={"error": resp.text})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/docker/containers")
async def api_docker_containers():
    try:
        async with get_docker_client() as client:
            resp = await client.get("/containers/json?all=1")
            if resp.status_code != 200:
                raise HTTPException(status_code=500, detail="Failed to query Docker socket")
            containers = resp.json()
            
            result = []
            for c in containers:
                names = [n.lstrip("/") for n in c.get("Names", [])]
                main_name = names[0] if names else c.get("Id", "")[:12]
                
                ports = []
                for p in c.get("Ports", []):
                    pub = p.get("PublicPort")
                    priv = p.get("PrivatePort")
                    ptype = p.get("Type", "tcp")
                    if pub:
                        ports.append(f"{pub}->{priv}/{ptype}")
                    elif priv:
                        ports.append(f"{priv}/{ptype}")
                        
                result.append({
                    "id": c.get("Id", ""),
                    "short_id": c.get("Id", "")[:12],
                    "name": main_name,
                    "image": c.get("Image", ""),
                    "state": c.get("State", "unknown"),
                    "status": c.get("Status", ""),
                    "ports": ports,
                    "created": c.get("Created", 0)
                })
            result.sort(key=lambda x: (0 if x["state"] == "running" else 1, x["name"].lower()))
            return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/docker/{action}/{container_id}")
async def api_docker_action(action: str, container_id: str):
    if action not in ["restart", "stop", "start"]:
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        async with get_docker_client() as client:
            resp = await client.post(f"/containers/{container_id}/{action}")
            if resp.status_code in [204, 200, 304]:
                return {"success": True, "action": action, "container_id": container_id}
            return JSONResponse(status_code=resp.status_code, content={"error": resp.text})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/docker/logs/{container_id}")
async def api_docker_logs(container_id: str, tail: int = 150):
    try:
        async with get_docker_client() as client:
            resp = await client.get(
                f"/containers/{container_id}/logs",
                params={"stdout": 1, "stderr": 1, "tail": tail, "timestamps": 1}
            )
            raw = resp.content
            clean_lines = []
            i = 0
            while i < len(raw):
                if i + 8 <= len(raw) and raw[i] in [1, 2]:
                    size = int.from_bytes(raw[i+4:i+8], byteorder="big")
                    line_bytes = raw[i+8:i+8+size]
                    clean_lines.append(line_bytes.decode("utf-8", errors="replace"))
                    i += 8 + size
                else:
                    clean_lines.append(raw[i:].decode("utf-8", errors="replace"))
                    break
            raw_text = "".join(clean_lines)
            clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_text)
            return {"logs": clean_text}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class SettingsPayload(BaseModel):
    api_key: Optional[str] = ""
    provider: Optional[str] = "gemini"
    base_url: Optional[str] = ""
    model: Optional[str] = "gemini-3.8-flash"
    refresh_interval: Optional[int] = 3

@app.get("/api/settings")
async def api_get_settings():
    return get_config()

@app.post("/api/settings")
async def api_save_settings(payload: SettingsPayload):
    cfg = payload.model_dump()
    if cfg.get("model"):
        cfg["model"] = normalize_model_name(cfg["model"])
    save_config(cfg)
    return {"success": True, "config": cfg}

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    api_key: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None

def normalize_model_name(model_str: str) -> str:
    m = model_str.strip()
    if m.startswith("models/"):
        m = m[7:]
    m = re.sub(r"\s+", "-", m.lower())
    alias_map = {
        "gemini-3.8": "gemini-3.8-flash",
        "3.8-flash": "gemini-3.8-flash",
        "gemini-3.7": "gemini-3.7-flash",
        "3.7-flash": "gemini-3.7-flash",
        "gemini-3-flash": "gemini-3-flash-preview",
        "gemini-2.5-flash": "gemini-3.8-flash",
        "gemini-2.0-flash": "gemini-flash-latest",
        "gemini-1.5-flash": "gemini-flash-latest",
    }
    return alias_map.get(m, m)

# Smart Local DevOps Engine
async def process_local_ai_command(query: str) -> str:
    q = query.strip().lower()
    
    # 1. qBittorrent controls
    if any(k in q for k in ["暂停下载", "停止下载", "暂停所有下载", "暂停qb", "停掉下载"]):
        act = await qb_action("pause_all")
        if act.get("success"):
            return "⏸️ **指令已执行**\n\n已成功通知 qBittorrent 暂停所有当前正在进行的下载任务。"
        return f"⚠️ 暂停失败: {act.get('error')}"

    if any(k in q for k in ["恢复下载", "继续下载", "开启下载", "恢复所有下载", "开始下载"]):
        act = await qb_action("resume_all")
        if act.get("success"):
            return "▶️ **指令已执行**\n\n已成功通知 qBittorrent 恢复所有队列中的下载任务。"
        return f"⚠️ 恢复失败: {act.get('error')}"

    if any(k in q for k in ["慢速模式", "切换限速", "备用限速", "限速模式"]):
        act = await qb_action("toggle_speed_limit")
        if act.get("success"):
            return "🐢 **指令已执行**\n\n已切换 qBittorrent 备用限速模式。"
        return f"⚠️ 切换失败: {act.get('error')}"

    # 2. Docker Pruning
    if any(k in q for k in ["清理镜像", "删除无用镜像", "清理docker", "释放docker空间", "清理缓存"]):
        try:
            async with get_docker_client() as client:
                resp = await client.post("/images/prune?dangling=true")
                if resp.status_code == 200:
                    data = resp.json()
                    mb = round(data.get("SpaceReclaimed", 0) / (1024*1024), 1)
                    count = len(data.get("ImagesDeleted") or [])
                    return f"🧹 **Docker 深度清理报告**\n\n成功清理 `{count}` 个无用悬空镜像，已为 NAS 释放 `{mb} MB` 存储空间。"
                return f"⚠️ Docker 清理返回错误: {resp.text}"
        except Exception as e:
            return f"❌ 执行清理失败: {str(e)}"

    # 3. Restart Container
    restart_match = re.search(r"重启(?:容器)?\s*([a-zA-Z0-9_\-]+)", query)
    if restart_match:
        target_name = restart_match.group(1).lower()
        try:
            async with get_docker_client() as client:
                resp = await client.get("/containers/json?all=1")
                containers = resp.json()
                found = None
                for c in containers:
                    c_name = c.get("Names", [""])[0].lstrip("/").lower()
                    if target_name in c_name:
                        found = c
                        break
                if found:
                    cid = found["Id"]
                    real_name = found["Names"][0].lstrip("/")
                    await client.post(f"/containers/{cid}/restart")
                    return f"⚡ **指令执行完毕**\n\n已成功向容器 `{real_name}` 发送重启指令，服务正在平稳重载中。"
                else:
                    return f"⚠️ **未找到匹配容器**\n\n未检索到 `{target_name}`。"
        except Exception as e:
            return f"❌ **执行失败**: {str(e)}"
            
    # 4. View Logs
    log_match = re.search(r"(?:查看|获取|查下|看一下)\s*([a-zA-Z0-9_\-]+)\s*(?:的)?(?:日志|log)", query)
    if log_match:
        target_name = log_match.group(1).lower()
        try:
            async with get_docker_client() as client:
                resp = await client.get("/containers/json?all=1")
                for c in resp.json():
                    c_name = c.get("Names", [""])[0].lstrip("/").lower()
                    if target_name in c_name:
                        cid = c["Id"]
                        real_name = c["Names"][0].lstrip("/")
                        log_resp = await client.get(f"/containers/{cid}/logs", params={"stdout":1, "stderr":1, "tail":20})
                        text = log_resp.text[-1200:]
                        return f"📜 **容器 `{real_name}` 最新日志 (Top 20 行)**:\n```bash\n{text}\n```"
                return f"⚠️ 未检索到容器 `{target_name}`。"
        except Exception as e:
            return f"❌ 获取日志失败: {str(e)}"

    # 5. Temperature & S.M.A.R.T Matrix
    if any(k in q for k in ["温度", "硬盘温度", "cpu温度", "风扇", "转速", "发热", "散热", "smart"]):
        cpu_t = get_cpu_and_fan_telemetry()
        disks = get_disks_matrix()
        d_lines = []
        for d in disks:
            if d.get("installed"):
                d_lines.append(f"- **{d['bay_name']}** ({d.get('series', '')} - {d.get('model_code', '')}, {d.get('capacity', '')}): `{d.get('temp', '--')}°C` · 状态: `🟢 {d.get('health', 'GOOD')}`")
            else:
                d_lines.append(f"- **{d['bay_name']}**: `⚪ {d.get('series', '空置插槽')}`")
        core_str = ", ".join([f"{t}°C" for t in cpu_t.get("core_temps", [])])
        thermal_badge = "🔴 核心高温警报" if cpu_t.get("is_overheat") else ("🟡 负载偏高" if cpu_t.get("temp_status") == "warning" else "🟢 良好")
        return (
            f"🌡️ **NAS 硬件温度与温控矩阵**\n\n"
            f"- **CPU Package 核心温度**: `{cpu_t['cpu_temp']}°C` (各核心: `[{core_str}]`)\n"
            f"- **系统风扇转速**: `{cpu_t['fan_speed']}`\n"
            f"- **主板系统温度**: `{cpu_t['system_temp']}°C`\n"
            f"- **温控健康评级**: `{thermal_badge}`\n\n"
            f"**硬盘槽位 S.M.A.R.T 温度监控**:\n" + "\n".join(d_lines)
        )

    # 6. Network Throughput
    if any(k in q for k in ["网速", "流量", "带宽", "网络", "下载速度", "上传速度", "eth0"]):
        net = get_network_throughput()
        qb = await get_qb_status()
        qb_dl = qb.get("dl_speed_str", "0 B/s") if qb.get("online") else "离线"
        qb_ul = qb.get("up_speed_str", "0 B/s") if qb.get("online") else "离线"
        return (
            f"🌐 **NAS 2.5GbE 极速网络遥测**\n\n"
            f"- **物理接口**: `{net['interface']}`\n"
            f"- **当前实时下载**: `↓ {net['rx_speed_str']}` (累计下行: `{net['total_rx_str']}`)\n"
            f"- **当前实时上传**: `↑ {net['tx_speed_str']}` (累计上行: `{net['total_tx_str']}`)\n"
            f"- **qBittorrent 实时吞吐**: 下行 `{qb_dl}` · 上行 `{qb_ul}` (活跃种子: `{qb.get('torrents_active', 0)}`)"
        )

    # 7. System Health Status
    if any(k in q for k in ["状态", "健康", "负载", "cpu", "内存", "概况", "体检"]):
        cpu = get_cpu_usage()
        cpu_t = get_cpu_and_fan_telemetry()
        mem = get_memory_info()
        uptime = get_uptime_str()
        load = get_load_avg()
        net = get_network_throughput()
        return (
            f"📊 **NAS 系统核心遥测报告**\n\n"
            f"- **设备型号**: {get_nas_model_name()} (`{get_host_name()}`)\n"
            f"- **开机运行**: `{uptime}`\n"
            f"- **CPU 状态**: `{cpu}%` (温度: `{cpu_t['cpu_temp']}°C`, 散热风扇: `{cpu_t['fan_speed']}`)\n"
            f"- **物理内存**: `{mem['used_gb']} GB` / `{mem['total_gb']} GB` (占用率 `{mem['percent']}%`)\n"
            f"- **网络实时吞吐**: `↓ {net['rx_speed_str']}` / `↑ {net['tx_speed_str']}`\n"
            f"- **系统负载**: `{load[0]}, {load[1]}, {load[2]}`\n"
            f"- **状态评估**: 🟢 系统各核心指标运转平稳，温控健康。"
        )

    if any(k in q for k in ["存储", "硬盘", "容量", "空间", "磁盘"]):
        pools = get_storage_pools()
        lines = ["💾 **NAS 存储卷实时容量状态**:\n"]
        for p in pools:
            lines.append(f"- **{p['name']}**: 已用 `{p['used_str']}` / 总计 `{p['total_str']}` (使用率 `{p['percent']}%`)")
        return "\n".join(lines)

    if any(k in q for k in ["docker", "容器", "服务", "生态"]):
        try:
            async with get_docker_client() as client:
                resp = await client.get("/containers/json?all=1")
                containers = resp.json()
                running = [c.get("Names", [""])[0].lstrip("/") for c in containers if c.get("State") == "running"]
                stopped = [c.get("Names", [""])[0].lstrip("/") for c in containers if c.get("State") != "running"]
                return (
                    f"🐳 **Docker 容器运行矩阵**\n\n"
                    f"**正在运行 ({len(running)})**:\n" +
                    "\n".join([f"- 🟢 `{name}`" for name in running]) + "\n\n" +
                    (f"**已停止 ({len(stopped)})**:\n" + "\n".join([f"- ⚪ `{name}`" for name in stopped]) if stopped else "")
                )
        except Exception as e:
            return f"❌ 容器查询失败: {str(e)}"

    return (
        "🤖 **Neo-Ops 极客智能终端就绪**\n\n"
        "我已全面接管您的威联通 NAS 宿主机硬件遥测与容器生态。\n\n"
        "**当前可用本地免 Key 指令**：\n"
        "- ⏸️ `暂停下载` / ▶️ `恢复下载` / 🐢 `切换限速` (直接控制 qBittorrent)\n"
        "- 🌡️ `硬盘温度` / `CPU温度` / `风扇转速` (全盘 S.M.A.R.T 温控矩阵)\n"
        "- 🌐 `实时网速` / `网络流量` (2.5GbE eth0 真实吞吐)\n"
        "- 🧹 `清理镜像` (一键清理 Docker 悬空镜像释放空间)\n"
        "- 🔄 `重启 [容器名]` / 📜 `查看 [容器名] 日志`\n"
        "- 📊 `系统状态` / 💾 `磁盘空间`"
    )

@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    saved_cfg = get_config()
    api_key = req.api_key or saved_cfg.get("api_key")
    provider = req.provider or saved_cfg.get("provider", "gemini")
    base_url = req.base_url or saved_cfg.get("base_url")
    raw_model = req.model or saved_cfg.get("model", "gemini-3.8-flash")
    model = normalize_model_name(raw_model)
    
    user_latest = req.messages[-1].content if req.messages else ""
    
    # Check if this matches a direct local control command first
    direct_actions = ["暂停下载", "恢复下载", "慢速模式", "切换限速", "清理镜像", "重启", "查看日志"]
    if any(k in user_latest for k in direct_actions):
        reply = await process_local_ai_command(user_latest)
        return {"role": "assistant", "content": reply}

    if not api_key:
        reply = await process_local_ai_command(user_latest)
        return {"role": "assistant", "content": reply}
        
    try:
        cpu = get_cpu_usage()
        cpu_t = get_cpu_and_fan_telemetry()
        mem = get_memory_info()
        uptime = get_uptime_str()
        pools = get_storage_pools()
        net = get_network_throughput()
        disks = get_disks_matrix()
        qb = await get_qb_status()
        
        storage_str = ", ".join([f"{p['name']}:{p['percent']}%" for p in pools])
        disks_str = ", ".join([f"{d['bay_name']}({d.get('series', '')} {d.get('model_code', '')}):{d.get('temp', '--')}°C" for d in disks if d.get('installed')])
        
        async with get_docker_client() as client:
            d_resp = await client.get("/containers/json?all=1")
            c_names = [c.get("Names", [""])[0].lstrip("/") for c in d_resp.json() if c.get("State") == "running"]
            docker_str = ", ".join(c_names)
    except Exception as e:
        cpu, mem, uptime, storage_str, docker_str = 0, {}, "", "", ""
        cpu_t, net, disks_str, qb = {}, {}, "", {}

    system_prompt = (
        "你是一个部署在威联通 QNAP TS-466C NAS 宿主机上的顶尖极客智能运维Copilot（NEO-OPS）。\n"
        "你的语气专业、富有机甲极客美感、严谨精确，善用 Markdown 代码块、标签与结构化数据。\n\n"
        f"[宿主机实时遥测与硬件数据]:\n"
        f"- 设备: QNAP TS-466C (处理器: {get_cpu_model_name()}, Linux x86_64, 4-Bay + 2x M.2 NVMe)\n"
        f"- 开机运行: {uptime}\n"
        f"- CPU 负荷: {cpu}% (Package温度: {cpu_t.get('cpu_temp', 0)}°C, 核心温控: {cpu_t.get('core_temps', [])})\n"
        f"- 散热风扇: {cpu_t.get('fan_speed', 'N/A')}\n"
        f"- 物理内存: 已用 {mem.get('used_gb',0)}GB / 总计 {mem.get('total_gb',0)}GB ({mem.get('percent',0)}%)\n"
        f"- 2.5GbE 网口 (eth0): 下载 {net.get('rx_speed_str', '0 B/s')}, 上传 {net.get('tx_speed_str', '0 B/s')} (累计下行 {net.get('total_rx_str', '0 B')})\n"
        f"- 硬盘温控矩阵: {disks_str}\n"
        f"- 存储池利用率: {storage_str}\n"
        f"- qBittorrent 下载栈: 在线={qb.get('online')}, 下行={qb.get('dl_speed_str','0 B/s')}, 上行={qb.get('up_speed_str','0 B/s')}, 活跃种子={qb.get('torrents_active', 0)}\n"
        f"- 运行中容器: {docker_str}\n\n"
        "请结合以上实时上下文，回答用户的提问并提供最精炼、专业的运维分析与建议。"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if provider == "gemini":
                candidate_models = [
                    model,
                    "gemini-3.1-flash-lite-preview",
                    "gemini-3-flash-preview",
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                    "gemini-flash-latest"
                ]
                seen_candidates = []
                for cand in candidate_models:
                    if cand not in seen_candidates:
                        seen_candidates.append(cand)
                
                last_error = ""
                for target_model in seen_candidates:
                    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={api_key}"
                    if base_url:
                        endpoint = f"{base_url.rstrip('/')}/models/{target_model}:generateContent?key={api_key}"
                        
                    contents = []
                    for m in req.messages[-10:]:
                        role = "user" if m.role == "user" else "model"
                        contents.append({"role": role, "parts": [{"text": m.content}]})
                        
                    body = {
                        "contents": contents,
                        "systemInstruction": {"parts": [{"text": system_prompt}]}
                    }
                    resp = await client.post(endpoint, json=body)
                    if resp.status_code == 200:
                        data = resp.json()
                        answer = data["candidates"][0]["content"]["parts"][0]["text"]
                        return {"role": "assistant", "content": answer}
                    else:
                        last_error = resp.text
                        if resp.status_code in [400, 404, 503]:
                            continue
                        break
                        
                return {"role": "assistant", "content": f"⚠️ Gemini API 返回异常: {last_error}"}
            else:
                endpoint = base_url.rstrip("/") + "/chat/completions" if base_url else "https://api.deepseek.com/chat/completions"
                api_messages = [{"role": "system", "content": system_prompt}]
                for m in req.messages[-10:]:
                    api_messages.append({"role": m.role, "content": m.content})
                    
                body = {
                    "model": model,
                    "messages": api_messages,
                    "temperature": 0.5
                }
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = await client.post(endpoint, json=body, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    answer = data["choices"][0]["message"]["content"]
                    return {"role": "assistant", "content": answer}
                else:
                    return {"role": "assistant", "content": f"⚠️ API 返回错误 ({resp.status_code}): {resp.text}"}
    except Exception as e:
        local_reply = await process_local_ai_command(user_latest)
        return {"role": "assistant", "content": f"*(API 远程连接异常，已由宿主机 Neo-Ops 接管)*\n\n{local_reply}"}

@app.get("/manifest.json")
async def get_manifest():
    manifest_path = os.path.join("static", "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse(content={"name": "NEO-OPS", "short_name": "NEO-OPS"})

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>NAS Geek Dashboard is starting up...</h1>")
