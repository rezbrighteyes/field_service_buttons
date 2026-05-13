# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class ProjectTask(models.Model):
    _inherit = 'project.task'

    # ─────────────────────────────────────────────
    # Existing buttons (unchanged)
    # ─────────────────────────────────────────────

    def action_create_sale_order(self):
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            raise UserError(_('This task has no customer set. Please set a customer first.'))
        ctx = {k: v for k, v in self.env.context.items() if k != 'default_tag_ids'}
        sale_order = self.env['sale.order'].with_context(ctx).create({
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

    # ─────────────────────────────────────────────
    # Auto-update parent state when sub-task changes
    # ─────────────────────────────────────────────

    def _update_parent_state(self):
        """Recompute parent task state based on all sub-task states."""
        parent = self.parent_id
        if not parent:
            return

        subtasks = parent.child_ids
        if not subtasks:
            return

        total = len(subtasks)
        canceled = subtasks.filtered(lambda t: t.state == '1_canceled')
        done = subtasks.filtered(lambda t: t.state == '1_done')
        closed = canceled | done
        canceled_count = len(canceled)

        # Determine new parent state
        if len(closed) < total:
            new_state = '01_in_progress'
        elif canceled_count == total:
            new_state = '1_canceled'
        else:
            new_state = '1_done'

        # Only write if state actually changed (avoids recursion)
        if parent.state != new_state:
            parent.sudo().write({'state': new_state})

            # If more than half are canceled but we're marking Done, notify managers
            if new_state == '1_done' and canceled_count > total / 2:
                group = self.env.ref('project.group_project_manager')
                self.env.cr.execute("""
                    SELECT u.partner_id
                    FROM res_users u
                    JOIN res_groups_users_rel r ON r.uid = u.id
                    WHERE r.gid = %s AND u.active = true
                """, [group.id])
                partner_ids = [row[0] for row in self.env.cr.fetchall()]
                if partner_ids:
                    parent.message_post(
                        body=_(
                            '⚠️ Run completed but <b>%d of %d</b> sub-tasks were canceled. '
                            'Please review this run.'
                        ) % (canceled_count, total),
                        partner_ids=partner_ids,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                    )

    @api.model_create_multi
    def create(self, vals_list):
        tasks = super().create(vals_list)
        for task in tasks:
            task._update_parent_state()
        return tasks

    def write(self, vals):
        state_fields = {'state', 'stage_id', 'kanban_state'}
        if state_fields & vals.keys():
            for task in self:
                # Block manual state change on parent tasks for non-managers
                if task.child_ids:
                    is_manager = self.env.user.has_group('project.group_project_manager')
                    if not is_manager:
                        raise UserError(_(
                            'You cannot change the status of a run task manually. '
                            'It will update automatically when all sub-tasks are completed.'
                        ))
        result = super().write(vals)
        # After saving, recompute parent state for any affected sub-task
        if state_fields & vals.keys():
            for task in self:
                task._update_parent_state()
        return result
