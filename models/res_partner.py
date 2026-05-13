# -*- coding: utf-8 -*-
import logging
from odoo import models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _message_update_content(self, message, *, body, attachment_ids=None, **kwargs):
        """
        Block edit/remove on FSM-originated messages in customer chatter.

        Messages posted to res.partner from FSM task flows are tagged with
        x_is_fsm_mirror=True on the mail.message record.  The controller
        sudo()s the message, but self (res.partner) is not sudo'd, so
        self.env.user is the real requesting user.
        """
        self.ensure_one()
        if (
            message.sudo().x_is_fsm_mirror
            and not self.env.su
            and not self.env.user.has_group('reza_field_service_buttons.group_fsm_controllers')
        ):
            _logger.warning(
                "FSM_CHATTER_BLOCK _message_update_content denied on res.partner: "
                "user=%s(id=%s) message_id=%s res_id=%s",
                self.env.user.login,
                self.env.user.id,
                message.id,
                self.id,
            )
            raise ValidationError(_(
                'You cannot edit or delete Field Service customer chatter messages.'
            ))
        return super()._message_update_content(
            message, body=body, attachment_ids=attachment_ids, **kwargs
        )
