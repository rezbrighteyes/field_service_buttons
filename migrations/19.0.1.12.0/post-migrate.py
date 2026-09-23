# -*- coding: utf-8 -*-
"""Credit / return became a stored model (it was a TransientModel).

Odoo converts the model in place: the table, its rows and its foreign keys
stay, `ir_model.transient` is updated by the reflection, and the vacuum simply
stops clearing the rows.  The only gap is the new `user_id` (Rep) column,
which the record rules read, so fill it from `create_uid` for the rows that
survived the last vacuum.  Without it those rows would be invisible to the
rep who started them.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE reza_fsm_credit_return_wizard
           SET user_id = create_uid
         WHERE user_id IS NULL
           AND create_uid IS NOT NULL
        """
    )
    _logger.info(
        "reza_field_service_buttons: set the rep on %s existing credit / return rows",
        cr.rowcount,
    )
