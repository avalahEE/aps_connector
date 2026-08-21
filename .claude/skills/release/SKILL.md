---
name: release
description: Release the APS Connector module across the 19.0, 18.0 and 17.0 branches - version bump, the release number on the Apps Store description, ports, local upgrade check, and how to verify what the Apps Store is actually serving. Use whenever a connector change is being shipped, a branch is being ported, or someone asks which version a customer should be on.
---

# Releasing the connector

Three branches carry the same module: `19.0`, `18.0`, `17.0`. A release is not done until all
three are pushed and the version is stated in three places that must agree.

## The three places the version lives

1. `aps_connector/__manifest__.py` - `'version'`. This is what Odoo records on upgrade.
2. `aps_connector/static/description/index.html` - the `Latest release:` line near the bottom.
   This is the only place the number is visible on the Apps Store page, so it must be
   updated in the same commit as the manifest. If it disagrees, it lies to the customer.
3. Nothing else. The store page itself shows only the series (`v 19.0`).

Two more things are per-branch and easy to miss when porting a patch: the `git clone -b`
line in `README.md` must name that branch, and anything in the README describing behaviour
has to be true for that release too.


## Steps

Work on 19.0 first, then port. Never edit the three branches in parallel - the ports are a
patch of the 19.0 commit, so a diverged branch is caught immediately.

```
# 1. change + bump on 19.0, description line to the same number, then
git commit && git push origin 19.0
git format-patch -1 HEAD --stdout > /tmp/port.patch

# 2. port (worktrees under the scratchpad, or fresh clones)
git apply --exclude='*__manifest__.py' /tmp/port.patch
# bump that branch's own version, fix its description line, commit, push origin 18.0 / 17.0
```

Version numbers are per series and independent: `19.0.1.19.0` may sit next to `18.0.1.15.0`.
Only the last two components track releases; never renumber to make them line up.

## Verify before telling anyone

Upgrade the module in each local stack and check that disk and database agree - a restart
alone loads new Python but leaves the recorded version behind:

```
docker compose exec -T odoo /entrypoint.sh odoo -d aps19 -u aps_connector --stop-after-init --no-http
```

```python
m = env['ir.module.module'].search([('name', '=', 'aps_connector')])
print(m.installed_version, m.latest_version)   # disk, database - Odoo's labels are swapped
```

`installed_version` is the version **on disk** (label: "Latest Version"), `latest_version` is
the one **in the database** (label: "Installed Version"). Odoo's own source says so.

## What the Apps Store is serving

The store syncs from the registered git repo on its own schedule, so it lags a push by hours.
The page never prints the granular version, but the Odoo.sh deploy link does:

```
for v in 19.0 18.0 17.0; do
  s=$(curl -s "https://apps.odoo.com/apps/modules/$v/aps_connector" | grep -o "odoo-sh/aps_connector/[0-9.]*" | head -1 | sed 's|.*/||')
  g=$(git show "origin/${v}:aps_connector/__manifest__.py" | grep -m1 "'version'" | sed 's/[^0-9.]//g')   # braces matter in zsh
  echo "$v store=$s git=$g"
done
```

Only say "take the latest" once these match. Until then the customer would pull the older
mirror and the version they end up with is not the one that carries the fix.

## What to tell a customer

Their own instance is the authority. `Manufacturing -> Configuration -> APS Connector` shows
**Connector version**, computed from the installed module. The module is flagged as an
application, so it also appears in Apps without clearing the default filter.

Upgrading on their side: pull the branch, restart Odoo, `Apps -> Update Apps List`, then
Upgrade the module. A sync does not do it and neither does a restart on its own.
