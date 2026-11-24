import json

import requests

from odoo import models, api, _
from odoo.exceptions import AccessDenied, UserError

import logging

_logger = logging.getLogger(__name__)

ALLOWED_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789`@!\"#$%&'()*+,-./:;<=>?[\\]^_{}~"


def valid_password(password):
    return all(c in ALLOWED_CHARS for c in password)


class ResUsers(models.Model):
    """Inherits partner, adds Cyclos fields in the partner form, and functions"""

    _inherit = "res.users"

    def _set_password(self):
        for user in self:
            if valid_password(user.password):
                super(ResUsers, user)._set_password()
                continue
            raise UserError(
                _("Password must contain only the following characters: %s")
                % ALLOWED_CHARS
            )

    ## We need to override auth_signup logic here because, it
    ## invalidates the token before trying ``_set_password``
    @api.model
    def signup(self, values, token=None):
        if "password" in values:
            if not valid_password(values["password"]):
                raise UserError(
                    _("Password must contain only the following characters: %s")
                    % ALLOWED_CHARS
                )
        return super(ResUsers, self).signup(values, token)

    ## OAuth in cyclos needs some specific calls (that are not implemented)
    ## it requires:
    ##  - POST
    ##  - special header
    @api.model
    def _auth_oauth_rpc(self, endpoint, access_token):
        return requests.post(
            endpoint, headers={"Authorization": "Bearer %s" % access_token}, timeout=10
        ).json()

    ## Cyclos token is in 'token' not 'access_token'
    @api.model
    def auth_oauth(self, provider, params):
        if not params.get("access_token"):
            if params.get("token"):
                params["access_token"] = params["token"]
        return super(ResUsers, self).auth_oauth(provider, params)

    ## Cyclos returns 'preferred_username' instead of 'user_id'
    @api.model
    def _auth_oauth_validate(self, provider, access_token):
        validation = super(ResUsers, self)._auth_oauth_validate(provider, access_token)
        if not validation.get("user_id") and validation.get("preferred_username"):
            validation["user_id"] = validation["preferred_username"]
        return validation

    ## Cyclos OAuth login: reconcile with an existing Odoo user, and
    ## guarantee the ``res.partner.backend`` exists.
    ##
    ## ``oauth_data`` is what the base module calls ``validation``: the
    ## verified OAuth identity claims (token validation response merged
    ## with the userinfo response), see ``auth_oauth`` core.
    ##
    ## The base ``_auth_oauth_signin`` only matches on
    ## ``(oauth_uid, provider)`` and silently creates a fresh user
    ## otherwise.  For Cyclos we want, in order:
    ##   1. an existing user already linked to this Cyclos identity, or
    ##   2. the owner of the Cyclos wallet whose ``cyclos_id`` is this
    ##      identity (the OIDC ``sub`` is the Cyclos user id): its user,
    ##      or a new user on that partner, or
    ##   3. an existing Odoo user with the same e-mail (then link it), or
    ##   4. a freshly created user (base behaviour).
    ## then ensure the partner owns the Cyclos wallet backend.
    ##
    ## Step 2 does not need the e-mail claim, which Cyclos only sends when
    ## the e-mail is an enabled profile field of the user's product.
    ##
    ## Every precondition specific to Cyclos fails loud rather than
    ## silently degrading to a half-configured account.  Non-Cyclos
    ## providers keep the base behaviour untouched.
    @api.model
    def _auth_oauth_signin(self, provider_id, oauth_data, params):
        oauth_provider = self.env["auth.oauth.provider"].browse(provider_id)
        ## Cyclos detection needs BOTH signals: the provider endpoint
        ## convention (provider-side knowledge) and the Cyclos-specific
        ## ``preferred_username`` OIDC claim in the userinfo.
        if not (
            oauth_provider._is_cyclos()
            and oauth_data.get("preferred_username")
        ):
            return super(ResUsers, self)._auth_oauth_signin(
                provider_id, oauth_data, params
            )

        # This *is* a Cyclos provider: it must resolve to a currency.
        # An empty result here means a misconfiguration (e.g. the provider
        # endpoint host and the currency ``cyclos_server_url`` drifted
        # apart) -- fail loud instead of logging in a wallet-less user.
        alt_currency = oauth_provider._cyclos_alt_currency()
        if not alt_currency:
            raise AccessDenied(
                _(
                    "Cyclos OAuth provider %(provider)s does not match any "
                    "Cyclos currency; check that a res.alt.currency "
                    "cyclos_server_url shares the provider's domain."
                )
                % {"provider": oauth_provider.name}
            )

        oauth_uid = oauth_data["user_id"]
        email = oauth_data.get("email")

        # 1. already linked to this Cyclos identity?
        oauth_user = self.search(
            [("oauth_uid", "=", oauth_uid), ("oauth_provider_id", "=", provider_id)]
        )
        if len(oauth_user) > 1:
            raise AccessDenied(
                _("Several Odoo users are linked to Cyclos identity %s.")
                % oauth_uid
            )

        # 2. otherwise, the owner of the Cyclos wallet of this identity
        if not oauth_user:
            wallet_partner = self._cyclos_wallet_partner(alt_currency, oauth_uid)
            if wallet_partner:
                oauth_user = self._cyclos_user_for_wallet_partner(
                    wallet_partner, provider_id, oauth_data, params
                )

        if not oauth_user:
            # A Cyclos login without wallet nor usable e-mail cannot be
            # reconciled and would otherwise create a junk
            # ``provider_<n>_user_<uid>`` account.
            if not email:
                raise AccessDenied(
                    _(
                        "Cyclos OAuth login for %(uid)s returned no e-mail "
                        "and matches no Cyclos wallet; the 'email' scope "
                        "must be requested and the Cyclos user must have "
                        "an e-mail."
                    )
                    % {"uid": oauth_uid}
                )

            # 3. reconcile by e-mail with an existing Odoo user
            candidates = self._cyclos_users_for_email(email)
            if len(candidates) > 1:
                raise AccessDenied(
                    _(
                        "Several Odoo users share the e-mail %s; "
                        "cannot reconcile the Cyclos login."
                    )
                    % email
                )
            if candidates:
                oauth_user = candidates
                oauth_user.write(
                    {
                        "oauth_provider_id": provider_id,
                        "oauth_uid": oauth_uid,
                    }
                )
            else:
                # 3.5. no user, but maybe a pre-existing partner with this
                # e-mail (e.g. a member without a login yet): create the
                # user *on that partner* instead of letting the base signup
                # duplicate it.
                partner = self._cyclos_partner_for_email(email)
                # 4. otherwise the base create-user behaviour
                oauth_user = self._cyclos_signup(
                    provider_id, oauth_data, params, partner
                )

        # By construction we now hold exactly one user.
        oauth_user.write({"oauth_access_token": params["access_token"]})
        oauth_user.sudo()._cyclos_ensure_wallet(alt_currency, oauth_uid)
        return oauth_user.login

    @api.model
    def _cyclos_users_for_email(self, email):
        """Odoo users whose login, or else e-mail, is ``email``."""
        return self.search([("login", "=", email)]) or self.search(
            [("email", "=", email)]
        )

    @api.model
    def _cyclos_wallet_partner(self, alt_currency, cyclos_owner_id):
        """Return the partner owning the ``alt_currency`` wallet(s) whose
        ``cyclos_id`` is ``cyclos_owner_id``, or an empty recordset.

        Several wallets with that id are accepted only when they belong to
        the same partner (duplicated imports exist); otherwise the identity
        is ambiguous and we refuse to guess.
        """
        wallets = self.env["res.partner.backend"].sudo().search(
            [
                ("type", "=", "cyclos"),
                ("alt_currency_id", "=", alt_currency.id),
                ("cyclos_id", "=", cyclos_owner_id),
            ]
        )
        partners = wallets.mapped("partner_id")
        if len(partners) > 1:
            raise AccessDenied(
                _(
                    "Cyclos identity %(uid)s is the owner of the Cyclos "
                    "wallet of several partners (%(partners)s); cannot "
                    "choose one."
                )
                % {
                    "uid": cyclos_owner_id,
                    "partners": partners.mapped("display_name"),
                }
            )
        return partners

    @api.model
    def _cyclos_user_for_wallet_partner(self, partner, provider_id, oauth_data, params):
        """Return the user to log in as ``partner``, the owner of the
        Cyclos wallet of identity ``oauth_data["user_id"]``: its only user,
        now linked to that identity, or a new user created on it.
        """
        oauth_uid = oauth_data["user_id"]
        email = oauth_data.get("email")
        if email:
            others = self._cyclos_users_for_email(email).filtered(
                lambda u: u.partner_id != partner
            )
            if others:
                raise AccessDenied(
                    _(
                        "The Cyclos e-mail %(email)s belongs to user(s) "
                        "%(logins)s, not to %(partner)s, the owner of the "
                        "Cyclos wallet %(uid)s."
                    )
                    % {
                        "email": email,
                        "logins": others.mapped("login"),
                        "partner": partner.display_name,
                        "uid": oauth_uid,
                    }
                )

        users = partner.with_context(active_test=False).user_ids
        if len(users) > 1:
            raise AccessDenied(
                _(
                    "%(partner)s, the owner of the Cyclos wallet %(uid)s, "
                    "has several users (%(logins)s); cannot choose one."
                )
                % {
                    "partner": partner.display_name,
                    "uid": oauth_uid,
                    "logins": users.mapped("login"),
                }
            )
        if users:
            if users.oauth_uid:
                raise AccessDenied(
                    _(
                        "User %(login)s of %(partner)s, the owner of the "
                        "Cyclos wallet %(uid)s, is already linked to another "
                        "OAuth identity (%(other)s)."
                    )
                    % {
                        "login": users.login,
                        "partner": partner.display_name,
                        "uid": oauth_uid,
                        "other": users.oauth_uid,
                    }
                )
            users.write({"oauth_provider_id": provider_id, "oauth_uid": oauth_uid})
            return users

        # The partner's own e-mail first: base signup writes the login
        # e-mail on the partner, and Odoo's partner data stays the reference
        # when the member changed the e-mail in Cyclos only.
        login = partner.email or email
        if not login:
            raise AccessDenied(
                _(
                    "Neither Cyclos nor %(partner)s, the owner of the Cyclos "
                    "wallet %(uid)s, gives an e-mail to use as login."
                )
                % {"partner": partner.display_name, "uid": oauth_uid}
            )
        return self._cyclos_signup(
            provider_id, dict(oauth_data, email=login), params, partner
        )

    @api.model
    def _cyclos_signup(self, provider_id, oauth_data, params, partner):
        """Create the user of this Cyclos identity through the base
        ``_auth_oauth_signin``, on ``partner`` when given.

        Base ``signup(values, token)`` attaches the new user to the
        partner carrying the signup token; we inject that token in the
        OAuth ``state`` where the base ``_auth_oauth_signin`` expects it.
        """
        if partner:
            partner.sudo().signup_prepare()
            state = json.loads(params.get("state") or "{}")
            state["t"] = partner.sudo().signup_token
            params = dict(params, state=json.dumps(state))

        login = super(ResUsers, self)._auth_oauth_signin(
            provider_id, oauth_data, params
        )
        users = self.search([("login", "=", login)])
        if not users:
            raise AccessDenied(
                _("Could not resolve the just-created Cyclos user %s.") % login
            )
        if len(users) > 1:
            raise AccessDenied(
                _("Ambiguous login %(login)s: %(n)s users matched.")
                % {"login": login, "n": len(users)}
            )
        return users

    @api.model
    def _cyclos_partner_for_email(self, email):
        """Return the pre-existing *main profile* ``res.partner`` to attach
        the new Cyclos login to, or an empty recordset.

        Only main profiles are considered (public/position profiles are
        projections of a main one).  Several matching main profiles is an
        ambiguity we refuse to guess on.  A matching main profile that
        already has a user is an inconsistency: that user should have been
        found by the e-mail reconciliation step -- reaching this point
        means the user's login/e-mail diverged from its partner's e-mail,
        and silently creating a second user for the same partner (or
        skipping it) would hide the problem.
        """
        partners = self.env["res.partner"].sudo().search(
            [("email", "=", email), ("is_main_profile", "=", True)]
        )
        if len(partners) > 1:
            raise AccessDenied(
                _(
                    "Several main profile partners share the e-mail %s; "
                    "cannot attach the Cyclos login."
                )
                % email
            )
        if partners.user_ids:
            raise AccessDenied(
                _(
                    "Partner %(partner)s (e-mail %(email)s) already has "
                    "user(s) %(logins)s whose login/e-mail do not match; "
                    "cannot attach the Cyclos login."
                )
                % {
                    "partner": partners.display_name,
                    "email": email,
                    "logins": partners.user_ids.mapped("login"),
                }
            )
        return partners

    def _cyclos_ensure_wallet(self, alt_currency, cyclos_owner_id):
        """Guarantee the user's partner owns the ``res.partner.backend``
        for the Cyclos identity ``cyclos_owner_id`` (the wallet owner id
        in Cyclos vocabulary).

        A partner may legitimately own several wallets; we only check
        whether the specific Cyclos owner id already has a backend:

        - if it exists for this partner -> idempotent, return it;
        - if it exists for a *different* partner -> integrity error, raise;
        - otherwise -> create it.
        """
        self.ensure_one()
        if not alt_currency:
            raise AccessDenied(
                _("No Cyclos currency to attach the wallet backend to.")
            )

        partner = self.partner_id
        backend_obj = self.env["res.partner.backend"].sudo()

        existing = backend_obj.search(
            [
                ("type", "=", "cyclos"),
                ("alt_currency_id", "=", alt_currency.id),
                ("cyclos_id", "=", cyclos_owner_id),
            ]
        )
        if existing:
            other_partner = existing.filtered(
                lambda b: b.partner_id != partner
            )
            if other_partner:
                raise AccessDenied(
                    _(
                        "Cyclos wallet %(cyclos)s is already attached to "
                        "another partner (%(others)s) for currency "
                        "%(currency)s."
                    )
                    % {
                        "cyclos": cyclos_owner_id,
                        "others": other_partner.mapped("partner_id.display_name"),
                        "currency": alt_currency.name,
                    }
                )
            return existing

        # ``type`` is a related field on ``alt_currency_id.engine``; it is
        # derived from the currency, not set explicitly.
        return backend_obj.create(
            {
                "partner_id": partner.id,
                "alt_currency_id": alt_currency.id,
                "cyclos_id": cyclos_owner_id,
                "cyclos_status": "active",
                "name": "cyclos:{}".format(cyclos_owner_id),
            }
        )
