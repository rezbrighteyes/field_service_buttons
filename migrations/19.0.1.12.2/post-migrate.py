# -*- coding: utf-8 -*-
"""Repair credits that were open on screen when the model became stored.

1. Rep = OdooBot.  Adding the `user_id` column fills existing rows with the
   field default, and that default ran as the superuser - so every credit
   carried over from the transient table got OdooBot as its rep.  The
   19.0.1.12.0 script only filled NULLs, so it changed nothing.  Take the rep
   from `create_uid` instead.
2. No draft credit note.  The draft is made by the line sync, which never ran
   for lines entered before the upgrade.  Run it once for every unfinished
   credit that has products, so the office sees them in Accounting.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE reza_fsm_credit_return_wizard
           SET user_id = create_uid
         WHERE (user_id IS NULL OR user_id = %s)
           AND create_uid IS NOT NULL
           AND create_uid <> %s
        """,
        (SUPERUSER_ID, SUPERUSER_ID),
    )
    _logger.info("reza_field_service_buttons: repaired the rep on %s credits", cr.rowcount)

    env = api.Environment(cr, SUPERUSER_ID, {})
    credits = env["reza.fsm.credit.return.wizard"].search([
        ("state", "=", "draft"),
        ("move_id", "=", False),
        ("line_ids.product_id", "!=", False),
    ])
    for credit in credits:
        try:
            with cr.savepoint():
                credit.with_company(credit.company_id)._reza_fsm_sync_draft_credit_note()
        except Exception:
            _logger.exception("reza_field_service_buttons: could not draft credit %s", credit.id)
    _logger.info("reza_field_service_buttons: drafted credit notes for %s carried-over credits", len(credits))
