import json

from odoo.tests.common import TransactionCase


class TestRepOperationDashboard(TransactionCase):
    def test_chart_drilldown_menus_have_the_rep_operations_action(self):
        dashboard = self.env.ref(
            "reza_field_service_buttons.dashboard_reza_fsm_rep_operations"
        )
        menu_references = json.loads(dashboard.spreadsheet_data).get(
            "chartOdooMenusReferences", {}
        )
        expected_menu_xmlid = (
            "reza_field_service_buttons.menu_reza_fsm_rep_operation_report"
        )
        expected_action = self.env.ref(
            "reza_field_service_buttons.action_reza_fsm_rep_operation_report"
        )

        self.assertEqual(len(menu_references), 4)
        self.assertNotIn("industry_fsm.fsm_menu_reporting", menu_references.values())
        for menu_xmlid in menu_references.values():
            self.assertEqual(menu_xmlid, expected_menu_xmlid)
            self.assertEqual(self.env.ref(menu_xmlid).action, expected_action)
