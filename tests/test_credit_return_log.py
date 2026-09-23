# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from .test_credit_return_draft import SIGNATURE_PNG


@tagged('post_install', '-at_install')
class TestCreditReturnLog(TransactionCase):
    """Cover the append-only product log of a rep credit.

    On production, abandoned credits could be traced only through orphan
    signature attachments: who, when and which customer survived, but the
    products were gone for good.  Every add, change and remove now writes a
    log row that keeps its own copy of the values, so it outlives the credit
    and its draft credit note, and a rep can neither change nor delete it.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.location = self.env['stock.location'].create({
            'name': 'Log Test Van',
            'usage': 'internal',
            'company_id': self.company.id,
        })
        self.env.user.reza_icw_allowed_rep_location_ids = [
            (6, 0, [self.location.id])
        ]
        self.project = self.env['project.project'].create({
            'name': 'Log Test FSM Project',
            'is_fsm': True,
            'company_id': self.company.id,
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Log Test Customer',
        })
        self.task = self.env['project.task'].create({
            'name': 'Log Test Visit',
            'project_id': self.project.id,
            'partner_id': self.partner.id,
        })
        self.product = self.env['product.product'].create({
            'name': 'Log Test Product',
            'default_code': 'LOGTEST-1',
            'type': 'consu',
            'list_price': 10.0,
        })
        self.credit_reason = self.env.ref(
            'reza_field_service_buttons.credit_reason_damaged'
        )
        self.Log = self.env['reza.fsm.credit.return.log']

    def _new_credit(self):
        action = self.task.action_create_credit_note()
        return self.env['reza.fsm.credit.return.wizard'].browse(action['res_id'])

    def _logs(self, credit_id, action=None):
        domain = [('credit_ref_id', '=', credit_id)]
        if action:
            domain.append(('action', '=', action))
        return self.Log.search(domain, order='id')

    def test_add_change_remove_each_write_a_row(self):
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 1)
        credit._update_order_line_info(self.product.id, 3)
        credit._update_order_line_info(self.product.id, 0)

        self.assertEqual(
            self._logs(credit.id).mapped('action'), ['add', 'change', 'remove'],
        )
        add = self._logs(credit.id, 'add')
        self.assertEqual(add.product_id, self.product)
        self.assertEqual(add.product_code, 'LOGTEST-1')
        self.assertEqual(add.quantity, 1)
        self.assertEqual(add.user_id, self.env.user)
        self.assertEqual(add.task_ref_id, self.task.id)
        self.assertEqual(add.partner_name, self.partner.display_name)
        self.assertEqual(add.company_id, self.company)
        self.assertEqual(add.move_ref_id, credit.move_id.id)
        change = self._logs(credit.id, 'change')
        self.assertEqual(change.old_quantity, 1)
        self.assertEqual(change.quantity, 3)
        self.assertIn('quantity', change.changes)
        remove = self._logs(credit.id, 'remove')
        self.assertEqual(remove.quantity, 3)
        self.assertEqual(remove.product_name, self.product.display_name)

    def test_an_unchanged_save_writes_no_change_row(self):
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 2)
        credit.line_ids.write({'quantity': 2})
        self.assertFalse(self._logs(credit.id, 'change'))

    def test_rows_outlive_the_draft_and_the_credit(self):
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 4)
        credit_id = credit.id
        draft_id = credit.move_id.id

        credit.move_id.unlink()
        deleted = self._logs(credit_id, 'draft_deleted')
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted.move_ref_id, draft_id)
        self.assertEqual(deleted.product_id, self.product)
        self.assertEqual(deleted.quantity, 4)

        # Reps may not delete a credit; the office (sudo here) can.
        credit.sudo().unlink()
        rows = self._logs(credit_id)
        self.assertEqual(
            rows.mapped('action'), ['add', 'draft_deleted', 'credit_deleted'],
        )
        self.assertFalse(rows.credit_return_id, 'The link clears; the row stays.')
        self.assertEqual(set(rows.mapped('credit_ref_id')), {credit_id})
        self.assertEqual(self._logs(credit_id, 'add').move_ref_id, draft_id)
        self.assertEqual(self._logs(credit_id, 'credit_deleted').product_name,
                         self.product.display_name)
        self.assertEqual(self.task.reza_fsm_credit_log_count, 3)

    def test_draft_recreated_is_logged(self):
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 1)
        credit.move_id.unlink()
        credit._update_order_line_info(self.product.id, 2)
        recreated = self._logs(credit.id, 'draft_recreated')
        self.assertEqual(len(recreated), 1)
        self.assertEqual(recreated.move_ref_id, credit.move_id.id)

    def test_sign_save_back_and_confirm_are_logged(self):
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 2)
        credit.line_ids.write({
            'return_location_id': self.location.id,
            'credit_reason_ids': [(6, 0, self.credit_reason.ids)],
        })
        credit.write({'signature': SIGNATURE_PNG})
        credit.action_cancel_credit_return()
        credit.action_create_credit_note()

        self.assertTrue(self._logs(credit.id, 'sign'))
        self.assertTrue(self._logs(credit.id, 'save_back'))
        confirm = self._logs(credit.id, 'confirm')
        self.assertEqual(len(confirm), 1)
        self.assertEqual(confirm.move_name, credit.credit_note_name)
        self.assertEqual(confirm.reasons, self.credit_reason.name)
        self.assertEqual(confirm.return_location_name, self.location.display_name)

    def test_a_rep_cannot_change_delete_or_see_other_rows(self):
        rep = self.env['res.users'].create({
            'name': 'Log Test Rep',
            'login': 'log-test-rep@example.com',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('industry_fsm.group_fsm_user').id,
            ])],
        })
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 1)
        row = self._logs(credit.id, 'add')
        self.assertTrue(row)

        with self.assertRaises(AccessError):
            row.with_user(rep).write({'quantity': 99})
        with self.assertRaises(AccessError):
            row.with_user(rep).unlink()
        self.assertFalse(
            self.Log.with_user(rep).search([('id', '=', row.id)]),
            'A rep must not read a row another user wrote.',
        )
        self.env.invalidate_all()
        self.assertTrue(row.exists())
        self.assertEqual(row.quantity, 1)

    def test_even_a_controller_cannot_change_a_row(self):
        # The test environment runs as the superuser, which the log allows,
        # so prove the append-only rule with a real FSM Controller.
        controller = self.env['res.users'].create({
            'name': 'Log Test Controller',
            'login': 'log-test-controller@example.com',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('reza_field_service_buttons.group_fsm_controllers').id,
            ])],
        })
        credit = self._new_credit()
        credit._update_order_line_info(self.product.id, 1)
        row = self._logs(credit.id, 'add')
        self.assertTrue(row.with_user(controller).search([('id', '=', row.id)]))
        with self.assertRaises(AccessError):
            row.with_user(controller).write({'quantity': 5})
        with self.assertRaises(AccessError):
            row.with_user(controller).unlink()
