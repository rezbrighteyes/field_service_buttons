from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestVanShortageMessage(TransactionCase):
    """IT-41: a van shortage must not be reported as a main warehouse order."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env["res.partner"].create({"name": "Van Shortage Customer"})
        cls.order = cls.env["sale.order"].create({"partner_id": partner.id})
        cls.SaleOrder = type(cls.env["sale.order"])

    def _block(self, main_warehouse, shortage_lines):
        with patch.object(self.SaleOrder, "_reza_fsm_user_can_confirm_main_warehouse", return_value=False), \
                patch.object(self.SaleOrder, "_reza_fsm_is_main_warehouse_supply_order", return_value=main_warehouse), \
                patch.object(self.SaleOrder, "_reza_fsm_rep_shortage_lines", return_value=shortage_lines):
            return self.order._reza_fsm_block_rep_main_warehouse_confirm()

    def _last_body(self):
        return self.order.message_ids.sorted("id")[-1:].body or ""

    def test_van_shortage_names_the_product(self):
        line = SimpleNamespace(product_id=SimpleNamespace(display_name="Pen Readers (2.5)"))
        action = self._block(False, [(line, 0.0, 4.0)])
        message = action["params"]["message"]
        self.assertEqual(action["params"]["title"], "Not Enough Stock in the Van")
        self.assertIn("Pen Readers (2.5): ordered 4, van has 0", message)
        self.assertNotIn("main warehouse", message.lower())
        self.assertIn("Van stock confirmation blocked", self._last_body())

    def test_main_warehouse_keeps_old_message(self):
        action = self._block(True, [])
        self.assertEqual(action["params"]["title"], "Office Review Required")
        self.assertIn("Main warehouse confirmation blocked", self._last_body())

    def test_van_with_stock_is_not_blocked(self):
        self.assertFalse(self._block(False, []))
