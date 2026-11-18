-- deactivate cyclos transactions
UPDATE res_alt_currency
   SET cyclos_server_password = NULL;

-- forget Cyclos temporary passwords and password setup links
UPDATE res_partner_backend
   SET cyclos_initial_password = NULL,
       cyclos_password_setup_token = NULL;
