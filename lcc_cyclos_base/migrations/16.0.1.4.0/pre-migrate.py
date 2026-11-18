import logging

_logger = logging.getLogger(__name__)

TEMPLATE_XMLID = ("lcc_cyclos_base", "mail_template_cyclos_password_setup")


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    """Move to password setup links that open a Cyclos session.

    The currency kept the address of the Cyclos forgotten password page;
    it now keeps the address of the Cyclos web interface, where Odoo sends
    the holder logged in: only the scheme and host of the old value are
    kept. The password setup e-mail template described the forgotten
    password steps: it is removed, so that the module update creates it
    again, with its translations (a template protected from updates is
    otherwise never reloaded).
    """
    if _column_exists(cr, "res_alt_currency", "cyclos_password_setup_url"):
        cr.execute(
            "ALTER TABLE res_alt_currency "
            "RENAME COLUMN cyclos_password_setup_url TO cyclos_web_url"
        )
        cr.execute(
            "UPDATE res_alt_currency "
            "SET cyclos_web_url = substring(cyclos_web_url from '^[a-z]+://[^/?#]+') "
            "WHERE cyclos_web_url IS NOT NULL"
        )
        _logger.info("Cyclos web address of %s currencies kept", cr.rowcount)
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = %s AND name = %s "
        "AND model = 'mail.template' RETURNING res_id",
        TEMPLATE_XMLID,
    )
    template_ids = [row[0] for row in cr.fetchall()]
    if template_ids:
        cr.execute("DELETE FROM mail_template WHERE id = ANY(%s)", (template_ids,))
        _logger.info("Old Cyclos password setup e-mail template removed")
