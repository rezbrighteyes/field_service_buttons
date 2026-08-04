# -*- coding: utf-8 -*-
import base64
import logging
from email.utils import formataddr

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


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
    signature = fields.Image(
        string="Customer Signature",
        copy=False,
        readonly=True,
        attachment=True,
        max_width=1024,
        max_height=1024,
    )
    reza_fsm_customer_signature = fields.Image(
        string="FSM Customer Signature",
        copy=False,
        readonly=True,
        attachment=True,
        max_width=1024,
        max_height=1024,
    )
    signed_by = fields.Char(
        string="Customer Signed By",
        copy=False,
        readonly=True,
    )
    signed_on = fields.Datetime(
        string="Customer Signed On",
        copy=False,
        readonly=True,
    )
    reza_fsm_customer_signed_by = fields.Char(
        string="FSM Customer Signed By",
        copy=False,
        readonly=True,
    )
    reza_fsm_customer_signed_on = fields.Datetime(
        string="FSM Customer Signed On",
        copy=False,
        readonly=True,
    )
    is_signed = fields.Boolean(string="Is Signed", compute="_compute_is_signed")

    @api.depends("signature", "reza_fsm_customer_signature")
    def _compute_is_signed(self):
        for move in self:
            move.is_signed = bool(
                move.reza_fsm_customer_signature or move.signature
            )

    def write(self, vals):
        result = super().write(vals)
        if vals.get("signature") or vals.get("reza_fsm_customer_signature"):
            for move in self.filtered(
                lambda credit_note: credit_note._reza_fsm_is_signable_credit_note()
            ):
                update_vals = {}
                if not move.reza_fsm_customer_signed_by:
                    update_vals["reza_fsm_customer_signed_by"] = (
                        move.signed_by or move.partner_id.name
                    )
                if not move.reza_fsm_customer_signed_on:
                    update_vals["reza_fsm_customer_signed_on"] = (
                        move.signed_on or fields.Datetime.now()
                    )
                if update_vals:
                    move.write(update_vals)
                if not move.env.context.get("reza_fsm_skip_signed_credit_note_attachment"):
                    move._reza_fsm_attach_signed_credit_note()
        return result

    def action_post(self):
        result = super().action_post()
        self._reza_fsm_process_credit_return_events()
        return result

    def _reza_fsm_is_signable_credit_note(self):
        self.ensure_one()
        return self.move_type == "out_refund"

    def _reza_fsm_attach_signed_credit_note(self):
        self.ensure_one()
        try:
            report = self.env["ir.actions.report"]._render_qweb_pdf(
                "account.account_invoices",
                self.id,
            )
        except Exception:
            _logger.exception("Could not attach signed credit note PDF for %s", self.display_name)
            self.message_post(body=_("Credit note signed by %s.") % (
                self.reza_fsm_customer_signed_by
                or self.signed_by
                or self.partner_id.name
            ))
            return False

        filename = "%s_signed_credit_note" % (self.name or self.display_name)
        self.message_post(
            attachments=[("%s.pdf" % filename, report[0])],
            body=_("Credit note signed by %s.") % (
                self.reza_fsm_customer_signed_by
                or self.signed_by
                or self.partner_id.name
            ),
        )
        return True

    # ------------------------------------------------------------------
    # Emailing the credit note to the customer
    # ------------------------------------------------------------------
    def _reza_fsm_credit_note_email_from(self):
        """Sender address the outgoing mail server will accept unchanged.

        ir.mail_server rewrites any From that is missing from its from_filter,
        and the from_filter on this database lists only the notifications@ and
        bounce@ addresses of the three alias domains.  The company partner
        address (sales@rockos.com.au for Liaise) is not one of them, so mail
        sent from it went out branded as the Edbert notification address.  The
        company's own alias domain holds the address the server is configured
        to send as.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        address = company.alias_domain_id.default_from_email
        if not address:
            return False
        return formataddr((company.name or "", address))

    def _reza_fsm_credit_note_reply_to(self):
        """Send replies to a real mailbox rather than back into Odoo."""
        self.ensure_one()
        company = self.company_id or self.env.company
        return company.partner_id.email_formatted or company.email or False

    def _reza_fsm_credit_note_email_subject(self):
        self.ensure_one()
        return _("%(company)s credit note %(number)s") % {
            "company": self.company_id.name or "",
            "number": self.name or "",
        }

    def _reza_fsm_credit_note_email_body(self):
        """Body with enough detail that the store can recognise the email.

        The previous body was one unbranded sentence with a PDF attached, which
        reads like spam to a customer who has never had mail from this system.
        """
        self.ensure_one()
        currency = self.currency_id
        amount = "%s%.2f" % (currency.symbol or "", self.amount_total)
        invoice_date = self.invoice_date.strftime("%d/%m/%Y") if self.invoice_date else ""
        return Markup(
            "<p>Hello %s,</p>"
            "<p>Please find attached credit note <strong>%s</strong> dated %s "
            "for <strong>%s</strong>.</p>"
            "<p>This covers the stock credited in store by %s. The copy you "
            "signed is attached as a PDF.</p>"
            "<p>Reply to this email if anything looks wrong.</p>"
            "<p>%s</p>"
        ) % (
            self.partner_id.name or "",
            self.name or "",
            invoice_date,
            amount,
            self.invoice_user_id.name or self.create_uid.name or "",
            self.company_id.name or "",
        )

    def _reza_fsm_get_credit_note_attachment(self):
        """PDF of the credit note, stored on the move itself.

        Kept on account.move (not the FSM task) so office staff can open it
        from the credit note, and rendered once - a second call reuses it.
        """
        self.ensure_one()
        move = self.sudo()
        filename = "%s.pdf" % (move.name or "credit_note")
        Attachment = self.env["ir.attachment"].sudo()
        attachment = Attachment.search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", move.id),
            ("name", "=", filename),
        ], limit=1)
        if attachment:
            return attachment
        try:
            pdf, _content_type = self.env["ir.actions.report"].sudo().with_company(
                move.company_id
            ).with_context(
                allowed_company_ids=[move.company_id.id]
            )._render_qweb_pdf("account.account_invoices", move.id)
        except Exception:
            _logger.exception("Could not render credit note PDF for %s", move.name)
            return False
        return Attachment.create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf),
            "mimetype": "application/pdf",
            "res_model": "account.move",
            "res_id": move.id,
        })

    def _reza_fsm_send_credit_note_email(
        self, email_to=None, subject=None, body_html=None, author_id=None
    ):
        """Email the credit note to the customer and record it on the move.

        Returns the mail.mail, or False when there is nothing to send to.  The
        mail carries model/res_id so it lands in the credit note's own chatter -
        the earlier implementation created a bare mail.mail attached to nothing,
        which left no trace on the credit note and never moved `is_move_sent`.
        """
        self.ensure_one()
        move = self.sudo()
        email_to = (email_to or move.partner_id.email or "").strip()
        if not email_to:
            _logger.warning(
                "No customer email address for credit note %s - not sent.", move.name
            )
            return False
        email_from = move._reza_fsm_credit_note_email_from()
        if not email_from:
            _logger.warning(
                "No alias domain on company %s - credit note %s not sent.",
                move.company_id.display_name,
                move.name,
            )
            return False
        values = {
            "subject": subject or move._reza_fsm_credit_note_email_subject(),
            "body_html": body_html or move._reza_fsm_credit_note_email_body(),
            "email_from": email_from,
            "email_to": email_to,
            # model/res_id are what put this mail in the credit note's chatter.
            # `record_name` is deliberately not set - it is non-stored and
            # readonly on mail.message in Odoo 19.
            "model": "account.move",
            "res_id": move.id,
            "author_id": author_id or self.env.user.partner_id.id,
            "auto_delete": False,
        }
        reply_to = move._reza_fsm_credit_note_reply_to()
        if reply_to:
            values["reply_to"] = reply_to
        attachment = move._reza_fsm_get_credit_note_attachment()
        if attachment:
            values["attachment_ids"] = [(4, attachment.id)]
        mail = self.env["mail.mail"].sudo().create(values)
        # Send now instead of waiting up to an hour for the mail queue cron -
        # the rep is standing at the counter telling the customer it has gone.
        mail.send()
        move.write({"is_move_sent": True})
        return mail

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
