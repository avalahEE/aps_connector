# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo.tests import tagged

from .common import ApsApiCase


@tagged('post_install', '-at_install', 'aps_connector')
class TestOrderDeadline(ApsApiCase):
    """An MO made for a sale order is due when the order ships: the scheduled date of
    its delivery, or the order's expected date before there is one. A kit with a one-day
    lead time on an order shipping with a 150-day structure is not late in August."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env['res.partner'].create({'name': 'Pergola Co'})
        cls.structure = cls.make_template('Structure', sale_delay=150)
        cls.kit = cls.make_template('Maintenance Kit', sale_delay=1)
        cls.post = cls.make_template('Post')
        cls.boms = {}
        for template in (cls.structure, cls.kit, cls.post):
            cls.boms[template.id] = cls.env['mrp.bom'].create({
                'product_tmpl_id': template.id, 'product_qty': 1,
                'operation_ids': [(0, 0, {'name': 'Make', 'workcenter_id': cls.workcenter.id,
                                          'time_cycle_manual': 60, 'sequence': 10})],
            })

    def setUp(self):
        super().setUp()
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'picking_policy': 'one',
            'order_line': [(0, 0, {'product_id': t.product_variant_id.id, 'product_uom_qty': 1})
                           for t in (self.structure, self.kit)],
        })

    def make_mo(self, template, origin):
        mo = self.env['mrp.production'].create({
            'product_id': template.product_variant_id.id, 'bom_id': self.boms[template.id].id,
            'product_qty': 1, 'origin': origin, 'date_start': datetime.now() + timedelta(days=1),
        })
        mo.action_confirm()
        return mo

    def due(self, *mos, **params):
        records = {r['externalId']: r for r in self.call('orders', limit=5000, **params)['records']}
        return [records.get(str(mo.id), {}).get('dueDate') for mo in mos]

    def iso(self, dt):
        return dt.isoformat() + 'Z'

    def delivery(self):
        return self.so.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing')

    def test_every_mo_of_the_order_is_due_when_the_delivery_ships(self):
        self.so.action_confirm()
        structure_mo = self.make_mo(self.structure, self.so.name)
        kit_mo = self.make_mo(self.kit, self.so.name)
        post_mo = self.make_mo(self.post, structure_mo.name)
        ship = self.iso(self.delivery().scheduled_date)
        self.assertEqual(self.due(structure_mo, kit_mo, post_mo), [ship, ship, ship])

    def test_moving_the_delivery_moves_the_deadline(self):
        self.so.action_confirm()
        kit_mo = self.make_mo(self.kit, self.so.name)
        later = datetime.now().replace(microsecond=0) + timedelta(days=200)
        self.delivery().scheduled_date = later
        self.assertEqual(self.due(kit_mo), [self.iso(later)])

    def test_before_there_is_a_delivery_the_expected_date_counts(self):
        kit_mo = self.make_mo(self.kit, self.so.name)
        self.assertFalse(self.so.picking_ids)
        # a quotation's expected date counts from now, so allow the seconds between the two
        due = datetime.fromisoformat(self.due(kit_mo)[0].rstrip('Z'))
        self.assertLess(abs(due - self.so.expected_date), timedelta(minutes=1))

    def test_an_order_shipped_in_full_leaves_the_mo_its_own_deadline(self):
        self.so.action_confirm()
        kit_mo = self.make_mo(self.kit, self.so.name)
        kit_mo.date_deadline = datetime.now().replace(microsecond=0) + timedelta(days=9)
        self.delivery().write({'state': 'done'})
        self.assertEqual(self.due(kit_mo), [self.iso(kit_mo.date_deadline)])

    def test_an_incremental_sync_brings_the_mo_of_a_rescheduled_delivery(self):
        self.so.action_confirm()
        kit_mo = self.make_mo(self.kit, self.so.name)
        later = datetime.now().replace(microsecond=0) + timedelta(days=200)
        self.delivery().scheduled_date = later
        self.env.flush_all()
        # the last sync ran after everything above; only the delivery changes after it
        since = datetime.now() + timedelta(hours=1)
        self.assertEqual(self.due(kit_mo, since=since.strftime('%Y-%m-%d %H:%M:%S')), [None])
        self.env.cr.execute("UPDATE stock_picking SET write_date = %s WHERE id = ANY(%s)",
                            (since + timedelta(minutes=1), self.delivery().ids))
        self.assertEqual(self.due(kit_mo, since=since.strftime('%Y-%m-%d %H:%M:%S')), [self.iso(later)])

    def test_an_mo_without_a_sale_order_keeps_its_own_deadline(self):
        mo = self.make_mo(self.kit, False)
        mo.date_deadline = datetime.now().replace(microsecond=0) + timedelta(days=9)
        self.assertEqual(self.due(mo), [self.iso(mo.date_deadline)])

    def test_on_hold_is_sent(self):
        mo = self.make_mo(self.kit, False)
        records = {r['externalId']: r for r in self.call('orders', limit=5000)['records']}
        self.assertFalse(records[str(mo.id)]['onHold'])
        mo.aps_on_hold = True
        records = {r['externalId']: r for r in self.call('orders', limit=5000)['records']}
        self.assertTrue(records[str(mo.id)]['onHold'])
