# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo.tests import tagged

from .common import ApsApiCase


@tagged('post_install', '-at_install', 'aps_connector')
class TestSupplyExport(ApsApiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.part = cls.make_storable('Supply Part')
        cls.vendor = cls.env['res.partner'].create({'name': 'Test Vendor'})

    def records_for(self, endpoint, product):
        result = self.call(endpoint)
        return [r for r in result['records'] if r['productExternalId'] == str(product.id)]

    def test_on_hand_stock_is_dated_as_already_available(self):
        self.add_stock(self.part, 25)
        record = self.records_for('inventory', self.part)[0]
        self.assertEqual(record['availableDate'], '1970-01-01T00:00:00Z',
                         'stock on hand must not be dated at the moment of the sync')
        self.assertEqual(record['quantityAvailable'], 25.0)

    def test_stock_outside_the_warehouse_is_not_supply(self):
        scrap_location = self.env['stock.location'].search([('usage', '=', 'inventory')], limit=1)
        production_location = self.env['stock.location'].search([('usage', '=', 'production')], limit=1)
        for location in (scrap_location, production_location):
            quant = self.env['stock.quant'].create({
                'product_id': self.part.id,
                'location_id': location.id,
                'inventory_quantity': 40,
            })
            quant.action_apply_inventory()

        locations = {r['locationExternalId'] for r in self.records_for('inventory', self.part)}
        self.assertNotIn(str(scrap_location.id), locations)
        self.assertNotIn(str(production_location.id), locations)

    def test_reservations_beyond_stock_do_not_produce_negative_supply(self):
        quant = self.add_stock(self.part, 5)
        quant.reserved_quantity = 9        # an adjustment left more reserved than present
        record = self.records_for('inventory', self.part)[0]
        self.assertEqual(record['quantityAvailable'], 0.0)

    def test_purchase_line_reports_what_is_still_coming(self):
        receipt = datetime.now() + timedelta(days=14)
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.part.id,
                'product_qty': 30,
                'price_unit': 5,
                'name': self.part.name,
                'date_planned': receipt,
                self.po_line_uom_field(): self.part.uom_id.id,
            })],
        })
        po.button_confirm()

        record = self.records_for('purchase_orders', self.part)[0]
        self.assertEqual(record['quantityAvailable'], 30.0)
        self.assertEqual(record['referenceNumber'], po.name)
        self.assertTrue(record['availableDateKnown'])
        self.assertTrue(record['availableDate'].startswith(receipt.strftime('%Y-%m-%d')))

    def test_purchase_line_without_a_planned_date_falls_back_to_the_order(self):
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.part.id,
                'product_qty': 7,
                'price_unit': 5,
                'name': self.part.name,
                self.po_line_uom_field(): self.part.uom_id.id,
            })],
        })
        po.button_confirm()
        try:
            with self.cr.savepoint():
                po.order_line.write({'date_planned': False})
        except Exception:
            # Odoo 17 has a check constraint that keeps a confirmed line dated,
            # so the case this guards against cannot arise there
            self.skipTest('this release requires a planned date on confirmed lines')

        record = next(r for r in self.records_for('purchase_orders', self.part)
                      if r['referenceNumber'] == po.name)
        self.assertIsNotNone(record['availableDate'],
                             'an undated line used to reach APS as 1970, i.e. already here')
