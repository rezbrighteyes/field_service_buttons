# -*- coding: utf-8 -*-
from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCreditScrap(TransactionCase):
    """Cover the Inventory half of a Credit Scrap.

    Before this, a Credit Scrap recorded a reason and nothing else: the goods
    left no trace in Inventory while the credit note still posted the COGS
    reversal, so the ledger said the stock came back when no stock came back.
    A Credit Scrap must now receive the goods and scrap them, and it must
    never stop the credit note when it cannot.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company.id)], limit=1, order='sequence, id',
        )
        self.landing = self.warehouse.lot_stock_id
        self.partner = self.env['res.partner'].create({
            'name': 'Scrap Test Customer',
        })
        self.storable = self.env['product.product'].create({
            'name': 'Scrap Test Storable',
            'type': 'consu',
            'is_storable': True,
            'list_price': 10.0,
        })
        self.service_like = self.env['product.product'].create({
            'name': 'Scrap Test Non Storable',
            'type': 'consu',
            'is_storable': False,
            'list_price': 10.0,
        })
        self.scrap_tag = self.env['stock.scrap.reason.tag'].create({
            'name': 'Scrap Test Reason Tag',
        })
        self.scrap_reason = self.env['reza.fsm.credit.return.reason'].create({
            'name': 'Scrap Test Reason',
            'reason_type': 'scrap',
            'scrap_reason_tag_id': self.scrap_tag.id,
        })
        self.return_location = self.env['stock.location'].create({
            'name': 'Scrap Test Van',
            'usage': 'internal',
            'company_id': self.company.id,
        })

    def _available(self, product, location):
        return self.env['stock.quant']._get_available_quantity(product, location)

    def _credit_note(self, product, outcome, quantity=2.0):
        """Build a posted-ready credit note carrying one credit return event."""
        move = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'invoice_date': fields.Date.context_today(self.env.user),
        })
        move_line = self.env['account.move.line'].create({
            'move_id': move.id,
            'product_id': product.id,
            'quantity': quantity,
            'product_uom_id': product.uom_id.id,
            'price_unit': product.list_price,
            'name': product.display_name,
            'reza_fsm_credit_return_outcome': outcome,
        })
        values = {
            'date': fields.Date.context_today(self.env.user),
            'outcome': outcome,
            'move_id': move.id,
            'move_line_id': move_line.id,
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'quantity': quantity,
        }
        if outcome == 'credit_scrap':
            values['scrap_reason_id'] = self.scrap_reason.id
        else:
            values['return_location_id'] = self.return_location.id
        event = self.env['reza.fsm.credit.return.event'].create(values)
        return move, event

    def test_credit_scrap_creates_a_validated_scrap_order(self):
        """The goods come back, get written off, and the stock nets to zero."""
        before = self._available(self.storable, self.landing)
        move, event = self._credit_note(self.storable, 'credit_scrap')
        move.action_post()

        self.assertFalse(
            event.reza_scrap_error,
            'The scrap reported a problem: %s' % event.reza_scrap_error,
        )
        self.assertTrue(event.scrap_id, 'No Scrap Order was created.')
        self.assertEqual(event.scrap_id.state, 'done')
        self.assertEqual(event.scrap_id.scrap_qty, 2.0)
        self.assertEqual(event.scrap_id.location_id, self.landing)
        self.assertEqual(event.scrap_id.scrap_reason_tag_ids, self.scrap_tag)
        self.assertEqual(event.scrap_id.origin, move.name)
        if 'reza_partner_id' in event.scrap_id._fields:
            self.assertEqual(
                event.scrap_id.reza_partner_id, self.partner,
                'The Scrap Order must say whose goods were written off.',
            )
        self.assertTrue(event.stock_move_id, 'No receipt move was created.')
        self.assertEqual(event.stock_move_id.state, 'done')
        self.assertEqual(event.stock_move_id.location_dest_id, self.landing)
        self.assertEqual(
            self._available(self.storable, self.landing),
            before,
            'Receipt and scrap must net to zero at the landing location.',
        )

    def test_credit_scrap_never_blocks_the_credit_note(self):
        """A product with no stock to write off records the problem, not a raise."""
        move, event = self._credit_note(self.service_like, 'credit_scrap')
        move.action_post()

        self.assertEqual(
            move.state, 'posted',
            'The credit note must post even when the write-off cannot happen.',
        )
        self.assertFalse(event.scrap_id)
        self.assertTrue(
            event.reza_scrap_error,
            'A failed write-off must be recorded for the office.',
        )
        self.assertIn('not a storable product', event.reza_scrap_error)
        self.assertEqual(event.state, 'done')

    def test_credit_scrap_is_not_repeated_on_a_second_pass(self):
        """Processing the same event twice must not scrap the goods twice."""
        move, event = self._credit_note(self.storable, 'credit_scrap')
        move.action_post()
        scrap = event.scrap_id
        self.assertTrue(scrap)

        event._reza_fsm_try_create_scrap()

        self.assertEqual(event.scrap_id, scrap, 'A second Scrap Order was created.')
        self.assertEqual(
            self.env['stock.scrap'].search_count([('origin', '=', move.name)]),
            1,
        )

    def test_credit_return_still_only_moves_stock(self):
        """Regression: a Credit Return returns goods and scraps nothing."""
        before = self._available(self.storable, self.return_location)
        move, event = self._credit_note(self.storable, 'credit_return')
        move.action_post()

        self.assertTrue(event.stock_move_id)
        self.assertEqual(event.stock_move_id.state, 'done')
        self.assertEqual(event.stock_move_id.location_dest_id, self.return_location)
        self.assertFalse(event.scrap_id, 'A Credit Return must not scrap anything.')
        self.assertEqual(
            self._available(self.storable, self.return_location),
            before + 2.0,
            'A Credit Return must put the goods back on the shelf.',
        )

    def test_manual_scrap_button_is_office_only(self):
        """A rep must not be able to post a write-off by calling the method.

        The groups= on the button only hides it. This proves the method itself
        refuses, which is what an RPC caller would hit.
        """
        rep = self.env['res.users'].create({
            'name': 'Scrap Test Rep',
            'login': 'scrap_test_rep',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('industry_fsm.group_fsm_user').id,
            ])],
        })
        # Guard the guard: if the fixture accidentally granted the group, the
        # assertRaises below would pass for the wrong reason.
        self.assertFalse(
            rep.has_group('reza_field_service_buttons.group_fsm_controllers'))
        self.assertFalse(rep.has_group('base.group_system'))

        move, event = self._credit_note(self.service_like, 'credit_scrap')
        move.action_post()
        self.assertFalse(event.scrap_id)

        with self.assertRaises(UserError):
            event.with_user(rep).action_reza_fsm_create_scrap()

        # And the office can.
        event.scrap_reason_id = self.scrap_reason
        self.assertTrue(
            self.env.user.has_group('reza_field_service_buttons.group_fsm_controllers')
            or self.env.user.has_group('base.group_system'))

    def test_scrap_reason_tag_falls_back_to_a_matching_name(self):
        """With no tag set by hand, a tag of the same name reaches the Scrap Order.

        This is the branch production actually uses: the shipped credit reasons
        carry no mapping, so "Damaged" has to find the "Damaged" tag on its own.
        Post the credit note rather than calling the resolver alone, or the test
        would pass even if the tag never landed on the scrap.
        """
        self.scrap_reason.scrap_reason_tag_id = False
        named_tag = self.env['stock.scrap.reason.tag'].create({
            'name': self.scrap_reason.name,
        })
        move, event = self._credit_note(self.storable, 'credit_scrap')
        move.action_post()

        self.assertFalse(event.reza_scrap_error)
        self.assertTrue(event.scrap_id, 'No Scrap Order was created.')
        self.assertEqual(event.scrap_id.state, 'done')
        self.assertEqual(
            event.scrap_id.scrap_reason_tag_ids, named_tag,
            'The Scrap Order must carry the tag matched from the reason name.',
        )
