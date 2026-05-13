# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError

# Field Service project stage IDs (project.task.type)
# 11 = In Progress, 12 = Done, 13 = Cancelled
FSM_STAGE_IN_PROGRESS = 11
FSM_STAGE_DONE = 12
FSM_STAGE_CANCELLED = 13

STATE_TO_STAGE = {
    '01_in_progress': FSM_STAGE_IN_PROGRESS,
    '1_done':         FSM_STAGE_DONE,
    '1_canceled':     FSM_STAGE_CANCELLED,
}

CLOSED_STATES = {'1_done', '1_canceled'}


class ProjectTask(models.Model):
    _inherit = 'project.task'

    # Computed proxy field used in the FSM form view.
    # The base widget (project_task_state_selection) hardcodes all 6 state options
    # regardless of what the server sends, so we expose a separate field whose
    # selection metadata contains only the 3 allowed FSM states.  The base
    # state_selection widget reads its option list from the server field metadata,
    # so it will show exactly these 3 items.
    fsm_state = fields.Selection(
        selection=[
            ('01_in_progress', 'In Progress'),
            ('1_canceled',     'Cancelled'),
            ('1_done',         'Done'),
        ],
        string='Status',
        compute='_compute_fsm_state',
        inverse='_set_fsm_state',
    )

    _VALID_FSM_STATES = frozenset(['01_in_progress', '1_canceled', '1_done'])

    @api.depends('state')
    def _compute_fsm_state(self):
        for task in self:
            task.fsm_state = task.state if task.state in self._VALID_FSM_STATES else '01_in_progress'

    def _set_fsm_state(self):
        for task in self:
            task.state = task.fsm_state

    # ─────────────────────────────────────────────
    # Override _compute_state to respect subtask completion
    # ─────────────────────────────────────────────

    @api.depends('stage_id', 'depend_on_ids.state', 'child_ids.state')
    def _compute_state(self):
        for task in self:
            # If this is a parent task and all subtasks are closed, determine state from subtasks
            if task.child_ids:
                subtasks = task.child_ids
                total = len(subtasks)
                closed = subtasks.filtered(lambda t: t.state in CLOSED_STATES)
                if len(closed) == total:
                    canceled = subtasks.filtered(lambda t: t.state == '1_canceled')
                    if len(canceled) == total:
                        task.state = '1_canceled'
                    else:
                        task.state = '1_done'
                    continue
                # Not all closed — fall through to in_progress
                task.state = '01_in_progress'
                continue

            # Original Odoo logic for tasks without subtasks
            dependent_open_tasks = []
            if task.allow_task_dependencies:
                dependent_open_tasks = [
                    t for t in task.depend_on_ids if t.state not in CLOSED_STATES
                ]
            if dependent_open_tasks:
                if task.state not in CLOSED_STATES:
                    task.state = '04_waiting_normal'
            elif task.state not in CLOSED_STATES:
                task.state = '01_in_progress'

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
    # Auto-update parent state + stage when sub-task changes
    # ─────────────────────────────────────────────

    def _update_parent_state(self):
        """Recompute parent task state and stage based on all sub-task states."""
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

        if len(closed) < total:
            new_state = '01_in_progress'
        elif canceled_count == total:
            new_state = '1_canceled'
        else:
            new_state = '1_done'

        new_stage_id = STATE_TO_STAGE.get(new_state)

        # Only write if something actually changed
        changed = {}
        if parent.state != new_state:
            changed['state'] = new_state
        if new_stage_id and parent.stage_id.id != new_stage_id:
            changed['stage_id'] = new_stage_id

        if changed:
            parent.sudo().write(changed)

            # Notify managers if >50% sub-tasks canceled but run is Done
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
                # Block manual state/stage change on parent tasks for non-managers
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
