# Phase 1 Security

## Controls implemented

| Control | Implementation |
|---|---|
| Users / roles | `users`, `roles`, `user_roles` |
| Roles | admin, operator, reviewer, viewer, auditor |
| Password hashing | PBKDF2-SHA256, 390k iterations, random salt |
| Sessions | HttpOnly cookie `pis_session` |
| CSRF | Double-submit cookie `pis_csrf` + header `X-CSRF-Token` on mutating cookie requests |
| Write auth | All POST job/review/user routes use `require_write(...)` |
| Export | Requires login + `export:read` |
| Audit | `operation_audits` for login, user create, job actions, review, export |
| Health | Returns `database: configured` (no absolute path) |
| Admin bootstrap | `scripts/bootstrap_admin.py` + env `POLICY_ADMIN_PASSWORD` (no default password) |

## Permission matrix

| Capability | admin | operator | reviewer | viewer | auditor |
|---|---|---|---|---|---|
| read | Y | Y | Y | Y | Y |
| jobs:write | Y | Y | N | N | N |
| review:write | Y | N | Y | N | N |
| export:read | Y | Y | Y | Y | Y |
| users:admin | Y | N | N | N | N |
| audit:read | Y | N | N | N | Y |

## Deploy notes (ops)

### Nginx HTTPS sample

See `docs/phase1/02_security_nginx.conf.example` content below.

```nginx
server {
    listen 80;
    server_name policy-inspect.example;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name policy-inspect.example;
    ssl_certificate     /etc/ssl/certs/policy-inspect.crt;
    ssl_certificate_key /etc/ssl/private/policy-inspect.key;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
# Close public :6001 or restrict to VPN/jump host only.
```

### Non-root process

```bash
useradd -r -s /usr/sbin/nologin policyapp
chown -R policyapp:policyapp /opt/policy-inspection-system
# PM2/systemd: run as policyapp, not root
```

### Port 6001

- Prefer disable public bind on 6001
- If retained temporarily: firewall allowlist only
- Application remains on `127.0.0.1:8765`

### Cookie secure flag

Set `POLICY_COOKIE_SECURE=1` behind HTTPS.
