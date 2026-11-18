import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

## Columns of the two group settings that the rules replace, with the
## name and holder domain of the rule each one becomes.
OLD_GROUP_COLUMNS = (
    ("cyclos_user_group_person", "Persons", "[('is_company', '=', False)]", 10),
    ("cyclos_user_group_company", "Companies", "[('is_company', '=', True)]", 20),
)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    """Replace the person and company group settings of Cyclos currencies by
    group rules giving the same groups, then drop their columns."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    for column, name, domain, sequence in OLD_GROUP_COLUMNS:
        if not _column_exists(cr, "res_alt_currency", column):
            continue
        cr.execute(
            f"SELECT id, {column} FROM res_alt_currency "
            f"WHERE engine = 'cyclos' AND coalesce(trim({column}), '') != ''"
        )
        rows = cr.fetchall()
        env["cyclos.group.rule"].create(
            [
                {
                    "alt_currency_id": currency_id,
                    "name": name,
                    "sequence": sequence,
                    "partner_domain": domain,
                    "cyclos_group": group.strip(),
                }
                for currency_id, group in rows
            ]
        )
        _logger.info("Cyclos group rule %r created on %s currencies", name, len(rows))
        cr.execute(f"ALTER TABLE res_alt_currency DROP COLUMN {column}")
