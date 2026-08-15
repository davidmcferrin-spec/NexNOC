#!/usr/bin/env bash
###############################################################################
# setup.sh — NexNOC installer for Debian / Ubuntu LTS
#
# Idempotent. Run from the git checkout as root:
#
#   sudo ./setup.sh              full install (apt, units, Apache, bootstrap)
#   sudo ./setup.sh update       rsync code + restart running units
#   sudo ./setup.sh --check      sanity checks only
#   sudo ./setup.sh status       systemctl snapshot
#
# Layout:
#   /opt/nexnoc              code
#   /etc/nexnoc/config.json  inventory (sites/devices/trunks/signals)
#   /etc/nexnoc/nexnoc.env   credentials (0600/0640, never in git)
#   /var/lib/nexnoc/noc.db   SQLite
#
# Python is stdlib only — no pip. SNMP uses snmpget; LDAP uses ldapsearch.
# Database is SQLite (see schema.sql). MySQL is not wired up.
#
# Env overrides: NEXNOC_PREFIX NEXNOC_DATA NEXNOC_ETC NEXNOC_LOG
#                NEXNOC_WEB_PORT NEXNOC_SERVER_NAME
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${NEXNOC_PREFIX:-/opt/nexnoc}"
DATA="${NEXNOC_DATA:-/var/lib/nexnoc}"
ETC="${NEXNOC_ETC:-/etc/nexnoc}"
LOG="${NEXNOC_LOG:-/var/log/nexnoc}"
WEB_PORT="${NEXNOC_WEB_PORT:-8080}"
SERVER_NAME="${NEXNOC_SERVER_NAME:-}"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
ok()   { echo "${GREEN}[ OK ]${RESET} $*"; }
warn() { echo "${YELLOW}[WARN]${RESET} $*"; WARNINGS+=("$*"); }
fail() { echo "${RED}[FAIL]${RESET} $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }
WARNINGS=()

COMMAND="install"

usage() {
  cat <<EOF
Install and maintain NexNOC on Debian or Ubuntu LTS (Apache + SQLite).

Usage:
  sudo $0                 First-time / re-run install
  sudo $0 update          Refresh code from this checkout, restart units
  sudo $0 --check         Sanity checks only
  sudo $0 status          systemctl snapshot
  $0 --help

Env overrides:
  NEXNOC_PREFIX       code dest          (default /opt/nexnoc)
  NEXNOC_DATA         SQLite dir         (default /var/lib/nexnoc)
  NEXNOC_ETC          config + env       (default /etc/nexnoc)
  NEXNOC_LOG          log dir            (default /var/log/nexnoc)
  NEXNOC_WEB_PORT     loopback HTTP port (default 8080)
  NEXNOC_SERVER_NAME  Apache ServerName  (default: hostname -f)

After install, edit ${ETC}/config.json and ${ETC}/nexnoc.env, then:
  sudo systemctl restart nexnoc-poller
EOF
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "run as root: sudo $0 ${COMMAND}"
  fi
}

detect_os() {
  local id=""
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && echo "${ID}")"
  fi
  case "${id}" in
    debian|ubuntu) ok "OS: ${id} ($(. /etc/os-release && echo "${PRETTY_NAME}"))" ;;
    *)
      warn "expected Debian or Ubuntu LTS; found '${id:-unknown}'. Continuing anyway."
      ;;
  esac
}

default_server_name() {
  if [[ -n "${SERVER_NAME}" ]]; then
    printf '%s' "${SERVER_NAME}"
    return
  fi
  local host
  host="$(hostname -f 2>/dev/null || true)"
  if [[ -z "${host}" || "${host}" == "(none)" ]]; then
    host="$(hostname 2>/dev/null || echo nexnoc.local)"
  fi
  printf '%s' "${host}"
}

install_packages() {
  step "APT packages"
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v apt-get >/dev/null 2>&1; then
    fail "apt-get not found — this installer targets Debian/Ubuntu"
  fi
  apt-get update -qq
  apt-get install -y \
    python3 \
    apache2 \
    snmp \
    ldap-utils \
    sqlite3 \
    rsync \
    curl \
    ca-certificates
  ok "python3 apache2 snmp ldap-utils sqlite3 rsync"
}

ensure_user_and_dirs() {
  step "User and directories"
  if ! id -u nexnoc >/dev/null 2>&1; then
    useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin nexnoc
    ok "created system user nexnoc"
  else
    ok "user nexnoc already exists"
  fi
  mkdir -p "${PREFIX}" "${DATA}" "${ETC}" "${LOG}" "${DATA}/tiles"
  chown root:root "${PREFIX}"
  chown nexnoc:nexnoc "${DATA}" "${LOG}"
  chown root:nexnoc "${ETC}"
  chmod 0755 "${PREFIX}"
  chmod 0750 "${DATA}" "${ETC}" "${LOG}"
  ok "${PREFIX}  ${ETC}  ${DATA}  ${LOG}"
}

sync_code() {
  step "Install code → ${PREFIX}"
  if [[ "${ROOT}" == "${PREFIX}" ]]; then
    ok "running from ${PREFIX} — skip rsync"
    return
  fi
  rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.db' \
    --exclude '*.db-journal' \
    --exclude 'config.json' \
    --exclude '.env' \
    --exclude 'tiles' \
    "${ROOT}/" "${PREFIX}/"
  if [[ -f "${PREFIX}/scripts/nexnoc-traphandle" ]]; then
    chmod 755 "${PREFIX}/scripts/nexnoc-traphandle"
  fi
  ok "rsync ${ROOT} → ${PREFIX}"
}

install_config() {
  step "Config and credentials"
  if [[ ! -f "${ETC}/config.json" ]]; then
    install -m 640 -o root -g nexnoc \
      "${PREFIX}/config.example.json" "${ETC}/config.json"
    ok "wrote ${ETC}/config.json from example — edit before relying on it"
  else
    ok "keeping existing ${ETC}/config.json"
  fi

  if [[ ! -f "${ETC}/nexnoc.env" ]]; then
    install -m 640 -o root -g nexnoc \
      "${PREFIX}/config/nexnoc.env.example" "${ETC}/nexnoc.env"
    warn "wrote ${ETC}/nexnoc.env from example — replace change_me values"
  else
    chmod 640 "${ETC}/nexnoc.env"
    chown root:nexnoc "${ETC}/nexnoc.env"
    ok "keeping existing ${ETC}/nexnoc.env"
  fi

  if [[ -d "${ROOT}/.git" ]]; then
    printf '%s\n' "${ROOT}" > "${DATA}/git-source"
    chown nexnoc:nexnoc "${DATA}/git-source"
    chmod 644 "${DATA}/git-source"
  fi
}

substitute_paths() {
  local src="$1"
  local dest="$2"
  sed \
    -e "s|/opt/nexnoc|${PREFIX}|g" \
    -e "s|/var/lib/nexnoc|${DATA}|g" \
    -e "s|/etc/nexnoc|${ETC}|g" \
    -e "s|/var/log/nexnoc|${LOG}|g" \
    -e "s|--port 8080|--port ${WEB_PORT}|g" \
    "${src}" > "${dest}"
}

install_units() {
  step "systemd units"
  local unit
  for unit in nexnoc-poller.service nexnoc-web.service nexnoc-trapd.service; do
    [[ -f "${PREFIX}/systemd/${unit}" ]] || fail "missing ${PREFIX}/systemd/${unit}"
    substitute_paths "${PREFIX}/systemd/${unit}" "/etc/systemd/system/${unit}"
    chmod 644 "/etc/systemd/system/${unit}"
    ok "installed ${unit}"
  done
  systemctl daemon-reload
}

install_apache() {
  step "Apache reverse proxy"
  local name
  name="$(default_server_name)"
  [[ -f "${PREFIX}/config/apache-nexnoc.conf" ]] || fail "missing Apache template"

  if ! command -v a2enmod >/dev/null 2>&1; then
    fail "a2enmod not found — install apache2"
  fi
  a2enmod proxy proxy_http headers >/dev/null
  ok "enabled proxy proxy_http headers"

  sed \
    -e "s|@@SERVER_NAME@@|${name}|g" \
    -e "s|@@WEB_PORT@@|${WEB_PORT}|g" \
    "${PREFIX}/config/apache-nexnoc.conf" \
    > /etc/apache2/sites-available/nexnoc.conf
  chmod 644 /etc/apache2/sites-available/nexnoc.conf
  a2ensite nexnoc >/dev/null
  ok "site nexnoc (ServerName ${name} → 127.0.0.1:${WEB_PORT})"

  if apache2ctl configtest >/dev/null 2>&1; then
    systemctl enable apache2 >/dev/null 2>&1 || true
    if systemctl is-active --quiet apache2; then
      systemctl reload apache2
      ok "apache2 reloaded"
    else
      systemctl enable --now apache2
      ok "apache2 started"
    fi
  else
    apache2ctl configtest || true
    fail "apache2ctl configtest failed"
  fi
}

bootstrap_db() {
  step "SQLite bootstrap"
  (
    cd "${PREFIX}"
    sudo -u nexnoc /usr/bin/python3 "${PREFIX}/poller.py" \
      --config "${ETC}/config.json" \
      --db "${DATA}/noc.db" \
      --bootstrap-only
  )
  chown nexnoc:nexnoc "${DATA}/noc.db"
  chmod 640 "${DATA}/noc.db"
  ok "${DATA}/noc.db"
}

start_services() {
  step "Enable services"
  systemctl enable --now nexnoc-web nexnoc-poller nexnoc-trapd
  sleep 1
  if systemctl is-active --quiet nexnoc-web; then
    ok "nexnoc-web is active"
  else
    warn "nexnoc-web failed to start — journalctl -u nexnoc-web"
  fi
  if systemctl is-active --quiet nexnoc-poller; then
    ok "nexnoc-poller is active"
  else
    warn "nexnoc-poller failed to start — journalctl -u nexnoc-poller"
  fi
  if systemctl is-active --quiet nexnoc-trapd; then
    ok "nexnoc-trapd is active"
  else
    warn "nexnoc-trapd failed to start — journalctl -u nexnoc-trapd (UDP 162 / CAP_NET_BIND_SERVICE)"
  fi
}

restart_running() {
  step "Restart units"
  systemctl daemon-reload
  local unit
  for unit in nexnoc-web nexnoc-poller nexnoc-trapd; do
    if ! systemctl cat "${unit}" >/dev/null 2>&1; then
      warn "${unit} is not installed — run sudo $0 (full install)"
      continue
    fi
    systemctl enable "${unit}" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "${unit}"; then
      systemctl restart "${unit}"
      ok "restarted ${unit}"
    else
      systemctl start "${unit}" || true
      ok "started ${unit}"
    fi
  done
  if systemctl is-active --quiet apache2; then
    systemctl reload apache2 || true
  fi
}

cmd_check() {
  step "Sanity checks"
  command -v python3 >/dev/null && ok "python3: $(command -v python3)" \
    || fail "python3 missing"
  command -v apache2ctl >/dev/null && ok "apache2ctl present" \
    || warn "apache2ctl missing"
  command -v snmpget >/dev/null && ok "snmpget present" \
    || warn "snmpget missing — apt install snmp (needed for Net Insight)"
  command -v ldapsearch >/dev/null && ok "ldapsearch present" \
    || warn "ldapsearch missing — apt install ldap-utils (needed for LDAP login)"
  command -v sqlite3 >/dev/null && ok "sqlite3 present" \
    || warn "sqlite3 missing"

  [[ -f "${ETC}/config.json" ]] && ok "config ${ETC}/config.json" \
    || warn "missing ${ETC}/config.json"
  [[ -f "${ETC}/nexnoc.env" ]] && ok "env ${ETC}/nexnoc.env" \
    || warn "missing ${ETC}/nexnoc.env"
  [[ -f "${DATA}/noc.db" ]] && ok "db ${DATA}/noc.db" \
    || warn "missing ${DATA}/noc.db — bootstrap has not run"

  if command -v systemctl >/dev/null 2>&1; then
    for unit in nexnoc-web nexnoc-poller nexnoc-trapd apache2; do
      if systemctl is-active --quiet "${unit}" 2>/dev/null; then
        ok "${unit} active"
      else
        warn "${unit} not active"
      fi
    done
  fi

  if command -v apache2ctl >/dev/null 2>&1; then
    if apache2ctl configtest >/dev/null 2>&1; then
      ok "apache2ctl configtest"
    else
      warn "apache2ctl configtest failed"
    fi
    if apache2ctl -M 2>/dev/null | grep -q proxy_http_module; then
      ok "mod_proxy_http loaded"
    else
      warn "mod_proxy_http not loaded — a2enmod proxy proxy_http"
    fi
  fi

  if curl -fsS --max-time 3 "http://127.0.0.1:${WEB_PORT}/api/state" >/dev/null 2>&1; then
    ok "loopback /api/state on :${WEB_PORT}"
  else
    warn "loopback /api/state not reachable — start nexnoc-web"
  fi

  if (( ${#WARNINGS[@]} > 0 )); then
    echo
    echo "Warnings:"
    local w
    for w in "${WARNINGS[@]}"; do
      echo "  - ${w}"
    done
    return 1
  fi
  echo
  ok "all checks passed"
}

cmd_status() {
  systemctl status --no-pager --lines=8 nexnoc-web nexnoc-poller nexnoc-trapd apache2 || true
}

print_summary() {
  local name
  name="$(default_server_name)"
  cat <<EOF

Setup complete.

  Code:    ${PREFIX}
  Config:  ${ETC}/config.json
  Secrets: ${ETC}/nexnoc.env
  DB:      ${DATA}/noc.db
  Tiles:   ${DATA}/tiles     (empty until you run scripts/fetch_tiles.py)
  Apache:  http://${name}/   (proxies 127.0.0.1:${WEB_PORT})
  Kiosk:   http://${name}/kiosk

Next:
  1. Edit ${ETC}/config.json with real sites / devices / trunks
  2. Put credential values in ${ETC}/nexnoc.env (mode 0640)
  3. Optional — local map tiles (no CDN): python3 ${PREFIX}/scripts/fetch_tiles.py --out ${DATA}/tiles
     then set map.local_tile_dir to ${DATA}/tiles in config.json and restart nexnoc-web
  4. sudo ${PREFIX}/setup.sh update   # after git pull, or re-run install
  5. sudo systemctl restart nexnoc-poller

Maintenance:
  sudo $0 update
  sudo $0 --check
  sudo $0 status
  journalctl -u nexnoc-poller -u nexnoc-web -u nexnoc-trapd -f

SQLite only — MySQL is not supported. Sign in at / (admin/password or
user/password — change on first login). /kiosk stays anonymous. LDAP
needs ldap-utils and Admin → LDAP.
EOF
}

cmd_install() {
  require_root
  echo "NexNOC setup"
  echo "Source: ${ROOT}"
  detect_os
  install_packages
  ensure_user_and_dirs
  sync_code
  install_config
  install_units
  install_apache
  bootstrap_db
  start_services
  cmd_check || true
  print_summary
}

cmd_update() {
  require_root
  echo "NexNOC update"
  echo "Source: ${ROOT}"
  detect_os
  ensure_user_and_dirs
  sync_code
  install_config
  install_units
  if [[ -d /etc/apache2/sites-available ]]; then
    install_apache
  fi
  if [[ -f "${ETC}/config.json" ]]; then
    bootstrap_db
  fi
  restart_running
  cmd_check || true
  echo
  ok "update complete"
}

while (( $# > 0 )); do
  case "$1" in
    install|update|status)
      COMMAND="$1"
      ;;
    --check|check)
      COMMAND="check"
      ;;
    --update)
      COMMAND="update"
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "${COMMAND}" in
  install) cmd_install ;;
  update)  cmd_update ;;
  check)   cmd_check ;;
  status)  cmd_status ;;
  *)
    echo "Unknown command: ${COMMAND}" >&2
    usage >&2
    exit 2
    ;;
esac
