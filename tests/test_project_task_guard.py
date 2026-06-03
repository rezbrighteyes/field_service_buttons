# -*- coding: utf-8 -*-
from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProjectTaskStatusGuard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Project = self.env['project.project']
        self.Task = self.env['project.task']
        self.Partner = self.env['res.partner']

        project_user_group = self.env.ref('project.group_project_user')
        self.manager_group = self.env.ref('project.group_project_manager')
        self.fsm_controllers_group = self.env.ref(
            'reza_field_service_buttons.group_fsm_controllers'
        )

        self.non_manager = self.env['res.users'].create({
            'name': 'FSM Non Manager',
            'login': 'fsm_non_manager_test',
            'email': 'fsm_non_manager_test@example.com',
            'groups_id': [(6, 0, [project_user_group.id])],
        })
        self.controller = self.env['res.users'].create({
            'name': 'FSM Controller',
            'login': 'fsm_controller_reason_test',
            'email': 'fsm_controller_reason_test@example.com',
            'groups_id': [(6, 0, [self.fsm_controllers_group.id])],
        })

        self.project = self.Project.create({
            'name': 'FSM Guard Project',
            'is_fsm': True,
        })
        self.partner = self.Partner.create({'name': 'FSM Guard Customer'})

        self.parent = self.Task.create({
            'name': 'Parent Run',
            'project_id': self.project.id,
            'partner_id': self.partner.id,
        })
        self.child = self.Task.create({
            'name': 'Child Run',
            'project_id': self.project.id,
            'parent_id': self.parent.id,
            'partner_id': self.partner.id,
        })

    def test_non_manager_blocked_on_parent_manual_state_change(self):
        with self.assertRaises(ValidationError):
            self.parent.with_user(self.non_manager).write({'state': '1_done'})

    def test_manager_blocked_on_parent_manual_state_change(self):
        manager = self.env['res.users'].create({
            'name': 'FSM Manager',
            'login': 'fsm_manager_test',
            'email': 'fsm_manager_test@example.com',
            'groups_id': [(6, 0, [self.manager_group.id])],
        })
        with self.assertRaises(ValidationError):
            self.parent.with_user(manager).write({'state': '1_done'})

    def test_auto_update_flow_allowed_for_parent(self):
        self.child.with_user(self.non_manager).write({'state': '1_done'})
        self.parent.invalidate_recordset(['state'])
        self.assertEqual(self.parent.state, '1_done')

    def test_leaf_task_unchanged_for_non_manager(self):
        leaf = self.Task.create({
            'name': 'Leaf Task',
            'project_id': self.project.id,
        })
        leaf.with_user(self.non_manager).write({'state': '1_done'})
        self.assertEqual(leaf.state, '1_done')

    def test_next_visit_date_defaults_from_deadline_plus_six_weeks(self):
        task = self.Task.create({
            'name': 'Next Visit Task',
            'project_id': self.project.id,
            'date_deadline': '2026-05-29',
        })
        self.assertEqual(task.fsm_next_visit_date, date(2026, 7, 10))

    def test_next_visit_date_manual_override_is_preserved(self):
        task = self.Task.create({
            'name': 'Manual Next Visit Task',
            'project_id': self.project.id,
            'date_deadline': '2026-05-29',
        })
        task.write({'fsm_next_visit_date': '2026-07-17'})
        task.write({'date_deadline': '2026-06-05'})
        self.assertEqual(task.fsm_next_visit_date, date(2026, 7, 17))

    def test_subtask_search_matches_customer_details(self):
        customer = self.Partner.create({
            'name': 'Liberty Service Station',
            'city': 'Margate',
            'phone': '0730001111',
        })
        subtask = self.Task.create({
            'name': 'North Run Visit',
            'project_id': self.project.id,
            'parent_id': self.parent.id,
            'partner_id': customer.id,
        })

        for search_term in ('Margate', 'Liberty', '0730001111'):
            matches = self.Task.search([
                ('parent_id', '=', self.parent.id),
                ('display_name', 'ilike', search_term),
            ])
            self.assertIn(subtask, matches)

        name_search_matches = self.Task.name_search(
            'Margate',
            args=[('parent_id', '=', self.parent.id)],
            limit=10,
        )
        self.assertIn(subtask.id, [task_id for task_id, _name in name_search_matches])

    def test_cancelling_subtask_requires_reason(self):
        with self.assertRaises(ValidationError):
            self.child.with_user(self.non_manager).write({'state': '1_canceled'})

    def test_cancellation_reason_posts_to_parent_and_customer_chatter(self):
        self.child.with_user(self.non_manager).write({
            'fsm_cancellation_reason': 'car broke down',
            'state': '1_canceled',
        })
        parent_message = self.parent.message_ids.filtered(
            lambda message: 'Reason: car broke down' in (message.body or '')
        )
        customer_message = self.partner.message_ids.filtered(
            lambda message: 'Reason: car broke down' in (message.body or '')
        )
        self.assertTrue(parent_message)
        self.assertTrue(customer_message)

    def test_cancelled_subtask_reason_edit_is_controller_only(self):
        self.child.with_user(self.non_manager).write({
            'fsm_cancellation_reason': 'car broke down',
            'state': '1_canceled',
        })
        with self.assertRaises(ValidationError):
            self.child.with_user(self.non_manager).write({
                'fsm_cancellation_reason': 'changed later',
            })
        self.child.with_user(self.controller).write({
            'fsm_cancellation_reason': 'manager corrected reason',
        })
        self.assertEqual(self.child.fsm_cancellation_reason, 'manager corrected reason')

    def test_non_controller_cannot_delete_fsm_run_or_subtask(self):
        with self.assertRaises(ValidationError):
            self.parent.with_user(self.non_manager).unlink()
        with self.assertRaises(ValidationError):
            self.child.with_user(self.non_manager).unlink()

    def test_project_manager_cannot_delete_fsm_run(self):
        manager = self.env['res.users'].create({
            'name': 'FSM Delete Project Manager',
            'login': 'fsm_delete_project_manager_test',
            'email': 'fsm_delete_project_manager_test@example.com',
            'groups_id': [(6, 0, [self.manager_group.id])],
        })
        with self.assertRaises(ValidationError):
            self.parent.with_user(manager).unlink()

    def test_controller_can_delete_fsm_run(self):
        run = self.Task.create({
            'name': 'Controller Delete Run',
            'project_id': self.project.id,
        })
        run.with_user(self.controller).unlink()
        self.assertFalse(run.exists())
