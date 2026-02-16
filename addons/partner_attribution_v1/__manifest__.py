# -*- coding: utf-8 -*-
{
    "name": "Partner Attribution v1",
    "version": "17.0.1.0.0",
    "category": "Sales",
    "summary": "Manual partner-code attribution stored permanently, propagated to invoices, ledger + payout automation.",
    "depends": [
        "base",
        "sale_management",
        "account",
        "contacts",
        "product",
        "website",
        "portal",
        "mail",
        "auth_signup",
        "sale_crm",
        "crm",
    ],
    "data": [
        "security/security_groups.xml",
        "security/security.xml",
        "security/ir.model.access.csv",
        

        "data/ir_sequence.xml",
        "data/ir_sequence_payout_batch.xml",

        "views/res_partner_views.xml",
        "views/sale_order_views.xml",
        "views/account_move_views.xml",
        "views/attribution_search_views.xml",
        "views/payout_batch_views.xml",

        # MUST be before menus.xml (menus depends on actions)
        "views/partner_attribution_ledger_views.xml",

        "views/report_invoice.xml",
        "views/invoice_report_inherit.xml",
        "views/partner_contract_report.xml",

        "views/partner_inquiry_views.xml",

        "views/website_partner_pages.xml",
        "views/portal_partner_pages.xml",

        # menus near end
        "views/menus.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}