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

## Every release runs against every version, on real data

The module ships for three Odoo releases and their data models differ. A column that
exists in 19 and not in 18 does not fail loudly: the endpoint returns `Internal error`,
the sync logs a warning and carries on, and the feature is simply missing for a year.
Two of these shipped at once - `maintenance_request.scheduled_date` (the field is
`schedule_date`) and `schedule_end` (19 only) - so maintenance never blocked a work centre
on 17 or 18 at all.

Never reason about a field from one version's database. Check the column in each version
before writing SQL:

```
docker exec odoo18-db-1 psql -U odoo -d aps18 -t -A -c \
  "select column_name from information_schema.columns where table_name='mrp_workorder'"
```

and guard what differs at runtime, the way `stock_move.quantity` and
`mrp_workorder.sequence` already are:

```python
cr.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s", (t, c))
expr = 'the.column' if cr.fetchone() else 'a fallback'
```

Then prove it. `endpoint-check.py` beside this file calls every read endpoint and fails on
the first one that errors:

```
python3 .claude/skills/release/endpoint-check.py http://127.0.0.1:8080 <api-key>
```

Run it against 19, 18 and 17 - and against a customer database when you have one restored.
A release is not finished until all of them report zero broken endpoints. It catches what
unit tests cannot, because the fault is in the shape of somebody else's database.

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

The mirror can also be stuck on Odoo's side - in August 2026 they confirmed a fault in the
store's code import, with all three repos registered, Active and scanning cleanly. Registration
is therefore not proof the store is current; only the version comparison above is. When the
store lags, hand the customer the repository directly (`git clone -b <their series>`), which is
what the README on each branch already tells them to do.

## What to tell a customer

Their own instance is the authority. `Manufacturing -> Configuration -> APS Connector` shows
**Connector version**, computed from the installed module. The module is flagged as an
application, so it also appears in Apps without clearing the default filter.

Upgrading on their side: pull the branch, restart Odoo, `Apps -> Update Apps List`, then
Upgrade the module. A sync does not do it and neither does a restart on its own.
