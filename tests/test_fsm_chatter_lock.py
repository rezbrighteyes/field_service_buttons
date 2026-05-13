# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFSMChatterLock(TransactionCase):
    """
    Verify that FSM task chatter is hard-locked against mutation by non-controllers.

    Three mutation paths are tested:
      - write()              direct ORM write on mail.message (admin panel, API)
      - unlink()             direct ORM delete on mail.message
      - _message_update_content()  web-client path (/mail/message/update_content),
                             where the controller sudo()s the message but NOT the thread
    """

    def setUp(self):
        super().setUp()
        fsm_controllers_group = self.env.ref(
            'reza_field_service_buttons.group_fsm_controllers'
        )
        project_user_group = self.env.ref('project.group_project_user')

        self.non_controller = self.env['res.users'].create({
            'name': 'FSM Non-Controller',
            'login': 'fsm_non_ctrl_chatter_test',
            'email': 'fsm_non_ctrl_chatter@example.com',
            'groups_id': [(6, 0, [project_user_group.id])],
        })
        self.controller = self.env['res.users'].create({
            'name': 'FSM Controller',
            'login': 'fsm_ctrl_chatter_test',
            'email': 'fsm_ctrl_chatter@example.com',
            'groups_id': [(6, 0, [fsm_controllers_group.id])],
        })

        self.project = self.env['project.project'].create({
            'name': 'FSM Chatter Lock Project',
            'is_fsm': True,
        })
        self.task = self.env['project.task'].create({
            'name': 'FSM Chatter Lock Task',
            'project_id': self.project.id,
        })
        # Post a comment as controller so it's an editable message type
        self.message = self.task.with_user(self.controller).message_post(
            body='Original message body.',
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

        # Non-FSM task for confirming non-FSM messages are not locked
        self.non_fsm_project = self.env['project.project'].create({
            'name': 'Regular Project',
            'is_fsm': False,
        })
        self.non_fsm_task = self.env['project.task'].create({
            'name': 'Regular Task',
            'project_id': self.non_fsm_project.id,
        })
        self.non_fsm_message = self.non_fsm_task.with_user(self.non_controller).message_post(
            body='Non-FSM message.',
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        self.partner = self.env['res.partner'].create({'name': 'FSM Customer'})
        self.customer_audit_message = self.partner.with_user(self.controller).message_post(
            body='<!--fsm_audit_mirror-->Rep update from Test Rep on sub-task "X": hi',
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

    # ── write() path (direct ORM) ─────────────────────────────────────────

    def test_non_controller_cannot_write_fsm_message(self):
        with self.assertRaises(ValidationError):
            self.message.with_user(self.non_controller).write({'body': '<p>hacked</p>'})

    def test_controller_can_write_fsm_message(self):
        self.message.with_user(self.controller).write({'body': '<p>controller edit</p>'})
        self.assertEqual(self.message.body, '<p>controller edit</p>')

    def test_superuser_can_write_fsm_message(self):
        self.message.sudo().write({'body': '<p>su edit</p>'})
        self.assertEqual(self.message.body, '<p>su edit</p>')

    def test_non_fsm_message_write_not_blocked(self):
        """Non-FSM messages must not be affected by our guard."""
        self.non_fsm_message.with_user(self.non_controller).write({'body': '<p>edited</p>'})
        self.assertEqual(self.non_fsm_message.body, '<p>edited</p>')

    # ── unlink() path (direct ORM) ────────────────────────────────────────

    def test_non_controller_cannot_unlink_fsm_message(self):
        with self.assertRaises(ValidationError):
            self.message.with_user(self.non_controller).unlink()

    def test_controller_can_unlink_fsm_message(self):
        msg_id = self.message.id
        self.message.with_user(self.controller).unlink()
        self.assertFalse(self.env['mail.message'].browse(msg_id).exists())

    def test_superuser_can_unlink_fsm_message(self):
        extra = self.task.with_user(self.controller).message_post(
            body='To be deleted by su.',
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        extra_id = extra.id
        extra.sudo().unlink()
        self.assertFalse(self.env['mail.message'].browse(extra_id).exists())

    # ── _message_update_content() path (web-client RPC) ──────────────────
    # The controller sudo()s the message before calling this; the thread is not sudo'd.
    # Our override on project.task fires with the real user in env.

    def test_non_controller_cannot_edit_via_update_content(self):
        """Edit message body via the RPC path must be blocked."""
        with self.assertRaises(ValidationError):
            self.task.with_user(self.non_controller)._message_update_content(
                self.message, body='<p>edited body</p>'
            )

    def test_non_controller_cannot_remove_via_update_content(self):
        """Empty-body 'remove content' (This message has been removed) must be blocked."""
        with self.assertRaises(ValidationError):
            self.task.with_user(self.non_controller)._message_update_content(
                self.message, body=''
            )

    def test_controller_can_edit_via_update_content(self):
        self.task.with_user(self.controller)._message_update_content(
            self.message, body='<p>controller edited</p>'
        )

    def test_superuser_can_edit_via_update_content(self):
        self.task.sudo()._message_update_content(
            self.message, body='<p>su edited</p>'
        )

    def test_non_fsm_task_update_content_not_blocked(self):
        """_message_update_content on a non-FSM task must not be affected."""
        self.non_fsm_task.with_user(self.non_controller)._message_update_content(
            self.non_fsm_message, body='<p>regular edit</p>'
        )

    def test_non_controller_cannot_edit_customer_audit_message_via_update_content(self):
        with self.assertRaises(ValidationError):
            self.partner.with_user(self.non_controller)._message_update_content(
                self.customer_audit_message, body='edited'
            )

    def test_controller_can_edit_customer_audit_message_via_update_content(self):
        self.partner.with_user(self.controller)._message_update_content(
            self.customer_audit_message, body='edited by controller'
        )
