# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

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

    # Restore the three selection values that were deleted from ir.model.fields.selection
    # by a previous upgrade that overrode state with a 3-item list.
    # selection_add re-inserts them; ondelete guards records if this module is later removed.
    state = fields.Selection(
        selection_add=[
            ('02_changes_requested', 'Changes Requested'),
            ('03_approved', 'Approved'),
            ('04_waiting_normal', 'Waiting'),
        ],
        ondelete={
            '02_changes_requested': 'set default',
            '03_approved': 'set default',
            '04_waiting_normal': 'set default',
        },
    )
    worksheet_customer_notified = fields.Boolean(
        string='Worksheet Completion Notified to Customer',
        default=False,
        copy=False,
    )

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
            parent.with_context(
                allow_fsm_parent_status_auto=True,
                fsm_status_source='subtask_rollup',
            ).sudo().write(changed)

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
                    parent.with_user(SUPERUSER_ID).sudo().message_post(
                        body=_(
                            'Run completed, but %d of %d sub-tasks were cancelled. '
                            'Please review this run.'
                        ) % (canceled_count, total),
                        partner_ids=partner_ids,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                    )
                # Also leave an audit note on the customer chatter when available.
                if parent.partner_id:
                    parent.partner_id.with_user(SUPERUSER_ID).sudo().message_post(
                        body=_(
                            'Field Service run <b>%s</b> completed with %d of %d sub-tasks canceled '
                            '(over 50%% canceled).'
                        ) % (parent.display_name, canceled_count, total),
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
        previous_fsm_done = {}
        if 'fsm_done' in vals and vals.get('fsm_done'):
            previous_fsm_done = {task.id: bool(task.fsm_done) for task in self}
        previous_state = {}
        if 'state' in vals:
            previous_state = {task.id: task.state for task in self}

        state_fields = {'state', 'stage_id', 'kanban_state'}
        changed_state_fields = sorted(state_fields & set(vals.keys()))
        if changed_state_fields:
            _logger.info(
                "FSM_GUARD write called user=%s(%s) su=%s task_ids=%s fields=%s vals=%s context=%s",
                self.env.user.login,
                self.env.user.id,
                self.env.su,
                self.ids,
                changed_state_fields,
                vals,
                dict(self.env.context),
            )
            child_cmds = vals.get('child_ids') if isinstance(vals, dict) else False
            is_inline_subtask_update = bool(
                child_cmds
                and isinstance(child_cmds, list)
                and all(
                    isinstance(cmd, (list, tuple))
                    and len(cmd) >= 3
                    and cmd[0] == 1
                    and isinstance(cmd[2], dict)
                    for cmd in child_cmds
                )
            )
            for task in self:
                bypass_auto = bool(self.env.context.get('allow_fsm_parent_status_auto'))
                all_subtasks_closed = bool(task.child_ids) and all(
                    child.state in CLOSED_STATES for child in task.child_ids
                )
                requested_state = vals.get('state') if isinstance(vals, dict) else False
                valid_parent_finalize = bool(
                    all_subtasks_closed and requested_state in CLOSED_STATES
                )
                should_block = bool(
                    task.is_fsm
                    and task.child_ids
                    and not bypass_auto
                    and not is_inline_subtask_update
                    and not valid_parent_finalize
                )

                _logger.info(
                    "FSM_GUARD evaluate task_id=%s is_fsm=%s child_count=%s all_subtasks_closed=%s "
                    "inline_subtask_update=%s bypass_auto=%s valid_parent_finalize=%s -> block=%s",
                    task.id,
                    task.is_fsm,
                    len(task.child_ids),
                    all_subtasks_closed,
                    is_inline_subtask_update,
                    bypass_auto,
                    valid_parent_finalize,
                    should_block,
                )

                if should_block:
                    raise ValidationError(_(
                        'You cannot manually change the status or stage of a parent field service task. '
                        'It updates automatically from sub-task completion.'
                    ))
        result = super().write(vals)

        # Post completion to customer chatter once per task.
        # Trigger on worksheet completion or state transition to done.
        if ('fsm_done' in vals and vals.get('fsm_done')) or ('state' in vals and vals.get('state') == '1_done'):
            for task in self:
                was_done = previous_fsm_done.get(task.id, False)
                became_done_from_worksheet = ('fsm_done' in vals and vals.get('fsm_done') and not was_done)
                old_state = previous_state.get(task.id)
                became_done_from_state = ('state' in vals and vals.get('state') == '1_done' and old_state != '1_done')

                if not became_done_from_worksheet and not became_done_from_state:
                    continue
                if task.worksheet_customer_notified:
                    continue
                if not task.partner_id:
                    continue
                task.partner_id.message_post(
                    body=_(
                        'Worksheet completed for Field Service task <b>%s</b>.'
                    ) % (task.display_name,),
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
                task.sudo().with_context(mail_notrack=True).write({
                    'worksheet_customer_notified': True,
                })

        # Post rep completion/cancellation note on parent chatter for subtask updates.
        if 'state' in vals:
            for task in self:
                old_state = previous_state.get(task.id)
                if not task.parent_id:
                    continue
                if vals.get('state') not in CLOSED_STATES:
                    continue
                if old_state == vals.get('state'):
                    continue
                state_label = 'Done' if vals.get('state') == '1_done' else 'Cancelled'
                rep_name = self.env.user.name
                task.parent_id.message_post(
                    body=_(
                        'Sub-task "%s" was marked %s by %s.'
                    ) % (task.display_name, state_label, rep_name),
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )

        # After saving, recompute parent state for any affected sub-task
        if changed_state_fields:
            for task in self:
                task._update_parent_state()
        return result
