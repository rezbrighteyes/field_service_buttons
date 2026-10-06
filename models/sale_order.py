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
            if order._reza_fsm_is_main_warehouse_supply_order():
                order.message_post(body=_(
                    "Main warehouse confirmation blocked: field reps must leave "
                    "LIA/WH and other main warehouse orders for office review."
                ))
                return order._reza_fsm_main_warehouse_block_warning()
            # A van order is also held back when the van cannot cover it. Say
            # so, and name the products: the old "main warehouse" wording sent
            # reps and the office looking for the wrong problem (IT-41).
            shortage_lines = order._reza_fsm_rep_shortage_lines()
            if shortage_lines:
                summary = order._reza_fsm_shortage_summary(shortage_lines)
                order.message_post(body=_(
                    "Van stock confirmation blocked: %(location)s is short. %(summary)s",
                    location=order.reza_icw_rep_location_id.display_name,
                    summary=summary,
                ))
                return order._reza_fsm_van_shortage_block_warning(summary)
        return False

    def _reza_fsm_rep_shortage_lines(self):
        self.ensure_one()
        rep_shortage_lines = getattr(self, "_reza_icw_get_rep_shortage_lines", None)
        return rep_shortage_lines() if rep_shortage_lines else []

    def _reza_fsm_shortage_summary(self, shortage_lines):
        self.ensure_one()
        return "; ".join(
            _(
                "%(product)s: ordered %(ordered)s, van has %(available)s",
                product=line.product_id.display_name,
                ordered="%g" % (available_qty + shortage_qty),
                available="%g" % available_qty,
            )
            for line, available_qty, shortage_qty in shortage_lines
        )

    def _reza_fsm_user_can_confirm_main_warehouse(self):
        user = self.env.user
        office_groups = (
            "base.group_system",
            "reza_intercompany_warehouse.group_intercompany_warehouse_manager",
        )
        if any(user.has_group(group) for group in office_groups):
            return True
        rep_groups = (
            "reza_field_service_buttons.group_liaise_field_rep",
            "industry_fsm.group_fsm_user",
        )
        return not any(user.has_group(group) for group in rep_groups)

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

    def _reza_fsm_needs_office_warehouse_review(self):
        self.ensure_one()
        if self._reza_fsm_is_main_warehouse_supply_order():
            return True
        return bool(self._reza_fsm_rep_shortage_lines())

    def _reza_icw_user_must_select_rep_location(self):
        if (
            self._reza_fsm_user_can_confirm_main_warehouse()
            and self._reza_fsm_is_main_warehouse_supply_order()
        ):
            return False
        return super()._reza_icw_user_must_select_rep_location()

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

    def _reza_fsm_van_shortage_block_warning(self, summary):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Not Enough Stock in the Van"),
                "message": _(
                    "%(location)s cannot cover this order. %(summary)s. "
                    "Reduce or remove these lines, or ask the office to confirm.",
                    location=self.reza_icw_rep_location_id.display_name,
                    summary=summary,
                ),
                "type": "warning",
                "sticky": True,
            },
        }
