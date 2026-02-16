# -*- coding: utf-8 -*-
from odoo import http, _
from odoo.http import request

ROLE_MAP = {
    "ap": {"slug": "affiliate", "name": "Affiliate Partner", "desc": "Promote us and earn commission on successful referrals."},
    "lead": {"slug": "lead", "name": "Lead Partner", "desc": "Bring qualified leads; we handle the conversion process."},
    "sales_agent": {"slug": "sales-agent", "name": "Sales Agent", "desc": "Work deals end-to-end and earn commission on sales."},
    "sales_partner": {"slug": "sales-partner", "name": "Sales Partner (Buy–Sell)", "desc": "Resell/buy-sell under partner rules and pricing."},
}
SLUG_TO_ROLE = {v["slug"]: k for k, v in ROLE_MAP.items()}

COOKIE_NAME = "partner_code"
SESSION_KEY = "partner_code"


class PartnerWebsiteController(http.Controller):

    # -----------------------------
    # Referral capture (MANDATORY)
    # -----------------------------
    @http.route("/r/<string:code>", type="http", auth="public", website=True, sitemap=False)
    def referral_capture(self, code, **kwargs):
        code = (code or "").strip()
        next_url = (kwargs.get("next") or "/").strip() or "/"

        partner = request.env["res.partner"].sudo().search(
            [("partner_code", "=", code), ("partner_state", "=", "approved")],
            limit=1
        )
        if not partner:
            return request.redirect(next_url)

        request.session[SESSION_KEY] = code
        resp = request.redirect(next_url)
        resp.set_cookie(COOKIE_NAME, code, max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax")
        return resp

    @http.route("/r", type="http", auth="public", website=True, sitemap=False)
    def referral_capture_qs(self, **kwargs):
        code = (kwargs.get("pc") or "").strip()
        next_url = (kwargs.get("next") or "/").strip() or "/"
        if not code:
            return request.redirect(next_url)
        return self.referral_capture(code, **kwargs)

    @http.route("/r/clear", type="http", auth="public", website=True, sitemap=False)
    def referral_clear(self, **kwargs):
        next_url = (kwargs.get("next") or "/").strip() or "/"
        request.session.pop(SESSION_KEY, None)
        resp = request.redirect(next_url)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # -----------------------------
    # Website pages
    # -----------------------------
    @http.route("/partners", type="http", auth="public", website=True, sitemap=True)
    def partners_home(self, **kwargs):
        return request.render("partner_attribution_v1.website_partners_home", {"roles": ROLE_MAP})

    @http.route("/partners/<string:role_slug>", type="http", auth="public", website=True, sitemap=True)
    def partners_role_page(self, role_slug, **kwargs):
        role_key = SLUG_TO_ROLE.get(role_slug)
        if not role_key:
            return request.not_found()
        role = ROLE_MAP[role_key]
        return request.render(
            "partner_attribution_v1.website_partner_role_page",
            {"role_key": role_key, "role": role, "roles": ROLE_MAP}
        )

    @http.route("/partners/apply", type="http", auth="public", website=True, sitemap=True)
    def partners_apply(self, role=None, **kwargs):
        role_key = None
        if role:
            if role in ROLE_MAP:
                role_key = role
            elif role in SLUG_TO_ROLE:
                role_key = SLUG_TO_ROLE[role]

        return request.render(
            "partner_attribution_v1.website_partner_apply",
            {"roles": ROLE_MAP, "selected_role": role_key}
        )

    @http.route("/partners/apply/submit", type="http", auth="public", website=True, methods=["POST"], csrf=True)
    def partners_apply_submit(self, **post):
        Inquiry = request.env["partner.attribution.inquiry"].sudo()

        partner_role = (post.get("partner_role") or "").strip()
        applicant_name = (post.get("applicant_name") or post.get("name") or "").strip()
        applicant_company = (post.get("applicant_company") or post.get("company") or "").strip()
        email = (post.get("email") or "").strip()
        phone = (post.get("phone") or "").strip()
        note = (post.get("note") or post.get("notes") or "").strip()

        vat = (post.get("vat") or "").strip()
        iban = (post.get("iban") or "").strip()
        coc = (post.get("coc") or "").strip()
        irs = (post.get("irs") or "").strip()

        if partner_role not in ROLE_MAP:
            return request.render("partner_attribution_v1.website_partner_apply", {
                "roles": ROLE_MAP,
                "selected_role": None,
                "error": _("Please select a valid Partner Role."),
            })

        if not applicant_name:
            return request.render("partner_attribution_v1.website_partner_apply", {
                "roles": ROLE_MAP,
                "selected_role": partner_role,
                "error": _("Full Name is required."),
            })

        inquiry = Inquiry.create({
            "company_id": request.env.company.id,
            "applicant_name": applicant_name,
            "applicant_company": applicant_company or False,
            "email": email or False,
            "phone": phone or False,
            "note": note or False,
            "partner_role": partner_role,
            "vat": vat or False,
            "iban": iban or False,
            "coc": coc or False,
            "irs": irs or False,
        })

        return request.render(
            "partner_attribution_v1.website_partner_apply_done",
            {"inquiry": inquiry, "roles": ROLE_MAP, "role": ROLE_MAP[partner_role]}
        )

    @http.route("/partners/portal", type="http", auth="user", website=True, sitemap=False)
    def partners_portal(self, **kwargs):
        partner = request.env.user.partner_id.sudo()
        role = getattr(partner, "partner_role", False) if partner else False
        role_label = ROLE_MAP.get(role, {}).get("name") if role else ""

        Ledger = request.env["partner.attribution.ledger"].sudo()
        lines = Ledger.search([("partner_id", "=", partner.id)]) if partner else Ledger.browse([])

        payable_amount = sum(lines.filtered(lambda l: l.state == "payable").mapped("commission_amount"))
        paid_amount = sum(lines.filtered(lambda l: l.state == "paid").mapped("commission_amount"))
        on_hold_amount = sum(lines.filtered(lambda l: l.state == "on_hold").mapped("commission_amount"))

        ledger_lines = lines.sorted(lambda x: x.id, reverse=True)[:50]

        return request.render(
            "partner_attribution_v1.portal_partner_dashboard",
            {
                "partner": partner,
                "role": role,
                "role_label": role_label,
                "ledger_count": len(lines),
                "payable_amount": payable_amount,
                "paid_amount": paid_amount,
                "on_hold_amount": on_hold_amount,
                "ledger_lines": ledger_lines,
            }
        )

    @http.route("/partners/privacy", type="http", auth="public", website=True, sitemap=True)
    def partners_privacy(self, **kwargs):
        return request.render("partner_attribution_v1.website_partner_privacy", {})

    @http.route("/partners/terms", type="http", auth="public", website=True, sitemap=True)
    def partners_terms(self, **kwargs):
        return request.render("partner_attribution_v1.website_partner_terms", {})

    @http.route("/partners/contact", type="http", auth="public", website=True, sitemap=True)
    def partners_contact(self, **kwargs):
        return request.render("partner_attribution_v1.website_partner_contact", {})