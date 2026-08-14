# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFSMCreditNoteEmail(TransactionCase):
    """
    Cover the customer email raised for a rep credit note.

    The bug this guards against: the email was created as a bare mail.mail with
    no model/res_id and a From taken from the company partner.  That left no
    record on the credit note, never moved `is_move_sent`, and used an address
    the outgoing mail server rewrites because it is absent from `from_filter`.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.alias_domain = self.env['mail.alias.domain'].create({
            'name': 'fsm-credit-test.example.com',
            'default_from': 'notifications',
            'catchall_alias': 'catchall',
            'bounce_alias': 'bounce',
        })
        self.company.alias_domain_id = self.alias_domain
        self.partner = self.env['res.partner'].create({
            'name': 'FSM Credit Email Customer',
            'email': 'store@fsm-credit-test.example.com',
        })
        self.product = self.env['product.product'].create({
            'name': 'FSM Credit Email Product',
            'type': 'consu',
            'list_price': 10.0,
        })
        self.move = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 10.0,
            })],
        })

    def _sent_mails(self):
        return self.env['mail.mail'].search([
            ('model', '=', 'account.move'),
            ('res_id', '=', self.move.id),
        ])

    def test_email_from_uses_alias_domain_not_company_partner(self):
        """An address the mail server would rewrite must not be used.

        The company partner address is what the old code used, and where it is
        absent from the server's from_filter Odoo silently rewrites it, so the
        alias domain is the honest choice.
        """
        self.company.partner_id.email = 'sales@some-other-brand.example.com'
        email_from = self.move._reza_fsm_credit_note_email_from()
        self.assertIn('notifications@fsm-credit-test.example.com', email_from)
        self.assertNotIn('some-other-brand', email_from)

    def test_email_from_prefers_accounts_address_the_server_may_send_as(self):
        """A rep's credit note comes from Accounts, like the office's does.

        Only when the outgoing server is configured to pass that address
        through: otherwise it is rewritten and the alias is the truthful
        answer, which the test above covers.
        """
        accounts = 'accounts@fsm-credit-test.example.com'
        self.company.partner_id.email = accounts
        self.env['ir.mail_server'].create({
            'name': 'FSM credit test server',
            'smtp_host': 'smtp.fsm-credit-test.example.com',
            'from_filter': (
                'notifications@fsm-credit-test.example.com, %s' % accounts
            ),
        })
        email_from = self.move._reza_fsm_credit_note_email_from()
        self.assertIn(accounts, email_from)
        self.assertNotIn('notifications@', email_from)
        self.assertIn(self.company.name, email_from)

    def test_mail_server_accepts_matches_a_bare_domain_entry(self):
        """A from_filter entry may be a whole domain, not only an address.

        `mail.default.from_filter` holds a bare domain on this database, so a
        matcher that only compared full addresses would answer False for an
        address the server would in fact send unchanged.
        """
        Move = self.env['account.move']
        self.env['ir.mail_server'].create({
            'name': 'FSM credit test domain server',
            'smtp_host': 'smtp.fsm-credit-test.example.com',
            'from_filter': 'fsm-credit-test.example.com',
        })
        self.assertTrue(
            Move._reza_fsm_mail_server_accepts('accounts@fsm-credit-test.example.com')
        )
        self.assertFalse(
            Move._reza_fsm_mail_server_accepts('accounts@somewhere-else.example.com')
        )
        self.assertFalse(Move._reza_fsm_mail_server_accepts(''))
        self.assertFalse(Move._reza_fsm_mail_server_accepts('not-an-address'))

    def test_reply_to_is_a_real_mailbox(self):
        """Replies go to the company mailbox, not Odoo's catchall."""
        self.company.partner_id.email = 'sales@some-other-brand.example.com'
        reply_to = self.move._reza_fsm_credit_note_reply_to()
        self.assertIn('sales@some-other-brand.example.com', reply_to)

    def test_send_records_the_mail_on_the_credit_note(self):
        mail = self.move._reza_fsm_send_credit_note_email()
        self.assertTrue(mail, "the credit note should have been emailed")
        self.assertEqual(mail.model, 'account.move')
        self.assertEqual(mail.res_id, self.move.id)
        self.assertEqual(mail.email_to, 'store@fsm-credit-test.example.com')
        self.assertIn('notifications@fsm-credit-test.example.com', mail.email_from)
        self.assertEqual(self._sent_mails(), mail)

    def test_send_marks_the_move_as_sent(self):
        self.assertFalse(self.move.is_move_sent)
        self.move._reza_fsm_send_credit_note_email()
        self.assertTrue(
            self.move.is_move_sent,
            "is_move_sent must move so the office can tell the customer was emailed",
        )

    def test_no_customer_email_sends_nothing(self):
        self.partner.email = False
        self.assertFalse(self.move._reza_fsm_send_credit_note_email())
        self.assertFalse(self._sent_mails())
        self.assertFalse(self.move.is_move_sent)

    def test_no_alias_domain_sends_nothing(self):
        """Better to send nothing than to send from an address that is rewritten."""
        self.company.alias_domain_id = False
        self.assertFalse(self.move._reza_fsm_send_credit_note_email())
        self.assertFalse(self._sent_mails())

    def test_explicit_address_overrides_the_partner_email(self):
        mail = self.move._reza_fsm_send_credit_note_email(
            email_to='manager@fsm-credit-test.example.com'
        )
        self.assertEqual(mail.email_to, 'manager@fsm-credit-test.example.com')

    def test_body_names_the_credit_note_and_amount(self):
        # A draft move has no name yet, and `assertIn(False, body)` raises a
        # TypeError rather than telling you the body was wrong.  Name it so the
        # assertion tests the body instead of the fixture.
        self.move.name = 'CRTEST/00001'
        body = self.move._reza_fsm_credit_note_email_body()
        self.assertIn(self.move.name, body)
        self.assertIn('%.2f' % self.move.amount_total, body)
        self.assertIn(self.company.name, body)

    def test_attachment_is_reused_not_rendered_twice(self):
        first = self.move._reza_fsm_get_credit_note_attachment()
        if not first:
            self.skipTest("wkhtmltopdf is not available on this build")
        second = self.move._reza_fsm_get_credit_note_attachment()
        self.assertEqual(first, second)
        self.assertEqual(first.res_model, 'account.move')
        self.assertEqual(first.res_id, self.move.id)
