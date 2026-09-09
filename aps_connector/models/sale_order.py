# -*- coding: utf-8 -*-
import logging

import pytz
from markupsafe import Markup, escape

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def sale_orders_by_mo(cr, mo_ids):
    """The sale order each MO belongs to, sub-assemblies included.

    Odoo 19 removed procurement.group; stock.reference now links MOs and moves to sale
    orders through relay tables. A child MO reaches the order through the move that
    created it, and an MO with no reference at all through its origin chain. Used by the
    orders feed and by the write-back, which posts the order's planned completion.
    Returns {mo_id: {'id', 'name', 'customer'}}.
    """
    if not mo_ids:
        return {}
    cr.execute("""
        WITH RECURSIVE
        mo_so AS (
            -- Path A: MO → stock_reference (direct) → sale_order
            SELECT mp.id, so.id AS sale_order_id, so.name AS sale_order_name, rp.name AS customer_name
            FROM mrp_production mp
            JOIN stock_reference_production_rel srpr ON srpr.production_id = mp.id
            JOIN stock_reference_sale_rel srsr ON srsr.reference_id = srpr.reference_id
            JOIN sale_order so ON so.id = srsr.sale_id
            LEFT JOIN res_partner rp ON rp.id = so.partner_id
            WHERE mp.id = ANY(%(ids)s)

            UNION

            -- Path B: MO → finished move → move_dest → stock_reference → sale_order
            SELECT mp.id, so.id, so.name, rp.name
            FROM mrp_production mp
            JOIN stock_move sm ON sm.production_id = mp.id AND sm.state != 'cancel'
            JOIN stock_move_move_rel rel ON rel.move_orig_id = sm.id
            JOIN stock_move dest ON dest.id = rel.move_dest_id
            JOIN stock_reference_move_rel srmr ON srmr.move_id = dest.id
            JOIN stock_reference_sale_rel srsr ON srsr.reference_id = srmr.reference_id
            JOIN sale_order so ON so.id = srsr.sale_id
            LEFT JOIN res_partner rp ON rp.id = so.partner_id
            WHERE mp.id = ANY(%(ids)s)

            UNION ALL

            -- Recurse: children via stock_move chain
            SELECT child.id, parent_so.sale_order_id, parent_so.sale_order_name, parent_so.customer_name
            FROM mo_so parent_so
            JOIN stock_move sm ON sm.raw_material_production_id = parent_so.id
                AND sm.created_production_id IS NOT NULL
                AND sm.state != 'cancel'
            JOIN mrp_production child ON child.id = sm.created_production_id
            WHERE child.id = ANY(%(ids)s)
        ),
        -- Fallback: origin chain for MOs not found above
        origin_chain AS (
            SELECT mp.id AS original_id, mp.id AS current_id, mp.origin
            FROM mrp_production mp
            WHERE mp.id = ANY(%(ids)s)
              AND mp.id NOT IN (SELECT id FROM mo_so)
              AND mp.origin IS NOT NULL AND mp.origin != ''

            UNION ALL

            SELECT oc.original_id, parent.id, parent.origin
            FROM origin_chain oc
            JOIN mrp_production parent ON parent.name = oc.origin
            WHERE oc.origin LIKE 'WH/MO/%%'
        ),
        origin_so AS (
            SELECT DISTINCT ON (oc.original_id)
                oc.original_id AS id,
                COALESCE(so_ref.id, so_origin.id) AS sale_order_id,
                COALESCE(so_ref.name, so_origin.name) AS sale_order_name,
                COALESCE(rp_ref.name, rp_origin.name) AS customer_name
            FROM origin_chain oc
            JOIN mrp_production ancestor ON ancestor.id = oc.current_id
            -- Try stock_reference path on ancestor
            LEFT JOIN stock_reference_production_rel srpr
                ON srpr.production_id = ancestor.id
            LEFT JOIN stock_reference_sale_rel srsr
                ON srsr.reference_id = srpr.reference_id
            LEFT JOIN sale_order so_ref ON so_ref.id = srsr.sale_id
            LEFT JOIN res_partner rp_ref ON rp_ref.id = so_ref.partner_id
            -- Try origin = SO name (exact or prefix before '/')
            LEFT JOIN sale_order so_origin
                ON so_origin.name = ancestor.origin
                OR so_origin.name = split_part(ancestor.origin, '/', 1)
            LEFT JOIN res_partner rp_origin ON rp_origin.id = so_origin.partner_id
            WHERE so_ref.id IS NOT NULL OR so_origin.id IS NOT NULL
            ORDER BY oc.original_id
        )
        SELECT id, sale_order_id, sale_order_name, customer_name FROM mo_so
        UNION
        SELECT id, sale_order_id, sale_order_name, customer_name FROM origin_so
    """, {'ids': list(mo_ids)})
    return {
        row['id']: {'id': row['sale_order_id'], 'name': row['sale_order_name'], 'customer': row['customer_name']}
        for row in cr.dictfetchall()
    }


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    aps_planned_finish = fields.Datetime(
        string='Manufacturing complete (APS)',
        copy=False,
        readonly=True,
        help='When APS last planned the last manufacturing order of this sale order to finish',
    )
    aps_late_activity_id = fields.Many2one(
        'mail.activity',
        string='APS late activity',
        copy=False,
        readonly=True,
        ondelete='set null',
        help='The open activity telling the salesperson the plan is later than promised',
    )

    NOTE_MODES = ('NONE', 'MENTION', 'ACTIVITY')
    LATE_SUMMARY = 'Manufacturing planned later than promised'

    @api.model
    def _aps_note_options(self, options):
        """What APS asked for on this publish, with the defaults a connector on its own uses:
        a quiet note when the day changes, nobody told."""
        options = options or {}
        mode = options.get('mode') if options.get('mode') in self.NOTE_MODES else 'NONE'
        unit = 'WEEK' if options.get('unit') == 'WEEK' else 'DAY'
        tz = options.get('timezone') or self.env.company.partner_id.tz or 'UTC'
        try:
            pytz.timezone(tz)
        except pytz.UnknownTimeZoneError:
            _logger.warning('Unknown time zone %r for the sale order note, using UTC', tz)
            tz = 'UTC'
        return {'mode': mode, 'unit': unit, 'tz': tz}

    @api.model
    def aps_note_planned_finish_for(self, mo_ids, options=None):
        """Tell every sale order these MOs belong to when its manufacturing is now planned
        to complete.

        The date is the finish of the last MO of the whole order, sub-assemblies included,
        so it is read over all the order's MOs and not only the ones just written. A move
        inside the same day (or week) is kept but not written about. Returns how many
        orders got a note.
        """
        opts = self._aps_note_options(options)
        Production = self.env['mrp.production']
        cr = self.env.cr
        # the resolver reads the tables directly; the dates just written must be there
        self.env.flush_all()
        touched = {so['id'] for so in sale_orders_by_mo(cr, mo_ids).values() if so['id']}
        if not touched:
            return 0
        companies = Production.browse(mo_ids).mapped('company_id')
        open_mos = Production.search([('state', '!=', 'cancel'), ('company_id', 'in', companies.ids)])
        mos_by_so = {}
        for mo_id, so in sale_orders_by_mo(cr, open_mos.ids).items():
            if so['id'] in touched:
                mos_by_so.setdefault(so['id'], []).append(mo_id)
        noted = 0
        for so_id, ids in mos_by_so.items():
            dates = [mo.date_finished for mo in Production.browse(ids) if mo.date_finished]
            if dates and self.browse(so_id)._aps_note_planned_finish(max(dates), opts):
                noted += 1
        return noted

    def _aps_bucket(self, dt, opts):
        """The day, or the ISO week, an instant falls in on the plant's clock."""
        local = fields.Datetime.context_timestamp(self.with_context(tz=opts['tz']), dt)
        if opts['unit'] == 'WEEK':
            year, week, _ = local.isocalendar()
            return (year, week)
        return local.date()

    def _aps_note_planned_finish(self, planned, opts):
        self.ensure_one()
        planned = planned.replace(second=0, microsecond=0)
        previous = self.aps_planned_finish
        if previous and self._aps_bucket(previous, opts) == self._aps_bucket(planned, opts):
            # moved within the same day or week: remember it, say nothing
            if previous != planned:
                self.write({'aps_planned_finish': planned})
            return False
        self.write({'aps_planned_finish': planned})

        promised = self.commitment_date
        late = bool(promised) and self._aps_bucket(planned, opts) > self._aps_bucket(promised, opts)
        was_late = bool(promised and previous) and self._aps_bucket(previous, opts) > self._aps_bucket(promised, opts)

        tz = self.user_id.tz or opts['tz']
        show = lambda d: fields.Datetime.context_timestamp(self.with_context(tz=tz), d).strftime('%d %b %Y %H:%M')
        text = 'Manufacturing planned to complete %s (%s)' % (show(planned), tz)
        if previous:
            text += ', was %s' % show(previous)
        text += '.'
        if late:
            text += ' Later than the promised delivery of %s.' % show(promised)
        elif was_late:
            text += ' Within the promised delivery again.'
        text += ' Planned by APS.'

        # the activity first, so the note below is the last word in the chatter
        if late and opts['mode'] == 'ACTIVITY' and self.user_id:
            self._aps_late_activity(text)
        elif not late:
            self._aps_close_late_activity()

        seller = self.user_id.partner_id
        mention = opts['mode'] == 'MENTION' and late and bool(seller)
        body = escape(text)
        if mention:
            # the same @mention the chatter composer writes, so it reads as one
            body = Markup('<a href="#" data-oe-model="res.partner" data-oe-id="%d">@%s</a> ') % (seller.id, seller.name) + body
        self.message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
            partner_ids=seller.ids if mention else [],
        )
        return True

    def _aps_open_late_activity(self):
        """The order's APS activity if the salesperson has not closed it. A done activity is
        archived, not deleted, so existence alone says nothing."""
        activity = self.aps_late_activity_id.exists()
        if activity and activity.active and not activity.date_done:
            return activity
        return activity.browse()

    def _aps_late_activity(self, text):
        """One open activity per order: update it while the plan stays late, never a second."""
        self.ensure_one()
        activity = self._aps_open_late_activity()
        if activity:
            activity.write({'note': escape(text), 'user_id': self.user_id.id, 'date_deadline': fields.Date.context_today(self)})
            return activity
        activity = self.activity_schedule(
            'mail.mail_activity_data_todo',
            summary=self.LATE_SUMMARY,
            note=escape(text),
            user_id=self.user_id.id,
            date_deadline=fields.Date.context_today(self),
        )
        self.write({'aps_late_activity_id': activity.id})
        return activity

    def _aps_close_late_activity(self):
        self.ensure_one()
        activity = self._aps_open_late_activity()
        if activity:
            activity.action_feedback(feedback='Back within the promised delivery')
        if self.aps_late_activity_id:
            self.write({'aps_late_activity_id': False})
