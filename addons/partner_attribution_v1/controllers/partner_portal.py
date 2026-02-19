# -*- coding: utf-8 -*-
import base64
import os
import re
import secrets

from odoo import http
from odoo.http import request

ROLE_MAP = {
    "ap": {"name": "Affiliate Partner"},
    "lead": {"name": "Lead Partner"},
    "sales_agent": {"name": "Sales Agent"},
    "sales_partner": {"name": "Sales Partner (Buy–Sell)"},
}


def _safe_filename(name: str) -> str:
    """Prevent weird filenames / header injection / path tricks."""
    name = (name or "document").strip()
    name = name.replace("\x00", "")
    name = name.replace("\n", " ").replace("\r", " ")
    name = os.path.basename(name)  # drop any path
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")
    return name or "document"


class PartnerPortalController(http.Controller):

    @http.route("/partners/portal", type="http", auth="user", website=True, sitemap=False)
    def partners_portal(self, **kwargs):
        # IMPORTANT: no sudo() so record rules apply
        partner = request.env.user.partner_id
        if not partner:
            return request.redirect("/web/login")

        commercial = partner.commercial_partner_id or partner
        partner_ids = [partner.id]
        if commercial.id != partner.id:
            partner_ids.append(commercial.id)

        role = getattr(partner, "partner_role", False)
        role_label = ROLE_MAP.get(role, {}).get("name", "")

        # -------------------------
        # Ledger (record rules apply)
        # -------------------------
        if "partner.attribution.ledger" in request.env:
            Ledger = request.env["partner.attribution.ledger"]
            # ✅ match your record rule: own OR commercial partner
            lines = Ledger.search([("partner_id", "in", partner_ids)])
        else:
            lines = request.env["partner.attribution.ledger"].browse([])

        ledger_lines = lines.sorted(lambda x: x.id, reverse=True)[:50]

        payable_amount = sum(lines.filtered(lambda l: l.state == "payable").mapped("commission_amount"))
        paid_amount = sum(lines.filtered(lambda l: l.state == "paid").mapped("commission_amount"))
        on_hold_amount = sum(lines.filtered(lambda l: l.state == "on_hold").mapped("commission_amount"))

        # -------------------------
        # Documents (record rules apply)
        # -------------------------
        if "ir.attachment" in request.env:
            Attachment = request.env["ir.attachment"]
            partner_docs = Attachment.search(
                [
                    ("res_model", "=", "res.partner"),
                    ("res_id", "in", partner_ids),
                ],
                order="id desc",
                limit=50,
            )
        else:
            partner_docs = request.env["ir.attachment"].browse([])

        # -------------------------
        # Referral link (sudo OK only for system param)
        # -------------------------
        base_url = (request.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        referral_url = False
        if getattr(partner, "partner_code", False):
            referral_url = "%s/r/%s" % (base_url, partner.partner_code)

        # -------------------------
        # Leads (lead role)
        # -------------------------
        lead_records = request.env["crm.lead"].browse([])
        if role == "lead" and "crm.lead" in request.env:
            Lead = request.env["crm.lead"]
            lead_records = Lead.search(
                [("partner_id", "=", commercial.id)],
                order="id desc",
                limit=50,
            )

        # -------------------------
        # Sale Orders (sales_agent / ap / sales_partner)
        # -------------------------
        so_records = request.env["sale.order"].browse([])
        if role in ("sales_agent", "ap", "sales_partner") and "sale.order" in request.env:
            SO = request.env["sale.order"]
            if "attributed_partner_id" in SO._fields:
                # ✅ align with record rule (id or commercial)
                so_records = SO.search(
                    [("attributed_partner_id", "in", partner_ids)],
                    order="id desc",
                    limit=50,
                )

        # -------------------------
        # Invoices (affiliate summary)
        # -------------------------
        inv_records = request.env["account.move"].browse([])
        if role == "ap" and "account.move" in request.env:
            Move = request.env["account.move"]
            if "attributed_partner_id" in Move._fields:
                # ✅ align with record rule (id or commercial)
                inv_records = Move.search(
                    [
                        ("move_type", "in", ("out_invoice", "out_refund")),
                        ("state", "=", "posted"),
                        ("attributed_partner_id", "in", partner_ids),
                    ],
                    order="id desc",
                    limit=50,
                )

        # -------------------------
        # Pricelist (sales_partner)
        # -------------------------
        pricelist = partner.property_product_pricelist if role == "sales_partner" else False

        return request.render("partner_attribution_v1.portal_partner_dashboard", {
            "partner": partner,
            "role": role,
            "role_label": role_label,

            "ledger_count": len(lines),
            "payable_amount": payable_amount,
            "paid_amount": paid_amount,
            "on_hold_amount": on_hold_amount,
            "ledger_lines": ledger_lines,

            "partner_docs": partner_docs,

            "referral_url": referral_url,
            "lead_records": lead_records,
            "so_records": so_records,
            "inv_records": inv_records,
            "pricelist": pricelist,
        })

    @http.route("/partners/portal/profile/submit", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def partners_portal_profile_submit(self, **post):
        partner = request.env.user.partner_id
        if not partner:
            return request.redirect("/web/login")

        vals = {
            "phone": (post.get("phone") or "").strip() or False,
            "kyc_note": (post.get("kyc_note") or "").strip() or False,
        }
        if "kyc_status" in partner._fields:
            vals["kyc_status"] = "pending"

        partner.write(vals)
        return request.redirect("/partners/portal")

    @http.route("/partners/portal/documents/upload", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def partners_portal_documents_upload(self, **post):
        partner = request.env.user.partner_id
        if not partner:
            return request.redirect("/web/login")

        upload = request.httprequest.files.get("document")
        if not upload:
            return request.redirect("/partners/portal")

        raw = upload.read() or b""
        if not raw:
            return request.redirect("/partners/portal")

        # safety limit
        max_bytes = 10 * 1024 * 1024  # 10MB
        if len(raw) > max_bytes:
            return request.redirect("/partners/portal")

        allowed_mimes = {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        mimetype = (upload.mimetype or "").strip()
        if mimetype and mimetype not in allowed_mimes:
            return request.redirect("/partners/portal")

        filename = _safe_filename(upload.filename or "document")

        # block risky extensions
        ext = (os.path.splitext(filename)[1] or "").lower()
        blocked_ext = {".php", ".py", ".js", ".html", ".htm", ".exe", ".sh", ".bat", ".cmd"}
        if ext in blocked_ext:
            return request.redirect("/partners/portal")

        # ✅ store against commercial partner to match your record rule
        owner_partner = partner.commercial_partner_id or partner

        attachment_vals = {
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(raw),
            "mimetype": mimetype or "application/octet-stream",
            "res_model": "res.partner",
            "res_id": owner_partner.id,
            "res_name": owner_partner.display_name,
            "public": False,
        }

        att = request.env["ir.attachment"].sudo().create(attachment_vals)

        # Ensure access token exists (portal-safe /web/content links)
        if hasattr(att, "_ensure_access_token"):
            att.sudo()._ensure_access_token()
        else:
            if "access_token" in att._fields and not att.access_token:
                att.sudo().write({"access_token": secrets.token_urlsafe(24)})

        return request.redirect("/partners/portal")

    @http.route("/partners/portal/leads/submit", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def partners_portal_lead_submit(self, **post):
        partner = request.env.user.partner_id
        if not partner:
            return request.redirect("/web/login")

        role = getattr(partner, "partner_role", False)
        if role != "lead" or "crm.lead" not in request.env:
            return request.redirect("/partners/portal")

        name = (post.get("name") or "").strip()
        if not name:
            return request.redirect("/partners/portal")

        commercial = partner.commercial_partner_id or partner

        request.env["crm.lead"].create({
            "name": name,
            "partner_id": commercial.id,
            "contact_name": (post.get("contact_name") or "").strip() or False,
            "email_from": (post.get("email_from") or "").strip() or False,
            "phone": (post.get("phone") or "").strip() or False,
            "description": (post.get("description") or "").strip() or False,
        })

        return request.redirect("/partners/portal")