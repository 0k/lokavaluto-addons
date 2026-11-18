from odoo import _, api, fields, models


class CyclosGroupRulePreview(models.TransientModel):
    """Show which Cyclos group rule a contact would match, without creating
    anything in Odoo or Cyclos."""

    _name = "cyclos.group.rule.preview"
    _description = "Preview of the Cyclos group of a new wallet"

    alt_currency_id = fields.Many2one(
        "res.alt.currency",
        string="Currency",
        required=True,
        domain="[('engine', '=', 'cyclos')]",
    )
    partner_id = fields.Many2one("res.partner", string="Holder", required=True)
    rule_id = fields.Many2one(
        "cyclos.group.rule",
        string="Matching rule",
        compute="_compute_rule_id",
    )
    cyclos_group = fields.Char(string="Cyclos group", related="rule_id.cyclos_group")
    message = fields.Char(compute="_compute_rule_id")

    @api.depends("alt_currency_id", "partner_id")
    def _compute_rule_id(self):
        for preview in self:
            preview.rule_id = False
            preview.message = False
            if not (preview.alt_currency_id and preview.partner_id):
                continue
            preview.rule_id = preview.alt_currency_id.cyclos_group_rule_for_partner(
                preview.partner_id
            )
            if not preview.rule_id:
                preview.message = _(
                    "No rule matches this holder: Odoo would refuse to create "
                    "their Cyclos wallet."
                )
