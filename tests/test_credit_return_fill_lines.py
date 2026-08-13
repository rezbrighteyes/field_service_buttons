# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCreditReturnFillLines(TransactionCase):
    """Cover the bulk fill button on the rep credit / return wizard.

    A rep hands back a whole van load for one reason, so keying the same
    location and reason onto every line by hand is where the mistakes happen.
    The button must fill every Credit Return line and never touch a Credit
    Scrap line, which has no return location and draws its reason from a
    different list.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.location = self.env['stock.location'].create({
            'name': 'Fill Test Van',
            'usage': 'internal',
            'company_id': self.company.id,
        })
        self.other_location = self.env['stock.location'].create({
            'name': 'Fill Test Shed',
            'usage': 'internal',
            'company_id': self.company.id,
        })
        self.env.user.reza_icw_allowed_rep_location_ids = [
            (6, 0, [self.location.id, self.other_location.id])
        ]
        self.project = self.env['project.project'].create({
            'name': 'Fill Test FSM Project',
            'is_fsm': True,
            'company_id': self.company.id,
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Fill Test Customer',
        })
        self.task = self.env['project.task'].create({
            'name': 'Fill Test Visit',
            'project_id': self.project.id,
            'partner_id': self.partner.id,
        })
        self.product_a = self.env['product.product'].create({
            'name': 'Fill Test Product A',
            'type': 'consu',
            'list_price': 10.0,
        })
        self.product_b = self.env['product.product'].create({
            'name': 'Fill Test Product B',
            'type': 'consu',
            'list_price': 20.0,
        })
        self.credit_reason = self.env.ref(
            'reza_field_service_buttons.credit_reason_damaged'
        )
        self.scrap_reason = self.env['reza.fsm.credit.return.reason'].search(
            [('reason_type', 'in', ('scrap', 'both'))], limit=1,
        )
        self.wizard = self.env['reza.fsm.credit.return.wizard'].create({
            'task_id': self.task.id,
        })

    def _line(self, product, outcome='credit_return', **extra):
        values = {
            'wizard_id': self.wizard.id,
            'product_id': product.id,
            'quantity': 1.0,
            'outcome': outcome,
        }
        values.update(extra)
        return self.env['reza.fsm.credit.return.wizard.line'].create(values)

    def test_fill_applies_to_every_return_line(self):
        line_a = self._line(self.product_a)
        line_b = self._line(self.product_b)
        self.wizard.write({
            'bulk_return_location_id': self.location.id,
            'bulk_credit_reason_ids': [(6, 0, self.credit_reason.ids)],
            'bulk_note': '  Whole van back  ',
        })

        self.wizard.action_fill_return_lines()

        for line in (line_a, line_b):
            self.assertEqual(line.return_location_id, self.location)
            self.assertEqual(line.credit_reason_ids, self.credit_reason)
            self.assertEqual(line.note, 'Whole van back')

    def test_fill_overwrites_a_line_that_was_already_set(self):
        line = self._line(
            self.product_a, return_location_id=self.other_location.id,
        )

        self.wizard.write({'bulk_return_location_id': self.location.id})
        self.wizard.action_fill_return_lines()

        self.assertEqual(line.return_location_id, self.location)

    def test_fill_never_touches_a_scrap_line(self):
        scrap_line = self._line(
            self.product_a,
            outcome='credit_scrap',
            scrap_reason_id=self.scrap_reason.id,
        )
        return_line = self._line(self.product_b)
        self.wizard.write({
            'bulk_return_location_id': self.location.id,
            'bulk_credit_reason_ids': [(6, 0, self.credit_reason.ids)],
        })

        self.wizard.action_fill_return_lines()

        self.assertFalse(scrap_line.return_location_id)
        self.assertFalse(scrap_line.credit_reason_ids)
        self.assertEqual(scrap_line.scrap_reason_id, self.scrap_reason)
        self.assertEqual(return_line.return_location_id, self.location)

    def test_fill_only_sets_the_values_that_were_chosen(self):
        line = self._line(
            self.product_a, credit_reason_ids=[(6, 0, self.credit_reason.ids)],
        )

        self.wizard.write({'bulk_return_location_id': self.location.id})
        self.wizard.action_fill_return_lines()

        self.assertEqual(line.return_location_id, self.location)
        self.assertEqual(
            line.credit_reason_ids, self.credit_reason,
            'An empty bulk reason must leave the line reason alone.',
        )

    def test_fill_refuses_when_nothing_was_chosen(self):
        self._line(self.product_a)
        with self.assertRaises(ValidationError):
            self.wizard.action_fill_return_lines()

    def test_fill_refuses_when_there_are_no_return_lines(self):
        self._line(
            self.product_a,
            outcome='credit_scrap',
            scrap_reason_id=self.scrap_reason.id,
        )
        self.wizard.write({'bulk_return_location_id': self.location.id})
        with self.assertRaises(ValidationError):
            self.wizard.action_fill_return_lines()

    def test_fill_refuses_a_location_the_rep_is_not_allowed(self):
        # An intercompany warehouse manager is allowed every internal location,
        # so the guard cannot fire for one - and the account running the tests
        # normally is one.  Stand the runner down to a plain rep for this test
        # or it passes without ever reaching the check it exists to prove.
        manager_group = self.env.ref(
            'reza_intercompany_warehouse.group_intercompany_warehouse_manager'
        )
        if self.env.user.has_group(
            'reza_intercompany_warehouse.group_intercompany_warehouse_manager'
        ):
            self.env.user.write({'group_ids': [(3, manager_group.id)]})
            self.env.registry.clear_cache()
            self.wizard.invalidate_recordset(['allowed_return_location_ids'])

        self._line(self.product_a)
        stranger = self.env['stock.location'].create({
            'name': 'Fill Test Someone Elses Van',
            'usage': 'internal',
            'company_id': self.company.id,
        })
        # Written straight onto the record so the field domain cannot mask the
        # server-side guard, which is what an RPC caller would bypass.
        self.wizard.write({'bulk_return_location_id': stranger.id})
        self.assertNotIn(
            stranger, self.wizard.allowed_return_location_ids,
            'The fixture failed to stand the user down to a plain rep, so this '
            'test would pass without exercising the guard.',
        )
        with self.assertRaises(ValidationError):
            self.wizard.action_fill_return_lines()

    def test_fill_refuses_once_the_credit_note_is_confirmed(self):
        self._line(self.product_a)
        self.wizard.write({
            'state': 'done',
            'bulk_return_location_id': self.location.id,
        })
        with self.assertRaises(ValidationError):
            self.wizard.action_fill_return_lines()
