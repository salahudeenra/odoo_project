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
        # Security (order matters)
        "security/security_groups.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "security/security.xml",

        # Data
        "data/ir_sequence.xml",
        "data/ir_sequence_payout_batch.xml",
        "data/ir_cron.xml",

        # Backend views
        "views/res_partner_views.xml",
        "views/sale_order_views.xml",
        "views/account_move_views.xml",
        "views/attribution_search_views.xml",
        "views/payout_batch_views.xml",

        # MUST be before menus.xml
        "views/partner_attribution_ledger_views.xml",

        # Reports
        "views/report_invoice.xml",
        "views/invoice_report_inherit.xml",
        "views/partner_contract_report.xml",

        "views/partner_inquiry_views.xml",

        # Website/portal
        "views/website_partner_pages.xml",
        "views/portal_partner_pages.xml",

        # Menus last
        "views/menus.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}