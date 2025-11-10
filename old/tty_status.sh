#!/usr/bin/env bash
set -euo pipefail

PROM="http://192.168.1.24:9090"
NODE="node/r440"
TTY_DEV="${TTY_DEV:-/dev/tty1}"

# ── helpers ──────────────────────────────────────────────────────────────

get_uptime() {
  read -r seconds _ < /proc/uptime || seconds=0
  seconds=${seconds%.*}
  local d=$((seconds/86400))
  local h=$((seconds%86400/3600))
  local m=$((seconds%3600/60))
  local s=$((seconds%60))
  printf "up "
  ((d>0)) && printf "%dd " "$d"
  ((h>0)) && printf "%dh " "$h"
  ((m>0)) && printf "%dm " "$m"
  printf "%ds" "$s"
}

# Draw ONLY the host summary line (top line) — no full clear, no flicker
_draw_host_only() {
  local host="$1"
  {
    printf '\0337'            # save cursor
    printf '\033[H'           # move to row 1, col 1
    printf "%s" "$host"
    printf '\033[K'           # clear to end of line
    printf '\0338'            # restore cursor
  } > "$TTY_DEV"
}

# Full (re)draw when VM table changes — minimal flicker (no global clear)
_draw_full_screen() {
  local host="$1" table="$2"
  {
    printf '\033[H'           # move to top-left
    printf "%s\n" "$host"
    printf "%s\n" "$table"
    printf '\033[J'           # clear anything below
  } > "$TTY_DEV"
}

# ── main loop ─────────────────────────────────────────────────────────────

last_bulk=0
bulk_json=""
cached_table=""

while :; do
  now=$(date +%s)

  # every 5 s: one bulk scrape of all PVE metrics
  if (( now - last_bulk >= 5 )); then
    bulk_json=$(curl -s -G "$PROM/api/v1/query" \
      --data-urlencode "query={__name__=~'pve_.*'}")
    last_bulk=$now

    # rebuild VM table after fresh bulk
    table=$'\nVM/CT         \tCPU%   \tMEM%   \tDiskR  \tDiskW  \tNetIn  \tNetOut\n'

    # list guests (id, name, type)
    while IFS=$'\t' read -r id name type; do
      [[ -z "${id:-}" ]] && continue
      cpu_v=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_cpu_usage_ratio") | .value[1]' \
               <<<"$bulk_json" | awk '{printf "%.1f",$1*100}' 2>/dev/null)
      mem_t=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_memory_size_bytes") | .value[1]' \
               <<<"$bulk_json")
      mem_u=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_memory_usage_bytes") | .value[1]' \
               <<<"$bulk_json")
      mem_v="--"
      if [[ -n "${mem_t:-}" && -n "${mem_u:-}" && "${mem_t}" != "0" ]]; then
        mem_v=$(awk -v u="$mem_u" -v t="$mem_t" 'BEGIN{printf "%.1f",(u/t)*100}')
      fi
      readb=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_disk_read_bytes") | .value[1]' \
               <<<"$bulk_json" | awk '{printf "%.1f",$1/1048576}' 2>/dev/null)
      writeb=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_disk_write_bytes") | .value[1]' \
               <<<"$bulk_json" | awk '{printf "%.1f",$1/1048576}' 2>/dev/null)
      netin=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_net_in_bytes_total") | .value[1]' \
               <<<"$bulk_json" | awk '{printf "%.1f",$1/1048576}' 2>/dev/null)
      netout=$(jq -r --arg id "$id" \
               '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_net_out_bytes_total") | .value[1]' \
               <<<"$bulk_json" | awk '{printf "%.1f",$1/1048576}' 2>/dev/null)

      # choose color by type
      if [[ "${type}" == "lxc" ]]; then
        row=$(printf "\n\033[1;36m%-12s\033[0m \t%5s \t%6s \t%6s\t%6s\t%6s\t%6s\n\n" \
              "$name" "${cpu_v:---}" "${mem_v:---}" "${readb:---}" "${writeb:---}" "${netin:---}" "${netout:---}")
      else
        row=$(printf "\n\033[1;33m%-12s\033[0m \t%5s \t%6s \t%6s\t%6s\t%6s\t%6s\n\n" \
              "$name" "${cpu_v:---}" "${mem_v:---}" "${readb:---}" "${writeb:---}" "${netin:---}" "${netout:---}")
      fi
      table+="$row"
    done < <(jq -r '.data.result[] | select(.metric.__name__=="pve_guest_info") | [.metric.id,.metric.name,.metric.type] | @tsv' <<<"$bulk_json")

    cached_table="$table"
    full_redraw=1
  else
    full_redraw=0
  fi

  # extract host metrics from cached bulk (fast, every 1s)
  cpu=$(jq -r --arg id "$NODE" \
        '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_cpu_usage_ratio") | .value[1]' \
        <<<"$bulk_json" | awk '{printf "%.1f", $1*100}' 2>/dev/null)

  mem_sz=$(jq -r --arg id "$NODE" \
        '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_memory_size_bytes") | .value[1]' \
        <<<"$bulk_json")
  mem_used=$(jq -r --arg id "$NODE" \
        '.data.result[] | select(.metric.id==$id and .metric.__name__=="pve_memory_usage_bytes") | .value[1]' \
        <<<"$bulk_json")
  if [[ -n "${mem_sz:-}" && -n "${mem_used:-}" && "${mem_sz}" != "0" ]]; then
    mem_pct=$(awk -v u="$mem_used" -v t="$mem_sz" 'BEGIN{printf "%.1f", (u/t)*100}')
  else
    mem_pct="--"
  fi

  nvme=$(jq -r '.data.result[] | select(.metric.__name__=="nvme_temperature_celsius") | .value[1]' <<<"$bulk_json" | awk '{printf "%.0f",$1}' 2>/dev/null)
  up=$(get_uptime)

  hostline=$(printf "\033[1;32mCPU\033[0m: %4s%%  \033[1;34mMEM\033[0m: %4s%%  \033[1;33mVMs\033[0m:%2s  \033[1;35mNVMe\033[0m:%3s°C  %s" \
                "${cpu:---}" "${mem_pct:---}" "$(jq -r '.data.result[] | select(.metric.__name__=="pve_guest_info") | 1' <<<"$bulk_json" | wc -l)" "${nvme:---}" "$up")

  if (( full_redraw )); then
    _draw_full_screen "$hostline" "$cached_table" "\n"
  else
    _draw_host_only "$hostline"
  fi

  sleep 0.2

done
