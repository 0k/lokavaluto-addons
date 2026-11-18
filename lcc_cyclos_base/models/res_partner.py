import json
import logging
import secrets
import string

import requests
from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

## Length of the throwaway password given to a new Cyclos user: the
## maximum length of the login password type of the Cyclos instances
## Odoo connects to.
THROWAWAY_PASSWORD_LENGTH = 32
THROWAWAY_PASSWORD_ALPHABET = string.ascii_letters + string.digits

## Internal name of the Cyclos principal type a member logs in with.
CYCLOS_LOGIN_PRINCIPAL_TYPE = "username"


def throwaway_password():
    """Return a random password that nobody will ever know or use.

    It contains at least one lowercase letter, one uppercase letter and
    one digit, so that Cyclos password policies requiring them accept it.
    """
    while True:
        password = "".join(
            secrets.choice(THROWAWAY_PASSWORD_ALPHABET)
            for _i in range(THROWAWAY_PASSWORD_LENGTH)
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

        Cyclos sends no e-mail and Odoo keeps no password: Odoo gives the
        user a throwaway random password, then e-mails the member their
        login and the page where they set their own password (the Cyclos
        forgotten password page). The member's e-mail is therefore
        required.
        """
        if not alt_currency_id.cyclos_password_setup_url:
            raise UserError(
                _(
                    "%s has no Cyclos page to set a new member's password: "
                    "set it on the currency before creating Cyclos wallets."
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
            group = (
                alt_currency_id.cyclos_user_group_person
                if record.company_type == "person"
                else alt_currency_id.cyclos_user_group_company
            )
            password = throwaway_password()
            data = {
                "username": record.id,
                "name": record.name,
                "email": record.email,
                "group": group,
                ## Cyclos needs a value for each password type set at
                ## registration before it accepts to skip its activation
                ## e-mail. The member replaces this one with their own.
                "passwords": [
                    {
                        "type": alt_currency_id.cyclos_password_type,
                        "value": password,
                        "checkConfirmation": True,
                        "confirmationValue": password,
                        "forceChange": False,
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
