# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class CreditReturnReason(models.Model):
    _name = "reza.fsm.credit.return.reason"
    _description = "Field Service Credit Return Reason"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    reason_type = fields.Selection(
        [
            ("credit", "Credit Reason"),
            ("scrap", "Scrap Reason"),
            ("both", "Credit and Scrap"),
        ],
        required=True,
        default="credit",
    )
    requires_note = fields.Boolean(string="Requires Note")


class CreditReturnEvent(models.Model):
    _name = "reza.fsm.credit.return.event"
    _description = "Field Service Credit Return Event"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    date = fields.Date(default=fields.Date.context_today, required=True, tracking=True)
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done"), ("cancelled", "Cancelled")],
        default="draft",
        required=True,
        copy=False,
        tracking=True,
    )
    outcome = fields.Selection(
        [
            ("credit_return", "Credit Return"),
            ("credit_scrap", "Credit Scrap"),
        ],
        required=True,
        tracking=True,
    )
    move_id = fields.Many2one(
        "account.move",
        string="Credit Note",
        required=True,
        ondelete="cascade",
        index=True,
    )
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Credit Note Line",
        ondelete="cascade",
        index=True,
    )
    task_id = fields.Many2one("project.task", string="Field Service Task", index=True)
    partner_id = fields.Many2one("res.partner", string="Customer/Store", index=True)
    user_id = fields.Many2one("res.users", string="Rep", default=lambda self: self.env.user)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    product_id = fields.Many2one("product.product", required=True, index=True)
    product_uom_id = fields.Many2one("uom.uom", string="Unit of Measure", required=True)
    quantity = fields.Float(required=True, default=1.0)
    return_location_id = fields.Many2one(
        "stock.location",
        string="Credit Return Location",
        domain=[("usage", "=", "internal")],
    )
    credit_reason_ids = fields.Many2many(
        "reza.fsm.credit.return.reason",
        "reza_fsm_credit_return_event_reason_rel",
        "event_id",
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
    stock_move_id = fields.Many2one(
        "stock.move",
        string="Return Stock Move",
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        events = super().create(vals_list)
        for event in events.filtered(lambda item: item.name == "New"):
            event.name = _("%(outcome)s - %(product)s") % {
                "outcome": dict(event._fields["outcome"].selection).get(event.outcome),
                "product": event.product_id.display_name,
            }
        return events
