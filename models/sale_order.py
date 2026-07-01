# -*- coding: utf-8 -*-
from odoo import _, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_confirm(self):
        blocking_action = self._reza_fsm_block_rep_main_warehouse_confirm()
        if blocking_action:
            return blocking_action
        return super().action_confirm()

    def _reza_fsm_block_rep_main_warehouse_confirm(self):
        for order in self:
            if order._reza_fsm_user_can_confirm_main_warehouse():
                continue
            if not order._reza_fsm_is_main_warehouse_supply_order():
                continue
            order.message_post(body=_(
                "Main warehouse confirmation blocked: field reps must leave "
                "LIA/WH and other main warehouse orders for office review."
            ))
            return order._reza_fsm_main_warehouse_block_warning()
        return False

    def _reza_fsm_user_can_confirm_main_warehouse(self):
        user = self.env.user
        if not user.has_group("reza_field_service_buttons.group_liaise_field_rep"):
            return True
        office_groups = (
            "base.group_system",
            "sales_team.group_sale_manager",
            "reza_field_service_buttons.group_fsm_controllers",
            "reza_intercompany_warehouse.group_intercompany_warehouse_manager",
        )
        return any(user.has_group(group) for group in office_groups)

    def _reza_fsm_is_main_warehouse_supply_order(self):
        self.ensure_one()
        if not self.reza_icw_rep_location_id:
            return True
        selected_location_uses_central_stock = getattr(
            self,
            "_reza_icw_selected_location_uses_central_stock",
            None,
        )
        if selected_location_uses_central_stock:
            return selected_location_uses_central_stock()
        return True

    def _reza_fsm_main_warehouse_block_warning(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Office Review Required"),
                "message": _(
                    "This order must be reviewed and confirmed by the office "
                    "because it is being supplied from the main warehouse."
                ),
                "type": "warning",
                "sticky": True,
            },
        }
