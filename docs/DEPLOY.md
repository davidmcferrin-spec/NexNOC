# Go-live checklist

`setup.sh` gets NexNOC running; this is what to close out before treating
that install as production rather than a demo. None of this is code work —
it's operational steps plus a couple of scripts already in the repo to make
them repeatable. Run `sudo ./setup.sh --check` after each step; it flags
what's still open.

## 1. TLS

Off by default — `setup.sh` installs a plain `:80` vhost
(`config/apache-nexnoc.conf`) with the `:443` block present but commented
out. Apache serves `web/` and proxies only `/api/` to `nexnoc-web`.
The login form (and every session cookie) is plaintext HTTP until this
is done. The app side needs no change: `auth.request_is_secure()` already
reads `X-Forwarded-Proto` and sets the session cookie's `Secure` flag
accordingly — flipping the vhost is the entire fix.

```
apt install certbot python3-certbot-apache
certbot --apache -d <your-server-name>       # obtains cert, edits the vhost
systemctl reload apache2
```

If certs come from elsewhere (internal CA, existing wildcard), uncomment
the `:443` block in `config/apache-nexnoc.conf` by hand, point
`SSLCertificateFile`/`SSLCertificateKeyFile` at the real files, `a2enmod
ssl`, `systemctl reload apache2`. Either way, once `:443` is live, redirect
`:80`→`:443` in the vhost (add `Redirect permanent / https://...` or an
`RewriteRule`) so a stray plaintext request can't leak a cookie.

## 2. Firewall

`setup.sh` does not touch firewall rules — nothing stops it from being
reachable on every interface today. `nexnoc-web` itself only binds
`127.0.0.1:8080` (not exposed regardless), but Apache (`:80`/`:443`) and
`nexnoc-trapd` (`0.0.0.0:162/udp`) both listen on all interfaces.

Minimum with `ufw`:

```
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from <device-management-subnet> to any port 162 proto udp
ufw enable
```

Scope the trap rule to the actual subnet(s) your Appear/Haivision/Nimbra
devices live on — UDP 162 open to the world accepts (and logs, per
`trap_log`) forged traps from anywhere. If devices span multiple subnets,
repeat the `ufw allow from` line per subnet rather than opening it broadly.

## 3. Credentials

Device usernames, passwords, and SNMP secrets are stored on the device
row (Inventory, or `api_username` / `api_password` in `config.json` on
first import). `/etc/nexnoc/nexnoc.env` is no longer the device-secret
store. Before relying on this install:

- Confirm each polled device has a username/password (or SNMP community)
  on its Inventory row. Missing values show up as failed API/SNMP polls
  in `journalctl -u nexnoc-poller`.
- Change the seeded `admin`/`password` and `user`/`password` logins on
  first sign-in — `must_change_password` forces this, but confirm it
  actually happened for every seeded account (Admin → Users) rather than
  assuming.
- Turn on LDAP (Admin → LDAP) if AD group-based access is the real plan;
  local accounts are meant as a bootstrap/break-glass path, not the
  long-term login story for a whole NOC team.

## 4. Database backup

No automated backup exists out of the box. `noc.db` holds inventory, user
accounts (password hashes, not plaintext), and poll/trap history — losing
it isn't catastrophic (config.json + nexnoc.env can re-bootstrap
inventory) but does lose audit/poll history and any inventory edits made
only through the UI.

`scripts/nexnoc-backup-db` does an online `sqlite3 .backup` (safe under
WAL — doesn't block the poller or web process), gzips it, and prunes
anything older than `NEXNOC_BACKUP_KEEP` days (default 14):

```
sudo -u nexnoc /opt/nexnoc/scripts/nexnoc-backup-db
```

Add it to cron (as the `nexnoc` user, or root — the script doesn't care):

```
0 3 * * * sudo -u nexnoc /opt/nexnoc/scripts/nexnoc-backup-db >>/var/log/nexnoc/backup.log 2>&1
```

Copy `/var/lib/nexnoc/backups/*.db.gz` off-host on whatever schedule your
normal backup story uses — the script only handles the local rotation.

## 5. Already handled — no action needed

Confirmed in place, listed here so this checklist doesn't re-raise them:

- **systemd hardening**: all three units run as the unprivileged `nexnoc`
  user with `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`.
  `nexnoc-poller` and `nexnoc-trapd` also set `NoNewPrivileges`.
  `nexnoc-web` leaves it off so Admin → Services can `sudo` the
  allowlisted `nexnoc-svc` helper. `nexnoc-trapd` gets only
  `CAP_NET_BIND_SERVICE`, nothing broader, to bind UDP 162.
- **Log rotation**: `poller.py`/`trapd.py` use a stdlib `RotatingFileHandler`
  (5MB × 3 backups) when `--log-file` is set (both units set it).
  `nexnoc-web` logs to stdout → journald, which rotates itself.
  `audit.jsonl` self-rotates at 10MB (`audit.py`). Apache's own logs are
  covered by the distro's existing `/etc/logrotate.d/apache2`.
- **Credential storage**: device secrets live on the device row. The HTTP
  API never returns passwords. Never log credential values.
- **Cookie security**: `HttpOnly` + `SameSite=Lax` always; `Secure` turns
  on automatically once step 1 (TLS) is done — no separate flag to flip.
