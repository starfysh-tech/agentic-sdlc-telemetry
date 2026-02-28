#!/usr/bin/env bash
# manage.sh — agentic-sdlc-telemetry installer and manager
set -euo pipefail

INSTALL_DIR="$HOME/.claude/usage-data/sdlc-analytics"
SCRIPT_NAME="sdlc_extract.py"
CONFIG_FILE="$INSTALL_DIR/config.json"
DB_FILE="$INSTALL_DIR/sdlc_analytics.db"
REMOTE_URL="https://raw.githubusercontent.com/starfysh-tech/agentic-sdlc-telemetry/main/$SCRIPT_NAME"
PROJECTS_BASE="$HOME/.claude/projects"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

# ── Helpers ────────────────────────────────────────────────

is_installed() { [ -f "$INSTALL_DIR/$SCRIPT_NAME" ]; }

# Strip -Users-{user}-Code- prefix for readable display
display_name() {
  local username; username=$(whoami)
  echo "$1" | sed "s/^-Users-${username}-Code-//; s/^-Users-${username}-//"
}

# Identify base project dirs (those not prefixed by another dir name)
get_project_groups() {
  shopt -s nullglob
  local all_dirs=()
  for d in "$PROJECTS_BASE"/*/; do
    all_dirs+=("$(basename "${d%/}")")
  done
  shopt -u nullglob

  for name in "${all_dirs[@]}"; do
    local is_variant=false
    for other in "${all_dirs[@]}"; do
      [[ "$name" != "$other" && "$name" == "${other}-"* ]] && { is_variant=true; break; }
    done
    $is_variant && continue

    local count=0
    for other in "${all_dirs[@]}"; do
      [[ "$other" == "${name}-"* ]] && ((count++)) || true
    done
    echo "${name}|${count}|$(display_name "$name")"
  done
}

# Expand base names to actual project dirs (base + plugin variants)
resolve_dirs() {
  shopt -s nullglob
  for base in "$@"; do
    for d in "$PROJECTS_BASE"/*/; do
      local dname; dname=$(basename "${d%/}")
      [[ "$dname" == "$base" || "$dname" == "${base}-"* ]] && echo "${d%/}"
    done
  done
  shopt -u nullglob
}

read_config() {
  [ -f "$CONFIG_FILE" ] || return 0
  python3 -c "
import json
try:
    [print(b) for b in json.load(open('$CONFIG_FILE')).get('include_bases', [])]
except: pass
"
}

write_config() {
  mkdir -p "$INSTALL_DIR"
  local json='{\n  "include_bases": ['
  local sep=""
  for base in "$@"; do
    json+="${sep}\n    \"${base}\""
    sep=","
  done
  json+='\n  ]\n}'
  printf "$json\n" > "$CONFIG_FILE"
}

# ── Status ─────────────────────────────────────────────────

show_status() {
  echo ""
  printf "${BOLD}agentic-sdlc-telemetry${NC}\n"
  printf -- "─────────────────────────────────────────\n"

  if is_installed; then
    printf "Script:   ${GREEN}✓ installed${NC}\n"
  else
    printf "Script:   ${RED}✗ not installed${NC}\n"
  fi

  if [ -f "$DB_FILE" ] && command -v sqlite3 &>/dev/null; then
    local main sub git_ops prs last_run
    main=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM sessions WHERE is_subagent=0;" 2>/dev/null || echo "?")
    sub=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM sessions WHERE is_subagent=1;" 2>/dev/null || echo "?")
    git_ops=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM git_operations;" 2>/dev/null || echo "?")
    prs=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM pr_links;" 2>/dev/null || echo "?")
    last_run=$(sqlite3 "$DB_FILE" "SELECT value FROM extraction_meta WHERE key='last_run';" 2>/dev/null || echo "never")
    printf "Sessions: ${BOLD}%s${NC} main  ${BOLD}%s${NC} subagent\n" "$main" "$sub"
    printf "Git ops:  ${BOLD}%s${NC}  |  PRs: ${BOLD}%s${NC}\n" "$git_ops" "$prs"
    printf "Last run: %s\n" "$last_run"
  elif [ -f "$DB_FILE" ]; then
    printf "Database: ${GREEN}✓ exists${NC} (install sqlite3 for stats)\n"
  else
    printf "Database: ${YELLOW}not yet created${NC}\n"
  fi

  local include_count
  include_count=$(read_config | wc -l | tr -d ' ')
  if [ -f "$CONFIG_FILE" ] && [ "$include_count" -gt 0 ]; then
    printf "Projects: ${GREEN}✓ %s included${NC}\n" "$include_count"
  else
    printf "Projects: ${YELLOW}not configured${NC}\n"
  fi

  printf -- "─────────────────────────────────────────\n"
  echo ""
}

# ── Project Selection ──────────────────────────────────────

configure_projects() {
  echo ""
  printf "${BOLD}Configure included projects${NC}\n\n"

  local groups=()
  while IFS= read -r line; do
    groups+=("$line")
  done < <(get_project_groups)

  if [ ${#groups[@]} -eq 0 ]; then
    printf "${RED}No projects found in $PROJECTS_BASE${NC}\n"
    return 1
  fi

  local current_bases=()
  while IFS= read -r b; do
    current_bases+=("$b")
  done < <(read_config)

  local i=0
  for group in "${groups[@]}"; do
    IFS='|' read -r base count display <<< "$group"
    local marker="  [ ]"
    for cur in "${current_bases[@]:-}"; do
      [[ "$cur" == "$base" ]] && marker="  [x]" && break
    done
    local suffix=""
    [ "$count" -gt 0 ] && suffix=" ${BLUE}(+${count} plugin dir(s))${NC}"
    printf "%s %d. %s%b\n" "$marker" $((i+1)) "$display" "$suffix"
    ((i++)) || true
  done

  echo ""
  printf "Enter numbers to include (space-separated), 'a' for all: "
  read -r selection

  local new_bases=()
  if [[ "${selection:-}" =~ ^[Aa]$ ]]; then
    for group in "${groups[@]}"; do
      IFS='|' read -r base _ _ <<< "$group"
      new_bases+=("$base")
    done
  else
    for num in $selection; do
      [[ "$num" =~ ^[0-9]+$ ]] || continue
      local idx=$((num - 1))
      [ "$idx" -ge 0 ] && [ "$idx" -lt ${#groups[@]} ] || continue
      IFS='|' read -r base _ _ <<< "${groups[$idx]}"
      new_bases+=("$base")
    done
  fi

  if [ ${#new_bases[@]} -eq 0 ]; then
    printf "${YELLOW}No selection made. Configuration unchanged.${NC}\n"
    return 1
  fi

  write_config "${new_bases[@]}"
  printf "${GREEN}✓ Saved: %d project(s) included${NC}\n" ${#new_bases[@]}
  echo ""
}

# ── Install ────────────────────────────────────────────────

do_install() {
  echo ""
  printf "${BOLD}Installing agentic-sdlc-telemetry...${NC}\n\n"

  if ! command -v python3 &>/dev/null; then
    printf "${RED}ERROR: python3 not found${NC}\n"; exit 1
  fi
  local py_ver py_ok
  py_ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  py_ok=$(python3 -c "import sys; print('ok' if sys.version_info >= (3,9) else 'fail')")
  [ "$py_ok" = "ok" ] || { printf "${RED}ERROR: Python 3.9+ required (found %s)${NC}\n" "$py_ver"; exit 1; }
  printf "  Python:  ${GREEN}✓${NC} %s\n" "$py_ver"

  local source
  source="$(cd "$(dirname "$0")" && pwd)/$SCRIPT_NAME"
  [ -f "$source" ] || { printf "${RED}ERROR: %s not found alongside manage.sh${NC}\n" "$SCRIPT_NAME"; exit 1; }

  mkdir -p "$INSTALL_DIR"
  cp "$source" "$INSTALL_DIR/$SCRIPT_NAME"
  chmod +x "$INSTALL_DIR/$SCRIPT_NAME"
  printf "  Script:  ${GREEN}✓${NC} installed\n\n"

  configure_projects

  printf "${GREEN}✓ Installation complete${NC}\n"
  printf "  Run: ./manage.sh run\n\n"
}

# ── Update ─────────────────────────────────────────────────

do_update() {
  is_installed || { printf "${RED}Not installed. Run: ./manage.sh install${NC}\n"; exit 1; }
  echo ""
  printf "${BOLD}Updating from GitHub...${NC}\n"

  local tmp; tmp=$(mktemp)
  trap 'rm -f "$tmp"' RETURN

  curl -fsSL "$REMOTE_URL" -o "$tmp" || { printf "${RED}Download failed${NC}\n"; exit 1; }
  head -1 "$tmp" | grep -q "python" || { printf "${RED}Downloaded content is not a Python script${NC}\n"; exit 1; }

  mv "$tmp" "$INSTALL_DIR/$SCRIPT_NAME"
  chmod +x "$INSTALL_DIR/$SCRIPT_NAME"
  printf "${GREEN}✓ Updated${NC}\n\n"
}

# ── Uninstall ───────────────────────────────────────────────

do_uninstall() {
  echo ""
  printf "${BOLD}Uninstalling agentic-sdlc-telemetry${NC}\n\n"

  for f in "$INSTALL_DIR/$SCRIPT_NAME" "$CONFIG_FILE"; do
    [ -f "$f" ] && rm "$f" && printf "  ${GREEN}✓${NC} Removed %s\n" "$(basename "$f")"
  done

  if [ -f "$DB_FILE" ]; then
    echo ""
    printf "  Remove database (%s)? [y/N] " "$DB_FILE"
    read -r response
    if [[ "${response:-N}" =~ ^[Yy]$ ]]; then
      rm -f "$DB_FILE" "$DB_FILE-shm" "$DB_FILE-wal"
      printf "  ${GREEN}✓${NC} Removed database\n"
    else
      printf "  Database preserved\n"
    fi
  fi

  [ -d "$INSTALL_DIR" ] && [ -z "$(ls -A "$INSTALL_DIR")" ] && rmdir "$INSTALL_DIR" && \
    printf "  ${GREEN}✓${NC} Removed empty install dir\n"

  printf "\n${GREEN}Done.${NC}\n\n"
}

# ── Run ────────────────────────────────────────────────────

do_run() {
  is_installed || { printf "${RED}Not installed. Run: ./manage.sh install${NC}\n"; exit 1; }

  local bases=()
  while IFS= read -r b; do
    bases+=("$b")
  done < <(read_config)

  [ ${#bases[@]} -gt 0 ] || { printf "${YELLOW}No projects configured. Run: ./manage.sh config${NC}\n"; exit 1; }

  local dirs=()
  while IFS= read -r d; do
    dirs+=("$d")
  done < <(resolve_dirs "${bases[@]}")

  [ ${#dirs[@]} -gt 0 ] || { printf "${RED}No directories found for configured projects${NC}\n"; exit 1; }

  python3 "$INSTALL_DIR/$SCRIPT_NAME" -v --project-dirs "${dirs[@]}" "$@"
}

# ── Main Menu ──────────────────────────────────────────────

show_menu() {
  while true; do
    show_status
    if is_installed; then
      printf "  1) Run extraction\n"
      printf "  2) Configure projects\n"
      printf "  3) Update script\n"
      printf "  4) Uninstall\n"
    else
      printf "  1) Install\n"
    fi
    printf "  q) Quit\n\n"
    printf "Choice: "
    read -r choice
    echo ""

    if is_installed; then
      case "$choice" in
        1) do_run ;;
        2) configure_projects ;;
        3) do_update ;;
        4) do_uninstall; break ;;
        q|Q) break ;;
        *) printf "${YELLOW}Invalid choice${NC}\n\n" ;;
      esac
    else
      case "$choice" in
        1) do_install ;;
        q|Q) break ;;
        *) printf "${YELLOW}Invalid choice${NC}\n\n" ;;
      esac
    fi
  done
}

# ── Dispatch ───────────────────────────────────────────────

case "${1:-}" in
  install)   do_install ;;
  update)    do_update ;;
  uninstall) do_uninstall ;;
  config)    configure_projects ;;
  status)    show_status ;;
  run)       shift; do_run "$@" ;;
  "")        show_menu ;;
  *)
    printf "Usage: ./manage.sh [install|update|uninstall|config|status|run]\n"
    exit 1 ;;
esac
