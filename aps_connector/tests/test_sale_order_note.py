# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import tagged

from .common import ApsApiCase


def next_monday_06(weeks_ahead=2):
    today = datetime.utcnow().replace(hour=6, minute=0, second=0, microsecond=0)
    return today + timedelta(days=(7 - today.weekday()) % 7 + 7 * weeks_ahead)


@tagged('post_install', '-at_install', 'aps_connector')
class TestSaleOrderNote(ApsApiCase):
    """A publish tells the sale order when its manufacturing is now planned to complete:
    a quiet note when the day (or week) changes, the salesperson mentioned or given an
    activity only when the plan is later than the promised delivery."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # res.users.groups_id became group_ids in Odoo 19
        groups_field = 'group_ids' if 'group_ids' in cls.env['res.users']._fields else 'groups_id'
        cls.seller = cls.env['res.users'].create({
            'name': 'Sally Seller', 'login': 'sally', 'tz': 'Europe/Berlin',
            groups_field: [(6, 0, [cls.env.ref('sales_team.group_sale_salesman').id])],
        })
        cls.customer = cls.env['res.partner'].create({'name': 'Kitchen Co'})
        cls.cabinet = cls.make_template('Cabinet')
        cls.door = cls.make_template('Door')
        cls.cabinet_bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.cabinet.id, 'product_qty': 1,
            'operation_ids': [(0, 0, {'name': 'Assemble', 'workcenter_id': cls.workcenter.id, 'time_cycle_manual': 60, 'sequence': 10})],
        })
        cls.door_bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.door.id, 'product_qty': 1,
            'operation_ids': [(0, 0, {'name': 'Cut', 'workcenter_id': cls.workcenter2.id, 'time_cycle_manual': 60, 'sequence': 10})],
        })
        cls.monday = next_monday_06()

    def setUp(self):
        super().setUp()
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id, 'user_id': self.seller.id,
            'order_line': [(0, 0, {'product_id': self.cabinet.product_variant_id.id, 'product_uom_qty': 1})],
        })
        # the cabinet MO carries the order's name as its origin, the door MO the cabinet's
        self.cabinet_mo = self.make_mo(self.cabinet, self.cabinet_bom, self.so.name)
        self.door_mo = self.make_mo(self.door, self.door_bom, self.cabinet_mo.name)

    def make_mo(self, template, bom, origin):
        mo = self.env['mrp.production'].create({
            'product_id': template.product_variant_id.id, 'bom_id': bom.id, 'product_qty': 1,
            'origin': origin, 'date_start': datetime.now() + timedelta(days=1),
        })
        mo.action_confirm()
        mo.button_plan()
        return mo

    def publish(self, *pairs, **note):
        """Write each MO's work order to start at the given instant and run two hours."""
        params = {'conflictStrategy': 'FORCE_OVERWRITE', 'operations': [
            {'externalId': str(mo.workorder_ids[0].id),
             'operationStart': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
             'operationEnd': (start + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')}
            for mo, start in pairs
        ]}
        if note:
            params['salesOrderNote'] = note
        result = self.call('schedule/write_back', **params)
        self.so.invalidate_recordset()
        return result

    def move(self, start, **note):
        """The whole order ends when the door does; move the door."""
        return self.publish((self.cabinet_mo, self.monday), (self.door_mo, start), **note)

    def notes(self):
        return self.so.message_ids.filtered(lambda m: 'Planned by APS' in (m.body or '')).sorted('id')

    def late_activities(self):
        return self.so.activity_ids.filtered(lambda a: a.summary == self.so.LATE_SUMMARY)

    def shown(self, dt, tz='Europe/Berlin'):
        return fields.Datetime.context_timestamp(self.so.with_context(tz=tz), dt).strftime('%d %b %Y %H:%M')

    def test_defaults_are_a_quiet_note(self):
        result = self.publish((self.cabinet_mo, self.monday), (self.door_mo, self.monday + timedelta(days=2)))
        self.assertEqual(result['salesOrdersNoted'], 1)
        door_end = self.monday + timedelta(days=2, hours=2)
        self.assertEqual(self.so.aps_planned_finish, door_end, 'the door is a sub-assembly and finishes last')
        note = self.notes()
        self.assertEqual(len(note), 1)
        self.assertIn(self.shown(door_end), note.body)
        self.assertIn('Europe/Berlin', note.body, "in the salesperson's own zone")
        self.assertNotIn('@', note.body)
        self.assertFalse(note.partner_ids, 'nobody is told')
        self.assertFalse(self.late_activities())

    def test_a_move_inside_the_day_is_kept_but_not_written(self):
        self.move(self.monday)
        self.assertEqual(len(self.notes()), 1)
        result = self.move(self.monday + timedelta(hours=5))
        self.assertEqual(result['salesOrdersNoted'], 0)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.so.aps_planned_finish, self.monday + timedelta(hours=7), 'the exact time is still remembered')

    def test_a_move_to_another_day_names_the_old_date(self):
        self.move(self.monday)
        self.move(self.monday + timedelta(days=1))
        notes = self.notes()
        self.assertEqual(len(notes), 2)
        self.assertIn('was %s' % self.shown(self.monday + timedelta(hours=2)), notes[-1].body)

    def test_the_same_dates_again_say_nothing(self):
        self.move(self.monday)
        self.assertEqual(self.move(self.monday)['salesOrdersNoted'], 0)
        self.assertEqual(len(self.notes()), 1)

    def test_week_unit_is_silent_inside_the_week(self):
        self.move(self.monday, unit='WEEK')
        self.assertEqual(self.move(self.monday + timedelta(days=4), unit='WEEK')['salesOrdersNoted'], 0, 'Monday to Friday')
        self.assertEqual(self.move(self.monday + timedelta(days=7), unit='WEEK')['salesOrdersNoted'], 1, 'Friday to next Monday')
        self.assertEqual(len(self.notes()), 2)

    def test_week_boundary_is_on_the_plants_clock(self):
        # work ending Sunday 22:30 UTC is still this week in UTC and already Monday of next week in Berlin
        sunday_late = self.monday + timedelta(days=6, hours=14, minutes=30)
        self.move(self.monday, unit='WEEK', timezone='UTC')
        self.assertEqual(self.move(sunday_late, unit='WEEK', timezone='UTC')['salesOrdersNoted'], 0)
        other = self.env['sale.order'].create({
            'partner_id': self.customer.id, 'user_id': self.seller.id,
            'order_line': [(0, 0, {'product_id': self.cabinet.product_variant_id.id, 'product_uom_qty': 1})],
        })
        mo = self.make_mo(self.cabinet, self.cabinet_bom, other.name)
        self.publish((mo, self.monday), unit='WEEK', timezone='Europe/Berlin')
        self.assertEqual(self.publish((mo, sunday_late), unit='WEEK', timezone='Europe/Berlin')['salesOrdersNoted'], 1)

    def test_mention_only_when_later_than_promised(self):
        self.so.commitment_date = self.monday + timedelta(days=3, hours=10)
        self.move(self.monday, mode='MENTION')
        note = self.notes()[-1]
        self.assertNotIn('@', note.body, 'inside the promise: a quiet note')
        self.assertFalse(note.partner_ids)
        self.move(self.monday + timedelta(days=5), mode='MENTION')
        note = self.notes()[-1]
        self.assertIn('@Sally Seller', note.body)
        self.assertIn('data-oe-id="%d"' % self.seller.partner_id.id, note.body)
        self.assertIn(self.seller.partner_id, note.partner_ids, 'the salesperson is told')
        self.assertIn('Later than the promised delivery of %s' % self.shown(self.so.commitment_date), note.body)
        self.assertFalse(self.late_activities(), 'mention mode makes no activity')

    def test_late_on_the_same_day_as_the_promise_is_not_late(self):
        self.so.commitment_date = self.monday + timedelta(days=2, hours=1)
        self.move(self.monday + timedelta(days=2, hours=8), mode='MENTION')
        self.assertNotIn('@', self.notes()[-1].body)

    def test_activity_when_late_one_per_order(self):
        self.so.commitment_date = self.monday + timedelta(days=3, hours=10)
        self.move(self.monday, mode='ACTIVITY')
        self.assertFalse(self.late_activities())

        self.move(self.monday + timedelta(days=5), mode='ACTIVITY')
        activity = self.late_activities()
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.seller)
        self.assertEqual(activity.date_deadline, fields.Date.context_today(self.so))
        self.assertIn(self.shown(self.monday + timedelta(days=5, hours=2)), activity.note)
        self.assertEqual(self.so.aps_late_activity_id, activity)
        self.assertNotIn('@', self.notes()[-1].body, 'the activity is the notification')

        self.move(self.monday + timedelta(days=6), mode='ACTIVITY')
        self.assertEqual(self.late_activities(), activity, 'still late: the same activity, brought up to date')
        self.assertIn(self.shown(self.monday + timedelta(days=6, hours=2)), activity.note)

        self.move(self.monday + timedelta(days=1), mode='ACTIVITY')
        self.assertFalse(self.late_activities(), 'back inside the promise: the activity is done')
        self.assertFalse(self.so.aps_late_activity_id)
        self.assertIn('Within the promised delivery again', self.notes()[-1].body)

    def test_activity_the_salesperson_already_closed_is_not_resurrected_until_the_next_change(self):
        self.so.commitment_date = self.monday + timedelta(days=1)
        self.move(self.monday + timedelta(days=5), mode='ACTIVITY')
        self.late_activities().action_feedback(feedback='Called the customer')
        self.assertFalse(self.late_activities())
        self.assertEqual(self.move(self.monday + timedelta(days=5, hours=1), mode='ACTIVITY')['salesOrdersNoted'], 0)
        self.assertFalse(self.late_activities(), 'a move inside the day does not bring it back')
        self.move(self.monday + timedelta(days=8), mode='ACTIVITY')
        self.assertEqual(len(self.late_activities()), 1, 'a real change while still late opens a new one')

    def test_an_activity_is_closed_even_if_the_mode_changed_meanwhile(self):
        self.so.commitment_date = self.monday + timedelta(days=1)
        self.move(self.monday + timedelta(days=5), mode='ACTIVITY')
        self.assertEqual(len(self.late_activities()), 1)
        self.move(self.monday, mode='NONE')
        self.assertFalse(self.late_activities())

    def test_no_salesperson_means_a_note_and_nothing_else(self):
        self.so.user_id = False
        self.so.commitment_date = self.monday + timedelta(days=1)
        result = self.move(self.monday + timedelta(days=5), mode='ACTIVITY')
        self.assertEqual(result['salesOrdersNoted'], 1)
        self.assertFalse(self.late_activities())
        note = self.notes()[-1]
        self.assertNotIn('@', note.body)
        self.assertIn('Later than the promised delivery', note.body, 'the fact is still written down')
        self.move(self.monday + timedelta(days=6), mode='MENTION')
        self.assertFalse(self.notes()[-1].partner_ids)

    def test_no_promised_date_is_never_late(self):
        self.assertFalse(self.so.commitment_date)
        self.move(self.monday + timedelta(days=30), mode='ACTIVITY')
        self.assertFalse(self.late_activities())
        self.assertNotIn('Later than', self.notes()[-1].body)

    def test_unknown_options_fall_back_to_the_defaults(self):
        result = self.move(self.monday, mode='SHOUT', unit='FORTNIGHT', timezone='Mars/Olympus')
        self.assertEqual(result['salesOrdersNoted'], 1)
        self.assertFalse(self.notes()[-1].partner_ids)

    def test_an_mo_without_a_sale_order_posts_nothing(self):
        loose = self.make_mo(self.door, self.door_bom, '')
        before = self.env['mail.message'].search_count([('body', 'ilike', 'Planned by APS')])
        result = self.publish((loose, self.monday), mode='ACTIVITY')
        self.assertEqual(result.get('salesOrdersNoted', 0), 0)
        self.assertEqual(self.env['mail.message'].search_count([('body', 'ilike', 'Planned by APS')]), before)
