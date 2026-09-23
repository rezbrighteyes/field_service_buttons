# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class CreditReturnLog(models.Model):
    """Append-only trail of every product a rep puts on, changes on or takes
    off a credit / return.

    Written 2026-09-23 after abandoned credits on production could be traced
    only through orphan signature attachments - who, when and which customer
    survived, the products did not.  Every row therefore copies what it
    needs as plain values (ids as Integers, names as Chars).  The Many2ones
    are conveniences only and use ondelete='set null', so deleting the
    credit, its draft credit note, the task or the product never deletes a
    row.  Nobody edits or deletes a row through the ORM; only the superuser
    (module uninstall, a deliberate shell repair) can.
    """

    _name = "reza.fsm.credit.return.log"
    _description = "Field Service Credit / Return Log"
    _order = "date desc, id desc"
    _rec_name = "action"

    date = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    action = fields.Selection(
        [
            ("add", "Product Added"),
            ("change", "Product Changed"),
            ("remove", "Product Removed"),
            ("sign", "Customer Signed"),
            ("confirm", "Confirmed"),
            ("save_back", "Saved & Left"),
            ("cancel", "Credit Cancelled"),
            ("draft_deleted", "Draft Credit Note Deleted"),
            ("draft_recreated", "Draft Credit Note Recreated"),
            ("credit_deleted", "Credit Deleted"),
            ("office_posted_unfinished", "Posted Unfinished by Office"),
        ],
        required=True,
        readonly=True,
        index=True,
    )
    user_id = fields.Many2one(
        "res.users", string="User", default=lambda self: self.env.user,
        readonly=True, index=True, ondelete="set null",
    )
    user_name = fields.Char(string="User Name", readonly=True)
    company_id = fields.Many2one(
        "res.company", readonly=True, index=True, ondelete="set null",
    )
    task_id = fields.Many2one(
        "project.task", string="Field Service Task", readonly=True,
        index="btree_not_null", ondelete="set null",
    )
    task_ref_id = fields.Integer(string="Task ID", readonly=True, index=True)
    task_name = fields.Char(string="Task Name", readonly=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", readonly=True,
        index="btree_not_null", ondelete="set null",
    )
    partner_name = fields.Char(string="Customer Name", readonly=True)
    credit_return_id = fields.Many2one(
        "reza.fsm.credit.return.wizard", string="Credit / Return",
        readonly=True, index="btree_not_null", ondelete="set null",
    )
    credit_ref_id = fields.Integer(string="Credit ID", readonly=True, index=True)
    credit_line_ref_id = fields.Integer(string="Credit Line ID", readonly=True)
    move_ref_id = fields.Integer(string="Credit Note ID", readonly=True, index=True)
    move_name = fields.Char(string="Credit Note", readonly=True)
    product_id = fields.Many2one(
        "product.product", readonly=True, index="btree_not_null", ondelete="set null",
    )
    product_name = fields.Char(string="Product Name", readonly=True)
    product_code = fields.Char(string="Internal Reference", readonly=True)
    quantity = fields.Float(readonly=True)
    old_quantity = fields.Float(string="Previous Qty", readonly=True)
    uom_name = fields.Char(string="Unit", readonly=True)
    price_unit = fields.Float(string="Price", readonly=True)
    old_price_unit = fields.Float(string="Previous Price", readonly=True)
    outcome = fields.Selection(
        [
            ("credit_return", "Credit Return"),
            ("credit_scrap", "Credit Scrap"),
        ],
        readonly=True,
    )
    return_location_id = fields.Many2one(
        "stock.location", readonly=True, ondelete="set null",
    )
    return_location_name = fields.Char(string="Return Location", readonly=True)
    reasons = fields.Char(string="Reasons", readonly=True)
    note = fields.Text(readonly=True)
    changes = fields.Char(string="What Changed", readonly=True)

    # ------------------------------------------------------------------
    # Append-only
    # ------------------------------------------------------------------
    def write(self, vals):
        if not self.env.su:
            raise AccessError(_("Credit / return log rows cannot be changed."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_("Credit / return log rows cannot be deleted."))
        return super().unlink()

    # ------------------------------------------------------------------
    # Writing rows
    # ------------------------------------------------------------------
    @api.model
    def _reza_fsm_credit_values(self, credit):
        """Header values copied from the credit, as plain values."""
        credit = credit.sudo()
        task = credit.task_id
        partner = credit.partner_id
        move = credit.move_id
        return {
            "company_id": credit.company_id.id,
            "task_id": task.id,
            "task_ref_id": task.id,
            "task_name": task.display_name,
            "partner_id": partner.id,
            "partner_name": partner.display_name,
            "credit_return_id": credit.id,
            "credit_ref_id": credit.id,
            "move_ref_id": move.id or credit.last_move_ref_id or 0,
            "move_name": move.name if move and move.name != "/" else False,
        }

    @api.model
    def _reza_fsm_line_values(self, line):
        """Product values copied from a credit line, as plain values."""
        line = line.sudo()
        product = line.product_id
        reasons = line.credit_reason_ids | line.scrap_reason_id
        return {
            "credit_line_ref_id": line.id,
            "product_id": product.id,
            "product_name": product.display_name,
            "product_code": product.default_code,
            "quantity": line.quantity,
            "uom_name": (line.product_uom_id or product.uom_id).display_name,
            "price_unit": line.price_unit,
            "outcome": line.outcome,
            "return_location_id": line.return_location_id.id,
            "return_location_name": line.return_location_id.display_name,
            "reasons": ", ".join(reasons.mapped("name")) or False,
            "note": line.note,
        }

    @api.model
    def _reza_fsm_move_line_values(self, move_line):
        """Product values copied from a credit note line, as plain values.

        Used when the draft credit note is deleted, so the log keeps what the
        draft said even if the credit itself is gone too.
        """
        move_line = move_line.sudo()
        product = move_line.product_id
        reasons = move_line.reza_fsm_credit_reason_ids | move_line.reza_fsm_scrap_reason_id
        return {
            "product_id": product.id,
            "product_name": product.display_name or move_line.name,
            "product_code": product.default_code,
            "quantity": move_line.quantity,
            "uom_name": move_line.product_uom_id.display_name,
            "price_unit": move_line.price_unit,
            "outcome": move_line.reza_fsm_credit_return_outcome,
            "return_location_id": move_line.reza_fsm_credit_return_location_id.id,
            "return_location_name": move_line.reza_fsm_credit_return_location_id.display_name,
            "reasons": ", ".join(reasons.mapped("name")) or False,
            "note": move_line.reza_fsm_credit_note,
        }

    @api.model
    def _reza_fsm_log(self, action, credit, line_values_list=None, **extra):
        """Write one row per product (or one header row when there is none).

        Runs as sudo so the row is written whatever the caller may read, and
        keeps the acting user: sudo() does not change env.uid.
        """
        header = self._reza_fsm_credit_values(credit)
        header.update({
            "action": action,
            "user_id": self.env.uid,
            "user_name": self.env.user.name,
        })
        header.update(extra)
        vals_list = [
            {**header, **line_values} for line_values in (line_values_list or [])
        ] or [header]
        return self.sudo().create(vals_list)
