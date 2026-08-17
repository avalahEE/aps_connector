# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo.tests import tagged

from .common import ApsApiCase


@tagged('post_install', '-at_install', 'aps_connector')
class TestOrderComponents(ApsApiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.part = cls.make_storable('Bracket')
        cls.filler = cls.make_storable('Filler')
        cls.add_stock(cls.part, 100)

        cls.frame = cls.make_template('Frame')
        cls.bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.frame.id,
            'product_qty': 1,
            'operation_ids': [
                (0, 0, {'name': 'Cut', 'workcenter_id': cls.workcenter.id,
                        'time_cycle_manual': 30, 'sequence': 10}),
                (0, 0, {'name': 'Assemble', 'workcenter_id': cls.workcenter2.id,
                        'time_cycle_manual': 20, 'sequence': 20}),
            ],
        })
        cls.op_cut, cls.op_assemble = cls.bom.operation_ids[0], cls.bom.operation_ids[1]
        cls.env['mrp.bom.line'].create({
            'bom_id': cls.bom.id, 'product_id': cls.part.id, 'product_qty': 2,
            'operation_id': cls.op_assemble.id,       # consumed at the LAST step
        })
        cls.env['mrp.bom.line'].create({
            'bom_id': cls.bom.id, 'product_id': cls.filler.id, 'product_qty': 1,
        })

        cls.mo = cls.env['mrp.production'].create({
            'product_id': cls.frame.product_variant_id.id,
            'bom_id': cls.bom.id,
            'product_qty': 5,
            'date_start': datetime.now() + timedelta(days=1),
        })
        cls.mo.action_confirm()
        cls.mo.button_plan()
        cls.mo.action_assign()

    def order_record(self):
        result = self.call('orders', limit=5000)
        return next(r for r in result['records'] if r['externalId'] == str(self.mo.id))

    def test_components_are_absolute_quantities_for_this_order(self):
        components = {c['productExternalId']: c for c in self.order_record()['components']}
        # 2 per unit x 5 units, not the per-unit BOM ratio
        self.assertEqual(components[str(self.part.id)]['quantity'], 10.0)

    def test_component_names_the_work_order_that_consumes_it(self):
        components = {c['productExternalId']: c for c in self.order_record()['components']}
        assemble_wo = self.mo.workorder_ids.filtered(lambda w: w.operation_id == self.op_assemble)

        self.assertEqual(components[str(self.part.id)]['workOrderExternalId'],
                         str(assemble_wo.id))
        self.assertEqual(components[str(self.part.id)]['operationExternalId'],
                         str(self.op_assemble.id))

    def test_component_without_an_operation_reports_none(self):
        components = {c['productExternalId']: c for c in self.order_record()['components']}
        self.assertIsNone(components[str(self.filler.id)]['operationExternalId'])

    def test_reserved_quantity_is_reported(self):
        components = {c['productExternalId']: c for c in self.order_record()['components']}
        part = components[str(self.part.id)]
        self.assertEqual(part['quantityReserved'], 10.0)
        self.assertTrue(part['isReserved'])
        # nothing in stock for the filler
        self.assertEqual(components[str(self.filler.id)]['quantityReserved'], 0.0)

    def test_cancelled_component_move_is_not_exported(self):
        move = self.mo.move_raw_ids.filtered(lambda m: m.product_id == self.filler)
        move._action_cancel()
        components = {c['productExternalId'] for c in self.order_record()['components']}
        self.assertNotIn(str(self.filler.id), components)
