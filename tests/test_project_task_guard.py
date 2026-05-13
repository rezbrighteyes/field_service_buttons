# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProjectTaskStatusGuard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Project = self.env['project.project']
        self.Task = self.env['project.task']

        project_user_group = self.env.ref('project.group_project_user')
        self.manager_group = self.env.ref('project.group_project_manager')

        self.non_manager = self.env['res.users'].create({
            'name': 'FSM Non Manager',
            'login': 'fsm_non_manager_test',
            'email': 'fsm_non_manager_test@example.com',
            'groups_id': [(6, 0, [project_user_group.id])],
        })

        self.project = self.Project.create({
            'name': 'FSM Guard Project',
            'is_fsm': True,
        })

        self.parent = self.Task.create({
            'name': 'Parent Run',
            'project_id': self.project.id,
        })
        self.child = self.Task.create({
            'name': 'Child Run',
            'project_id': self.project.id,
            'parent_id': self.parent.id,
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
