# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class AccountMove(models.Model):
    _inherit = "account.move"

    reza_fsm_task_id = fields.Many2one(
        "project.task",
        string="Field Service Task",
        copy=False,
        readonly=True,
        index=True,
    )
    reza_fsm_credit_return_event_ids = fields.One2many(
        "reza.fsm.credit.return.event",
        "move_id",
        string="Credit Return Events",
        readonly=True,
    )

    def action_post(self):
        result = super().action_post()
        self._reza_fsm_process_credit_return_events()
        return result

    def _reza_fsm_process_credit_return_events(self):
        for move in self:
            events = move.sudo().reza_fsm_credit_return_event_ids.filtered(
                lambda event: event.state == "draft"
            )
            for event in events:
                if event.outcome == "credit_return":
                    event._reza_fsm_create_return_stock_move()
                event.write({"state": "done"})


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    reza_fsm_credit_return_outcome = fields.Selection(
        [
            ("credit_return", "Credit Return"),
            ("credit_scrap", "Credit Scrap"),
        ],
        string="Credit Outcome",
        copy=False,
    )
    reza_fsm_credit_return_location_id = fields.Many2one(
        "stock.location",
        string="Credit Return Location",
        copy=False,
        domain=[("usage", "=", "internal")],
    )
    reza_fsm_credit_reason_ids = fields.Many2many(
        "reza.fsm.credit.return.reason",
        "reza_fsm_credit_return_line_reason_rel",
        "line_id",
        "reason_id",
        string="Credit Reasons",
        domain=[("reason_type", "in", ("credit", "both"))],
        copy=False,
    )
    reza_fsm_scrap_reason_id = fields.Many2one(
        "reza.fsm.credit.return.reason",
        string="Scrap Reason",
        domain=[("reason_type", "in", ("scrap", "both"))],
        copy=False,
    )
    reza_fsm_credit_note = fields.Text(string="Credit Return Note", copy=False)
    reza_fsm_credit_return_event_id = fields.Many2one(
        "reza.fsm.credit.return.event",
        string="Credit Return Event",
        copy=False,
        readonly=True,
    )

    def _get_invoice_report_description(self):
        self.ensure_one()
        if self.reza_fsm_credit_return_outcome and self.product_id:
            return self.product_id.name
        parent_method = getattr(super(), "_get_invoice_report_description", None)
        if parent_method:
            return parent_method()
        return self.name or ""


class CreditReturnEvent(models.Model):
    _inherit = "reza.fsm.credit.return.event"

    def _reza_fsm_create_return_stock_move(self):
        self.ensure_one()
        if self.stock_move_id:
            if self.stock_move_id.state != "done":
                self._reza_fsm_finalize_return_stock_move(self.stock_move_id)
            return self.stock_move_id
        if not self.return_location_id:
            raise ValidationError(_(
                "Credit Return location is required for %s."
            ) % self.product_id.display_name)

        source_location = self._reza_fsm_get_customer_source_location()
        Move = self.env["stock.move"].sudo().with_company(self.company_id)
        move_vals = {
            "company_id": self.company_id.id,
            "product_id": self.product_id.id,
            "product_uom_qty": self.quantity,
            "product_uom": self.product_uom_id.id,
            "location_id": source_location.id,
            "location_dest_id": self.return_location_id.id,
            "origin": self.move_id.name or self.move_id.invoice_origin or self.move_id.ref,
        }
        if "description_picking" in Move._fields:
            move_vals["description_picking"] = _("Credit Return: %s") % (
                self.product_id.display_name
            )
        stock_move = Move.create(move_vals)
        self._reza_fsm_finalize_return_stock_move(stock_move)
        self.write({"stock_move_id": stock_move.id})
        return stock_move

    def _reza_fsm_finalize_return_stock_move(self, stock_move):
        self.ensure_one()
        MoveLine = self.env["stock.move.line"].sudo().with_company(self.company_id)
        move_line_uom_field = (
            "product_uom_id" if "product_uom_id" in MoveLine._fields else "product_uom"
        )
        if hasattr(stock_move, "_action_confirm"):
            stock_move._action_confirm()
        if hasattr(stock_move, "_action_assign"):
            stock_move._action_assign()
        qty = stock_move.product_uom_qty
        if "picked" in stock_move._fields:
            stock_move.picked = True
        if stock_move.move_line_ids:
            for move_line in stock_move.move_line_ids:
                move_line.quantity = move_line.quantity or qty
                if "picked" in move_line._fields:
                    move_line.picked = True
        else:
            MoveLine.create({
                "move_id": stock_move.id,
                "company_id": self.company_id.id,
                "product_id": stock_move.product_id.id,
                move_line_uom_field: stock_move.product_uom.id,
                "quantity": qty,
                "location_id": stock_move.location_id.id,
                "location_dest_id": stock_move.location_dest_id.id,
            })
        if not hasattr(stock_move, "_action_done"):
            raise UserError(_("Odoo could not finalize the credit return stock move."))
        stock_move._action_done()
        return stock_move

    def _reza_fsm_get_customer_source_location(self):
        location = self.env.ref("stock.stock_location_customers", raise_if_not_found=False)
        if location:
            return location
        location = self.env["stock.location"].sudo().search(
            [("usage", "=", "customer")],
            limit=1,
        )
        if not location:
            raise UserError(_("No customer stock location is configured."))
        return location
