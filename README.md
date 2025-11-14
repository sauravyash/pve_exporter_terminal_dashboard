# Prometheus TTY Dashboard

A small, dependency-light terminal dashboard for Proxmox VE environments scraped by Prometheus.

It renders a live, flicker-free view directly on a Linux TTY (or any terminal), showing:

- Host CPU, memory, NVMe temperature and uptime  
- Per-VM / container CPU, memory, disk and network usage  
- (Optional) Minecraft server online players, driven by Prometheus metrics  

The layout, metrics and styling are fully configurable via a single YAML file (`config.yml`).

---

## Features

- **Proxmox + Prometheus**  
  - Uses Prometheus’ HTTP API (`/api/v1/query`) to pull metrics from a PVE Prometheus exporter.
  - Designed around `pve_*` metrics: `pve_guest_info`, `pve_cpu_usage_ratio`, `pve_memory_*`, disk, network, etc.

- **TTY-native, no curses**  
  - Pure VT100 / ANSI escape codes for smooth, flicker-free updates.
  - Works great on `/dev/tty1` as a login/dashboard console, or in any regular terminal.

- **Declarative YAML config** (`config.yml`)  
  - Define metrics, derived expressions, and how to display them (headers, tables, lists).
  - Centralised color palette + macros via `${colors.*}` references.
  - Layout section to control which views appear and in what order.

- **Safe derived expressions**  
  - Simple math expressions (`+ - * / // % **`) with a tiny AST sandbox.
  - Per-row vs global derived values for richer tables.

- **Two ways to run**  
  - A fully configurable engine (`pve_ttydash.py` + `config.yml`).
  - A very small, single-file script (`simple_pve_tty_dash.py`) that hardcodes common Proxmox metrics.

---

## Repository Layout

```text
.
├── dash_engine.py         # Core engine: Prometheus client, safe eval, renderer
├── pve_ttydash.py         # Thin launcher around DashboardEngine + YAML config
├── simple_pve_tty_dash.py # Minimal hard-coded dashboard script
├── config.yml             # Example configuration for Proxmox + Minecraft
├── tty-status.service     # Example systemd unit for a login TTY dashboard
├── LICENSE                # BSD-3-Clause
└── README.md              # This file
```

---

## Requirements

* A Linux host (tested on Proxmox VE / Debian-like environments).
* Python 3.8+.
* Prometheus scraping:
  * (Recommended) A Proxmox exporter that exposes `pve_*` metrics (for proxmox data).
  * (Optional) a Minecraft exporter exposing `minecraft_*` metrics.
* Basic terminal with ANSI/VT100 support.

### Python dependencies

Only **PyYAML** is required:

```bash
pip install pyyaml
```

You can also pin it in a venv if you prefer:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml
```

---

## Quickstart (YAML engine)

1. **Clone the repo**

```bash
git clone https://github.com/sauravyash/pve_exporter_terminal_dashboard.git
cd pve_exporter_terminal_dashboard
```

2. **Edit `config.yml`**

At minimum, update the Prometheus URL and node ID:

```yaml
datasources:
  prometheus:
    base_url: http://<prometheus-host>:9090
    timeout_s: 3.0

globals:
  vars:
    node_id: node/<your-node-name>
```

3. **Run in your current terminal**

```bash
export CONFIG=./config.yml
export TTY_DEV=/dev/tty        # or leave default
python3 pve_ttydash.py
```

Press **Ctrl-C** to exit.

4. **Run on a dedicated console (e.g. `/dev/tty1`)**

```bash
sudo CONFIG=/usr/local/etc/pve_ttydash.yml \
     TTY_DEV=/dev/tty1 \
     python3 /usr/local/bin/pve_ttydash.py
```

You’ll typically wire this up with a systemd service (see below).

> **Note:** `pve_ttydash.py` uses the `CONFIG` environment variable (default: `config.yml`) and `TTY_DEV` (default: `/dev/tty`).

---

## Alternative: Minimal one-file dashboard

If you just want a simple Proxmox dashboard with minimal moving parts, use:

```bash
python3 simple_pve_tty_dash.py
```

Environment variables:

* `PROM` – Prometheus base URL (default: `http://192.168.1.24:9090`)
* `NODE` – Proxmox node ID label (default: `node/r440`)
* `TTY_DEV` – Target TTY (default: `/dev/tty`)

Example:

```bash
PROM=http://prometheus:9090 \
NODE=node/r440 \
TTY_DEV=/dev/tty1 \
sudo python3 simple_pve_tty_dash.py
```

This script:

* Scrapes `pve_*` metrics using a single PromQL expression.
* Shows a host line and a simple VM/CT table.
* Has no YAML config and fewer features, but is very easy to deploy.

---

## Configuration Reference (`config.yml`)

The YAML config drives nearly everything the engine does.

### Top-level structure (simplified)

```yaml
version: 1

datasources:
  prometheus:
    base_url: http://192.168.1.24:9090
    timeout_s: 3.0

globals:
  refresh:
    fast_s: 0.2   # host header update interval
    bulk_s: 5.0   # full Prometheus scrape interval
  vars:
    node_id: node/r440
  defaults:
    missing_value: '---'
    formats:
      percent: '%.1f%%'
      number: '%.1f'
      kb: '%.1f KB'
      mb: '%.1f MB'
      temp_c: '%.0f°C'

metrics:
  - id: host_cpu_decimal
    query: 'pve_cpu_usage_ratio{id="${node_id}"}'
    query_type: instant
  # ...

derived:
  - id: host_cpu
    expr: 100 * host_cpu_decimal
  # ...

views:
  - id: host_header
    type: header
    # ...

  - id: guest_table
    type: table
    # ...

  - id: mc_dash
    type: list
    # ...

layout:
  - view: host_header
  - view: guest_table
  - view: mc_dash

colors:
  # color palette for ${colors.*} macros
```

---

### Globals

```yaml
globals:
  refresh:
    fast_s: 0.2   # How often the header is redrawn
    bulk_s: 5.0   # How often Prometheus is queried
  vars:
    node_id: node/r440
  defaults:
    missing_value: '---'
    formats:
      percent: '%.1f%%'
      number: '%.1f'
      kb: '%.1f KB'
      mb: '%.1f MB'
      temp_c: '%.0f°C'
```

* `vars` values can be referenced in metric queries as `${node_id}`.
* `defaults.formats` define default printf-style formatting names you can reuse.

---

### Metrics

Each entry in `metrics:` is a PromQL expression:

```yaml
metrics:
  - id: host_cpu_decimal
    query: 'pve_cpu_usage_ratio{id="${node_id}"}'
    query_type: instant

  - id: guest_info
    query: |
      ( ... big PromQL expression for guest state ... )
    query_type: instant
    expose_labels:
      - id
      - name
      - type
      - state

  - id: guest_disk_read
    query: rate(pve_disk_read_bytes[5m])
    query_type: instant
  # ...
```

Key fields:

* `id`: internal name used in derived expressions and views.
* `query`: PromQL string (supports `${vars}` substitution).
* `query_type`: `instant` for now.
* `expose_labels`: which labels to keep from the result; used to build row contexts (e.g. VM ID, name, type, state).

---

### Derived values

`derived:` lets you compute new values from metrics or other deriveds:

```yaml
derived:
  - id: host_cpu
    expr: 100 * host_cpu_decimal

  - id: host_mem_pct
    expr: 100 * host_mem_used / host_mem_total

  - id: guest_cpu_pct
    per_row: true
    expr: 100 * guest_cpu

  - id: guest_mem_pct
    per_row: true
    expr: 100 * guest_mem_used / guest_mem_total
```

* `expr` is evaluated in a restricted AST sandbox.
* `per_row: true` means the expression is evaluated per guest row with that row’s labels and values.

---

### Views

Three types are currently used in `config.yml`:

1. **Header**

   Single line of host info, with builtin computed values:

   ```yaml
   - id: host_header
     type: header
     title: Host Summary
     template: >
       ${uptime}
       ${colors.bright.green}CPU${colors.reset}: ${host_cpu|percent:1}
       ${colors.bright.blue}MEM${colors.reset}: ${host_mem_pct|percent:1}
       ${colors.bright.yellow}VMs${colors.reset}:${vm_count}
       ${colors.bright.magenta}NVMe${colors.reset}:${nvme_temp|temp_c:0}
     computed_values:
       vm_count:
         from_metric: guest_info
         op: count
       uptime:
         builtin: uptime
   ```

   * `template` supports `${var|format:decimals}` syntax.
   * You can use `${colors.*}` macros defined under `colors:`.

2. **Table**

   VM / CT detail table:

   ```yaml
   - id: guest_table
     type: table
     title: VMs and Containers
     source:
       rows_from:
         anchor_metric: guest_info
         join_on_label: id
       preferred_labels:
         name: name
         type: type
       sort:
         by: guest_cpu_pct
         order: desc
     columns:
       - id: name
         title: ${colors.bright.yellow}VM${colors.reset}${colors.bright.magenta}/${colors.reset}${colors.bright.cyan}CT${colors.reset}
         value: ${name}
         style:
           color_by_label:
             type:
               lxc: ${colors.bright.cyan}
               qemu: ${colors.bright.yellow}
           reset: ${colors.reset}
         width: 12
       - id: status
         title: State
         value: ${state}
         width: 12
       - id: cpu
         title: CPU%
         value: ${guest_cpu_pct}
         format: percent
         decimals: 1
       # ...
   ```

   * `rows_from.anchor_metric` tells the engine where VM rows come from.
   * `join_on_label` controls how other metrics are joined (e.g. on `id`).
   * `columns[*].format` can be `number`, `percent`, `kb`, `mb`, `temp_c`, or the special `-b` (“bytes with auto unit”).

3. **List**

   Minecraft player list (if you export those metrics):

   ```yaml
   - id: mc_dash
     type: list
     title: MC Online Users
     source:
       items_from:
         metric: mc_player_list
         labels: [player]
     item:
       template: "${colors.bright.cyan}${player}${colors.reset}"
   ```

---

### Layout

Controls the order of views in the TTY:

```yaml
layout:
  - view: host_header
  - view: guest_table
  - view: mc_dash
```

Add or remove entries to change which blocks are rendered.

---

### Colors and macros

The color palette is defined once:

```yaml
colors:
  reset: "\e[0m"
  bright:
    green:   "\e[1;32m"
    blue:    "\e[1;34m"
    yellow:  "\e[1;33m"
    magenta: "\e[1;35m"
    cyan:    "\e[1;36m"
    red:     "\e[1;31m"
    white:   "\e[1;37m"
```

You can then refer to them anywhere in the config as `${colors.bright.green}`, etc.
The engine substitutes these before rendering.

---

## Systemd / login TTY integration

An example systemd unit is provided as `tty-status.service`. It’s intended to run a small wrapper script on a TTY (e.g. `/dev/tty1`):

```ini
[Unit]
Description=Live Proxmox console dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=TTY_DEV=/dev/tty1
ExecStart=/usr/local/sbin/tty_status.sh
Restart=always
RestartSec=1
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

A simple `tty_status.sh` might look like:

```bash
#!/usr/bin/env bash
CONFIG=/usr/local/etc/pve_ttydash.yml
TTY_DEV=${TTY_DEV:-/dev/tty1}

/usr/bin/python3 /usr/local/bin/pve_ttydash.py
```

Then:

```bash
sudo cp tty-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tty-status.service
```

This gives you a constantly updating dashboard on the chosen TTY after boot.

---

## Troubleshooting

* **Blank screen / no data**

  * Check that Prometheus is reachable from this host.
  * Confirm the `base_url` in `config.yml` is correct.
  * Verify `pve_*` metrics exist in Prometheus (e.g. in the Prometheus UI).

* **Wrong node data**

  * Check `globals.vars.node_id` matches your Proxmox node label.

* **MC player list always empty**

  * Ensure `minecraft_player_online`, `minecraft_query_up`, and `minecraft_status_up` (or equivalent) are scraped by Prometheus.
  * Remove the `mc_dash` view from `layout:` if you don’t use Minecraft metrics.

* **Terminal messed up after exit**

  * If the cursor stays hidden or screen looks odd, run `reset` in the shell or switch to another TTY and back.

---

## License

This project is licensed under the **BSD-3-Clause License**.
See [`LICENSE`](LICENSE) for details.
