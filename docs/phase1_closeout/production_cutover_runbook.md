# Production Cutover Runbook (Phase1 + Closeout)

> **Do not execute blindly.** This document is the authorized procedure.  
> Passwords/keys below are **placeholders only**.

## 1. Preconditions

- Independent acceptance checklist signed
- Maintenance window approved
- Backup storage available
- TLS certificates ready
- Non-root OS user `policyapp` created
- Operators know rollback contacts

## 2. Maintenance window

- Suggested: off-peak 60–90 minutes
- Announce read-only / downtime for scan & review UI

## 3. Git version & rollback anchor

```bash
cd /opt/policy-inspection-system
git rev-parse HEAD > /var/backups/policy-inspect/HEAD_BEFORE_CUTOVER.txt
git status --short
# Deploy the accepted tree (tag or commit hash agreed in acceptance)
# ROLLBACK_ANCHOR=<previous_known_good_commit>
```

## 4. Production database backup

```bash
ts=$(date +%Y%m%d%H%M%S)
cp -a data/policy_inspector.db /var/backups/policy-inspect/policy_inspector.db.$ts
sha256sum data/policy_inspector.db | tee /var/backups/policy-inspect/db.$ts.sha256
# Optional: stop writers first if hot copy is unacceptable
```

## 5. Copy migration rehearsal (mandatory)

```bash
cp -a data/policy_inspector.db /tmp/policy_inspector.rehearsal.db
python scripts/run_closeout_migration_copy.py \
  --source /tmp/policy_inspector.rehearsal.db \
  --target /tmp/policy_inspector.rehearsal.migrated.db \
  --report /tmp/closeout_rehearsal_report.json
# Verify counts in report; fix issues BEFORE touching production DB
```

## 6. Admin initialization

```bash
export APP_ENV=production
export POLICY_ADMIN_USERNAME=admin
export POLICY_ADMIN_PASSWORD='<SET_STRONG_PASSWORD_HERE>'
# Never commit or log this value
python scripts/bootstrap_admin.py --db data/policy_inspector.db --username "$POLICY_ADMIN_USERNAME"
unset POLICY_ADMIN_PASSWORD
```

## 7. APP_ENV=production

```bash
export APP_ENV=production
export POLICY_COOKIE_SECURE=1
# Ensure process manager injects the same env
```

## 8. Forbid POLICY_AUTH_DISABLED

```bash
# Must be unset or 0
unset POLICY_AUTH_DISABLED
# Startup must fail if set to 1 under production
```

## 9. Nginx HTTPS

- Terminate TLS on 443
- `proxy_pass http://127.0.0.1:8765`
- HSTS enabled
- See `docs/phase1/02_security.md` sample

## 10. Close or restrict port 6001

```bash
# Example firewall — adjust to site policy
ufw deny 6001/tcp
# or bind nginx only on internal interface for legacy port
```

## 11. Non-root service user

```bash
# systemd example User=policyapp
chown -R policyapp:policyapp /opt/policy-inspection-system
```

## 12. Production migration command

```bash
# Only after rehearsal OK and backup verified
python scripts/run_phase1_migration.py --db data/policy_inspector.db --report /var/backups/policy-inspect/phase1_prod.json --force-backfill
python - <<'PY'
from app.db import Database
from app.migrations.closeout import run_closeout_migration
from app.knowledge.organizations import seed_organizations
db=Database('data/policy_inspector.db')
with db.connect() as conn:
    print(run_closeout_migration(conn, force=True))
    print(seed_organizations(conn))
PY
```

## 13. Service restart commands

```bash
# Example — adapt to PM2/systemd
systemctl restart policy-inspection
# or: pm2 restart policy-inspection
systemctl status policy-inspection
# Confirm log line: [startup] app_env=production auth_enabled=True
```

## 14. Login & permission smoke

- Login as admin / reviewer / viewer
- viewer cannot POST jobs
- reviewer can review, cannot POST /api/users
- CSRF required on writes

## 15. Job / review / Excel smoke

- Open one completed job
- Confirm UI shows **客户正式问题** vs 线索合计
- Export Excel; 问题汇总 excludes AGENCY-001
- Evidence shows review_history after a test review on non-prod sample if available

## 16. Post-migration DB stats

```bash
sqlite3 data/policy_inspector.db "SELECT COUNT(*) FROM rule_findings; SELECT COUNT(*) FROM finding_reviews; SELECT COUNT(*) FROM review_decisions;"
sha256sum data/policy_inspector.db
```

## 17. Log checks

- No stack traces on startup
- No password/token in logs
- Auth failures return 401/403 only

## 18. Rollback conditions

- Auth cannot login for >15 min
- Migration corruption / missing tables
- Formal counts wildly inconsistent vs pre-check
- Critical scan path broken

## 19. Code rollback

```bash
git checkout <ROLLBACK_ANCHOR>
# reinstall deps if needed
systemctl restart policy-inspection
```

## 20. Database rollback

```bash
systemctl stop policy-inspection
cp -a /var/backups/policy-inspect/policy_inspector.db.$ts data/policy_inspector.db
sha256sum -c /var/backups/policy-inspect/db.$ts.sha256
systemctl start policy-inspection
```

## 21. 24-hour observation

- [ ] Login success rate
- [ ] Job create/pause/resume
- [ ] Review append growth in `finding_reviews`
- [ ] Disk growth of DB + logs
- [ ] No POLICY_AUTH_DISABLED in process env
- [ ] Nginx 443 only public
- [ ] formal_finding_count stable vs business expectation
