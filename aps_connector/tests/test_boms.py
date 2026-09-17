# -*- coding: utf-8 -*-
from odoo.tests import tagged

from .common import ApsApiCase


@tagged('post_install', '-at_install', 'aps_connector')
class TestBomExport(ApsApiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        colour = cls.env['product.attribute'].create({'name': 'Colour', 'create_variant': 'always'})
        black, white = cls.env['product.attribute.value'].create([
            {'name': 'Black', 'attribute_id': colour.id},
            {'name': 'White', 'attribute_id': colour.id},
        ])
        cls.sheet_black = cls.make_storable('Sheet Black')
        cls.sheet_white = cls.make_storable('Sheet White')
        cls.screws = cls.make_storable('Screws')

        cls.panel = cls.make_template('Panel', **{
            'attribute_line_ids': [(0, 0, {
                'attribute_id': colour.id,
                'value_ids': [(6, 0, [black.id, white.id])],
            })],
        })
        ptav = {v.product_attribute_value_id.name: v.id
                for v in cls.panel.attribute_line_ids.product_template_value_ids}
        cls.bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.panel.id,       # template level: applies to both variants
            'product_qty': 1,
            'bom_line_ids': [
                (0, 0, {'product_id': cls.sheet_black.id, 'product_qty': 1,
                        'bom_product_template_attribute_value_ids': [(6, 0, [ptav['Black']])]}),
                (0, 0, {'product_id': cls.sheet_white.id, 'product_qty': 1,
                        'bom_product_template_attribute_value_ids': [(6, 0, [ptav['White']])]}),
                (0, 0, {'product_id': cls.screws.id, 'product_qty': 8}),
            ],
            'operation_ids': [
                (0, 0, {'name': 'Cut', 'workcenter_id': cls.workcenter.id,
                        'time_cycle_manual': 60, 'sequence': 10}),
                (0, 0, {'name': 'Paint Black', 'workcenter_id': cls.workcenter2.id,
                        'time_cycle_manual': 30, 'sequence': 20,
                        'bom_product_template_attribute_value_ids': [(6, 0, [ptav['Black']])]}),
            ],
        })
        cls.variant_black = cls.panel.product_variant_ids.filtered(
            lambda p: 'Black' in p.display_name)
        cls.variant_white = cls.panel.product_variant_ids.filtered(
            lambda p: 'White' in p.display_name)

    def components_of(self, result, variant):
        return {line['componentProductExternalId']
                for line in result['bomLines']
                if line['parentProductExternalId'] == str(variant.id)}

    def test_each_variant_gets_only_its_own_components(self):
        result = self.call('boms', limit=5000)

        black = self.components_of(result, self.variant_black)
        white = self.components_of(result, self.variant_white)

        self.assertEqual(black, {str(self.sheet_black.id), str(self.screws.id)})
        self.assertEqual(white, {str(self.sheet_white.id), str(self.screws.id)})
        self.assertNotIn(str(self.sheet_white.id), black,
                         "the black panel must not carry the white sheet")

    def test_variant_restricted_operation_is_not_exported_for_the_other_variant(self):
        result = self.call('boms', limit=5000)
        names = {
            str(self.variant_black.id): set(),
            str(self.variant_white.id): set(),
        }
        for op in result['operations']:
            if op['productExternalId'] in names:
                names[op['productExternalId']].add(op['operationName'])

        self.assertEqual(names[str(self.variant_black.id)], {'Cut', 'Paint Black'})
        self.assertEqual(names[str(self.variant_white.id)], {'Cut'})

    def test_multi_variant_external_ids_stay_unique(self):
        result = self.call('boms', limit=5000)
        ours = [l['externalId'] for l in result['bomLines']
                if l['parentProductExternalId'] in (str(self.variant_black.id),
                                                    str(self.variant_white.id))]
        self.assertEqual(len(ours), len(set(ours)), 'external ids collided across variants')

    def test_single_variant_bom_keeps_plain_line_ids(self):
        plain = self.make_template('Plain')
        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': plain.id,
            'product_qty': 1,
            'bom_line_ids': [(0, 0, {'product_id': self.screws.id, 'product_qty': 2})],
        })
        result = self.call('boms', limit=5000)
        line = next(l for l in result['bomLines']
                    if l['parentProductExternalId'] == str(plain.product_variant_id.id))
        # upgrading the connector must not churn external ids for the common case
        self.assertEqual(line['externalId'], str(bom.bom_line_ids.id))

    def test_bom_lines_carry_their_unit_of_measure(self):
        result = self.call('boms', limit=5000)
        line = next(l for l in result['bomLines']
                    if l['parentProductExternalId'] == str(self.variant_white.id)
                    and l['componentProductExternalId'] == str(self.sheet_white.id))
        self.assertEqual(line['unitOfMeasure'], self.sheet_white.uom_id.name)

    # Only the BOMs a plan can reach. A customer with 14,048 BOMs and open orders for a few
    # hundred products was sent the first 5000 by id, 250,000 lines, none of them chosen.

    def make_bom(self, product, components):
        return self.env['mrp.bom'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'product_qty': 1,
            'bom_line_ids': [(0, 0, {'product_id': c.id, 'product_qty': 1}) for c in components],
        })

    def test_asked_for_products_bring_their_boms_and_the_boms_below_them(self):
        frame = self.make_storable('Frame')            # made, inside the cabinet
        rail = self.make_storable('Rail')              # made, inside the frame
        steel = self.make_storable('Steel')
        cabinet = self.make_storable('Cabinet')
        unrelated = self.make_storable('Unrelated')
        self.make_bom(cabinet, [frame, self.screws])
        self.make_bom(frame, [rail])
        self.make_bom(rail, [steel])
        self.make_bom(unrelated, [self.screws])

        result = self.call('boms', productExternalIds=[str(cabinet.id)])
        self.assertTrue(result['scoped'])
        self.assertEqual(result['total'], 3)
        parents = {l['parentProductExternalId'] for l in result['bomLines']}
        self.assertEqual(parents, {str(cabinet.id), str(frame.id), str(rail.id)})
        self.assertNotIn(str(unrelated.id), {p['externalId'] for p in result['products']})

    def test_a_template_bom_is_sent_for_the_variant_asked_for_under_the_same_ids(self):
        everything = self.call('boms', limit=5000)
        self.assertFalse(everything['scoped'])
        black_ids = {l['externalId'] for l in everything['bomLines']
                     if l['parentProductExternalId'] == str(self.variant_black.id)}

        result = self.call('boms', productExternalIds=[str(self.variant_black.id)])
        self.assertEqual({l['parentProductExternalId'] for l in result['bomLines']}, {str(self.variant_black.id)})
        self.assertEqual({l['externalId'] for l in result['bomLines']}, black_ids,
                         'the rows APS already holds must be the rows it is sent again')
        self.assertEqual(self.components_of(result, self.variant_black),
                         {str(self.sheet_black.id), str(self.screws.id)})
        self.assertEqual({o['operationName'] for o in result['operations']}, {'Cut', 'Paint Black'})

    def test_nothing_asked_for_is_nothing_sent_and_a_loop_in_the_boms_ends(self):
        self.assertEqual(self.call('boms', productExternalIds=[])['total'], 0)
        a = self.make_storable('Loop A')
        b = self.make_storable('Loop B')
        self.make_bom(a, [b])
        # Odoo refuses a BOM that contains its own product through the form; written past
        # the check, the walk still has to end
        bom_b = self.make_bom(b, [self.screws])
        self.env.cr.execute('UPDATE mrp_bom_line SET product_id = %s WHERE bom_id = %s', (a.id, bom_b.id))
        self.env.invalidate_all()
        result = self.call('boms', productExternalIds=[str(a.id)])
        self.assertEqual(result['total'], 2)

    def test_the_scoped_answer_is_paged_like_the_other(self):
        made = [self.make_storable('Paged %s' % i) for i in range(3)]
        for product in made:
            self.make_bom(product, [self.screws])
        ids = [str(p.id) for p in made]
        first = self.call('boms', productExternalIds=ids, limit=2, offset=0)
        second = self.call('boms', productExternalIds=ids, limit=2, offset=2)
        self.assertEqual((first['total'], second['total']), (3, 3))
        parents = [l['parentProductExternalId'] for l in first['bomLines'] + second['bomLines']]
        self.assertEqual(sorted(parents), sorted(ids))

    def test_an_archived_variant_asked_for_gets_ids_of_its_own(self):
        self.variant_white.active = False
        try:
            result = self.call('boms', productExternalIds=[str(self.variant_black.id), str(self.variant_white.id)])
            ids = [l['externalId'] for l in result['bomLines']]
            self.assertEqual(len(ids), len(set(ids)), 'two variants under one line id share one row in APS')
            self.assertEqual({l['parentProductExternalId'] for l in result['bomLines']},
                             {str(self.variant_black.id), str(self.variant_white.id)})
        finally:
            self.variant_white.active = True

