# -*- coding: utf-8 -*-
import logging
from odoo import models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class MailMessage(models.Model):
    _inherit = 'mail.message'

    def _fsm_task_messages(self):
        """Return the subset of self that are messages on FSM project tasks."""
        task_msgs = self.filtered(lambda m: m.model == 'project.task' and m.res_id)
        if not task_msgs:
            return self.browse()
        task_ids = list({m.res_id for m in task_msgs})
        tasks = self.env['project.task'].browse(task_ids).exists()
        fsm_task_ids = set(
            tasks.filtered(lambda t: bool(t.is_fsm or t.project_id.is_fsm)).ids
        )
        return task_msgs.filtered(lambda m: m.res_id in fsm_task_ids)

    def _fsm_customer_audit_messages(self):
        """Return mirrored FSM customer-audit messages on res.partner chatter."""
        partner_msgs = self.filtered(lambda m: m.model == 'res.partner' and m.body)
        if not partner_msgs:
            return self.browse()
        return partner_msgs.filtered(lambda m: '<!--fsm_audit_mirror-->' in (m.body or ''))

    def _check_fsm_chatter_mutation(self, method_name):
        """
        Raise ValidationError if the current user cannot mutate FSM task chatter.
        Allowed: superuser (env.su) or group_fsm_controllers.
        Logs all denied attempts with user/message/model context.

        NOTE: This guards the direct ORM path (admin panel, XML-RPC, scripted writes).
        The web-client /mail/message/update_content path is blocked earlier, at
        ProjectTask._message_update_content, because the controller sudos the message
        before writing and env.su would bypass this check.
        """
        if self.env.su:
            return
        if self.env.user.has_group('reza_field_service_buttons.group_fsm_controllers'):
            return
        blocked = self._fsm_task_messages() | self._fsm_customer_audit_messages()
        if blocked:
            for msg in blocked:
                _logger.warning(
                    "FSM_CHATTER_BLOCK method=%s denied: "
                    "user=%s(id=%s) message_id=%s model=%s res_id=%s",
                    method_name,
                    self.env.user.login,
                    self.env.user.id,
                    msg.id,
                    msg.model,
                    msg.res_id,
                )
            raise ValidationError(_(
                'You cannot edit or delete chatter messages on Field Service tasks.'
            ))

    def write(self, vals):
        self._check_fsm_chatter_mutation('write')
        return super().write(vals)

    def unlink(self):
        self._check_fsm_chatter_mutation('unlink')
        return super().unlink()
