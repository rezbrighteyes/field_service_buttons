# -*- coding: utf-8 -*-
from odoo import models, _
from odoo.exceptions import ValidationError


class MailMessage(models.Model):
    _inherit = 'mail.message'

    def _fsm_task_messages(self):
        task_msgs = self.filtered(lambda m: m.model == 'project.task' and m.res_id)
        if not task_msgs:
            return self.browse()
        task_ids = list({m.res_id for m in task_msgs})
        tasks = self.env['project.task'].browse(task_ids).exists()
        fsm_task_ids = set(
            tasks.filtered(lambda t: bool(t.is_fsm or t.project_id.is_fsm)).ids
        )
        return task_msgs.filtered(lambda m: m.res_id in fsm_task_ids)

    def _check_fsm_message_change_allowed(self):
        if self.env.su:
            return
        if self.env.user.has_group('reza_field_service_buttons.group_fsm_controllers'):
            return
        blocked = self._fsm_task_messages()
        if blocked:
            raise ValidationError(_(
                'You cannot edit or delete chatter messages on Field Service tasks.'
            ))

    def write(self, vals):
        self._check_fsm_message_change_allowed()
        return super().write(vals)

    def unlink(self):
        self._check_fsm_message_change_allowed()
        return super().unlink()
