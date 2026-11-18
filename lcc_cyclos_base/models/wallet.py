import json
import logging
import secrets
from urllib.parse import urlencode

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.lcc_lokavaluto_app_connection import tools

from .res_partner import CYCLOS_LOGIN_PRINCIPAL_TYPE

_logger = logging.getLogger(__name__)

## Odoo route of the link e-mailed to set a new holder's Cyclos password.
PASSWORD_SETUP_ROUTE = "/cyclos/set-password"
PASSWORD_SETUP_TOKEN_BYTES = 32
## Internal name of the Cyclos channel of its web interface.
CYCLOS_WEB_CHANNEL = "main"
## Internal route of a Cyclos content page, followed by its internal name.
CYCLOS_CONTENT_PAGE_ROUTE = "page-content!page="
## Status of a Cyclos password the holder chose themselves.
CYCLOS_PASSWORD_CHOSEN_STATUS = "active"


class ResPartnerBackend(models.Model):
    """Add backend commom property for local currency."""

    _inherit = "res.partner.backend"

    cyclos_create_response = fields.Text(string="Cyclos create response", tracking=True)
    cyclos_id = fields.Char(string="Cyclos id", tracking=True)
    cyclos_login = fields.Char(
        string="Cyclos login",
        help="Login of the Cyclos user of this wallet, as Cyclos returned "
        "it at creation (Cyclos may generate it).",
        tracking=True,
    )
    cyclos_status = fields.Char(string="Cyclos Status", tracking=True)
    cyclos_initial_password = fields.Char(
        string="Cyclos temporary password",
        help="Password Odoo gave the Cyclos user at creation, kept until "
        "Cyclos reports that the holder chose their own.",
        groups="base.group_system",
        copy=False,
    )
    cyclos_password_setup_token = fields.Char(
        string="Cyclos password setup token",
        help="Secret part of the link of the last password setup e-mail.",
        groups="base.group_system",
        copy=False,
        index=True,
    )
    cyclos_password_pending = fields.Boolean(
        string="Cyclos password not chosen yet",
        help="The holder has not chosen their Cyclos password yet: Odoo "
        "still keeps the temporary password.",
        copy=False,
        tracking=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("cyclos_id"):
                vals["ident"] = vals.get("cyclos_id")
        return super(ResPartnerBackend, self).create(vals_list)

    def write(self, vals):
        if vals.get("cyclos_id"):
            vals["ident"] = vals.get("cyclos_id")
        return super(ResPartnerBackend, self).write(vals)

    def get_wallet_json_data(self):
        """Returns normalized wallet data in JSON

        By default, and if no wallet in self, only return alt_currency json data.
        Need to be overrided by financial backend add-ons.

        """

        data = super().get_wallet_json_data()
        if self.cyclos_id:
            data["accounts"].append(
                {
                    "owner_id": self.cyclos_id,
                    "url": self.alt_currency_id.cyclos_server_url,
                    "active": self.status == "active",
                    "is_topup_allowed": self.is_topup_allowed,
                    "is_payment_request_allowed": self.is_payment_request_allowed,
                },
            )

        safe_wallet_partner = self.alt_currency_id.cyclos_debit_wallet_partner

        if safe_wallet_partner and self.is_reconversion_allowed:
            safe_wallet_profile_info = safe_wallet_partner.lcc_profile_info()
            if safe_wallet_profile_info:
                if len(safe_wallet_profile_info) > 1:
                    msg = "Safe partner has more than one public profile"
                    raise ValueError(msg)

                ## Safe wallet is configured and has a public profile
                data["safe_wallet_recipient"] = safe_wallet_profile_info[0]

                monujo_backends = (
                    safe_wallet_partner.lcc_backend_ids._update_search_data(
                        [self.alt_currency_id.uri],
                    )
                )
                if len(monujo_backends) > 1:
                    msg = "Safe partner has more than one wallet"
                    raise ValueError(msg)
                data["safe_wallet_recipient"]["monujo_backends"] = monujo_backends

            else:
                _logger.error(
                    "Safe wallet %s has no public profile",
                    safe_wallet_partner.name,
                )
        return data

    @api.depends("name", "type", "cyclos_status")
    def _compute_status(self) -> None:
        super()._compute_status()
        for rec in self:
            if rec.type == "cyclos":
                if rec.cyclos_status == "active":
                    rec.status = "active"
                elif rec.cyclos_status == "blocked":
                    rec.status = "blocked"
                elif rec.cyclos_status == "disabled":
                    rec.status = "inactive"
                elif rec.cyclos_status == "pending":
                    rec.status = "to_confirm"
                else:
                    rec.status = ""

    def cyclos_validate_user(self) -> None:
        for record in self:
            if record.cyclos_status == "pending":
                res = record.alt_currency_id.cyclos_rest_call(
                    "POST",
                    f"/{record.cyclos_id}/registration/validate",
                )
                _logger.debug(f"res: {res.text}")
                data = json.loads(res.text)
                if data.get("status", False) and data.get("status") == "active":
                    record.write(
                        {
                            "cyclos_status": data.get("status", ""),
                            "cyclos_create_response": res.text,
                        },
                    )

    def cyclos_activate_user(self) -> None:
        for record in self:
            if record.cyclos_status != "active":
                data = {"status": "active", "comment": "Activated by Odoo"}
                record.alt_currency_id.cyclos_rest_call(
                    "POST",
                    f"/{record.cyclos_id}/status",
                    data=data,
                )
                res = record.alt_currency_id.cyclos_rest_call(
                    "GET",
                    f"/{record.cyclos_id}/status",
                )
                _logger.debug(f"res: {res}")
                data_res = json.loads(res.text)
                record.write(
                    {
                        "cyclos_status": data_res.get("status", ""),
                    },
                )

    def cyclos_block_user(self) -> None:
        for record in self:
            if record.cyclos_status != "blocked":
                data = {"status": "blocked", "comment": "Blocked by Odoo"}
                record.alt_currency_id.cyclos_rest_call(
                    "POST",
                    f"/{record.cyclos_id}/status",
                    data=data,
                )
                res = record.alt_currency_id.cyclos_rest_call(
                    "GET",
                    f"/{record.cyclos_id}/status",
                )
                _logger.debug(f"res: {res}")
                data_res = json.loads(res.text)
                record.write(
                    {
                        "cyclos_status": data_res.get("status", ""),
                    },
                )

    def cyclos_disable_user(self) -> None:
        for record in self:
            if record.cyclos_status != "disabled":
                data = {"status": "disabled", "comment": "Disabled by Odoo"}
                record.alt_currency_id.cyclos_rest_call(
                    "POST",
                    f"/{record.cyclos_id}/status",
                    data=data,
                )
                res = record.alt_currency_id.cyclos_rest_call(
                    "GET",
                    f"/{record.cyclos_id}/status",
                )
                _logger.debug(f"res: {res}")
                data_res = json.loads(res.text)
                record.write(
                    {
                        "cyclos_status": data_res.get("status", ""),
                    },
                )

    def cyclos_send_password_setup_email(self) -> None:
        """E-mail the holder a new link to choose their Cyclos password.

        The link replaces the one of any previous e-mail.
        """
        template = self.env.ref("lcc_cyclos_base.mail_template_cyclos_password_setup")
        for record in self:
            if not record.cyclos_password_pending:
                raise UserError(
                    _("The holder of %s already chose their Cyclos password.")
                    % record.display_name
                )
            missing = [
                label
                for label, value in (
                    (_("Cyclos login"), record.cyclos_login),
                    (_("holder e-mail"), record.partner_id.email),
                    (
                        _("Cyclos web address"),
                        record.alt_currency_id.cyclos_web_url,
                    ),
                )
                if not value
            ]
            if missing:
                raise UserError(
                    _("Cannot send the password setup e-mail of %s: missing %s.")
                    % (record.display_name, ", ".join(missing))
                )
            token = secrets.token_urlsafe(PASSWORD_SETUP_TOKEN_BYTES)
            record.sudo().cyclos_password_setup_token = token
            link = f"{record.get_base_url()}{PASSWORD_SETUP_ROUTE}/{token}"
            template.with_context(cyclos_password_setup_link=link).send_mail(
                record.id
            )

    @api.model
    def _cyclos_wallet_by_setup_token(self, token):
        """Return the wallet whose password setup link carries ``token``."""
        if not token:
            return self.browse()
        wallets = self.sudo().search(
            [("type", "=", "cyclos"), ("cyclos_password_setup_token", "=", token)]
        )
        if len(wallets) > 1:
            raise UserError(_("Several wallets share a password setup link."))
        return wallets

    def cyclos_password_setup_redirect(self) -> str:
        """Return where the password setup link sends the holder.

        While the holder has not chosen their password: the Cyclos web
        interface, with a session Odoo opens with the temporary password,
        where Cyclos asks for the new one, then shows the currency's
        return page if set. Afterwards: the Cyclos web interface, at its
        login page.
        """
        self.ensure_one()
        web_url = self.alt_currency_id.cyclos_web_url.rstrip("/")
        if not self.cyclos_password_pending or self._cyclos_check_password_chosen():
            return f"{web_url}/"
        password = self.sudo().cyclos_initial_password
        if not password:
            raise UserError(
                _("Odoo no longer has the temporary Cyclos password of %s.")
                % self.display_name
            )
        res = self.alt_currency_id.cyclos_rest_call(
            "POST",
            "/auth/session",
            api_login=self.cyclos_login,
            api_password=password,
            headers={
                "Channel": CYCLOS_WEB_CHANNEL,
                "Principal-Type": CYCLOS_LOGIN_PRINCIPAL_TYPE,
            },
        )
        query = {"sessionToken": json.loads(res.text)["sessionToken"]}
        return_page = self.alt_currency_id.cyclos_password_setup_return_page
        if return_page:
            ## Cyclos opens this internal page once the password is chosen.
            query["returnTo"] = f"{CYCLOS_CONTENT_PAGE_ROUTE}{return_page}"
        return f"{web_url}/?{urlencode(query)}"

    def _cyclos_check_password_chosen(self) -> bool:
        """Forget the temporary password if Cyclos reports the holder's own.

        Return whether the holder chose their password. Errors reaching
        Cyclos are raised: the temporary password is then kept.
        """
        self.ensure_one()
        res = self.alt_currency_id.cyclos_rest_call(
            "GET",
            f"/{self.cyclos_id}/passwords/{self.alt_currency_id.cyclos_password_type}",
        )
        if json.loads(res.text)["status"] != CYCLOS_PASSWORD_CHOSEN_STATUS:
            return False
        self.sudo().write(
            {"cyclos_initial_password": False, "cyclos_password_pending": False}
        )
        return True

    @api.model
    def _cron_cyclos_check_password_setup(self) -> None:
        """Forget the temporary passwords that holders have replaced."""
        for wallet in self.sudo().search(
            [("type", "=", "cyclos"), ("cyclos_password_pending", "=", True)]
        ):
            try:
                wallet._cyclos_check_password_chosen()
            except Exception:
                _logger.exception(
                    "Could not check the Cyclos password of wallet %s: "
                    "its temporary password is kept.",
                    wallet.id,
                )

    def cyclos_sync_user_fields(self) -> None:
        """Send to Cyclos the fields of its user computed by the mapping of
        the currency; the fields not mapped keep their Cyclos value."""
        for record in self:
            currency = record.alt_currency_id
            values = currency._cyclos_user_values(record)
            res = currency.cyclos_rest_call(
                "GET", f"/users/{record.cyclos_id}/data-for-edit"
            )
            edit_data = json.loads(res.text)
            currency._cyclos_check_editable(
                values, edit_data["profileFieldActions"], record.display_name
            )
            user = edit_data["user"]
            currency.cyclos_rest_call(
                "PUT",
                f"/users/{record.cyclos_id}",
                data=currency._cyclos_user_payload(values, user),
            )

    def force_cyclos_password(self, password) -> None:
        for record in self:
            data = {
                "newPassword": password,
                "checkConfirmation": True,
                "newPasswordConfirmation": password,
                "forceChange": False,
            }
            try:
                record.alt_currency_id.cyclos_rest_call(
                    "POST",
                    "/{}/passwords/{}/change".format(
                        record.cyclos_id,
                        record.alt_currency_id.cyclos_password_type,
                    ),
                    data=data,
                )
            except ValueError as e:
                if (
                    len(e.args) > 1
                    and e.args[1].status_code == 422
                    and "newPassword" in e.args[1].json().get("properties", [])
                ):
                    _logger.debug("Ignoring Cyclos NewPassword Error !")
                else:
                    raise
            except requests.exceptions.HTTPError as e:
                if (
                    e.response.status_code == 409
                    and e.response.json().get("code") == "staleEntity"
                ):
                    _logger.debug(
                        "Ignoring Cyclos staleEntity on password change "
                        "(concurrent request already applied it)"
                    )
                else:
                    raise

    def cyclos_create_user_token(self, api_login, api_password):
        self.ensure_one()
        for record in self:
            res = record.alt_currency_id.cyclos_rest_call(
                "POST",
                "/auth/session",
                data={"timeoutInSeconds": 90000},
                api_login=api_login,
                api_password=api_password,
            )
            _logger.debug(f"res TOKEN: {res.text}")
            data = json.loads(res.text)
            return data.get("sessionToken", False)
        return None

    def cyclos_remove_user_token(self, api_login, api_password) -> None:
        for record in self:
            res = record.alt_currency_id.cyclos_rest_call(
                "DELETE",
                "/auth/session",
                api_login=api_login,
                api_password=api_password,
            )
            _logger.debug(f"res: {res.text}")

    def credit_wallet(self, amount=0):
        """Send credit request to the financial backend."""
        self.ensure_one()
        res = super().credit_wallet(amount)
        if self.type != "cyclos":
            return res

        data = {
            "amount": amount,
            "description": f"Credited by {self.alt_currency_id.name}",
            "subject": self.cyclos_id,
            "type": (
                self.alt_currency_id.cyclos_credit_payment_type_pro
                if self.partner_id.is_company
                else self.alt_currency_id.cyclos_credit_payment_type_user
            ),
        }
        _logger.debug(f"data: {data}")
        response = self.alt_currency_id.cyclos_rest_call(
            "POST",
            "/system/payments",
            data=data,
        )
        _logger.debug(f"response: {response}")
        # TODO: need to check response
        return {"success": True, "response": response}

    def get_wallet_data(self):
        self.ensure_one()
        data = super().get_wallet_data()
        if self.type == "cyclos":
            data = [
                "cyclos:cyclos",
                self.cyclos_id,
            ]
        return data

    def get_wallet_balance(self):
        self.ensure_one()
        res = super().get_wallet_balance()
        if self.type != "cyclos":
            return res
        try:
            res = self.alt_currency_id.cyclos_rest_call(
                "GET", f"/{self.cyclos_id}/accounts"
            )
            _logger.debug(f"res: {res}")
        except Exception as e:
            _logger.exception(tools.format_last_exception())
            return {
                "success": False,
                "response": "",
                "error_message": f"Failed to get wallet balance: {e}",
            }

        data_res = json.loads(res.text)
        balance = float(data_res[0].get("status", {}).get("balance", ""))

        return {"success": True, "response": balance}
