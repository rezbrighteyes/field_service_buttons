# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class CreditReturnWizard(models.TransientModel):
    _name = "reza.fsm.credit.return.wizard"
    _description = "Field Service Credit / Return"

    task_id = fields.Many2one("project.task", required=True, readonly=True)
    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        related="task_id.partner_id",
        readonly=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        related="task_id.company_id",
        readonly=True,
    )
    allowed_return_location_ids = fields.Many2many(
        "stock.location",
        compute="_compute_allowed_return_location_ids",
        string="Allowed Return Locations",
    )
    line_ids = fields.One2many(
        "reza.fsm.credit.return.wizard.line",
        "wizard_id",
        string="Products",
    )

    @api.depends("task_id", "company_id")
    def _compute_allowed_return_location_ids(self):
        for wizard in self:
            wizard.allowed_return_location_ids = wizard._get_allowed_return_locations()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        task_id = self.env.context.get("default_task_id") or self.env.context.get("active_id")
        if task_id and "task_id" in fields_list:
            values["task_id"] = task_id
        return values

    def action_create_credit_note(self):
        self.ensure_one()
        if not self.partner_id:
            raise ValidationError(_("This task has no customer set."))
        lines = self.line_ids.filtered("product_id")
        if not lines:
            raise ValidationError(_("Add at least one product."))
        lines._validate_credit_return_lines()

        move = self.env["account.move"].with_company(self.company_id).create({
            "move_type": "out_refund",
            "partner_id": self.partner_id.id,
            "partner_shipping_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "invoice_date": fields.Date.context_today(self),
            "invoice_origin": self.task_id.name,
            "narration": _("Created from Field Service task: %s") % self.task_id.name,
            "reza_fsm_task_id": self.task_id.id,
        })

        Event = self.env["reza.fsm.credit.return.event"].with_company(self.company_id)
        for wizard_line in lines:
            product_uom = wizard_line.product_uom_id or wizard_line.product_id.uom_id
            reason_text = ", ".join(wizard_line.credit_reason_ids.mapped("name"))
            scrap_reason = wizard_line.scrap_reason_id.name if wizard_line.scrap_reason_id else ""
            outcome_label = dict(wizard_line._fields["outcome"].selection).get(
                wizard_line.outcome
            )
            line_name_parts = [wizard_line.product_id.display_name, outcome_label]
            if reason_text:
                line_name_parts.append(_("Reason: %s") % reason_text)
            if scrap_reason:
                line_name_parts.append(_("Scrap: %s") % scrap_reason)

            move_line = self.env["account.move.line"].with_company(self.company_id).create({
                "move_id": move.id,
                "product_id": wizard_line.product_id.id,
                "quantity": wizard_line.quantity,
                "product_uom_id": product_uom.id,
                "price_unit": wizard_line.price_unit,
                "name": " - ".join(line_name_parts),
                "reza_fsm_credit_return_outcome": wizard_line.outcome,
                "reza_fsm_credit_return_location_id": wizard_line.return_location_id.id,
                "reza_fsm_credit_reason_ids": [
                    (6, 0, wizard_line.credit_reason_ids.ids)
                ],
                "reza_fsm_scrap_reason_id": wizard_line.scrap_reason_id.id,
                "reza_fsm_credit_note": wizard_line.note,
            })
            event = Event.create({
                "date": fields.Date.context_today(self),
                "outcome": wizard_line.outcome,
                "move_id": move.id,
                "move_line_id": move_line.id,
                "task_id": self.task_id.id,
                "partner_id": self.partner_id.id,
                "user_id": self.env.user.id,
                "company_id": self.company_id.id,
                "product_id": wizard_line.product_id.id,
                "product_uom_id": product_uom.id,
                "quantity": wizard_line.quantity,
                "return_location_id": wizard_line.return_location_id.id,
                "credit_reason_ids": [(6, 0, wizard_line.credit_reason_ids.ids)],
                "scrap_reason_id": wizard_line.scrap_reason_id.id,
                "note": wizard_line.note,
            })
            move_line.write({"reza_fsm_credit_return_event_id": event.id})

        return {
            "type": "ir.actions.act_window",
            "name": _("Credit Note"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "target": "current",
        }

    def _get_allowed_return_locations(self):
        self.ensure_one()
        Location = self.env["stock.location"]
        company = self.company_id or self.env.company
        if self.env.user.has_group(
            "reza_intercompany_warehouse.group_intercompany_warehouse_manager"
        ):
            return Location.search([
                ("usage", "=", "internal"),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", company.id),
            ])

        assigned_locations = self.env.user.reza_icw_allowed_rep_location_ids.filtered(
            lambda location: (
                location.usage == "internal"
                and (not location.company_id or location.company_id == company)
            )
        )
        warehouse_locations = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", company.id),
        ]).mapped("lot_stock_id")
        return assigned_locations | warehouse_locations


class CreditReturnWizardLine(models.TransientModel):
    _name = "reza.fsm.credit.return.wizard.line"
    _description = "Field Service Credit / Return Line"

    wizard_id = fields.Many2one(
        "reza.fsm.credit.return.wizard",
        required=True,
        ondelete="cascade",
    )
    allowed_return_location_ids = fields.Many2many(
        "stock.location",
        related="wizard_id.allowed_return_location_ids",
        readonly=True,
    )
    product_id = fields.Many2one(
        "product.product",
        string="Product",
        required=True,
        domain=[("type", "!=", "service")],
    )
    quantity = fields.Float(required=True, default=1.0)
    product_uom_id = fields.Many2one(
        "uom.uom",
        string="Unit",
    )
    price_unit = fields.Float(string="Unit Price")
    outcome = fields.Selection(
        [
            ("credit_return", "Credit Return"),
            ("credit_scrap", "Credit Scrap"),
        ],
        required=True,
        default="credit_return",
    )
    return_location_id = fields.Many2one(
        "stock.location",
        string="Return Location",
        domain="[('id', 'in', allowed_return_location_ids)]",
    )
    credit_reason_ids = fields.Many2many(
        "reza.fsm.credit.return.reason",
        "reza_fsm_credit_return_wizard_line_reason_rel",
        "line_id",
        "reason_id",
        string="Credit Reasons",
        domain=[("reason_type", "in", ("credit", "both"))],
    )
    scrap_reason_id = fields.Many2one(
        "reza.fsm.credit.return.reason",
        string="Scrap Reason",
        domain=[("reason_type", "in", ("scrap", "both"))],
    )
    note = fields.Text()

    @api.onchange("product_id")
    def _onchange_product_id(self):
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id
            self.price_unit = self.product_id.lst_price

    @api.onchange("outcome")
    def _onchange_outcome(self):
        if self.outcome == "credit_scrap":
            self.return_location_id = False

    def _validate_credit_return_lines(self):
        for line in self:
            precision_rounding = (
                (line.product_uom_id or line.product_id.uom_id).rounding
                or line.product_id.uom_id.rounding
                or 0.01
            )
            if float_compare(
                line.quantity,
                0.0,
                precision_rounding=precision_rounding,
            ) <= 0:
                raise ValidationError(_(
                    "Quantity must be greater than zero for %s."
                ) % line.product_id.display_name)
            if line.outcome == "credit_return" and not line.return_location_id:
                raise ValidationError(_(
                    "Select a return location for %s."
                ) % line.product_id.display_name)
            if (
                line.outcome == "credit_return"
                and line.return_location_id not in line.allowed_return_location_ids
            ):
                raise ValidationError(_(
                    "%s is not an allowed return location."
                ) % line.return_location_id.display_name)
            reasons_requiring_note = (
                line.credit_reason_ids.filtered("requires_note")
                | line.scrap_reason_id.filtered("requires_note")
            )
            if reasons_requiring_note and not (line.note or "").strip():
                raise ValidationError(_(
                    "Add a note when using Other as a reason for %s."
                ) % line.product_id.display_name)
