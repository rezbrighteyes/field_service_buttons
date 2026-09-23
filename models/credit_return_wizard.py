# -*- coding: utf-8 -*-
import base64

import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.osv import expression
from odoo.tools import float_compare


_logger = logging.getLogger(__name__)

# Context key: suppress the per-line draft credit note sync while a batch of
# line changes is written, so the sync runs once at the end instead.
SKIP_DRAFT_SYNC = "reza_fsm_skip_draft_credit_sync"

# Line fields that change what the draft credit note says.
DRAFT_SYNC_LINE_FIELDS = {
    "product_id",
    "quantity",
    "product_uom_id",
    "price_unit",
    "outcome",
    "return_location_id",
    "credit_reason_ids",
    "scrap_reason_id",
    "note",
}


class CreditReturnWizard(models.Model):
    """A rep's credit / return for one customer visit.

    This was a TransientModel until 19.0.1.12.0. A rep who left the screen
    lost the whole credit when the vacuum cleared the transient rows - at
    least 31 signed credits went that way on production (Aug-Sep 2026). It
    is now a stored record the rep can reopen from the visit, and it keeps a DRAFT out_refund in step with
    its lines, so the office can see an unfinished credit in Accounting.
    The model keeps its old name so the views and the modules that extend it
    (reza_rep_return_docket) keep working.
    """

    _name = "reza.fsm.credit.return.wizard"
    _inherit = "product.catalog.mixin"
    _description = "Field Service Credit / Return"
    _order = "id desc"

    # cascade matches the foreign key the transient model already had, so
    # converting the model does not change the table.
    task_id = fields.Many2one(
        "project.task", required=True, readonly=True, index=True, ondelete="cascade",
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        related="task_id.partner_id",
        readonly=True,
    )
    # Stored so the multi-company record rule can filter on it.
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        related="task_id.company_id",
        readonly=True,
        store=True,
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Rep",
        default=lambda self: self.env.user,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Confirmed"), ("cancel", "Cancelled")],
        default="draft",
        readonly=True,
        index=True,
    )
    credit_note_id = fields.Integer(copy=False, readonly=True)
    credit_note_name = fields.Char(string="Credit Note", copy=False, readonly=True)
    # The out_refund this credit fills in.  It is a draft until the rep
    # confirms, and the same record is posted then.  Deleting it in
    # Accounting clears the link, and the next line change makes a new one.
    move_id = fields.Many2one(
        "account.move",
        string="Draft Credit Note",
        copy=False,
        readonly=True,
        ondelete="set null",
        index=True,
    )
    # Plain copy of the last draft credit note id.  It survives the office
    # deleting the draft, so the log can say the draft was recreated.
    last_move_ref_id = fields.Integer(copy=False, readonly=True)
    log_ids = fields.One2many(
        "reza.fsm.credit.return.log",
        "credit_return_id",
        string="Log",
        readonly=True,
    )
    has_draft_credit_note = fields.Boolean(
        compute="_compute_has_draft_credit_note",
        compute_sudo=True,
    )
    line_count = fields.Integer(compute="_compute_line_count", string="Products")
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
    bulk_return_location_id = fields.Many2one(
        "stock.location",
        string="Fill Return Location",
        domain="[('id', 'in', allowed_return_location_ids)]",
        copy=False,
        help="Pick the van or shed, then press Fill Return Lines to apply it to "
             "every Credit Return line. Credit Scrap lines are never touched.",
    )
    bulk_credit_reason_ids = fields.Many2many(
        "reza.fsm.credit.return.reason",
        "reza_fsm_credit_return_wizard_bulk_reason_rel",
        "wizard_id",
        "reason_id",
        string="Fill Credit Reasons",
        domain=[("reason_type", "in", ("credit", "both"))],
        copy=False,
    )
    bulk_note = fields.Text(
        string="Fill Note",
        copy=False,
        help="Optional. Required by the reasons that ask for a note (Other).",
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

    @api.depends("move_id", "move_id.state")
    def _compute_has_draft_credit_note(self):
        for wizard in self:
            wizard.has_draft_credit_note = bool(
                wizard.move_id and wizard.move_id.state == "draft"
            )

    @api.depends("line_ids")
    def _compute_line_count(self):
        for wizard in self:
            wizard.line_count = len(wizard.line_ids)

    @api.depends("task_id", "credit_note_name", "create_date")
    def _compute_display_name(self):
        for wizard in self:
            if wizard.credit_note_name:
                wizard.display_name = wizard.credit_note_name
            else:
                wizard.display_name = _("Credit - %(task)s (unfinished)") % {
                    "task": wizard.task_id.display_name or "",
                }

    def write(self, vals):
        if "line_ids" in vals and not self.env.context.get(SKIP_DRAFT_SYNC):
            # A form save sends every line change at once.  Sync the draft
            # credit note once afterwards rather than once per line.
            result = super(
                CreditReturnWizard, self.with_context(**{SKIP_DRAFT_SYNC: True})
            ).write(vals)
            self._reza_fsm_sync_draft_credit_note()
        else:
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
                self.env["reza.fsm.credit.return.log"]._reza_fsm_log(
                    "sign", wizard, note=wizard.signed_by,
                )
        return result

    def unlink(self):
        Log = self.env["reza.fsm.credit.return.log"]
        for wizard in self:
            Log._reza_fsm_log(
                "credit_deleted",
                wizard,
                [Log._reza_fsm_line_values(line) for line in wizard.line_ids],
            )
        return super().unlink()

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

    def action_fill_return_lines(self):
        """Stamp one location / reason / note onto every Credit Return line.

        Reps hand back a whole van load for the same reason, so keying the same
        two fields onto twenty lines is where the mistakes happen.  Credit Scrap
        lines are deliberately skipped - a scrap has no return location and its
        reason comes from a different list.
        """
        self.ensure_one()
        if self.state != "draft":
            raise ValidationError(_(
                "This credit return is already confirmed or cancelled."
            ))
        location = self.bulk_return_location_id
        reasons = self.bulk_credit_reason_ids
        note = (self.bulk_note or "").strip()
        if not location and not reasons and not note:
            raise ValidationError(_(
                "Choose a return location, a credit reason or a note to fill in first."
            ))
        if location and location not in self.allowed_return_location_ids:
            raise ValidationError(_(
                "%s is not one of your return locations."
            ) % location.display_name)
        lines = self.line_ids.filtered(
            lambda line: line.product_id and line.outcome == "credit_return"
        )
        if not lines:
            raise ValidationError(_(
                "There are no Credit Return lines to fill in. Credit Scrap lines "
                "are left alone on purpose."
            ))
        values = {}
        if location:
            values["return_location_id"] = location.id
        if reasons:
            values["credit_reason_ids"] = [(6, 0, reasons.ids)]
        if note:
            values["note"] = note
        lines.write(values)
        return False

    @api.depends("task_id", "company_id", "user_id")
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
        if self.state == "cancel":
            raise ValidationError(_("This credit return was cancelled."))
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
        # Post the draft this credit has kept in step with its lines, rather
        # than raising a second credit note.  The sync below also makes the
        # draft when the office deleted it while the rep had the credit open.
        self._reza_fsm_sync_draft_credit_note()
        move = self._reza_fsm_get_draft_credit_note()
        if not move:
            raise ValidationError(_("The draft credit note could not be created."))
        Event = self.env["reza.fsm.credit.return.event"].sudo().with_company(self.company_id)
        # Save the customer signature separately once the credit note exists.
        # Account moves can add defaults during create, so writing this directly
        # to the record guarantees the Tax Credit Note report receives it.
        # The signed PDF is attached once after posting below.  The date is
        # the day the rep confirms, not the day the draft was started.
        move.with_context(reza_fsm_skip_signed_credit_note_attachment=True).write({
            "invoice_date": fields.Date.context_today(self),
            "invoice_origin": self.task_id.name,
            "reza_fsm_customer_signature": self.signature,
            "reza_fsm_customer_signed_by": self.signed_by or self.partner_id.name,
            "reza_fsm_customer_signed_on": self.signed_on or fields.Datetime.now(),
        })

        for wizard_line in lines:
            product_uom = wizard_line.product_uom_id or wizard_line.product_id.uom_id
            move_line = wizard_line.sudo().move_line_id
            if not move_line or move_line.move_id != move:
                raise ValidationError(_(
                    "The draft credit note is missing the line for %s. "
                    "Close the credit and open it again."
                ) % wizard_line.product_id.display_name)
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

        move.with_context(reza_fsm_credit_return_confirm=True).action_post()
        move._reza_fsm_attach_signed_credit_note()
        self.write({
            "state": "done",
            "credit_note_id": move.id,
            "credit_note_name": move.name,
        })
        Log = self.env["reza.fsm.credit.return.log"]
        Log._reza_fsm_log(
            "confirm", self, [Log._reza_fsm_line_values(line) for line in lines],
        )
        self._reza_fsm_schedule_credit_note_email(move)

        return {
            "type": "ir.actions.act_window",
            "name": _("Credit / Return"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Draft credit note kept in step with the lines
    # ------------------------------------------------------------------
    def _reza_fsm_get_draft_credit_note(self, create=False):
        """Return the draft out_refund of this credit, sudo'd to its company.

        Reps have read-only access to account.move, so every accounting
        write here runs as sudo, pinned to the credit's company - the same
        pattern the confirm step has always used.  The rep's access to the
        visit is checked by the callers.

        Raises when the office has already posted or cancelled the draft:
        the rep must not change a credit note that Accounting has taken over.
        """
        self.ensure_one()
        move = self.sudo().move_id.with_company(self.company_id)
        if move:
            if move.state == "posted":
                raise ValidationError(_(
                    "The office has already posted credit note %s for this "
                    "credit. Ask the office to make any change."
                ) % (move.name or ""))
            if move.state == "cancel":
                raise ValidationError(_(
                    "The office has cancelled the draft credit note for this "
                    "credit. Ask the office before you continue."
                ))
            return move
        if not create:
            return move
        if not self.partner_id:
            raise ValidationError(_("This task has no customer set."))
        move = self.env["account.move"].sudo().with_company(self.company_id).create({
            "move_type": "out_refund",
            "partner_id": self.partner_id.id,
            "partner_shipping_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "invoice_origin": self.task_id.name,
            "reza_fsm_task_id": self.task_id.id,
            "reza_fsm_credit_return_id": self.id,
        })
        previous_move_ref = self.last_move_ref_id
        self.sudo().with_context(**{SKIP_DRAFT_SYNC: True}).write({
            "move_id": move.id,
            "last_move_ref_id": move.id,
        })
        if previous_move_ref:
            self.env["reza.fsm.credit.return.log"]._reza_fsm_log(
                "draft_recreated",
                self,
                note=_("The draft credit note %s was gone; a new one was made.")
                % previous_move_ref,
            )
        return move

    def _reza_fsm_credit_move_line_values(self, wizard_line):
        product_uom = wizard_line.product_uom_id or wizard_line.product_id.uom_id
        return {
            "product_id": wizard_line.product_id.id,
            "quantity": wizard_line.quantity,
            "product_uom_id": product_uom.id,
            "price_unit": wizard_line.price_unit,
            "name": wizard_line.product_id.display_name,
            "reza_fsm_credit_return_outcome": wizard_line.outcome,
            "reza_fsm_credit_return_location_id": wizard_line.return_location_id.id,
            "reza_fsm_credit_reason_ids": [(6, 0, wizard_line.credit_reason_ids.ids)],
            "reza_fsm_scrap_reason_id": wizard_line.scrap_reason_id.id,
            "reza_fsm_credit_note": wizard_line.note,
        }

    @api.model
    def _reza_fsm_move_line_differs(self, move_line, values):
        """True when writing `values` would change `move_line`.

        Only a real change is written, so opening and saving the credit does
        not keep rewriting the draft (and its taxes) for nothing.
        """
        for field_name, value in values.items():
            field = move_line._fields[field_name]
            current = move_line[field_name]
            if field.type == "many2many":
                if set(current.ids) != set(value[0][2]):
                    return True
            elif field.type == "many2one":
                if current.id != (value or False):
                    return True
            elif field.type == "float":
                if float_compare(current or 0.0, value or 0.0, precision_digits=6):
                    return True
            elif (current or False) != (value or False):
                return True
        return False

    def _reza_fsm_sync_draft_credit_note(self):
        """Make the draft credit note match the credit lines.

        The first product creates the draft, so the office can see the
        unfinished credit in Accounting from then on.  Each line keeps a link
        to its credit note line: a changed line updates it, a new line adds
        one, and a line the rep deleted takes its credit note line with it.
        Lines the office added by hand (no credit outcome) are left alone.
        A draft that loses every line is kept, empty, and reused.
        """
        for wizard in self.exists():
            if wizard.state != "draft":
                continue
            lines = wizard.line_ids.filtered("product_id")
            move = wizard._reza_fsm_get_draft_credit_note(create=bool(lines))
            if not move:
                continue
            if move.partner_id != wizard.partner_id and wizard.partner_id:
                move.write({
                    "partner_id": wizard.partner_id.id,
                    "partner_shipping_id": wizard.partner_id.id,
                })
            MoveLine = self.env["account.move.line"].sudo().with_company(wizard.company_id)
            kept = MoveLine
            for wizard_line in lines:
                values = wizard._reza_fsm_credit_move_line_values(wizard_line)
                move_line = wizard_line.sudo().move_line_id
                if move_line and move_line.move_id == move:
                    if wizard._reza_fsm_move_line_differs(move_line, values):
                        move_line.write(values)
                else:
                    move_line = MoveLine.create({**values, "move_id": move.id})
                    wizard_line.sudo().with_context(**{SKIP_DRAFT_SYNC: True}).write({
                        "move_line_id": move_line.id,
                    })
                kept |= move_line
            stale = move.invoice_line_ids.filtered(
                lambda line: line.reza_fsm_credit_return_outcome and line not in kept
            )
            if stale:
                stale.unlink()
        return True

    def _reza_fsm_schedule_credit_note_email(self, move):
        """Email the customer once this transaction has safely committed.

        Sending inline would put the credit note in the customer's inbox before
        the transaction that created it is durable, and mail.mail._send re-raises
        SMTPServerDisconnected before it honours raise_exception=False - so a
        transient Microsoft 365 fault would roll the whole credit return back.
        A post-commit callback runs after the commit, on its own cursor, and is
        discarded for free if this transaction is rolled back instead.
        """
        self.ensure_one()
        task = self.task_id
        if not move:
            return False
        # Finance gate (2026-08-10): credit notes must be reviewed before the
        # customer sees them, so automatic sending is OFF unless someone turns
        # it on deliberately. Settings > Technical > System Parameters,
        # `reza_fsm.credit_note_auto_send` = True re-enables it with no deploy.
        # The note is still raised and posted; only the email is withheld. The
        # office sends it by hand from the credit note in Accounting - the rep
        # has no send button (2026-08-13).
        auto_send = self.env["ir.config_parameter"].sudo().get_param(
            "reza_fsm.credit_note_auto_send", "False"
        )
        if str(auto_send).strip().lower() not in ("true", "1", "yes"):
            task.sudo().message_post(
                body=_(
                    "Credit note %s was NOT emailed to the customer: automatic "
                    "sending is switched off pending finance review. Send it "
                    "from the credit note in Accounting once the pricing has "
                    "been checked."
                ) % (move.name or ""),
                subtype_xmlid="mail.mt_note",
            )
            return False
        if not move.partner_id.email:
            task.sudo().message_post(
                body=_(
                    "Credit note %s was not emailed: the customer has no email "
                    "address. Add one and send it from Accounting."
                ) % (move.name or ""),
                subtype_xmlid="mail.mt_note",
            )
            return False

        move_id = move.id
        task_id = task.id
        author_id = self.env.user.partner_id.id
        registry = self.env.registry

        def _send():
            # Callbacks.run() does not guard callbacks, so an exception escaping
            # here would break the request that just committed the credit note.
            try:
                with registry.cursor() as cr:
                    send_env = api.Environment(cr, SUPERUSER_ID, {})
                    credit_note = send_env["account.move"].browse(move_id).exists()
                    if not credit_note:
                        return
                    mail = credit_note._reza_fsm_send_credit_note_email(
                        author_id=author_id
                    )
                    if mail:
                        body = _("Credit note %s was emailed to %s.") % (
                            credit_note.name,
                            mail.email_to,
                        )
                    else:
                        body = _(
                            "Credit note %s could not be emailed. Send it from "
                            "Accounting, or ask the office to check the mail setup."
                        ) % (credit_note.name,)
                    send_env["project.task"].browse(task_id).message_post(
                        body=body,
                        subtype_xmlid="mail.mt_note",
                        author_id=author_id,
                    )
            except Exception:
                _logger.exception(
                    "Could not email FSM credit note %s to the customer", move_id
                )

        self.env.cr.postcommit.add(_send)
        return True

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
        # Repair an already-confirmed return when it was signed before the
        # dedicated credit-note signature fields were introduced.
        if self.signature and not move.reza_fsm_customer_signature:
            move.with_context(reza_fsm_skip_signed_credit_note_attachment=True).write({
                "reza_fsm_customer_signature": self.signature,
                "reza_fsm_customer_signed_by": self.signed_by or self.partner_id.name,
                "reza_fsm_customer_signed_on": self.signed_on or fields.Datetime.now(),
            })
        filename = "%s_credit_note.pdf" % (move.name or self.credit_note_name)
        Attachment = self.env["ir.attachment"].sudo()
        attachment = Attachment.search([
            ("res_model", "=", "project.task"),
            ("res_id", "=", self.task_id.id),
            ("name", "=", filename),
        ], limit=1)
        report = self.env["ir.actions.report"].sudo().with_company(
            self.company_id
        ).with_context(allowed_company_ids=[self.company_id.id])
        pdf, _content_type = report._render_qweb_pdf(
            "account.account_invoices", move.id
        )
        if attachment:
            attachment.write({
                "datas": base64.b64encode(pdf),
                "mimetype": "application/pdf",
            })
            return attachment
        return Attachment.create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf),
            "mimetype": "application/pdf",
            "res_model": "project.task",
            "res_id": self.task_id.id,
        })

    # Both buttons below were removed from the wizard header on 2026-08-13:
    # reps neither print nor email their own credit notes, the office does both
    # from the credit note in Accounting. The methods are kept so restoring the
    # buttons is a view change alone.
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
            "subject": move._reza_fsm_credit_note_email_subject(),
            "body_html": move._reza_fsm_credit_note_email_body(),
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Email Credit Note"),
            "res_model": "reza.fsm.credit.return.send.wizard",
            "res_id": send_wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_open_customer_task(self):
        """Return the rep to the customer visit that started this return."""
        self.ensure_one()
        self.task_id.check_access_rights("read")
        self.task_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": _("Field Service Task"),
            "res_model": "project.task",
            "res_id": self.task_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_cancel_credit_return(self):
        """Leave the credit and go back to the visit.

        Nothing is deleted: the credit and its draft credit note stay, and the
        rep reopens them from the visit with the Credit / Return button.
        """
        self.ensure_one()
        if self.state != "done":
            self.env["reza.fsm.credit.return.log"]._reza_fsm_log("save_back", self)
        return {
            "type": "ir.actions.act_window",
            "name": _("Field Service Task"),
            "res_model": "project.task",
            "res_id": self.task_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_discard_credit_return(self):
        """Cancel an unfinished credit the rep does not want.

        The draft credit note is deleted.  The credit itself is kept as
        Cancelled, not deleted, and every product on it goes to the log, so
        the office can still see what was started and dropped.
        """
        self.ensure_one()
        if self.state != "draft":
            raise ValidationError(_("Only an unfinished credit can be cancelled."))
        Log = self.env["reza.fsm.credit.return.log"]
        Log._reza_fsm_log(
            "cancel", self, [Log._reza_fsm_line_values(line) for line in self.line_ids],
        )
        move = self.sudo().move_id
        self.write({"state": "cancel"})
        if move and move.state == "draft":
            move.with_company(self.company_id).unlink()
        return self.action_open_customer_task()

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

    def _is_readonly(self):
        self.ensure_one()
        return self.state != "draft"

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
                "price": self._reza_credit_price(product),
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
            return self._reza_credit_price(product)

        values = {
            "quantity": quantity,
            "product_uom_id": product.uom_id.id,
            "price_unit": self._reza_credit_price(product, quantity),
        }
        if line:
            line.write(values)
        else:
            self.env["reza.fsm.credit.return.wizard.line"].create({
                **values,
                "wizard_id": self.id,
                "product_id": product.id,
            })
        return self._reza_credit_price(product, quantity)

    def _reza_credit_price(self, product, quantity=1.0):
        """The price this customer is actually owed for one unit.

        The credit-return flow used to hand back ``product.lst_price`` -- the
        catalogue price -- ignoring the customer's pricelist entirely. For a
        customer on a 15% pricelist that credits 15% more than they paid, and
        because the created move line passes ``price_unit`` in its create
        values, the line is protected from ``_compute_price_unit`` and
        ``d3_product_rrp``'s pricelist logic can never correct it. Measured on
        production 2026-08-04: $375.72 over-credited across four notes.

        Falls back to the catalogue price whenever a pricelist cannot be
        resolved, which is the same value as before -- so this can only ever
        move a price toward what the customer was charged, never away from it.
        """
        if not product:
            return 0.0
        company = self.company_id or self.env.company
        product = product.with_company(company)
        partner = self.partner_id
        if not partner:
            return product.lst_price
        pricelist = partner.with_company(company).property_product_pricelist
        if not pricelist:
            return product.lst_price
        try:
            price = pricelist._get_product_price(
                product, quantity or 1.0, uom=product.uom_id,
            )
        except Exception:  # noqa: BLE001 - a price lookup must never block a credit
            _logger.exception(
                "FSM credit return: pricelist lookup failed for product %s; "
                "falling back to the catalogue price.", product.id,
            )
            return product.lst_price
        if price is None or price < 0:
            return product.lst_price
        return price

    def _get_allowed_return_locations(self):
        """The van / shed list comes from the credit's REP, not the viewer.

        Several reps are also intercompany warehouse managers, which used to
        open every internal location (other reps' vans, LWH/Stock, WH/Stock)
        to them here. Office staff opening a rep's stored credit must see the
        rep's locations too. An FSM Controller gets the full list, but only on
        SOMEONE ELSE's credit - several reps are controllers as well, and on
        their own credit they still get just their own van / shed.
        """
        self.ensure_one()
        Location = self.env["stock.location"]
        # sudo: only the credit's own rep and company are read, and a viewer
        # who is not the rep may not pass the credit's record rule.
        credit = self.sudo()
        company = credit.company_id or self.env.company
        rep = credit.user_id or self.env.user
        if rep != self.env.user and self.env.user.has_group(
            "reza_field_service_buttons.group_fsm_controllers"
        ):
            return Location.search([
                ("usage", "=", "internal"),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", company.id),
            ])

        assigned_locations = rep.sudo().reza_icw_allowed_rep_location_ids.filtered(
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
        mail = move._reza_fsm_send_credit_note_email(
            email_to=email_to,
            subject=self.subject,
            body_html=self.body_html,
            author_id=self.env.user.partner_id.id,
        )
        if not mail:
            raise ValidationError(_(
                "The credit note could not be emailed. Ask the office to check "
                "the outgoing mail settings."
            ))
        credit_return.task_id.sudo().message_post(
            body=_("Credit note %s was emailed to %s.") % (move.name, email_to),
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


class CreditReturnWizardLine(models.Model):
    _name = "reza.fsm.credit.return.wizard.line"
    _description = "Field Service Credit / Return Line"

    wizard_id = fields.Many2one(
        "reza.fsm.credit.return.wizard",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="wizard_id.company_id",
        store=True,
        index=True,
    )
    # The matching line on the draft credit note.  Cleared when the office
    # deletes that line or the whole draft; the next sync then recreates it.
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Credit Note Line",
        copy=False,
        readonly=True,
        ondelete="set null",
        index="btree_not_null",
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

    # Every add, change and remove is written to reza.fsm.credit.return.log
    # after the draft sync, so the row carries the draft credit note id.  The
    # log is written even when the sync is deferred (form save): the product
    # values are what matter, and they are known here.
    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        if not self.env.context.get(SKIP_DRAFT_SYNC):
            lines.wizard_id._reza_fsm_sync_draft_credit_note()
        Log = self.env["reza.fsm.credit.return.log"]
        for line in lines:
            Log._reza_fsm_log("add", line.wizard_id, [Log._reza_fsm_line_values(line)])
        return lines

    def write(self, vals):
        tracked = DRAFT_SYNC_LINE_FIELDS.intersection(vals)
        Log = self.env["reza.fsm.credit.return.log"]
        before = {line.id: Log._reza_fsm_line_values(line) for line in self} if tracked else {}
        result = super().write(vals)
        if tracked and not self.env.context.get(SKIP_DRAFT_SYNC):
            self.wizard_id._reza_fsm_sync_draft_credit_note()
        for line in self if tracked else ():
            old = before[line.id]
            new = Log._reza_fsm_line_values(line)
            changed = [
                key for key in (
                    "product_name", "quantity", "uom_name", "price_unit", "outcome",
                    "return_location_name", "reasons", "note",
                )
                if (old[key] or False) != (new[key] or False)
            ]
            if not changed:
                continue
            Log._reza_fsm_log(
                "change",
                line.wizard_id,
                [new],
                old_quantity=old["quantity"],
                old_price_unit=old["price_unit"],
                changes=", ".join(
                    "%s: %s -> %s" % (key, old[key] or "", new[key] or "")
                    for key in changed
                ),
            )
        return result

    def unlink(self):
        Log = self.env["reza.fsm.credit.return.log"]
        removed = [(line.wizard_id, Log._reza_fsm_line_values(line)) for line in self]
        wizards = self.wizard_id
        result = super().unlink()
        if not self.env.context.get(SKIP_DRAFT_SYNC):
            # The sync deletes the credit note lines these lines left behind.
            wizards.exists()._reza_fsm_sync_draft_credit_note()
        for wizard, line_values in removed:
            if wizard.exists():
                Log._reza_fsm_log("remove", wizard, [line_values])
        return result

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
            self.price_unit = self.wizard_id._reza_credit_price(
                self.product_id, self.quantity or 1.0,
            )

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
