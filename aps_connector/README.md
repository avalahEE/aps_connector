# APS Connector for Odoo

Odoo module that provides a JSON-RPC API for bi-directional integration with [APS 4 Manufacturing](https://aps4mfg.com) (Advanced Planning and Scheduling).

## What it does

The APS Connector exposes Odoo manufacturing data to APS 4 Manufacturing for scheduling, and writes optimized schedules back to Odoo.

**Read from Odoo:**
- Manufacturing Orders (with Sale Order linkage, components, parent/child MO hierarchy)
- Operations / Work Orders (with dependencies, resource assignments, date ranges)
- Work Centers (with calendars, capacity, efficiency, alternative resources)
- Calendars (week patterns with shift definitions)
- Calendar Exceptions (leaves, holidays)
- Products (manufactured/purchased items)
- Bills of Materials
- Maintenance requests (blocking maintenance windows)
- Inventory levels and purchase order lines

**Write back to Odoo:**
- Scheduled start/finish dates on work orders
- Conflict detection using `write_date` to prevent overwriting concurrent changes
- Material re-reservation for rescheduled work orders
- Unplanning work orders APS deliberately left out, when the planner asks for it on publish
  (an operation arrives with no dates and the whole MO is unplanned, Odoo's own way; orders
  that have started or finished are left alone)

## Installation

1. Clone the appropriate branch into your Odoo addons directory:
   ```bash
   git clone -b 17.0 git@github.com:avalahEE/aps_connector.git
   ```
2. Restart Odoo and update the apps list
3. Install the **APS Connector** module

## Configuration

1. Go to **Manufacturing > Configuration > APS Settings**
2. Set the API key (stored as SHA-256 hash)

The same page shows the installed connector version, which is what support will ask for.

## Technical Details

- Uses raw SQL for bulk reads to maximize performance
- API key authentication with SHA-256 hashing and constant-time comparison
- Rate limiting (IP and API key prefix based) to protect against brute force
- Supports incremental sync via `write_date` filtering
- All responses use camelCase field names matching APS TypeScript entities
- Sale Order linkage follows the `sale_mrp` chain (`procurement_group` -> `stock_move` -> `created_production_id`), with recursive resolution for sub-MOs

## Dependencies

`base`, `mrp`, `sale_mrp`, `stock`, `purchase`, `resource`

## License

LGPL-3
