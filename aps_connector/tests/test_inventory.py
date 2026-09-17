# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from types import SimpleNamespace

from odoo.tests import tagged

from ..controllers.aps_api import open_receipt
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

    # A purchase line is supply for as long as a receipt is open for it. A customer's order
    # was received in full and 14 panels went back to the vendor; ordered minus received
    # said 14 were still coming, on every sync, for a year.

    def confirmed_po(self, product, qty, **line_vals):
        vals = {
            'product_id': product.id,
            'product_qty': qty,
            'price_unit': 5,
            'name': product.name,
            'date_planned': datetime.now() + timedelta(days=14),
            self.po_line_uom_field(): product.uom_id.id,
        }
        vals.update(line_vals)
        po = self.env['purchase.order'].create({'partner_id': self.vendor.id, 'order_line': [(0, 0, vals)]})
        po.button_confirm()
        return po

    def receive(self, po, qty, backorder):
        picking = po.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        move = picking.move_ids
        move.quantity = qty
        move.picked = True
        ctx = {'skip_backorder': True}
        if not backorder:
            ctx['picking_ids_not_to_backorder'] = picking.ids
        picking.with_context(**ctx).button_validate()
        self.assertEqual(picking.state, 'done')

    def po_records(self, po):
        result = self.call('purchase_orders')
        return [r for r in result['records'] if r['referenceNumber'] == po.name], result

    def test_partial_receipt_with_a_backorder_exports_the_remainder(self):
        po = self.confirmed_po(self.part, 30)
        self.receive(po, 10, backorder=True)
        records, _ = self.po_records(po)
        self.assertEqual([r['quantityAvailable'] for r in records], [20.0])
        backorder_move = po.order_line.move_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
        self.assertTrue(records[0]['availableDate'].startswith(backorder_move.date.strftime('%Y-%m-%d')))

    def test_a_receipt_closed_short_is_not_supply(self):
        po = self.confirmed_po(self.part, 30)
        self.receive(po, 16, backorder=False)
        self.assertLess(po.order_line.qty_received, po.order_line.product_qty,
                        'the line itself still looks open, which is what used to be exported')
        records, _ = self.po_records(po)
        self.assertEqual(records, [])

    def test_a_service_or_a_consumable_on_a_purchase_order_is_not_supply(self):
        service = self.env['product.product'].create({'name': 'Freight', 'type': 'service'})
        consumable = self.env['product.product'].create({'name': 'Shop Rags', 'type': 'consu'})
        for product in (service, consumable):
            po = self.confirmed_po(product, 5)
            records, result = self.po_records(po)
            self.assertEqual(records, [], '%s is a cost, not material to wait for' % product.name)
            self.assertNotIn(str(product.id), [p['externalId'] for p in result['products']])

    def test_quantity_is_in_the_products_unit(self):
        dozen = self.env.ref('uom.product_uom_dozen', raise_if_not_found=False)
        if not dozen:
            self.skipTest('no dozen in this database')
        po = self.confirmed_po(self.part, 2, **{self.po_line_uom_field(): dozen.id})
        records, _ = self.po_records(po)
        self.assertEqual([r['quantityAvailable'] for r in records], [24.0])

    def test_a_move_that_does_not_come_into_stock_is_not_supply(self):
        po = self.confirmed_po(self.part, 8)
        customers = self.env['stock.location'].search([('usage', '=', 'customer')], limit=1)
        po.order_line.move_ids.write({'location_dest_id': customers.id})
        records, _ = self.po_records(po)
        self.assertEqual(records, [], 'shipped from the vendor straight to a customer')

    def open_move(self, po):
        return po.order_line.move_ids.filtered(lambda m: m.state not in ('done', 'cancel'))

    def send_back(self, po, qty, done):
        received = po.order_line.move_ids.filtered(lambda m: m.state == 'done')[:1]
        Move = self.env['stock.move']
        move = Move.create({
            # the description of a move is gone in 19
            **({'name': 'return'} if 'name' in Move._fields else {}),
            'product_id': self.part.id,
            'product_uom_qty': qty,
            'product_uom': self.part.uom_id.id,
            'location_id': self.stock_location.id,
            'location_dest_id': self.vendor.property_stock_supplier.id,
            'purchase_line_id': po.order_line.id,
            'origin_returned_move_id': received.id,
            'to_refund': True,
        })
        move._action_confirm()
        if done:
            move.quantity = qty
            move.picked = True
            move._action_done()
        return move

    def test_received_in_full_and_partly_sent_back_is_not_supply(self):
        po = self.confirmed_po(self.part, 40)
        self.receive(po, 40, backorder=False)
        self.send_back(po, 14, done=True)
        self.assertEqual(po.order_line.qty_received, 26,
                         'the line looks 14 short, which is what used to be exported')
        records, _ = self.po_records(po)
        self.assertEqual(records, [])

    def test_a_receipt_that_does_not_leave_from_a_vendor_location_is_still_supply(self):
        # a subcontractor's location is internal, another company of the group ships from transit
        internal = self.env['stock.location'].create(
            {'name': 'Subcontractor', 'usage': 'internal', 'location_id': self.stock_location.location_id.id})
        transit = self.env['stock.location'].create({'name': 'Between companies', 'usage': 'transit'})
        for source in (internal, transit):
            po = self.confirmed_po(self.part, 10)
            self.open_move(po).write({'location_id': source.id})
            records, _ = self.po_records(po)
            self.assertEqual([r['quantityAvailable'] for r in records], [10.0], source.name)

    def test_a_line_lowered_after_a_partial_receipt_counts_what_will_really_come(self):
        po = self.confirmed_po(self.part, 30)
        self.receive(po, 10, backorder=True)
        self.send_back(po, 5, done=False)
        records, _ = self.po_records(po)
        self.assertEqual([r['quantityAvailable'] for r in records], [15.0],
                         'a backorder of 20 with 5 on their way back')

    def test_without_receipt_moves_the_line_itself_decides(self):
        po = self.confirmed_po(self.part, 9)
        po.order_line.move_ids.write({'purchase_line_id': False})
        self.assertIsNone(open_receipt(po.order_line))
        records, _ = self.po_records(po)
        self.assertEqual([r['quantityAvailable'] for r in records], [9.0])
        # a database without purchase_stock has no moves on the line at all
        self.assertIsNone(open_receipt(SimpleNamespace(_fields={})))
