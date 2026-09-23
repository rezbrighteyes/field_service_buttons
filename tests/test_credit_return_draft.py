# -*- coding: utf-8 -*-
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

# 1x1 transparent PNG - the smallest image fields.Image will accept.
SIGNATURE_PNG = (
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk'
    b'YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


@tagged('post_install', '-at_install')
class TestCreditReturnDraft(TransactionCase):
    """Cover the stored rep credit and its draft credit note.

    The credit was a TransientModel: a rep who left the screen lost it when
    the vacuum cleared the rows, and pressing Credit / Return again opened an
    empty one.  Eight credits were lost that way on production.  A credit is
    now a stored record the rep reopens from the visit, and its first product
    creates a DRAFT out_refund that follows the lines and is the very record
    Create & Confirm posts.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.location = self.env['stock.location'].create({
            'name': 'Draft Test Van',
            'usage': 'internal',
            'company_id': self.company.id,
        })
        self.env.user.reza_icw_allowed_rep_location_ids = [
            (6, 0, [self.location.id])
        ]
        self.project = self.env['project.project'].create({
            'name': 'Draft Test FSM Project',
            'is_fsm': True,
            'company_id': self.company.id,
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Draft Test Customer',
        })
        self.task = self.env['project.task'].create({
            'name': 'Draft Test Visit',
            'project_id': self.project.id,
            'partner_id': self.partner.id,
        })
        self.product_a = self.env['product.product'].create({
            'name': 'Draft Test Product A',
            'type': 'consu',
            'list_price': 10.0,
        })
        self.product_b = self.env['product.product'].create({
            'name': 'Draft Test Product B',
            'type': 'consu',
            'list_price': 20.0,
        })
        self.credit_reason = self.env.ref(
            'reza_field_service_buttons.credit_reason_damaged'
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _press_credit_button(self):
        action = self.task.action_create_credit_note()
        self.assertEqual(action['res_model'], 'reza.fsm.credit.return.wizard')
        self.assertTrue(action.get('res_id'), 'The button must open one credit.')
        return self.env['reza.fsm.credit.return.wizard'].browse(action['res_id'])

    def _moves_of(self, credit):
        return self.env['account.move'].search([
            ('reza_fsm_credit_return_id', '=', credit.id),
        ])

    def _product_lines(self, move):
        return move.invoice_line_ids.filtered(
            lambda line: line.reza_fsm_credit_return_outcome
        )

    def _make_ready(self, credit):
        credit.line_ids.write({
            'return_location_id': self.location.id,
            'credit_reason_ids': [(6, 0, self.credit_reason.ids)],
        })
        credit.write({'signature': SIGNATURE_PNG})

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_leave_and_reopen_keeps_lines_and_signature(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 2)
        credit.write({'signature': SIGNATURE_PNG})

        # The rep leaves the screen. Nothing may hold the record in memory.
        self.env.invalidate_all()
        reopened = self._press_credit_button()

        self.assertEqual(reopened, credit, 'The button opened a new, empty credit.')
        self.assertEqual(reopened.line_ids.product_id, self.product_a)
        self.assertEqual(reopened.line_ids.quantity, 2)
        self.assertTrue(reopened.signature)
        self.assertTrue(reopened.signed_on)
        self.assertEqual(self.task.reza_fsm_open_credit_count, 1)

    def test_the_credit_model_is_no_longer_transient(self):
        Credit = self.env['reza.fsm.credit.return.wizard']
        self.assertFalse(Credit._transient)
        self.assertFalse(self.env['reza.fsm.credit.return.wizard.line']._transient)

    def test_first_product_creates_exactly_one_draft(self):
        credit = self._press_credit_button()
        self.assertFalse(self._moves_of(credit), 'An empty credit must not make a draft.')

        credit._update_order_line_info(self.product_a.id, 1)
        moves = self._moves_of(credit)
        self.assertEqual(len(moves), 1)
        self.assertEqual(credit.move_id, moves)
        self.assertEqual(moves.state, 'draft')
        self.assertEqual(moves.move_type, 'out_refund')
        self.assertEqual(moves.partner_id, self.partner)
        self.assertEqual(moves.company_id, self.company)
        self.assertEqual(moves.reza_fsm_task_id, self.task)
        self.assertIn(
            moves.name, (False, '/'),
            'A draft must not take a credit note number.',
        )

        credit._update_order_line_info(self.product_b.id, 3)
        self.assertEqual(self._moves_of(credit), moves, 'A second draft was created.')
        self.assertEqual(len(self._product_lines(moves)), 2)

    def test_line_edits_follow_onto_the_draft(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 1)
        credit._update_order_line_info(self.product_b.id, 1)
        move = credit.move_id
        line_a = credit.line_ids.filtered(lambda l: l.product_id == self.product_a)
        line_b = credit.line_ids.filtered(lambda l: l.product_id == self.product_b)

        # Quantity through the catalogue, price and reason through the list.
        credit._update_order_line_info(self.product_a.id, 4)
        line_a.write({
            'price_unit': 7.5,
            'return_location_id': self.location.id,
            'credit_reason_ids': [(6, 0, self.credit_reason.ids)],
        })
        move_line_a = line_a.move_line_id
        self.assertEqual(move_line_a.move_id, move)
        self.assertEqual(move_line_a.quantity, 4)
        self.assertEqual(move_line_a.price_unit, 7.5)
        self.assertEqual(move_line_a.reza_fsm_credit_return_location_id, self.location)
        self.assertEqual(move_line_a.reza_fsm_credit_reason_ids, self.credit_reason)

        # A form save sends line changes through the credit itself.
        credit.write({'line_ids': [(1, line_b.id, {'quantity': 6})]})
        self.assertEqual(line_b.move_line_id.quantity, 6)

        # Removing a product in the catalogue removes its credit note line.
        move_line_b = line_b.move_line_id
        credit._update_order_line_info(self.product_b.id, 0)
        self.assertFalse(move_line_b.exists())
        self.assertEqual(self._product_lines(move).product_id, self.product_a)
        self.assertEqual(self._moves_of(credit), move)

    def test_confirm_posts_the_same_draft(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 2)
        credit._update_order_line_info(self.product_b.id, 1)
        draft = credit.move_id
        self._make_ready(credit)

        credit.action_create_credit_note()

        self.assertEqual(credit.state, 'done')
        self.assertEqual(credit.credit_note_id, draft.id)
        self.assertEqual(draft.state, 'posted')
        self.assertTrue(draft.name and draft.name != '/')
        self.assertEqual(credit.credit_note_name, draft.name)
        task_moves = self.env['account.move'].search([
            ('reza_fsm_task_id', '=', self.task.id),
        ])
        self.assertEqual(task_moves, draft, 'Confirm created a second credit note.')
        self.assertEqual(len(self._product_lines(draft)), 2)
        events = draft.reza_fsm_credit_return_event_ids
        self.assertEqual(len(events), 2, 'One return event per credit line.')
        self.assertTrue(draft.reza_fsm_customer_signature)
        self.assertEqual(self.task.reza_fsm_open_credit_count, 0)

    def test_second_credit_on_the_same_task(self):
        first = self._press_credit_button()
        first._update_order_line_info(self.product_a.id, 1)
        self._make_ready(first)
        first.action_create_credit_note()

        second = self._press_credit_button()
        self.assertNotEqual(second, first, 'A confirmed credit must not reopen.')
        self.assertFalse(second.line_ids)
        second._update_order_line_info(self.product_b.id, 1)
        self.assertTrue(second.move_id)
        self.assertNotEqual(second.move_id, first.move_id)
        self._make_ready(second)
        second.action_create_credit_note()

        task_moves = self.env['account.move'].search([
            ('reza_fsm_task_id', '=', self.task.id),
        ])
        self.assertEqual(len(task_moves), 2)
        self.assertEqual(set(task_moves.mapped('state')), {'posted'})

    def test_several_unfinished_credits_open_the_list(self):
        Credit = self.env['reza.fsm.credit.return.wizard']
        Credit.create({'task_id': self.task.id})
        Credit.create({'task_id': self.task.id})
        action = self.task.action_create_credit_note()
        self.assertEqual(action['res_model'], 'reza.fsm.credit.return.wizard')
        self.assertFalse(action.get('res_id'))
        self.assertEqual(action['domain'], [('task_id', '=', self.task.id)])

    def test_draft_deleted_in_accounting_is_recreated(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 1)
        old_draft = credit.move_id
        old_draft.unlink()
        self.assertFalse(credit.move_id, 'Deleting the draft must clear the link.')

        credit._update_order_line_info(self.product_a.id, 2)
        new_draft = credit.move_id
        self.assertTrue(new_draft)
        self.assertEqual(self._product_lines(new_draft).quantity, 2)

    def test_cancel_keeps_the_draft(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 1)
        draft = credit.move_id
        action = credit.action_cancel_credit_return()
        self.assertEqual(action['res_model'], 'project.task')
        self.assertTrue(credit.exists())
        self.assertTrue(draft.exists())
        self.assertEqual(draft.state, 'draft')

    def test_cancel_credit_deletes_the_draft_and_keeps_a_record(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 2)
        move = credit.move_id
        self.assertTrue(move)

        credit.action_discard_credit_return()
        self.assertEqual(credit.state, 'cancel')
        self.assertFalse(move.exists(), 'The draft credit note must be deleted.')
        log = self.env['reza.fsm.credit.return.log'].search([
            ('credit_ref_id', '=', credit.id), ('action', '=', 'cancel'),
        ])
        self.assertTrue(log, 'The cancelled products must be in the log.')
        self.assertEqual(log[0].product_id, self.product_a)

        again = self._press_credit_button()
        self.assertNotEqual(again, credit, 'A cancelled credit must not reopen.')
        with self.assertRaises(ValidationError):
            credit.action_create_credit_note()

    def _office_user(self, login, can_post):
        groups = [
            self.env.ref('base.group_user').id,
            self.env.ref('account.group_account_invoice').id,
        ]
        if can_post:
            groups.append(self.env.ref(
                'reza_field_service_buttons.group_post_unfinished_rep_credit'
            ).id)
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, groups)],
        })

    def test_only_the_post_group_may_post_an_unfinished_credit(self):
        member = self._office_user('draft-test-poster@example.com', True)
        other = self._office_user('draft-test-clerk@example.com', False)
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 1)
        draft = credit.move_id

        # The banner shows for everybody, member or not.
        self.env.invalidate_all()
        self.assertTrue(draft.with_user(other).reza_fsm_credit_unfinished)
        self.assertTrue(draft.with_user(member).reza_fsm_credit_unfinished)

        with self.assertRaisesRegex(UserError, 'Post unfinished rep credits'):
            draft.with_user(other).action_post()
        self.assertEqual(draft.state, 'draft')

        draft.with_user(member).action_post()
        self.assertEqual(draft.state, 'posted')
        self.assertFalse(
            draft.reza_fsm_credit_return_event_ids,
            'An office post has no return events, so no stock comes back.',
        )
        log = self.env['reza.fsm.credit.return.log'].search([
            ('credit_ref_id', '=', credit.id),
            ('action', '=', 'office_posted_unfinished'),
        ])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.user_id, member)
        self.assertEqual(log.move_ref_id, draft.id)
        self.assertEqual(log.product_id, self.product_a)
        self.assertTrue(draft.message_ids.filtered(
            lambda m: 'without rep confirmation' in (m.body or '')
            and member.name in (m.body or '')
        ))

    def test_rep_cannot_edit_after_the_office_posts(self):
        credit = self._press_credit_button()
        credit._update_order_line_info(self.product_a.id, 1)
        draft = credit.move_id
        draft.with_context(reza_fsm_credit_return_confirm=True).action_post()
        with self.assertRaisesRegex(ValidationError, 'already posted'):
            credit._update_order_line_info(self.product_a.id, 5)
