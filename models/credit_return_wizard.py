# -*- coding: utf-8 -*-
import base64

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.osv import expression
from odoo.tools import float_compare


class CreditReturnWizard(models.TransientModel):
    _name = "reza.fsm.credit.return.wizard"
    _inherit = "product.catalog.mixin"
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
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Confirmed")],
        default="draft",
        readonly=True,
    )
    credit_note_id = fields.Integer(copy=False, readonly=True)
    credit_note_name = fields.Char(string="Credit Note", copy=False, readonly=True)
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
    signature = fields.Image(
        string="Customer Signature",
        copy=False,
        max_width=1024,
        max_height=1024,
    )
    signed_by = fields.Char(string="Customer Signed By", copy=False)
    signed_on = fields.Datetime(string="Customer Signed On", copy=False)
    is_signed = fields.Boolean(string="Is Signed", compute="_compute_is_signed")

    @api.depends("signature")
    def _compute_is_signed(self):
        for wizard in self:
            wizard.is_signed = bool(wizard.signature)

    def write(self, vals):
        result = super().write(vals)
        if vals.get("signature"):
            for wizard in self.filtered("signature"):
                update_vals = {}
                if not wizard.signed_by:
                    update_vals["signed_by"] = wizard.partner_id.name
                if not wizard.signed_on:
                    update_vals["signed_on"] = fields.Datetime.now()
                if update_vals:
                    super(CreditReturnWizard, wizard).write(update_vals)
        return result

    def action_open_signature(self):
        """Open signing only after the editable return lines have been saved."""
        self.ensure_one()
        lines = self.line_ids.filtered("product_id")
        if not lines:
            raise ValidationError(_("Add at least one product before signing the credit return."))
        lines._validate_credit_return_lines()
        signature_wizard = self.env["reza.fsm.credit.return.signature.wizard"].create({
            "credit_return_wizard_id": self.id,
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Customer Signature"),
            "res_model": "reza.fsm.credit.return.signature.wizard",
            "res_id": signature_wizard.id,
            "view_mode": "form",
            "target": "new",
        }

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
        # This controlled workflow is the only accounting operation field reps
        # may perform.  Keep their normal account.move access unchanged.
        self.task_id.check_access_rights("read")
        self.task_id.check_access_rule("read")
        if self.state == "done" or self.credit_note_id:
            raise ValidationError(_(
                "This credit return has already been confirmed as %s."
            ) % (self.credit_note_name or _("a credit note")))
        if not self.partner_id:
            raise ValidationError(_("This task has no customer set."))
        if not self.signature:
            raise ValidationError(_("Sign the credit return before creating the credit note."))
        lines = self.line_ids.filtered("product_id")
        if not lines:
            raise ValidationError(_("Add at least one product."))
        lines._validate_credit_return_lines()

        actor_id = self.env.user.id
        Move = self.env["account.move"].sudo().with_company(self.company_id)
        MoveLine = self.env["account.move.line"].sudo().with_company(self.company_id)
        Event = self.env["reza.fsm.credit.return.event"].sudo().with_company(self.company_id)
        move = Move.create({
            "move_type": "out_refund",
            "partner_id": self.partner_id.id,
            "partner_shipping_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "invoice_date": fields.Date.context_today(self),
            "invoice_origin": self.task_id.name,
            "reza_fsm_task_id": self.task_id.id,
        })
        # Save the customer signature separately once the credit note exists.
        # Account moves can add defaults during create, so writing this directly
        # to the new record guarantees the Tax Credit Note report receives it.
        # The signed PDF is attached once after posting below.
        move.with_context(reza_fsm_skip_signed_credit_note_attachment=True).write({
            "signature": self.signature,
            "signed_by": self.signed_by or self.partner_id.name,
            "signed_on": self.signed_on or fields.Datetime.now(),
        })

        for wizard_line in lines:
            product_uom = wizard_line.product_uom_id or wizard_line.product_id.uom_id
            move_line = MoveLine.create({
                "move_id": move.id,
                "product_id": wizard_line.product_id.id,
                "quantity": wizard_line.quantity,
                "product_uom_id": product_uom.id,
                "price_unit": wizard_line.price_unit,
                "name": wizard_line.product_id.display_name,
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
                "user_id": actor_id,
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

        move.action_post()
        move._reza_fsm_attach_signed_credit_note()
        self.write({
            "state": "done",
            "credit_note_id": move.id,
            "credit_note_name": move.name,
        })

        return {
            "type": "ir.actions.act_window",
            "name": _("Credit / Return"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _get_confirmed_credit_note(self):
        self.ensure_one()
        self.task_id.check_access_rights("read")
        self.task_id.check_access_rule("read")
        if not self.credit_note_id:
            raise ValidationError(_("Confirm the credit return before printing or emailing it."))
        move = self.env["account.move"].sudo().browse(self.credit_note_id).exists()
        if (
            not move
            or move.move_type != "out_refund"
            or move.reza_fsm_task_id.id != self.task_id.id
        ):
            raise ValidationError(_("The confirmed credit note is no longer available."))
        return move

    def _create_credit_note_pdf_attachment(self):
        """Render a rep-accessible PDF without exposing account.move to the rep."""
        self.ensure_one()
        move = self._get_confirmed_credit_note()
        filename = "%s_credit_note.pdf" % (move.name or self.credit_note_name)
        Attachment = self.env["ir.attachment"].sudo()
        attachment = Attachment.search([
            ("res_model", "=", "project.task"),
            ("res_id", "=", self.task_id.id),
            ("name", "=", filename),
        ], limit=1)
        if attachment:
            return attachment

        report = self.env["ir.actions.report"].sudo().with_company(
            self.company_id
        ).with_context(allowed_company_ids=[self.company_id.id])
        pdf, _content_type = report._render_qweb_pdf(
            "account.account_invoices", move.id
        )
        return Attachment.create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf),
            "mimetype": "application/pdf",
            "res_model": "project.task",
            "res_id": self.task_id.id,
        })

    def action_print_credit_note(self):
        self.ensure_one()
        attachment = self._create_credit_note_pdf_attachment()
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def action_open_send_credit_note(self):
        self.ensure_one()
        move = self._get_confirmed_credit_note()
        email_to = (move.partner_id.email or "").strip()
        if not email_to:
            raise ValidationError(_(
                "Add an email address to %s before sending this credit note."
            ) % move.partner_id.display_name)
        send_wizard = self.env["reza.fsm.credit.return.send.wizard"].create({
            "credit_return_wizard_id": self.id,
            "email_to": email_to,
            "subject": _("Credit Note %s") % move.name,
            "body_html": _(
                "<p>Please find your credit note attached.</p>"
            ),
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Email Credit Note"),
            "res_model": "reza.fsm.credit.return.send.wizard",
            "res_id": send_wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_open_customer_run(self):
        """Return the rep to the parent customer run for this visit."""
        self.ensure_one()
        self.task_id.check_access_rights("read")
        self.task_id.check_access_rule("read")
        customer_run = self.task_id.parent_id or self.task_id
        customer_run.check_access_rights("read")
        customer_run.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": _("Customer Runs"),
            "res_model": "project.task",
            "res_id": customer_run.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_cancel_credit_return(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Field Service Task"),
            "res_model": "project.task",
            "res_id": self.task_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_add_from_catalog(self):
        self.ensure_one()
        action = super().action_add_from_catalog()
        action["target"] = "new"
        action["context"] = {
            **action.get("context", {}),
            "active_model": self._name,
            "active_id": self.id,
            "active_ids": self.ids,
            "order_id": self.id,
            "reza_fsm_credit_return_catalog": True,
        }
        return action

    def _get_product_catalog_domain(self):
        domain = super()._get_product_catalog_domain()
        return expression.AND([
            domain,
            [("sale_ok", "=", True), ("type", "!=", "service")],
        ])

    def _get_action_add_from_catalog_extra_context(self):
        context = super()._get_action_add_from_catalog_extra_context()
        context.update({
            "product_catalog_currency_id": self.company_id.currency_id.id,
            "product_catalog_digits": self.line_ids._fields["price_unit"].get_digits(
                self.env
            ),
            "show_sections": False,
        })
        return context

    def _get_product_catalog_order_data(self, products, **kwargs):
        product_catalog = super()._get_product_catalog_order_data(products, **kwargs)
        for product in products:
            product_catalog[product.id].update({
                "price": product.lst_price,
                "quantity": 0,
            })
        return product_catalog

    def _get_product_catalog_record_lines(self, product_ids, **kwargs):
        grouped_lines = {}
        for line in self.line_ids.filtered(
            lambda wizard_line: wizard_line.product_id.id in product_ids
        ):
            grouped_lines.setdefault(
                line.product_id, self.env["reza.fsm.credit.return.wizard.line"]
            )
            grouped_lines[line.product_id] |= line
        return grouped_lines

    def _update_order_line_info(self, product_id, quantity, **kwargs):
        self.ensure_one()
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            return 0

        lines_for_product = self.line_ids.filtered(
            lambda wizard_line: wizard_line.product_id == product
        )
        line = lines_for_product[:1]
        quantity = quantity or 0
        if float_compare(
            quantity,
            0.0,
            precision_rounding=product.uom_id.rounding or 0.01,
        ) <= 0:
            lines_for_product.unlink()
            return product.lst_price

        values = {
            "quantity": quantity,
            "product_uom_id": product.uom_id.id,
            "price_unit": product.lst_price,
        }
        if line:
            line.write(values)
        else:
            self.env["reza.fsm.credit.return.wizard.line"].create({
                **values,
                "wizard_id": self.id,
                "product_id": product.id,
            })
        return product.lst_price

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
        # Reps may only return to their assigned van/shed locations.  Even if
        # a warehouse stock location was assigned in the user setup, never
        # expose a Liaise warehouse's root Stock location (for example,
        # LWH/Stock) as a credit-return destination.
        warehouse_stock_locations = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", company.id),
        ]).mapped("lot_stock_id")
        return assigned_locations - warehouse_stock_locations


class CreditReturnSignatureWizard(models.TransientModel):
    _name = "reza.fsm.credit.return.signature.wizard"
    _description = "Field Service Credit Return Signature"

    credit_return_wizard_id = fields.Many2one(
        "reza.fsm.credit.return.wizard",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        related="credit_return_wizard_id.partner_id",
        string="Customer",
        readonly=True,
    )
    signature = fields.Image(
        string="Customer Signature",
        required=True,
        max_width=1024,
        max_height=1024,
    )

    def action_confirm_signature(self):
        self.ensure_one()
        wizard = self.credit_return_wizard_id.exists()
        if not wizard:
            raise ValidationError(_("This credit return is no longer available."))
        wizard.task_id.check_access_rights("read")
        wizard.task_id.check_access_rule("read")
        wizard.write({
            "signature": self.signature,
            "signed_by": wizard.partner_id.name,
            "signed_on": fields.Datetime.now(),
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Credit / Return"),
            "res_model": wizard._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "current",
        }


class CreditReturnSendWizard(models.TransientModel):
    _name = "reza.fsm.credit.return.send.wizard"
    _description = "Email Field Service Credit Note"

    credit_return_wizard_id = fields.Many2one(
        "reza.fsm.credit.return.wizard",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        related="credit_return_wizard_id.partner_id",
        string="Customer",
        readonly=True,
    )
    email_to = fields.Char(string="Email To", required=True)
    subject = fields.Char(required=True)
    body_html = fields.Html(string="Message", required=True)

    def action_send_credit_note(self):
        self.ensure_one()
        credit_return = self.credit_return_wizard_id.exists()
        if not credit_return:
            raise ValidationError(_("This credit return is no longer available."))
        email_to = (self.email_to or "").strip()
        if not email_to:
            raise ValidationError(_("Enter the customer email address."))
        move = credit_return._get_confirmed_credit_note()
        attachment = credit_return._create_credit_note_pdf_attachment()
        email_from = (
            credit_return.company_id.partner_id.email_formatted
            or self.env.user.email_formatted
            or self.env.user.email
        )
        if not email_from:
            raise ValidationError(_("No sender email address is configured."))
        self.env["mail.mail"].sudo().create({
            "subject": self.subject,
            "body_html": self.body_html,
            "email_to": email_to,
            "email_from": email_from,
            "auto_delete": False,
            "attachment_ids": [(4, attachment.id)],
        })
        credit_return.task_id.sudo().message_post(
            body=_("Credit note %s was queued for %s.") % (move.name, email_to),
            subtype_xmlid="mail.mt_note",
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Credit / Return"),
            "res_model": credit_return._name,
            "res_id": credit_return.id,
            "view_mode": "form",
            "target": "current",
        }


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
    quantity = fields.Float(string="Qty", required=True, default=1.0)
    product_uom_id = fields.Many2one(
        "uom.uom",
        string="Unit",
    )
    price_unit = fields.Float(string="Price")
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

    def _get_product_catalog_lines_data(self, parent_record=None, **kwargs):
        line = self[:1]
        return {
            "quantity": sum(self.mapped("quantity")),
            "price": line.price_unit,
            "uomDisplayName": line.product_uom_id.display_name,
        }

    def action_duplicate_line(self):
        for line in self:
            line.copy({
                "wizard_id": line.wizard_id.id,
                "quantity": line.quantity,
                "product_id": line.product_id.id,
                "product_uom_id": line.product_uom_id.id,
                "price_unit": line.price_unit,
                "outcome": "credit_scrap" if line.outcome == "credit_return" else "credit_return",
                "return_location_id": False if line.outcome == "credit_return" else line.return_location_id.id,
                "credit_reason_ids": [(6, 0, [])],
                "scrap_reason_id": False,
                "note": False,
            })

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
            if line.outcome == "credit_return" and not line.credit_reason_ids:
                raise ValidationError(_(
                    "Select at least one credit reason for %s."
                ) % line.product_id.display_name)
            if line.outcome == "credit_scrap" and not line.scrap_reason_id:
                raise ValidationError(_(
                    "Select a scrap reason for %s."
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
