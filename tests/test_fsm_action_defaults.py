from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval


class TestFSMActionDefaults(TransactionCase):
    def test_cleanup_removes_date_defaults_only(self):
        action = self.env['ir.actions.act_window'].create({
            'name': 'FSM My Tasks',
            'res_model': 'project.task',
            'view_mode': 'list,form',
            'context': (
                "{'search_default_my_tasks': 1, "
                "'search_default_today_or_future': 1, "
                "'default_user_ids': [(4, uid)]}"
            ),
        })

        cleaned_count = self.env['project.task']._reza_cleanup_fsm_date_search_defaults()
        action.invalidate_recordset(['context'])

        self.assertGreaterEqual(cleaned_count, 1)
        self.assertIn('search_default_my_tasks', action.context)
        self.assertNotIn('search_default_today_or_future', action.context)
        self.assertIn('uid', action.context)
        context = safe_eval(action.context, {'uid': self.env.uid})
        self.assertEqual(context['search_default_my_tasks'], 1)
        self.assertEqual(context['default_user_ids'], [(4, self.env.uid)])
