#!/usr/bin/env python3
"""
dash_engine.py
A small engine that reads a YAML config describing Prometheus-backed host/guest data,
computes derived expressions (safe AST), and renders to a TTY using VT100 codes.
Decimal KB/MB (1 KB = 1000 B, 1 MB = 1,000,000 B).
"""
import re
import os
import time
import json
import math
from pprint import pprint
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("This program requires PyYAML. Install with: pip install pyyaml") from e

# ------------------------------- Utilities -------------------------------

ESC = "\x1b"

def open_tty(path: str):
    return open(path, "wb", buffering=0)

def w(tty, s: str):
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
    # save cursor, move to (1,1), write, clear EOL, restore
    w(tty, f"{ESC}7{ESC}[H{host_line}{ESC}[K{ESC}8")

def draw_full_screen(tty, host_line: str, body: str):
    w(tty, f"{ESC}[H{host_line}\n{body}\n{ESC}[J")

def fmt_value(val: Optional[float], fmt: str, decimals: int = 1) -> str:
    if val is None:
        return '---'
    try:
        if fmt == 'percent':
            return f"{val:.{decimals}f}%"
        if fmt == '-b':
            # auto format bytes
            units = ["B", "KB", "MB", "GB", "TB", "PB"]
            size = val
            unit_index = 0
            while size >= 1024 and unit_index < len(units) - 1:
                size = int(size) >> 10  # divide by 1024
                unit_index += 1
            suffix = units[unit_index]
            return f"{size:.{decimals}f} {suffix}"
        if fmt == 'kb':
            val = val / 1000
            return f"{val:.{decimals}f} KB"
        if fmt == 'mb':
            val = val / 1000000
            return f"{val:.{decimals}f} MB"
        if fmt == 'number':
            return f"{val:.{decimals}f}"
        if fmt == 'temp_c':
            return f"{val:.0f}°C"
        # fallback: printf-style string in config
        try:
            return (fmt % val)
        except Exception:
            return str(val)
    except Exception as e:
        print(e)
        return '---'

# ------------------------------- Safe Eval -------------------------------

import ast
import operator

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}

class SafeEval(ast.NodeVisitor):
    def __init__(self, ctx: Dict[str, Any]):
        self.ctx = ctx

    def visit_Expr(self, node):
        return self.visit(node.value)

    def visit_Name(self, node):
        return self.ctx.get(node.id, None)

    def visit_Constant(self, node):  # Py>=3.8
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("Only numeric constants allowed")

    # For Python <3.8 compatibility:
    def visit_Num(self, node):
        return float(node.n)

    def visit_UnaryOp(self, node):
        val = self.visit(node.operand)
        if val is None: return None
        op = _OPS.get(type(node.op))
        if not op: raise ValueError("Operator not allowed")
        return op(val)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right = self.visit(node.right)
        if left is None or right is None:
            return None
        op = _OPS.get(type(node.op))
        if not op: raise ValueError("Operator not allowed")
        try:
            return op(left, right)
        except ZeroDivisionError:
            return None

    def generic_visit(self, node):
        raise ValueError(f"Disallowed expression node: {type(node).__name__}")

def eval_expr(expr: str, ctx: Dict[str, Any]) -> Optional[float]:
    tree = ast.parse(expr, mode="eval")
    return SafeEval(ctx).visit(tree.body)

# ------------------------------ Data Classes -----------------------------

@dataclass
class MetricDef:
    id: str
    query: str
    query_type: str = "instant"
    expose_labels: List[str] = field(default_factory=list)

@dataclass
class DerivedDef:
    id: str
    expr: str
    per_row: bool = False

@dataclass
class ColumnDef:
    id: str
    title: str
    value: str
    format: str = "number"
    decimals: int = 1
    width: Optional[int] = None
    style: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TableSourceDef:
    rows_from: Dict[str, Any]
    preferred_labels: Dict[str, str] = field(default_factory=dict)
    sort: Dict[str, Any] = field(default_factory=dict)
    filter: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ViewDef:
    id: str
    type: str
    title: Optional[str] = None
    template: Optional[str] = None
    computed_values: Dict[str, Any] = field(default_factory=dict)
    source: Optional[TableSourceDef] = None
    columns: List[ColumnDef] = field(default_factory=list)

# ------------------------------ Prometheus -------------------------------

def prom_query(base_url: str, query: str, timeout: float = 3.0):
    url = f"{base_url}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def parse_results(result_json: dict) -> List[dict]:
    return (result_json or {}).get("data", {}).get("result", []) or []


# ------------------------------ Extra features --------------------------- 

def apply_color_macros(cfg):
    colors = cfg.get("colors", {})
    def resolve(path):
        val = colors
        for part in path.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return None
        return val
    def replace_colors(s):
        def sub(match):
            path = match.group(1)
            return resolve(path) or match.group(0)
        return re.sub(r"\$\{colors\.([a-zA-Z0-9_.-]+)\}", sub, s)
    # recursively apply to all strings in the config
    def recurse(node):
        if isinstance(node, dict):
            return {k: recurse(v) for k, v in node.items()}
        elif isinstance(node, list):
            return [recurse(v) for v in node]
        elif isinstance(node, str):
            return replace_colors(node)
        else:
            return node
    return recurse(cfg)

def clear_tty(tty_path="/dev/tty"):
    with open(tty_path, "wb", buffering=0) as tty:
        tty.write(b"\033c")  # full reset
        tty.flush()

ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

def visible_len(s: str) -> int:
    """Return printable length of string (ignoring ANSI codes)."""
    return len(ANSI_ESCAPE.sub('', s))

def pad_ansi(s: str, width: int, align='<'):
    """Pad colored strings properly, ignoring ANSI escape codes."""
    real_length = visible_len(s)
    pad_len = max(0, width - real_length)
    if align == '<':
        return s + ' ' * pad_len
    elif align == '>':
        return ' ' * pad_len + s
    elif align == '^':
        left = pad_len // 2
        right = pad_len - left
        return ' ' * left + s + ' ' * right
    else:
        raise ValueError("align must be '<', '>', or '^'")
# ------------------------------ Engine Core ------------------------------

class DashboardEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ds = cfg.get("datasources", {}).get("prometheus", {})
        self.base_url = self.ds.get("base_url")
        if not self.base_url:
            raise ValueError("datasources.prometheus.base_url is required")

        self.timeout_s = float(self.ds.get("timeout_s", 3.0))
        self.refresh_fast = float(cfg.get("globals", {}).get("refresh", {}).get("fast_s", 0.2))
        self.refresh_bulk = float(cfg.get("globals", {}).get("refresh", {}).get("bulk_s", 5.0))
        self.globals_vars = cfg.get("globals", {}).get("vars", {})

        self.metrics: List[MetricDef] = [
            MetricDef(**m) for m in cfg.get("metrics", [])
        ]
        self.derived: List[DerivedDef] = [
            DerivedDef(**d) for d in cfg.get("derived", [])
        ]
        # parse views
        self.views: List[ViewDef] = []
        for v in cfg.get("views", []):
            if v.get("type") == "table":
                source = v.get("source", {})
                view = ViewDef(
                    id=v["id"],
                    type=v["type"],
                    title=v.get("title"),
                    source=TableSourceDef(
                        rows_from=source.get("rows_from", {}),
                        preferred_labels=source.get("preferred_labels", {}),
                        sort=source.get("sort", {}),
                        filter=source.get("filter", {}),
                    ),
                    columns=[ColumnDef(**c) for c in v.get("columns", [])],
                )
            else:
                view = ViewDef(
                    id=v["id"],
                    type=v["type"],
                    title=v.get("title"),
                    template=v.get("template"),
                    computed_values=v.get("computed_values", {}),
                )
            self.views.append(view)

        self.layout: List[str] = [item["view"] for item in cfg.get("layout", [])]

        # indices populated at runtime
        self.series: List[dict] = []
        self.by_name_id: Dict[Tuple[str, str], List[float]] = {}
        self.by_name_only: Dict[str, List[float]] = {}
        self.rows: Dict[str, Dict[str, Any]] = {}  # id -> {labels, values}

    # ------------------ Query + index ------------------

    def _subst_vars(self, s: str) -> str:
        # replace ${var} using globals.vars
        out = s
        for k, v in self.globals_vars.items():
            out = out.replace("${" + k + "}", str(v))
        return out

    def bulk_fetch(self):
        # For simplicity, fetch metrics individually (still fast enough);
        # can be optimized by merging queries if desired.
        all_results = []
        for m in self.metrics:
            q = self._subst_vars(m.query)
            try:
                res = prom_query(self.base_url, q, self.timeout_s)
                result_series = parse_results(res)
                # annotate series with logical metric id for lookup
                for s in result_series:
                    s["_metric_id"] = m.id
                    s["_expose_labels"] = m.expose_labels
                all_results.extend(result_series)
            except Exception:
                # tolerate transient failures
                continue
        self.series = all_results
        self._reindex()

    def _reindex(self):
        self.by_name_id.clear()
        self.by_name_only.clear()
        self.rows.clear()
        
        

        # Build indices and guest rows (join by label 'id' when possible)
        for s in self.series:
            metric = s.get("metric", {})
            name = s.get("_metric_id") or metric.get("__name__")
            val_str = (s.get("value") or [None, None])[1]
            try:
                val = float(val_str)
            except Exception:
                continue

            # index by (name, id) and by name
            row_id = None
            if "id" in metric:
                row_id = str(metric["id"])
                self.by_name_id.setdefault((name, row_id), []).append(val)
            self.by_name_only.setdefault(name, []).append(val)

            # if this series exposes labels, keep them for row metadata
            expose = s.get("_expose_labels") or []

            if expose and row_id is not None:
                labels = {k: metric.get(k, "") for k in expose}
                r = self.rows.setdefault(row_id, {"labels": {}, "values": {}})
                r["labels"].update(labels)

        # also attach per-row metric values we care about
        for (name, rid), vals in self.by_name_id.items():
            r = self.rows.setdefault(rid, {"labels": {}, "values": {}})
            r["values"][name] = vals[0]  # take first value


    # ------------------ Contexts for eval ------------------

    def global_ctx(self) -> Dict[str, Any]:
        ctx = {}
        # pick first value per metric id
        for m in self.metrics:
            vals = self.by_name_only.get(m.id, [])
            if vals:
                ctx[m.id] = vals[0]
        return ctx

    def rows_ctx(self) -> Dict[str, Dict[str, Any]]:
        # build per-row contexts with labels + values
        out = {}
        for rid, r in self.rows.items():
            ctx = {}
            for k, v in r["labels"].items():
                # keep labels; numeric labels can also be used in expr
                try:
                    ctx[k] = float(v)
                except Exception:
                    # strings are kept, but evaluator ignores non-numeric
                    ctx[k] = v
            for k, v in r["values"].items():
                ctx[k] = v
            out[rid] = ctx 
        return out

    # ------------------ Derived evaluation ------------------

    def compute_derived(self, gctx: Dict[str, Any], rctxs: Dict[str, Dict[str, Any]]):
        derived_global: Dict[str, Any] = {}
        derived_rows: Dict[str, Dict[str, Any]] = {rid: {} for rid in rctxs}
        
        # simple multi-pass to resolve references among deriveds
        for _ in range(3):
            for d in self.derived:
                if d.per_row:
                    for rid, base in rctxs.items():
                        ctx = {}
                        ctx.update(gctx)
                        ctx.update(derived_global)
                        ctx.update(base)
                        ctx.update(derived_rows[rid])
                        try:
                            val = eval_expr(d.expr, ctx)
                        except Exception:
                            val = None
                        derived_rows[rid][d.id] = val
                else:
                    ctx = {}
                    ctx.update(gctx)
                    ctx.update(derived_global)
                    try:
                        val = eval_expr(d.expr, ctx)
                    except Exception:
                        val = None
                    derived_global[d.id] = val
        
        return derived_global, derived_rows

    # ------------------ Rendering ------------------

    def render_header(self, view: ViewDef, gctx: Dict[str, Any], dglob: Dict[str, Any]) -> str:
        template = view.template or ""
        # computed_values
        computed = {}
        for key, spec in (view.computed_values or {}).items():
            if isinstance(spec, dict) and spec.get("builtin") == "uptime":
                computed[key] = get_uptime()
            elif isinstance(spec, dict) and spec.get("from_metric"):
                src = spec["from_metric"]
                op = spec.get("op", "count")
                if op == "count":
                    computed[key] = len(self.rows) if src == "guest_info" else len(self.by_name_only.get(src, []))
                else:
                    computed[key] = None

        # Prepare a lookup for ${name|format:dec} tokens
        def replace_token(token: str) -> str:
            # token could be 'host_cpu|percent:1' or 'vm_count'
            name = token
            fmt = None
            dec = None
            if "|" in token:
                name, rest = token.split("|", 1)
                if ":" in rest:
                    fmt, dec_s = rest.split(":", 1)
                    try:
                        dec = int(dec_s)
                    except Exception:
                        dec = None
                else:
                    fmt = rest

            # lookup value in (computed > derived > globals > metrics)
            if name in computed:
                val = computed[name]
                if isinstance(val, (int, float)) and fmt:
                    return fmt_value(float(val), fmt, dec or 1)
                return str(val)
            if name in dglob:
                return fmt_value(dglob[name], fmt or "number", dec or 1)
            # global metrics direct
            gval = gctx.get(name)
            if gval is not None:
                return fmt_value(gval, fmt or "number", dec or 1)
            return self.cfg.get("globals", {}).get("defaults", {}).get("missing_value", "---")

        out = []
        buf = ""
        i = 0
        s = template
        while i < len(s):
            if s[i:i+2] == "${":
                # find closing }
                j = s.find("}", i+2)
                if j == -1:
                    buf += s[i:]
                    break
                token = s[i+2:j]
                buf += replace_token(token.strip())
                i = j + 1
            else:
                buf += s[i]
                i += 1
        try:
            # interpret \x1b, \t, \n, etc. from the YAML literal
            buf = buf.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
        out.append(buf)
        return "".join(out)

    def render_table(self, view: ViewDef, gctx: Dict[str, Any], drows: Dict[str, Dict[str, Any]]) -> str:
        # determine row set
        anchor_id = view.source.rows_from.get("anchor_metric")
        join_label = view.source.rows_from.get("join_on_label", "id")
        # Build rows list as (rid, ctx, labels)
        rows_list = []
        rctxs = self.rows_ctx()
        for rid, base in rctxs.items():
            # filter by presence of anchor metric labels (already ensured when rows built from exposed labels)
            rows_list.append((rid, base, self.rows[rid]["labels"]))

        # compute values per row for sorting/filtering
        def get_value(rid: str, base: Dict[str, Any], labelmap: Dict[str, str], expr: str) -> Optional[float]:
            # if it's ${name} style, resolve from ctx maps
            name = expr.strip()
            # allow derived id directly
            if name in drows.get(rid, {}):
                return drows[rid][name]
            # or metric / numeric in base
            val = base.get(name)
            if isinstance(val, (int, float)):
                return float(val)
            # not a numeric value
            return None

        # sorting
        sort = view.source.sort or {}
        sort_by = sort.get("by")
        sort_order_desc = (sort.get("order", "asc").lower() == "desc")
        if sort_by:
            rows_list.sort(key=lambda tup: (get_value(tup[0], tup[1], view.source.preferred_labels, sort_by) is None,
                                            get_value(tup[0], tup[1], view.source.preferred_labels, sort_by) or -math.inf),
                           reverse=sort_order_desc)
        
        
        # header
        header_cells = []
        for col in view.columns:
            title = col.title or col.id
            width = col.width
            if width:
                header_cells.append(pad_ansi(title, width))
            else:
                header_cells.append(title)
        header = "\t".join(header_cells)

        lines = [header]
        # render rows
        for rid, base, labels in rows_list:
            cells = []
            
            for col in view.columns:
                raw = col.value
                # support ${name} tokens in column values
                if raw.startswith("${") and raw.endswith("}"):
                    key = raw[2:-1].strip()
                    # prefer derived row values, then row ctx, then label

                    if key in drows.get(rid, {}):
                        val = drows[rid][key]
                        cell = fmt_value(val, col.format, col.decimals)
                    elif key in base:
                        v = base[key]
                        if isinstance(v, (int, float)):
                            cell = fmt_value(v, col.format, col.decimals) 
                        else:
                            cell = v
                    else:
                        v = labels.get(key, "")
                         
                        # labels are strings; style may colorize them, leave raw
                        cell = str(v) if v else self.cfg.get("globals", {}).get("defaults", {}).get("missing_value", "---")
                else:
                    cell = str(raw)
                
                # optional ANSI color by label mapping
                style = col.style or {}
                clr_by = (style.get("color_by_label") or {}).get("type")
                reset = style.get("reset", "")
                if clr_by:
                    tval = labels.get("type", "")
                    prefix = clr_by.get(tval)
                    if prefix:
                        cell = f"{prefix}{cell}{reset}"

                # optional width padding
                if col.width:
                    # cell = f"{cell.strip():<{col.width}}"
                    cell = pad_ansi(cell.strip(), col.width)
                    
                cells.append(cell)

            lines.append("\t".join(cells))

        return "\n".join(lines)

# ------------------------------- Runner ----------------------------------

def run_dashboard(cfg_path: str, tty_path: str):
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
        cfg = apply_color_macros(cfg)

    engine = DashboardEngine(cfg)
    
    clear_tty(tty_path)

    # Initial draw target
    tty = open_tty(tty_path)

    last_bulk = 0.0
    full_redraw = True
    cached_body = ""

    while True:
        now = time.time()
        if (now - last_bulk) >= engine.refresh_bulk:
            engine.bulk_fetch()
            gctx = engine.global_ctx()
            rctxs = engine.rows_ctx()
            dglob, drows = engine.compute_derived(gctx, rctxs)

            # build body (for all views except first header)
            body_parts = []
            for vid in engine.layout:
                view = next(v for v in engine.views if v.id == vid)
                if view.type == "table":
                    body_parts.append(engine.render_table(view, gctx, drows))
            cached_body = "\n".join(body_parts)
            full_redraw = True
            last_bulk = now
        else:
            gctx = engine.global_ctx()
            rctxs = engine.rows_ctx()
            dglob, drows = engine.compute_derived(gctx, rctxs)

        # render header (assume first view in layout is header)
        header_view_id = engine.layout[0]
        header_view = next(v for v in engine.views if v.id == header_view_id)
        hostline = engine.render_header(header_view, gctx, dglob)

        if full_redraw:
            draw_full_screen(tty, hostline, cached_body)
            full_redraw = False
        else:
            draw_host_only(tty, hostline)

        time.sleep(engine.refresh_fast)

# Entry point for manual testing
if __name__ == "__main__":
    cfg_path = os.environ.get("PVE_DASH_CFG", "config.yml")
    tty_path = os.environ.get("TTY_DEV", "/dev/tty")
    try:
        run_dashboard(cfg_path, tty_path)
    except KeyboardInterrupt:
        pass
