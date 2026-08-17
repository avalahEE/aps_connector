# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo.tests import tagged

from .common import ApsApiCase


@tagged('post_install', '-at_install', 'aps_connector')
class TestWorkOrderProgress(ApsApiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.widget = cls.make_template('Widget')
        cls.bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.widget.id,
            'product_qty': 1,
            'operation_ids': [
                (0, 0, {'name': 'Mill', 'workcenter_id': cls.workcenter.id,
                        'time_cycle_manual': 100, 'sequence': 10}),
            ],
        })

    def make_mo(self, qty=5):
        mo = self.env['mrp.production'].create({
            'product_id': self.widget.product_variant_id.id,
            'bom_id': self.bom.id,
            'product_qty': qty,
            'date_start': datetime.now() + timedelta(days=1),
        })
        mo.action_confirm()
        mo.button_plan()
        return mo

    def wo_record(self, workorder):
        result = self.call('operations', limit=5000,
                           statuses=['PENDING', 'SCHEDULED', 'IN_PROGRESS', 'COMPLETE'])
        return next(r for r in result['records'] if r['externalId'] == str(workorder.id))

    def test_untouched_work_order_is_planned_at_full_duration(self):
        mo = self.make_mo()
        wo = mo.workorder_ids[0]
        record = self.wo_record(wo)
        self.assertAlmostEqual(record['processTime'], wo.duration_expected, places=2)
        self.assertFalse(record['locked'])

    def test_time_already_logged_reduces_what_is_planned(self):
        mo = self.make_mo()
        wo = mo.workorder_ids[0]
        expected = wo.duration_expected
        wo.button_start()
        wo.duration = expected - 10          # ten minutes of work left

        record = self.wo_record(wo)
        self.assertEqual(record['status'], 'IN_PROGRESS')
        self.assertAlmostEqual(record['processTime'], 10, places=2)
        self.assertAlmostEqual(record['actualDuration'], expected - 10, places=2)
        self.assertFalse(record['locked'], 'work in progress must stay schedulable')

    def test_quantity_produced_reduces_what_is_planned(self):
        mo = self.make_mo(qty=5)
        wo = mo.workorder_ids[0]
        wo.button_start()
        wo.duration = 10                      # barely any time logged...
        wo.qty_produced = 4                   # ...but four of five are done

        record = self.wo_record(wo)
        # one unit of five left, so a fifth of the planned duration — Odoo
        # recomputes duration_expected as the order progresses, so read it now
        self.assertAlmostEqual(record['processTime'], wo.duration_expected / 5, places=2)

    def test_remaining_duration_never_reaches_zero(self):
        mo = self.make_mo()
        wo = mo.workorder_ids[0]
        wo.button_start()
        wo.duration = wo.duration_expected * 3    # long overrun

        record = self.wo_record(wo)
        self.assertGreaterEqual(record['processTime'], 1.0)

    def test_finished_work_order_is_locked(self):
        mo = self.make_mo(qty=1)
        wo = mo.workorder_ids[0]
        wo.button_start()
        wo.qty_producing = 1
        wo.button_finish()

        record = self.wo_record(wo)
        self.assertEqual(record['status'], 'COMPLETE')
        self.assertTrue(record['locked'], 'finished work is only there to hold its slot')
