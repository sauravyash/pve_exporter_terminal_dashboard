#!/usr/bin/env python3
import os
import sys
import time
import json
import math
import urllib.parse
import urllib.request

PROM = os.environ.get("PROM", "http://192.168.1.24:9090")
NODE = os.environ.get("NODE", "node/r440")
TTY_DEV = os.environ.get("TTY_DEV", "/dev/tty")

BULK_INTERVAL = 5.0
REFRESH_INTERVAL = 0.2

ESC = "\x1b"

def _open_tty():
    # open for write-only, unbuffered
    return open(TTY_DEV, "wb", buffering=0)

def _w(tty, s: str):
    tty.write(s.encode("utf-8", errors="ignore"))

def get_uptime() -> str:
    try:
        with open("/proc/uptime", "r") as f:
            seconds = int(float(f.read().split()[0]))
    except Exception:
        seconds = 0
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = ["up "]
    if d: parts.append(f"{d}d ")
    if h: parts.append(f"{h}h ")
    if m: parts.append(f"{m}m ")
    parts.append(f"{s}s")
    return "".join(parts)

def draw_host_only(tty, host_line: str):
    # save cursor, move home, draw line, clear to EOL, restore cursor
    _w(tty, f"{ESC}7{ESC}[H{host_line}{ESC}[K{ESC}8")

def draw_full_screen(tty, host_line: str, table: str):
    # move home, draw host + table, clear below
    _w(tty, f"{ESC}[H{host_line}\n{table}\n{ESC}[J")

def prom_query(prom_url: str, query: str, timeout: float = 2.5):
    url = f"{prom_url}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
    
def parse_results(bulk_json):
    """
    Build:
      - results: list of raw series
      - by_key: dict[(__name__, id)] -> list[float] (values as floats)
      - by_name_only: dict[__name__] -> list[float]
      - guests: list of dicts with id, name, type (for pve_guest_info)
    """
    results = (bulk_json or {}).get("data", {}).get("result", []) or []
    by_key = {}
    by_name_only = {}
    guests = []
    for series in results:
        metric = series.get("metric", {})
        name = metric.get("__name__")
        val_str = (series.get("value") or [None, None])[1]
        try:
            val = float(val_str)
        except (TypeError, ValueError):
            continue

        # index by (name,id) if id present
        mid = metric.get("id")
        if name and mid is not None:
            by_key.setdefault((name, str(mid)), []).append(val)
        if name:
            by_name_only.setdefault(name, []).append(val)

        # collect guest info
        if name == "pve_guest_info":
            guests.append({
                "id": str(metric.get("id", "")),
                "name": metric.get("name", ""),
                "type": metric.get("type", ""),
            })
    return results, by_key, by_name_only, guests

def fmt_percent(val):
    return f"{val:4.1f}"

def fmt_mb(val):
    # bytes -> MiB with 1 decimal
    return f"{val/1048576:6.1f}"

def safe_get_pct(used, total):
    if used is None or total in (None, 0):
        return None
    try:
        return (used / total) * 100.0
    except Exception:
        return None

def first_or_none(lst):
    return lst[0] if lst else None

def build_vm_table(by_key, guests):
    # header
    table = "\nVM/CT         \tCPU%   \tMEM%   \tDiskR  \tDiskW  \tNetIn  \tNetOut\n"
    for g in guests:
        gid = g["id"]
        gname = g["name"] or ""
        gtype = g["type"] or ""

        cpu_ratio = first_or_none(by_key.get(("pve_cpu_usage_ratio", gid), []))
        cpu_pct = None if cpu_ratio is None else (cpu_ratio * 100.0)

        mem_t = first_or_none(by_key.get(("pve_memory_size_bytes", gid), []))
        mem_u = first_or_none(by_key.get(("pve_memory_usage_bytes", gid), []))
        mem_pct = safe_get_pct(mem_u, mem_t)

        readb = first_or_none(by_key.get(("pve_disk_read_bytes", gid), []))
        writeb = first_or_none(by_key.get(("pve_disk_write_bytes", gid), []))
        netinb = first_or_none(by_key.get(("pve_net_in_bytes_total", gid), []))
        netoutb = first_or_none(by_key.get(("pve_net_out_bytes_total", gid), []))

        # choose color by type: lxc = cyan, else yellow
        color = "\x1b[1;36m" if gtype == "lxc" else "\x1b[1;33m"
        reset = "\x1b[0m"

        cpu_s   = "---" if cpu_pct is None else f"{cpu_pct:5.1f}"
        mem_s   = "---" if mem_pct is None else f"{mem_pct:6.1f}"
        read_s  = "---" if readb  is None else f"{readb/1048576:6.1f}"
        write_s = "---" if writeb is None else f"{writeb/1048576:6.1f}"
        neti_s  = "---" if netinb is None else f"{netinb/1048576:6.1f}"
        neto_s  = "---" if netoutb is None else f"{netoutb/1048576:6.1f}"

        row = f"\n{color}{gname:<12}{reset} \t{cpu_s} \t{mem_s} \t{read_s}\t{write_s}\t{neti_s}\t{neto_s}\n\n"
        table += row
    return table

def main():
    try:
        tty = _open_tty()
    except Exception as e:
        print(f"Failed to open TTY '{TTY_DEV}': {e}", file=sys.stderr)
        sys.exit(1)

    last_bulk = 0.0
    bulk_cache = None
    cached_table = ""
    full_redraw = True  # draw on first pass

    while True:
        now = time.time()

        # bulk scrape every 5s
        if (now - last_bulk) >= BULK_INTERVAL or bulk_cache is None:
            try:
                bulk_cache = prom_query(PROM, "{__name__=~'pve_.*|nvme_temperature_celsius'}")
                last_bulk = now
                _, by_key, by_name_only, guests = parse_results(bulk_cache)
                cached_table = build_vm_table(by_key, guests)
                full_redraw = True
            except Exception as e:
                # keep using previous cache if available; otherwise show placeholders
                _, by_key, by_name_only, guests = parse_results(bulk_cache or {})
                # don’t flip full_redraw unless we actually rebuilt the table
                pass
        else:
            # fast path: re-derive indices cheaply from cache
            _, by_key, by_name_only, guests = parse_results(bulk_cache or {})

        # host metrics from cached bulk
        cpu_ratio = first_or_none(by_key.get(("pve_cpu_usage_ratio", NODE), []))
        cpu_pct = None if cpu_ratio is None else cpu_ratio * 100.0

        mem_t = first_or_none(by_key.get(("pve_memory_size_bytes", NODE), []))
        mem_u = first_or_none(by_key.get(("pve_memory_usage_bytes", NODE), []))
        mem_pct = safe_get_pct(mem_u, mem_t)

        # nvme temp: may be multiple; match bash’s “first then round” behavior
        nvme_vals = by_name_only.get("nvme_temperature_celsius", [])
        nvme_c = None
        if nvme_vals:
            try:
                nvme_c = int(round(nvme_vals[0]))
            except Exception:
                nvme_c = None

        up = get_uptime()

        # VMs/CTs count
        vm_count = len(guests)

        # colors for host line labels
        hostline = (
            f"\x1b[1;32mCPU\x1b[0m: {('---' if cpu_pct is None else f'{cpu_pct:4.1f}')}%  "
            f"\x1b[1;34mMEM\x1b[0m: {('---' if mem_pct is None else f'{mem_pct:4.1f}')}%  "
            f"\x1b[1;33mVMs\x1b[0m:{vm_count:2d}  "
            f"\x1b[1;35mNVMe\x1b[0m:{('---' if nvme_c is None else f'{nvme_c:3d}')}°C  "
            f"{up}"
        )

        if full_redraw:
            draw_full_screen(tty, hostline, cached_table)
            full_redraw = False
        else:
            draw_host_only(tty, hostline)

        time.sleep(REFRESH_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # restore cursor (just in case) and exit cleanly
        try:
            with open(TTY_DEV, "wb", buffering=0) as tty:
                _w(tty, f"{ESC}[?25h")  # show cursor
        except Exception:
            pass
        sys.exit(0)

