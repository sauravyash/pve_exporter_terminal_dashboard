# Prometheus TTY Dashboard

A lightweight **terminal dashboard** for **Proxmox / Prometheus** environments.
Displays host + guest (VM/CT) metrics in a live-updating, flicker-free view directly on a TTY (or any terminal).

The dashboard is fully **configurable** via a single YAML file (`config.yml`) — defining metrics, derived expressions, formatting, colors, and display layout.

---

##  Overview

This tool uses:

* **Prometheus API (`/api/v1/query`)** to scrape current metric values
* **ANSI VT100 sequences** for smooth terminal rendering
* **Declarative YAML configuration** to define:

  * Which metrics to fetch
  * How to compute derived fields (CPU%, MB conversions, etc.)
  * How to display them in tables and headers

It’s written entirely in **Python 3**, with **no external dependencies except `PyYAML`**.

---

## File Structure

```
.
├── dash_engine.py        # Core logic: parser, query engine, renderer
├── pve_ttydash.py        # Thin launcher (loads config + runs loop)
├── config.yml            # Main configuration (you renamed it from pve_dashboard.yaml)
└── README.md             # This file
```

---

## Installation

### 1. Dependencies

Install PyYAML:

```bash
pip install pyyaml
```


## Running

For a live dashboard on your **current terminal**:

```bash
export TTY_DEV=/dev/tty
export PVE_DASH_CFG=./config.yml
python3 pve_ttydash.py
```

Or to run it on a **dedicated console** (`/dev/tty1`):

```bash
sudo TTY_DEV=/dev/tty1 PVE_DASH_CFG=./config.yml python3 pve_ttydash.py
```

Use **Ctrl +C** to exit.

---

## Core Concepts

### 1. Prometheus Integration

The engine queries your Prometheus endpoint defined in:

```yaml
datasources:
  prometheus:
    base_url: "http://192.168.1.24:9090"
    timeout_s: 3.0
```

All metrics are defined as **PromQL expressions** under `metrics:`.

You can use variables (like `${node_id}`) that are substituted at runtime.

---

### 2. Refresh Intervals

In `globals.refresh`:

```yaml
refresh:
  fast_s: 0.2   # header refresh rate
  bulk_s: 5.0   # full Prometheus scrape rate
```

The dashboard:

* Refreshes the **host summary** every `fast_s` seconds
* Performs a full Prometheus query sweep every `bulk_s` seconds

---

### 3. Variables

In `globals.vars`:

```yaml
vars:
  node_id: "node/r440"
```

These can be referenced in metric queries or templates as `${node_id}`.

---

### 4. Formats & Defaults

In `globals.defaults`:

```yaml
defaults:
  missing_value: "---"
  formats:
    percent: "%.1f%%"
    number: "%.1f"
    kb: "%.1f KB"
    mb: "%.1f MB"
    temp_c: "%.0f°C"
```

These control:

* How missing data is displayed
* Default number formats used by columns or templates

---

### **`metrics:` — What to Query**

Each metric has:

```yaml
- id: host_cpu
  query: pve_cpu_usage_ratio{id="${node_id}"}
  query_type: instant
```

| Field             | Description                                                    |
| ----------------- | -------------------------------------------------------------- |
| **id**            | Internal name used in derived formulas and templates           |
| **query**         | PromQL expression (supports `${var}` substitution)             |
| **query_type**    | `instant` (current snapshot). Can be extended to `range` later |
| **expose_labels** | Optional. For table rows (e.g. guest IDs, names, types)        |

#### Example — Guest info

```yaml
- id: guest_info
  query: pve_guest_info
  query_type: instant
  expose_labels: [id, name, type]
```

This tells the dashboard to:

* Use `pve_guest_info` results to build **table rows**
* Extract those labels (`id`, `name`, `type`) per guest

---

### **`derived:` — Computed Fields**

Allows defining simple expressions combining metrics or other deriveds.

```yaml
- id: host_mem_pct
  expr: "100 * host_mem_used / host_mem_total"
```

Each derived can specify:

| Key         | Description                                                          |
| ----------- | -------------------------------------------------------------------- |
| **id**      | Variable name to expose to views                                     |
| **expr**    | Expression evaluated safely via Python AST                           |
| **per_row** | `true` = evaluate once per guest row (row context); `false` = global |

#### Per-row example

```yaml
- id: guest_mem_pct
  per_row: true
  expr: "100 * guest_mem_used / guest_mem_total"
```

The engine builds a row context like:

```python
{ "id": "101", "guest_mem_used": 3.2e9, "guest_mem_total": 8.0e9 }
```

and safely evaluates the expression in that context.

#### Unit conversions

You can easily scale values:

```yaml
expr: "guest_disk_read / 1_000_000"  # bytes → MB
```

---

### **`views:` — How to Display**

Defines visual blocks in the dashboard.
Two types are currently supported:

* `header`: single-line host summary
* `table`: tabular guest/VM display

---

#### Header Example

```yaml
- id: host_header
  type: header
  template: "\u001b[1;32mCPU\u001b[0m: ${host_cpu|percent:1}  \u001b[1;34mMEM\u001b[0m: ${host_mem_pct|percent:1}  \u001b[1;33mVMs\u001b[0m:${vm_count}  \u001b[1;35mNVMe\u001b[0m:${nvme_temp|temp_c:0}  ${uptime}"
  computed_values:
    vm_count: { from_metric: guest_info, op: count }
    uptime:   { builtin: uptime }
```

**Template syntax:**

* `${var|format:decimals}` → format a metric or derived
* `${uptime}` → built-in system uptime
* ANSI escapes (`\u001b[...m`) are supported for color
* You can reference any metric or derived by ID

---

#### Table Example

```yaml
- id: guest_table
  type: table
  source:
    rows_from:
      anchor_metric: guest_info
      join_on_label: id
    preferred_labels: { name: name, type: type }
    sort: { by: guest_cpu_pct, order: desc }

  columns:
    - id: name
      title: "VM/CT"
      value: "${name}"
      style:
        color_by_label: { type: { lxc: "\u001b[1;36m", qemu: "\u001b[1;33m" } }
        reset: "\u001b[0m"
    - id: cpu
      title: "CPU%"
      value: "${guest_cpu_pct}"
      format: percent
    - id: mem
      title: "MEM%"
      value: "${guest_mem_pct}"
      format: percent
    - id: read
      title: "DiskR"
      value: "${guest_disk_read_mb}"
      format: mb
```

Each `column` defines:

| Field        | Meaning                                          |
| ------------ | ------------------------------------------------ |
| **id**       | Internal column name                             |
| **title**    | Header text                                      |
| **value**    | Expression or `${var}` reference                 |
| **format**   | One of `number`, `percent`, `kb`, `mb`, `temp_c` |
| **decimals** | Number of decimal places                         |
| **style**    | Optional ANSI color rules based on labels        |
| **width**    | Optional fixed column width                      |

---

### **`layout:` — Display Order**

Defines the order of views on screen:

```yaml
layout:
  - view: host_header
  - view: guest_table
```

You can add more blocks later (e.g., storage summary, network overview).

---

## Safe Derived Evaluation

All expressions in `derived:` are evaluated via a secure **AST-based sandbox**, supporting only:

```
+, -, *, /, //, %, **, parentheses
```

No function calls or imports are allowed, so you can safely include arbitrary math.

---

## ANSI Styling

You can colorize text using ANSI codes like:

```
\u001b[1;32m ... \u001b[0m   # green
\u001b[1;34m ... \u001b[0m   # blue
\u001b[1;33m ... \u001b[0m   # yellow
\u001b[1;36m ... \u001b[0m   # cyan
```

These can appear in:

* `header.template`
* `columns.style.color_by_label`
* Future threshold-based rules

---

## Example Output (on TTY)

```
CPU:  1.2%  MEM: 23.7%  VMs:6  NVMe:37°C  up 1d 2h 41m 2s
VM/CT          CPU%   MEM%   DiskR   DiskW   NetIn   NetOut
immich         6.2%   31.4%   12.2MB  0.2MB  5.1MB   3.7MB
pihole         0.1%   10.5%    0.0MB  0.0MB  0.1MB   0.1MB
homebridge     0.3%   15.1%    0.0MB  0.0MB  0.0MB   0.0MB
```

---

## Tips & Extensibility

* Add **NVMe SMART metrics**, fan speeds, or power readings by extending `metrics:` and `derived:`.
* You can introduce more **views** for disk or network summaries.
* The YAML parser supports multi-document setups, so future you could have separate configs per node.
* You can simulate data (for debugging) by replacing `prom_query()` in `dash_engine.py` with a fake JSON generator.

---

##  Roadmap Ideas

* [ ] Add range queries and aggregation (`avg_over_time`, etc.)
* [ ] Threshold color rules in YAML (`when: expr > value → color`)
* [ ] JSON exporter for external dashboards
* [ ] Systemd service template for boot-time startup

---

## License

MIT License — use freely, credit appreciated.

