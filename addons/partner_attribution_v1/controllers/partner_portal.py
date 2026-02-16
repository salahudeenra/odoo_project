# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request


ROLE_LABEL = {
    "ap": "Affiliate Partner",
    "lead": "Lead Partner",
    "sales_agent": "Sales Agent",
    "sales_partner": "Sales Partner (Buy–Sell)",
}


class PartnerPortalController(http.Controller):

    @http.route("/partners/portal", type="http", auth="user", website=True, sitemap=False)
    def partners_portal(self, **kwargs):
        partner = request.env.user.partner_id.sudo()
        role = getattr(partner, "partner_role", False)

        Ledger = request.env["partner.attribution.ledger"].sudo()

        ledger_lines = Ledger.search(
            [
                ("partner_id", "=", partner.id),
            ],
            order="id desc",
            limit=50,
        )

        return request.render(
            "partner_attribution_v1.portal_partner_dashboard",
            {
                "partner": partner,
                "role": role,
                "role_label": ROLE_LABEL.get(role, ""),
                "ledger_lines": ledger_lines,
            },
        )