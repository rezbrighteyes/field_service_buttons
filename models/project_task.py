# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext
from odoo.tools import format_date
from dateutil.relativedelta import relativedelta
from markupsafe import Markup

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


def _fsm_chatter_plaintext(body):
    if not body:
        return ''
    return (html2plaintext(body) or '').strip()


def _is_fsm_automatic_notification(body):
    return bool(body and 'o_mail_notification' in body)


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
    fsm_next_visit_date = fields.Date(
        string='Next Visit Date',
        copy=False,
        tracking=True,
        help='Next planned customer visit. Defaults from the task schedule plus the repeat interval, and can be manually overridden.',
    )
    fsm_next_visit_date_manual = fields.Boolean(
        string='Next Visit Date Manually Set',
        default=False,
        copy=False,
    )
    fsm_cancellation_reason = fields.Text(
        string='Cancellation Reason',
        copy=False,
        tracking=True,
        help='Required when a Field Service sub-task is cancelled.',
    )
    fsm_customer_activity_summary = fields.Char(
        string='Field Service Reminder',
        compute='_compute_fsm_customer_activity_summary',
        compute_sudo=True,
    )

    def _fsm_has_completed_worksheet(self):
        self.ensure_one()
        if self.fsm_done:
            return True

        worksheet_model_names = [
            model_name
            for model_name in self.env.registry.models
            if model_name.startswith('x_project_task_worksheet_template_')
        ]
        for model_name in worksheet_model_names:
            Worksheet = self.env[model_name].sudo()
            if 'x_project_task_id' not in Worksheet._fields:
                continue
            if Worksheet.search_count([('x_project_task_id', '=', self.id)], limit=1):
                return True
        return False

    def _fsm_format_activity_summary(self, activity, label):
        self.ensure_one()
        today = fields.Date.context_today(self)
        if activity.date_deadline == today:
            due = _('today')
        elif activity.date_deadline == today + relativedelta(days=1):
            due = _('tomorrow')
        elif activity.date_deadline:
            due = format_date(self.env, activity.date_deadline)
        else:
            due = _('no due date')

        summary = activity.summary or activity.activity_type_id.display_name or _('Activity')
        if activity.date_deadline and activity.date_deadline < today:
            label = _('Overdue %(label)s') % {'label': label}
        return _('%(label)s: %(summary)s due %(due)s') % {
            'label': label,
            'summary': summary,
            'due': due,
        }

    @api.depends('partner_id', 'parent_id', 'state', 'fsm_done')
    def _compute_fsm_customer_activity_summary(self):
        Activity = self.env['mail.activity'].sudo()
        partners = self.mapped('partner_id')
        task_activities_by_task = {}
        activities_by_partner = {}
        if self:
            task_activities = Activity.search([
                ('res_model', '=', 'project.task'),
                ('res_id', 'in', self.ids),
            ], order='date_deadline asc, id asc')
            for activity in task_activities:
                task_activities_by_task.setdefault(activity.res_id, Activity)
                task_activities_by_task[activity.res_id] |= activity
        if partners:
            activities = Activity.search([
                ('res_model', '=', 'res.partner'),
                ('res_id', 'in', partners.ids),
            ], order='date_deadline asc, id asc')
            for activity in activities:
                activities_by_partner.setdefault(activity.res_id, Activity)
                activities_by_partner[activity.res_id] |= activity

        for task in self:
            reminders = []

            task_activities = task_activities_by_task.get(task.id, Activity)
            if task_activities:
                reminders.append(task._fsm_format_activity_summary(
                    task_activities[0], _('Task reminder')
                ))

            if task.parent_id:
                if not task._fsm_has_completed_worksheet():
                    reminders.append(_('Complete the worksheet before finishing this visit.'))
                elif task.state not in CLOSED_STATES:
                    reminders.append(_('Mark this sub-task Done when the visit is finished.'))

            if reminders:
                task.fsm_customer_activity_summary = ' '.join(reminders)
                continue

            activities = activities_by_partner.get(task.partner_id.id, Activity)
            if not task.partner_id or not activities:
                task.fsm_customer_activity_summary = False
                continue

            first = activities[0]
            if len(activities) == 1:
                task.fsm_customer_activity_summary = task._fsm_format_activity_summary(
                    first, _('Customer reminder')
                )
            else:
                task.fsm_customer_activity_summary = _(
                    '%(count)s customer reminders. Next: %(reminder)s'
                ) % {
                    'count': len(activities),
                    'reminder': task._fsm_format_activity_summary(first, _('Customer reminder')),
                }

    @api.model
    def _search_display_name(self, operator, value):
        domain = super()._search_display_name(operator, value)
        if not value or operator in expression.NEGATIVE_TERM_OPERATORS:
            return domain

        extra_domains = [
            [(field_name, operator, value)]
            for field_name in (
                'partner_id.name',
                'partner_id.complete_name',
                'partner_id.city',
                'partner_id.street',
                'partner_id.street2',
                'partner_id.ref',
                'partner_id.phone',
                'partner_id.mobile',
                'partner_id.email',
                'parent_id.name',
            )
        ]
        return expression.OR([domain] + extra_domains)

    def _fsm_get_datetime_value(self, field_names):
        self.ensure_one()
        for field_name in field_names:
            if field_name in self._fields and self[field_name]:
                return self[field_name]
        return False

    def _fsm_get_repeat_interval_unit(self):
        self.ensure_one()
        interval = False
        unit = False

        if 'repeat_interval' in self._fields and self.repeat_interval:
            interval = self.repeat_interval
        if 'repeat_unit' in self._fields and self.repeat_unit:
            unit = self.repeat_unit

        recurrence = False
        if 'recurrence_id' in self._fields:
            recurrence = self.recurrence_id
        if recurrence:
            if not interval and 'repeat_interval' in recurrence._fields:
                interval = recurrence.repeat_interval
            if not unit and 'repeat_unit' in recurrence._fields:
                unit = recurrence.repeat_unit

        return int(interval or 6), (unit or 'week')

    def _fsm_get_next_visit_base_date(self):
        self.ensure_one()
        return self._fsm_get_datetime_value((
            'planned_date_end',
            'date_end',
            'date_deadline',
            'planned_date_begin',
        ))

    def _fsm_calculate_next_visit_date(self):
        self.ensure_one()
        base_date = self._fsm_get_next_visit_base_date()
        if not base_date:
            return False

        interval, unit = self._fsm_get_repeat_interval_unit()
        unit = (unit or 'week').lower()
        if unit in ('day', 'days'):
            delta = relativedelta(days=interval)
        elif unit in ('month', 'months'):
            delta = relativedelta(months=interval)
        elif unit in ('year', 'years'):
            delta = relativedelta(years=interval)
        else:
            delta = relativedelta(weeks=interval)
        return fields.Date.to_date(base_date + delta)

    def _fsm_sync_next_visit_date(self):
        for task in self:
            if task.fsm_next_visit_date_manual:
                continue
            next_visit_date = task._fsm_calculate_next_visit_date()
            if next_visit_date and task.fsm_next_visit_date != next_visit_date:
                task.with_context(fsm_sync_next_visit_date=True).sudo().write({
                    'fsm_next_visit_date': next_visit_date,
                })

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
                        subtype_xmlid='mail.mt_note',
                    )
                # Also leave an audit note on the customer chatter when available.
                if parent.partner_id:
                    _m = parent.partner_id.with_user(SUPERUSER_ID).sudo().message_post(
                        body=Markup(
                            _(
                                'Field Service run <b>{task}</b> completed with {canceled} of '
                                '{total} sub-tasks canceled (over 50% canceled).'
                            )
                        ).format(
                            task=parent.display_name,
                            canceled=canceled_count,
                            total=total,
                        ),
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    if _m:
                        _m.write({'x_is_fsm_mirror': True})

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('fsm_next_visit_date') and 'fsm_next_visit_date_manual' not in vals:
                vals['fsm_next_visit_date_manual'] = True
        tasks = super().create(vals_list)
        for task in tasks:
            task._fsm_sync_next_visit_date()
            task._update_parent_state()
        return tasks

    def write(self, vals):
        if 'fsm_next_visit_date' in vals and not self.env.context.get('fsm_sync_next_visit_date'):
            vals = dict(vals, fsm_next_visit_date_manual=True)

        if vals.get('state') == '1_canceled':
            for task in self:
                if not task.parent_id or task.state == '1_canceled':
                    continue
                reason = vals.get('fsm_cancellation_reason') or task.fsm_cancellation_reason
                if not (reason or '').strip():
                    raise ValidationError(_(
                        'Please enter a cancellation reason before cancelling this sub-task.'
                    ))

        if 'fsm_cancellation_reason' in vals and not self.env.su:
            can_manage_cancel_reason = self.env.user.has_group(
                'reza_field_service_buttons.group_fsm_controllers'
            )
            for task in self:
                same_write_is_cancelling = vals.get('state') == '1_canceled' and task.state != '1_canceled'
                if task.parent_id and task.state == '1_canceled' and not same_write_is_cancelling and not can_manage_cancel_reason:
                    raise ValidationError(_(
                        'You cannot change the cancellation reason after this sub-task has been cancelled.'
                    ))

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
                _m = task.partner_id.message_post(
                    body=Markup(
                        _('Worksheet completed for Field Service task <b>{task}</b>.')
                    ).format(task=task.display_name),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                if _m:
                    _m.sudo().write({'x_is_fsm_mirror': True})
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
                reason = (task.fsm_cancellation_reason or '').strip()
                parent_body = _('Sub-task "%s" was marked %s by %s.') % (
                    task.display_name, state_label, rep_name
                )
                customer_body = parent_body
                if vals.get('state') == '1_canceled' and reason:
                    parent_body = _(
                        'Sub-task "%s" was marked Cancelled by %s. Reason: %s'
                    ) % (task.display_name, rep_name, reason)
                    customer_body = parent_body
                task.parent_id.message_post(
                    body=parent_body,
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                customer = task.partner_id or task.parent_id.partner_id
                if customer:
                    _m = customer.with_user(SUPERUSER_ID).sudo().message_post(
                        body=customer_body,
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    if _m:
                        _m.write({'x_is_fsm_mirror': True})

        # After saving, recompute parent state for any affected sub-task
        if {
            'planned_date_begin',
            'planned_date_end',
            'date_begin',
            'date_end',
            'date_deadline',
            'repeat_interval',
            'repeat_unit',
            'recurrence_id',
        } & set(vals.keys()):
            self._fsm_sync_next_visit_date()

        if changed_state_fields:
            for task in self:
                task._update_parent_state()
        return result

    def unlink(self):
        blocked_tasks = self.filtered(
            lambda task: task.is_fsm or (task.project_id and task.project_id.is_fsm)
        )
        if blocked_tasks and not self.env.su and not self.env.user.has_group(
            'reza_field_service_buttons.group_fsm_controllers'
        ):
            raise ValidationError(_(
                'Only FSM Controllers can delete Field Service tasks or runs.'
            ))
        return super().unlink()

    def _message_update_content(self, message, *, body, attachment_ids=None, **kwargs):
        """
        Block FSM task chatter edits and content-removal for non-controllers.

        The /mail/message/update_content HTTP controller does `message = message.sudo()`
        before calling this method, so message.env.su is True and our write() guard in
        MailMessage is bypassed.  The thread (self) is NOT sudo'd — self.env.user is the
        real requesting user — so we can enforce the check here.

        Both "edit message" (body != '') and "remove content" / "This message has been
        removed" (body == '') flow through this method.
        """
        self.ensure_one()
        if self.is_fsm or (self.project_id and self.project_id.is_fsm):
            if not self.env.su and not self.env.user.has_group(
                'reza_field_service_buttons.group_fsm_controllers'
            ):
                _logger.warning(
                    "FSM_CHATTER_BLOCK _message_update_content denied: "
                    "user=%s(id=%s) message_id=%s model=project.task res_id=%s",
                    self.env.user.login,
                    self.env.user.id,
                    message.id,
                    self.id,
                )
                raise ValidationError(_(
                    'You cannot edit or delete chatter messages on Field Service tasks.'
                ))
        return super()._message_update_content(
            message, body=body, attachment_ids=attachment_ids, **kwargs
        )

    def message_post(self, **kwargs):
        message = super().message_post(**kwargs)
        if len(self) != 1:
            return message

        task = self
        if not task.parent_id:
            return message

        body = kwargs.get('body')
        if not body:
            return message
        if _is_fsm_automatic_notification(body):
            return message

        subtype_xmlid = kwargs.get('subtype_xmlid') or 'mail.mt_note'
        if subtype_xmlid not in ('mail.mt_note', 'mail.mt_comment'):
            return message

        customer = task.partner_id or task.parent_id.partner_id
        if not customer:
            return message

        rep_name = self.env.user.name
        clean_body = _fsm_chatter_plaintext(body)
        if not clean_body:
            return message
        _m = customer.with_user(SUPERUSER_ID).sudo().message_post(
            body=Markup(
                _('Rep update from {rep} on sub-task "{task}": {body}')
            ).format(
                rep=rep_name,
                task=task.display_name,
                body=clean_body,
            ),
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        if _m:
            _m.write({'x_is_fsm_mirror': True})
        return message
