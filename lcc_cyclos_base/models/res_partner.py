import json
import logging
import secrets
import string

import requests
from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

## Length of the temporary password given to a new Cyclos user: the
## maximum length of the login password type of the Cyclos instances
## Odoo connects to.
TEMPORARY_PASSWORD_LENGTH = 32
TEMPORARY_PASSWORD_ALPHABET = string.ascii_letters + string.digits

## Internal name of the Cyclos principal type a member logs in with.
CYCLOS_LOGIN_PRINCIPAL_TYPE = "username"


def temporary_password():
    """Return a random password for a new Cyclos user, until they choose theirs.

    It contains at least one lowercase letter, one uppercase letter and
    one digit, so that Cyclos password policies requiring them accept it.
    """
    while True:
        password = "".join(
            secrets.choice(TEMPORARY_PASSWORD_ALPHABET)
            for _i in range(TEMPORARY_PASSWORD_LENGTH)
        )
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


def cyclos_login(registration_result):
    """Return the login of the user Cyclos created, from its answer."""
    logins = [
        principal["value"]
        for principal in registration_result.get("principals", [])
        if principal["type"]["internalName"] == CYCLOS_LOGIN_PRINCIPAL_TYPE
    ]
    if len(logins) != 1:
        raise UserError(
            _(
                "Cyclos created the user %s but did not return its login: "
                "Odoo cannot tell the new member how to log in. Check this "
                "user in Cyclos before creating the wallet again."
            )
            % registration_result["user"]["id"]
        )
    return logins[0]


class ResPartner(models.Model):
    """Inherits partner, adds Cyclos fields in the partner form, and functions."""

    _inherit = "res.partner"

    def cyclos_add_user(self, alt_currency_id) -> None:
        """Create the Cyclos user and wallet of each partner.

        Cyclos sends no e-mail. Odoo gives the user a random password
        that Cyclos marks as to be changed, keeps it on the wallet, and
        e-mails the member a link: the link opens a Cyclos session with
        that password, where Cyclos makes the member choose their own.
        Odoo forgets the temporary password once Cyclos reports the change.
        The member's e-mail is therefore required.
        """
        if not alt_currency_id.cyclos_web_url:
            raise UserError(
                _(
                    "%s has no Cyclos web address: set it on the currency "
                    "before creating Cyclos wallets."
                )
                % alt_currency_id.display_name
            )
        for record in self:
            if not record.email:
                raise UserError(
                    _(
                        "%s has no e-mail address: Odoo needs it to send "
                        "the new member their login details."
                    )
                    % record.display_name
                )
            backend_obj = self.env["res.partner.backend"]
            rule = alt_currency_id.cyclos_group_rule_for_partner(record)
            if not rule:
                raise UserError(
                    _(
                        "No Cyclos group rule of %(currency)s matches "
                        "%(partner)s: add one on the currency to choose the "
                        "Cyclos group of their user."
                    )
                    % {
                        "currency": alt_currency_id.display_name,
                        "partner": record.display_name,
                    }
                )
            group = rule.cyclos_group
            password = temporary_password()
            data = {
                "username": record.id,
                "name": record.name,
                "email": record.email,
                "group": group,
                ## Cyclos needs a value for each password type set at
                ## registration before it accepts to skip its activation
                ## e-mail. Marked as to be changed, it lets a session
                ## opened with it change it without asking for it again.
                "passwords": [
                    {
                        "type": alt_currency_id.cyclos_password_type,
                        "value": password,
                        "checkConfirmation": True,
                        "confirmationValue": password,
                        "forceChange": True,
                    },
                ],
                "skipActivationEmail": True,
                "addresses": [
                    {
                        "name": record.name,
                        "addressLine1": record.street,
                        "addressLine2": record.street2,
                        "zip": record.zip,
                        "city": record.city,
                        "location": {
                            "latitude": record.partner_latitude,
                            "longitude": record.partner_longitude,
                        },
                        "defaultAddress": True,
                        "hidden": True,
                        "contactInfo": {
                            "email": record.email,
                            "mobilePhone": record.mobile.strip()
                            if record.mobile
                            else "",
                        },
                    },
                ],
            }
            ## Fields computed from the mapping of the currency, on the
            ## wallet about to be created.
            data = alt_currency_id._cyclos_user_payload(
                alt_currency_id._cyclos_user_values(
                    backend_obj.new(
                        {
                            "partner_id": record.id,
                            "alt_currency_id": alt_currency_id.id,
                            "type": "cyclos",
                        }
                    )
                ),
                data,
            )
            try:
                res = alt_currency_id.cyclos_rest_call(
                    "POST",
                    "/users",
                    data=data,
                )
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 422:
                    msg = alt_currency_id.build_cyclos_error_message(e)
                    if msg != "":
                        msg = "Cyclos serveur complained about:\n{}".format(
                            "\n".join(msg)
                        )
                        raise ValueError(
                            msg,
                            e.response,
                        )
                raise

            data = json.loads(res.text)
            if data:
                _logger.debug(f"data: {data}")
                wallet = backend_obj.create(
                    {
                        "partner_id": record.id,
                        "alt_currency_id": alt_currency_id.id,
                        "cyclos_id": data.get("user")["id"]
                        if data.get("user", False)
                        else "",
                        "cyclos_login": cyclos_login(data),
                        "cyclos_status": data.get("status", ""),
                        "name": "cyclos:{}".format(data.get("user")["id"]),
                        "type": "cyclos",
                        "cyclos_create_response": res.text,
                    },
                )
                wallet.sudo().write(
                    {
                        "cyclos_initial_password": password,
                        "cyclos_password_pending": True,
                    }
                )
                wallet.cyclos_send_password_setup_email()

    def show_app_access_buttons(self):
        # For Cyclos, we display the app access buttons on portal
        # only if the user has at least one activated Cyclos wallet
        res = super().show_app_access_buttons()
        for backend in self.lcc_backend_ids:
            if (backend.type == "cyclos") and (backend.status == "active"):
                res = True
                break
        return res
