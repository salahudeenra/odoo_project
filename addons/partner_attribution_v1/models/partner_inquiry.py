# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PartnerAttributionInquiry(models.Model):
    _name = "partner.attribution.inquiry"
    _description = "Partner Inquiry (Onboarding)"
    _order = "id desc"
    _rec_name = "name"

    # ----------------------------
    # Core fields (keep your existing)
    # ----------------------------
    name = fields.Char(string="Contact / Company Name", required=True)
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    notes = fields.Text(string="Notes / Requirements")

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    state = fields.Selection(
        [
            ("inquiry", "Inquiry"),
            ("enlisted", "Enlisted"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Status",
        default="inquiry",
        required=True,
        index=True,
        tracking=True,
    )

    partner_role = fields.Selection(
        selection=[
            ("ap", "Affiliate Partner"),
            ("lead", "Lead Partner"),
            ("sales_agent", "Sales Agent"),
            ("sales_partner", "Sales Partner (Buy–Sell)"),
        ],
        string="Partner Role (for enlistment)",
    )

    partner_id = fields.Many2one(
        "res.partner",
        string="Linked Partner",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    # ----------------------------
    # Fields required by your portal form / newer views
    # ----------------------------
    applicant_name = fields.Char(string="Applicant Name")
    applicant_company = fields.Char(string="Company")

    vat = fields.Char(string="VAT / Tax ID")
    iban = fields.Char(string="IBAN")
    coc = fields.Char(string="CoC Number")
    irs = fields.Char(string="IRS / Tax Ref")

    # Keep portal/view field name "note" but store in your existing "notes"
    note = fields.Text(string="Message / Note", compute="_compute_note", inverse="_inverse_note")

    # Optional attachments field used in your view
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "partner_attr_inquiry_attachment_rel",
        "inquiry_id",
        "attachment_id",
        string="Attachments",
        help="Uploaded documents for compliance/onboarding.",
    )

    @api.depends("notes")
    def _compute_note(self):
        for rec in self:
            rec.note = rec.notes

    def _inverse_note(self):
        for rec in self:
            rec.notes = rec.note

    # ----------------------------
    # existing logic
    # ----------------------------
    def _find_existing_partner(self):
        """Try to match by email/phone first (safe default)."""
        self.ensure_one()
        domain = [("company_id", "=", self.company_id.id)]
        candidates = self.env["res.partner"].sudo()

        if self.email:
            candidates |= self.env["res.partner"].sudo().search(domain + [("email", "=", self.email)], limit=1)
        if not candidates and self.phone:
            candidates |= self.env["res.partner"].sudo().search(domain + [("phone", "=", self.phone)], limit=1)
        return candidates[:1]

    def action_enlist_partner(self):
        """
        Inquiry -> Enlisted
        - Create or link a partner
        - Put partner_state = draft (NOT approved yet)
        """
        for rec in self:
            if rec.state != "inquiry":
                continue

            partner = rec._find_existing_partner()
            if not partner:
                vals = {
                    "name": rec.name,
                    "email": rec.email or False,
                    "phone": rec.phone or False,
                    "company_id": rec.company_id.id,
                }
                partner = self.env["res.partner"].sudo().create(vals)

            if rec.partner_role:
                partner.sudo().write({"partner_role": rec.partner_role})

            partner.sudo().write({"partner_state": "draft"})

            rec.sudo().write({
                "partner_id": partner.id,
                "state": "enlisted",
            })

        return True

    def action_approve_partner(self):
        """
        Enlisted -> Approved
        Calls your existing approval method on res.partner (partner_uid/partner_code generation)
        """
        for rec in self:
            if rec.state not in ("enlisted",):
                continue
            if not rec.partner_id:
                raise UserError(_("No partner linked. Click 'Enlist Partner' first."))

            if not rec.partner_id.partner_role:
                if rec.partner_role:
                    rec.partner_id.sudo().write({"partner_role": rec.partner_role})
                else:
                    raise UserError(_("Please set Partner Role (in inquiry or in contact) before approval."))

            rec.partner_id.sudo().action_approve_partner()
            rec.sudo().write({"state": "approved"})

        return True

    def action_open_partner(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("No partner linked yet. Click 'Enlist Partner' first."))

        return {
            "type": "ir.actions.act_window",
            "name": _("Partner"),
            "res_model": "res.partner",
            "view_mode": "form",
            "res_id": self.partner_id.id,
            "target": "current",
        }

    def action_reject(self):
        for rec in self:
            rec.sudo().write({"state": "rejected"})
        return True