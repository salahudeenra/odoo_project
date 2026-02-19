# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import re


def _iban_is_valid(iban: str) -> bool:
    iban = (iban or "").replace(" ", "").upper()
    if not iban or len(iban) < 15 or len(iban) > 34:
        return False
    if not re.match(r"^[A-Z0-9]+$", iban):
        return False

    rearranged = iban[4:] + iban[:4]
    digits = ""
    for ch in rearranged:
        digits += ch if ch.isdigit() else str(ord(ch) - 55)  # A=10..Z=35

    mod = 0
    for c in digits:
        mod = (mod * 10 + int(c)) % 97
    return mod == 1


class PartnerAttributionInquiry(models.Model):
    _name = "partner.attribution.inquiry"
    _description = "Partner Inquiry"
    _order = "id desc"
    _rec_name = "name"

    name = fields.Char(string="Inquiry Ref", required=True, copy=False, default=lambda self: _("New"))
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    applicant_name = fields.Char(string="Full Name", required=True)
    applicant_company = fields.Char(string="Company")
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    note = fields.Text(string="Message / Note")

    partner_role = fields.Selection(
        selection=[
            ("ap", "Affiliate Partner"),
            ("lead", "Lead Partner"),
            ("sales_agent", "Sales Agent"),
            ("sales_partner", "Sales Partner (Buy–Sell)"),
        ],
        string="Partner Role",
        required=True,
    )

    vat = fields.Char(string="VAT / Tax ID")
    iban = fields.Char(string="IBAN")
    coc = fields.Char(string="CoC Number")
    irs = fields.Char(string="IRS / Tax Ref")

    attachment_ids = fields.Many2many(
        "ir.attachment",
        "partner_inquiry_attachment_rel",
        "inquiry_id",
        "attachment_id",
        string="Documents",
    )

    state = fields.Selection(
        [
            ("inquiry", "Inquiry"),
            ("enlisted", "Enlisted (Screening)"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        default="inquiry",
        required=True,
        index=True,
    )

    partner_id = fields.Many2one("res.partner", string="Created Partner", readonly=True, copy=False)
    crm_lead_id = fields.Many2one("crm.lead", string="CRM Lead", readonly=True, copy=False)
    signup_url = fields.Char(string="Signup / Reset URL", readonly=True, copy=False)

    # ----------------------------
    # Admission Rules (stage-based)
    # ----------------------------
    def _validate_admission(self, stage="approve"):
        """
        stage:
          - 'enlist'  => light checks (screening can begin)
          - 'approve' => strict checks (must pass before creating partner + portal)
        """
        for rec in self:
            # ALWAYS (both stages): sanity checks
            if not rec.applicant_name:
                raise UserError(_("Full Name is required."))

            if rec.partner_role not in dict(self._fields["partner_role"].selection):
                raise UserError(_("Invalid Partner Role."))

            # Strict validations only on approve:
            if stage != "approve":
                return True

            # Email required for portal creation
            if not rec.email:
                raise UserError(_("Email is required for partner approval."))

            # IBAN required for commercial roles
            if rec.partner_role in ("sales_agent", "sales_partner"):
                if not rec.iban:
                    raise UserError(_("IBAN is required for this partner role."))
                if not _iban_is_valid(rec.iban):
                    raise UserError(_("Invalid IBAN. Please check formatting and checksum."))

            # VAT or CoC required for company-based roles
            if rec.partner_role in ("lead", "sales_agent", "sales_partner"):
                if not rec.vat and not rec.coc:
                    raise UserError(_("For this role, please provide at least one company identifier (VAT or CoC)."))

            # Docs required before approval for commercial roles
            if rec.partner_role in ("sales_agent", "sales_partner"):
                if not rec.attachment_ids:
                    raise UserError(_("Supporting documents must be uploaded before approval."))

        return True

    # ----------------------------
    # CRM Lead creation
    # ----------------------------
    def _ensure_crm_lead(self):
        self.ensure_one()
        if "crm.lead" not in self.env:
            return False
        if self.crm_lead_id:
            return self.crm_lead_id

        role_label = dict(self._fields["partner_role"].selection).get(self.partner_role)
        Lead = self.env["crm.lead"].sudo()

        lead = Lead.create({
            "name": "%s (%s)" % (self.applicant_company or self.applicant_name, role_label),
            "contact_name": self.applicant_name,
            "partner_name": self.applicant_company or False,
            "email_from": self.email or False,
            "phone": self.phone or False,
            "description": "\n".join(filter(None, [
                "Partner Role: %s" % role_label,
                "VAT: %s" % (self.vat or ""),
                "CoC: %s" % (self.coc or ""),
                "IRS: %s" % (self.irs or ""),
                "IBAN: %s" % (self.iban or ""),
                "",
                self.note or "",
            ])).strip() or False,
            "company_id": self.company_id.id,
        })

        self.sudo().write({"crm_lead_id": lead.id})
        return lead

    # ----------------------------
    # Portal user + fresh signup/reset link
    # ----------------------------
    def _ensure_portal_user_and_link(self, partner):
        email = (partner.email or "").strip().lower()
        if not email:
            raise UserError(_("Partner email is required to create a Portal login."))

        Users = self.env["res.users"].sudo()
        portal_group = self.env.ref("base.group_portal")

        user = Users.search([("login", "=", email)], limit=1)
        if not user:
            user = Users.create({
                "name": partner.name,
                "login": email,
                "email": email,
                "partner_id": partner.id,
                "groups_id": [(6, 0, [portal_group.id])],
                "active": True,
            })
        else:
            if user.partner_id.id != partner.id:
                user.write({"partner_id": partner.id})
            if portal_group.id not in user.groups_id.ids:
                user.write({"groups_id": [(4, portal_group.id)]})
            if not user.email:
                user.write({"email": email})

        try:
            user.action_reset_password()
        except Exception:
            pass

        partner = user.partner_id.sudo()
        partner.signup_prepare()

        base_url = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        signup_url = "%s/web/signup?token=%s" % (base_url, partner.signup_token)

        return user, signup_url

    # ----------------------------
    # Sequences
    # ----------------------------
    @api.model
    def create(self, vals):
        if vals.get("name") in (False, _("New"), "New"):
            vals["name"] = self.env["ir.sequence"].next_by_code("partner.attribution.inquiry") or _("New")
        return super().create(vals)

    # ----------------------------
    # Website submit helper (NEW)
    # ----------------------------
    def action_submit_from_website(self):
        """
        Website submit should:
          - create CRM lead for screening proof
          - move inquiry -> enlisted (screening started)
        It must NOT block on strict approval requirements like documents.
        """
        for rec in self:
            if rec.state in ("approved", "rejected"):
                continue

            # light checks only
            rec._validate_admission(stage="enlist")

            rec._ensure_crm_lead()

            if rec.state == "inquiry":
                rec.sudo().write({"state": "enlisted"})
        return True

    # ----------------------------
    # Actions (match view)
    # ----------------------------
    def action_enlist_partner(self):
        """Inquiry -> Enlisted, and create CRM Lead for screening."""
        for rec in self:
            if rec.state != "inquiry":
                continue
            rec._validate_admission(stage="enlist")
            rec._ensure_crm_lead()
            rec.sudo().write({"state": "enlisted"})
        return True

    def action_open_lead(self):
        self.ensure_one()
        if not self.crm_lead_id:
            return True
        return {
            "type": "ir.actions.act_window",
            "name": _("CRM Lead"),
            "res_model": "crm.lead",
            "res_id": self.crm_lead_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_open_partner(self):
        self.ensure_one()
        if not self.partner_id:
            return True
        return {
            "type": "ir.actions.act_window",
            "name": _("Partner"),
            "res_model": "res.partner",
            "res_id": self.partner_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_reject(self):
        for rec in self:
            if rec.state in ("approved", "rejected"):
                continue
            rec.sudo().write({"state": "rejected"})
        return True

    def action_approve_partner(self):
        Partner = self.env["res.partner"].sudo()

        for rec in self:
            if rec.state != "enlisted":
                raise UserError(_("Only Enlisted inquiries can be Approved."))

            # ✅ strict checks here
            rec._validate_admission(stage="approve")

            partner = False
            if rec.email:
                partner = Partner.search([("email", "=", (rec.email or "").strip())], limit=1)
            if not partner and rec.phone:
                partner = Partner.search([("phone", "=", (rec.phone or "").strip())], limit=1)

            vals = {
                "name": rec.applicant_name,
                "email": (rec.email or "").strip() or False,
                "phone": (rec.phone or "").strip() or False,
                "company_type": "company" if rec.applicant_company else "person",
                "comment": "\n".join(filter(None, [
                    ("Company: %s" % rec.applicant_company) if rec.applicant_company else "",
                    rec.note or "",
                ])).strip() or False,
            }
            if "vat" in Partner._fields and rec.vat:
                vals["vat"] = rec.vat

            if partner:
                partner.write(vals)
            else:
                partner = Partner.create(vals)

            if rec.iban:
                Bank = self.env["res.partner.bank"].sudo()
                iban_clean = (rec.iban or "").replace(" ", "").upper()
                existing = Bank.search([("partner_id", "=", partner.id), ("acc_number", "=", iban_clean)], limit=1)
                if not existing:
                    Bank.create({"partner_id": partner.id, "acc_number": iban_clean})

            if "partner_role" in partner._fields:
                partner.write({"partner_role": rec.partner_role})

            if hasattr(partner, "action_approve_partner"):
                partner.action_approve_partner()
            else:
                if "partner_state" in partner._fields:
                    partner.write({"partner_state": "approved"})

            if rec.attachment_ids:
                rec.attachment_ids.sudo().write({"res_model": "res.partner", "res_id": partner.id})

            _user, signup_url = rec._ensure_portal_user_and_link(partner)

            rec.sudo().write({
                "partner_id": partner.id,
                "signup_url": signup_url,
                "state": "approved",
            })

        return True