# -*- coding: utf-8 -*-
import json

from odoo.tests import HttpCase

API_KEY = 'test-api-key-aps-connector'


class ApsApiCase(HttpCase):
    """Calls the endpoints over HTTP the way APS does, API key and all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env['aps.sync.config'].create({'name': 'Test'})
        cls.config.set_api_key(API_KEY)
        cls.warehouse = cls.env['stock.warehouse'].search([], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.workcenter = cls.env['mrp.workcenter'].create({
            'name': 'Test WC', 'code': 'TEST-WC', 'time_efficiency': 100,
        })
        cls.workcenter2 = cls.env['mrp.workcenter'].create({
            'name': 'Test WC 2', 'code': 'TEST-WC2', 'time_efficiency': 100,
        })

    def call(self, endpoint, **params):
        params.setdefault('api_key', API_KEY)
        response = self.url_open(
            '/aps/api/v1/%s' % endpoint,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()
        self.assertNotIn('error', body, 'endpoint %s failed: %s' % (endpoint, body.get('error')))
        result = body['result']
        self.assertNotIn('error', result, 'endpoint %s returned %s' % (endpoint, result.get('error')))
        return result

    @classmethod
    def storable_vals(cls):
        """Odoo 18 introduced is_storable; before that a storable product is a type."""
        if 'is_storable' in cls.env['product.template']._fields:
            return {'is_storable': True}
        return {'type': 'product'}

    @classmethod
    def po_line_uom_field(cls):
        """purchase.order.line.product_uom was renamed to product_uom_id in Odoo 19."""
        fields = cls.env['purchase.order.line']._fields
        return 'product_uom_id' if 'product_uom_id' in fields else 'product_uom'

    @classmethod
    def make_storable(cls, name, **vals):
        vals.update(cls.storable_vals())
        return cls.env['product.product'].create(dict(vals, name=name))

    @classmethod
    def make_template(cls, name, **vals):
        vals.update(cls.storable_vals())
        return cls.env['product.template'].create(dict(vals, name=name))

    @classmethod
    def add_stock(cls, product, qty, lot=None):
        vals = {
            'product_id': product.id,
            'location_id': cls.stock_location.id,
            'inventory_quantity': qty,
        }
        if lot:
            vals['lot_id'] = lot.id
        quant = cls.env['stock.quant'].create(vals)
        quant.action_apply_inventory()
        return quant
