# -*- coding: utf-8 -*-
from odoo import models, fields


class MrpWorkorder(models.Model):
    """Extend Work Order for APS integration.

    Supports Odoo 17, 18, 19 - date_start/date_finished are writable in all versions.
    """
    _inherit = 'mrp.workorder'

    # Track if schedule came from APS
    aps_scheduled = fields.Boolean(
        string='Scheduled by APS',
        default=False,
        copy=False,
        help='Indicates this work order was scheduled by the APS system',
    )

    # Last sync timestamp
    aps_last_sync = fields.Datetime(
        string='Last APS Sync',
        copy=False,
    )

    def write_aps_schedule(self, scheduled_start, scheduled_end, workcenter_id=None):
        """
        Write schedule from APS system.

        Writes both date_start and date_finished directly.
        Also updates the associated calendar leave if it exists.

        Works on Odoo 17, 18, 19.

        Args:
            scheduled_start: datetime for operation start
            scheduled_end: datetime for operation end
            workcenter_id: optional workcenter ID for resource reassignment
        """
        self.ensure_one()

        if not scheduled_start or not scheduled_end:
            raise ValueError("Both scheduled_start and scheduled_end are required")

        vals = {
            'aps_scheduled': True,
            'aps_last_sync': fields.Datetime.now(),
            'date_start': scheduled_start,
            'date_finished': scheduled_end,
        }

        if workcenter_id:
            vals['workcenter_id'] = workcenter_id

        # Write to work order
        result = self.write(vals)

        # Also update the leave directly to ensure dates are preserved
        if self.leave_id:
            self.leave_id.write({
                'date_from': scheduled_start,
                'date_to': scheduled_end,
            })

        return result

    def get_scheduled_dates(self):
        """
        Get scheduled dates.

        Returns:
            dict with 'start' and 'end' datetime values
        """
        self.ensure_one()
        return {
            'start': self.date_start,
            'end': self.date_finished,
        }

    def clear_aps_schedule(self):
        """Clear APS scheduling flag"""
        return self.write({
            'aps_scheduled': False,
        })


class MrpProduction(models.Model):
    """Extend Manufacturing Order for APS integration"""
    _inherit = 'mrp.production'

    # Track APS sync status
    aps_synced = fields.Boolean(
        string='Synced to APS',
        default=False,
        copy=False,
    )
    aps_last_sync = fields.Datetime(
        string='Last APS Sync',
        copy=False,
    )

    def mark_aps_synced(self):
        """Mark as synced to APS"""
        return self.write({
            'aps_synced': True,
            'aps_last_sync': fields.Datetime.now(),
        })

    def update_dates_from_workorders(self):
        """
        Update MO date_start and date_finished based on work order dates.

        This mimics what Odoo's _plan_workorders() does:
        - date_start = min of all WO date_start
        - date_finished = max of all WO date_finished

        Should be called after APS publishes work order schedules.
        """
        self.ensure_one()
        workorders = self.workorder_ids.filtered(lambda wo: wo.date_start and wo.date_finished)

        if not workorders:
            return False

        date_start = min(wo.date_start for wo in workorders)
        date_finished = max(wo.date_finished for wo in workorders)

        self.with_context(force_date=True).write({
            'date_start': date_start,
            'date_finished': date_finished,
        })
        return True
