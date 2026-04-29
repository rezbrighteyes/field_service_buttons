# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ProjectTask(models.Model):
    _inherit = 'project.task'

    def action_create_sale_order(self):
        """Create a new Sale Order pre-filled with customer info from this task."""
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            raise UserError(_('This task has no customer set. Please set a customer first.'))

        sale_order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'partner_invoice_id': partner.id,
            'partner_shipping_id': partner.id,
            'origin': self.name,
            'note': _('Created from Field Service task: %s') % self.name,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Sale Order'),
            'res_model': 'sale.order',
            'res_id': sale_order.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_create_credit_note(self):
        """Create a new Credit Note pre-filled with customer info from this task."""
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            raise UserError(_('This task has no customer set. Please set a customer first.'))

        credit_note = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': partner.id,
            'invoice_origin': self.name,
            'narration': _('Created from Field Service task: %s') % self.name,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Credit Note'),
            'res_model': 'account.move',
            'res_id': credit_note.id,
            'view_mode': 'form',
            'target': 'current',
        }
