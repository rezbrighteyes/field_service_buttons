# -*- coding: utf-8 -*-
import base64
import logging
from email.utils import formataddr

import psycopg2
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
    @api.model
    def _reza_fsm_mail_server_accepts(self, address):
        """True when ir.mail_server would send this From unchanged.

        Any address missing from a server's `from_filter` is rewritten to the
        notification address, so asking the filter is the only way to know
        whether a chosen sender survives the trip.  A filter entry may be a
        whole domain as well as a full address - `mail.default.from_filter`
        holds a bare domain on this database - so both forms are matched.
        """
        normalized = (address or "").strip().lower()
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[-1]
        for server in self.env["ir.mail_server"].sudo().search([]):
            for chunk in (server.from_filter or "").split(","):
                chunk = chunk.strip().lower()
                if not chunk:
                    continue
                if chunk == normalized or ("@" not in chunk and chunk == domain):
                    return True
        return False

    def _reza_fsm_credit_note_email_from(self):
        """Sender address the outgoing mail server will accept unchanged.

        Prefer the company's Accounts mailbox, so a credit note a rep raises in
        store comes from the same address as one the office emails from the
        document form.  Odoo may only send as an address listed in the mail
        server's from_filter - and where the tenant has not granted SendAs on
        it, Microsoft refuses the message outright rather than rewriting it -
        so the filter is consulted rather than assumed.  Edbert has no Accounts
        mailbox and correctly falls through to its notifications alias.

        The fallback is the company's own alias domain, which is what this
        method used exclusively before: the company partner address
        (sales@rockos.com.au for Liaise at the time) was not in the filter, so
        mail sent from it went out branded as the Edbert notification address.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        accounts_address = company.partner_id.email or company.email
        if accounts_address and self._reza_fsm_mail_server_accepts(accounts_address):
            return formataddr((company.name or "", accounts_address))
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
                elif event.outcome == "credit_scrap":
                    event._reza_fsm_try_create_scrap()
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

        stock_move = self._reza_fsm_build_stock_move(
            self.return_location_id,
            _("Credit Return: %s") % self.product_id.display_name,
        )
        self.write({"stock_move_id": stock_move.id})
        return stock_move

    def _reza_fsm_build_stock_move(self, destination, description):
        """Bring the goods back from the customer into `destination`, done."""
        self.ensure_one()
        source_location = self._reza_fsm_get_customer_source_location()
        Move = self.env["stock.move"].sudo().with_company(self.company_id)
        move_vals = {
            "company_id": self.company_id.id,
            "product_id": self.product_id.id,
            "product_uom_qty": self.quantity,
            "product_uom": self.product_uom_id.id,
            "location_id": source_location.id,
            "location_dest_id": destination.id,
            "origin": self.move_id.name or self.move_id.invoice_origin or self.move_id.ref,
        }
        if "description_picking" in Move._fields:
            move_vals["description_picking"] = description
        stock_move = Move.create(move_vals)
        self._reza_fsm_finalize_return_stock_move(stock_move)
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

    # ------------------------------------------------------------------
    # Credit Scrap
    # ------------------------------------------------------------------

    def _reza_fsm_try_create_scrap(self):
        """Write scrapped goods off in Inventory, without ever blocking the rep.

        A Credit Scrap used to record a reason and nothing else.  The goods
        left no trace in Inventory while the credit note still posted the
        anglo-saxon COGS reversal, so the ledger said the stock came back when
        no stock came back.  Two legs fix that: a receipt from the customer
        location, then a Scrap Order out of it.  Quantity nets to zero and the
        write-off is visible in Inventory and in Accounting.

        The rep is standing at the counter when this runs, so a scrap that
        cannot be built is recorded for the office and never raised - the
        credit note is correct either way.  Both legs share one savepoint, so
        a failure can never leave received-but-not-scrapped stock behind.
        """
        self.ensure_one()
        if self.scrap_id:
            return self.scrap_id
        try:
            # The savepoint must be INSIDE the try and the except OUTSIDE the
            # with: catching within the block releases the savepoint instead
            # of rolling it back, which would keep the half-done receipt.
            with self.env.cr.savepoint():
                scrap = self._reza_fsm_create_scrap()
        except psycopg2.Error:
            # Let this one through. Odoo retries the whole request on a
            # serialization failure and nothing has committed yet, so
            # swallowing it would hide a recoverable clash from the retry.
            raise
        except Exception as error:  # noqa: BLE001 - recorded, never raised
            _logger.exception(
                "Could not scrap %s for credit note %s",
                self.product_id.display_name,
                self.move_id.name or self.move_id.display_name,
            )
            self._reza_fsm_report_scrap_problem(str(error) or type(error).__name__)
            return False
        if self.reza_scrap_error:
            self.write({"reza_scrap_error": False})
        return scrap

    def _reza_fsm_report_scrap_problem(self, reason):
        """Record why the write-off did not happen, where the office will see it."""
        self.ensure_one()
        message = _(
            "%(product)s was credited as scrap and Odoo could not write it off "
            "in Inventory: %(reason)s"
        ) % {
            "product": self.product_id.display_name,
            "reason": reason,
        }
        self.write({"reza_scrap_error": message[:500]})
        self.move_id.sudo().message_post(body=message)
        return message

    def _reza_fsm_create_scrap(self):
        """Receive the goods back, then scrap them. Raises on any problem."""
        self.ensure_one()
        product = self.product_id
        if not product.is_storable:
            raise UserError(_(
                "%s is not a storable product, so there is no stock to write off."
            ) % product.display_name)
        landing = self._reza_fsm_get_scrap_landing_location()
        receipt = self.stock_move_id
        if not receipt:
            receipt = self._reza_fsm_build_stock_move(
                landing,
                _("Credit Scrap: %s") % product.display_name,
            )
            self.write({"stock_move_id": receipt.id})
        elif receipt.state != "done":
            self._reza_fsm_finalize_return_stock_move(receipt)

        Scrap = self.env["stock.scrap"].sudo().with_company(self.company_id)
        scrap_vals = {
            "product_id": product.id,
            "product_uom_id": self.product_uom_id.id,
            "scrap_qty": self.quantity,
            "location_id": landing.id,
            "company_id": self.company_id.id,
            "origin": self.move_id.name or self.move_id.invoice_origin or self.move_id.ref,
        }
        # A reason is mandatory wherever reza_scrap_reason is installed, and
        # do_scrap() refuses the write-off without one.
        tag = self._reza_fsm_get_scrap_reason_tag()
        if tag:
            scrap_vals["scrap_reason_tag_ids"] = [(6, 0, tag.ids)]
        scrap = Scrap.create(scrap_vals)
        scrap.action_validate()
        if scrap.state != "done":
            # action_validate hands off to a wizard when the quantity is not
            # available, which would leave the receipt standing on its own.
            raise UserError(_(
                "The Scrap Order for %(product)s stayed in %(state)s. There was "
                "not enough stock at %(location)s to write off."
            ) % {
                "product": product.display_name,
                "state": scrap.state,
                "location": landing.display_name,
            })
        self.write({"scrap_id": scrap.id})
        return scrap

    def _reza_fsm_get_scrap_landing_location(self):
        """Where the goods land on paper before they are written off.

        The company's OWN warehouse stock, never `reza_icw_source_location_id`:
        Liaise's source location belongs to Edbert, and scrapping Liaise's
        goods out of Edbert's stock would write off the wrong company's
        inventory.
        """
        self.ensure_one()
        warehouse = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.company_id.id)],
            limit=1,
            order="sequence, id",
        )
        location = warehouse.lot_stock_id
        if not location:
            raise UserError(_(
                "%s has no warehouse stock location to receive the scrapped "
                "goods into."
            ) % self.company_id.display_name)
        return location

    def _reza_fsm_get_scrap_reason_tag(self):
        """Turn the rep's scrap reason into a Scrap Order reason tag.

        The two vocabularies are separate models, and the credit reasons ship
        under noupdate="1" - a mapping written into that data file would never
        reach a database that already holds the records.  So resolve it in
        code, and let the optional tag on the reason override it by hand.
        """
        self.ensure_one()
        Tag = self.env["stock.scrap.reason.tag"].sudo()
        # A tag flagged as a staff purchase can never be validated from here:
        # reza_scrap_reason refuses do_scrap() until a person is named, and
        # nobody took these goods. Picking one would disable the write-off for
        # that reason silently.
        domain = []
        if "reza_requires_staff_member" in Tag._fields:
            domain = [("reza_requires_staff_member", "=", False)]
        reason = self.scrap_reason_id
        if reason.scrap_reason_tag_id:
            return reason.scrap_reason_tag_id
        if reason.name:
            match = Tag.search(domain + [("name", "=ilike", reason.name)], limit=1)
            if match:
                return match
        fallback = self.env.ref(
            "reza_scrap_reason.scrap_reason_customer_return",
            raise_if_not_found=False,
        )
        if fallback:
            return fallback
        return Tag.search(
            domain + [("name", "=ilike", "Customer Return - Unsaleable")], limit=1)

    def action_reza_fsm_create_scrap(self):
        """Build a missing Scrap Order by hand, from the office.

        This one DOES raise. A rep mid-transaction must not be stopped, but an
        office user who pressed the button is asking what went wrong.

        The `groups=` on the button hides it; it does not stop an RPC caller,
        and a rep holds write on their own events while this method writes
        stock and posts a journal entry under sudo(). So the entitlement is
        checked here as well. `sudo()` does not satisfy `has_group`, which is
        what makes this guard real rather than decorative.
        """
        if not (
            self.env.user.has_group("reza_field_service_buttons.group_fsm_controllers")
            or self.env.user.has_group("base.group_system")
        ):
            raise UserError(_(
                "Only the office can create a Scrap Order from a Credit Scrap. "
                "It writes stock off and posts a journal entry."
            ))
        for event in self:
            if event.outcome != "credit_scrap":
                raise UserError(_(
                    "%s is not a Credit Scrap, so it has nothing to write off."
                ) % event.display_name)
            if event.scrap_id:
                continue
            # Roll the receipt back if the scrap half fails, then re-raise so
            # the office sees the reason. Without this a shell or RPC caller
            # would be left holding a receipt with no write-off against it -
            # the browser is only safe because the request rolls itself back.
            with self.env.cr.savepoint():
                event._reza_fsm_create_scrap()
            event.write({"reza_scrap_error": False})
        return True
