# -*- coding: utf-8 -*-
import base64

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

try:
    import psycopg2
except Exception:
    psycopg2 = None


class ResPartner(models.Model):
    _inherit = "res.partner"

    # ----------------------------
    # KYC fields
    # ----------------------------
    kyc_status = fields.Selection(
        selection=[
            ("not_submitted", "Not Submitted"),
            ("pending", "Pending Review"),
            ("verified", "Verified"),
            ("complete", "Complete"),
            ("rejected", "Rejected"),
        ],
        string="KYC Status",
        default="not_submitted",
        copy=False,
        tracking=True,
        store=True,
        readonly=False,
    )
    kyc_note = fields.Text(string="KYC Notes", copy=False)
    kyc_verified_on = fields.Datetime(string="KYC Verified On", copy=False, readonly=True)

    kyc_blocked = fields.Boolean(
        string="KYC Blocked",
        default=False,
        copy=False,
        tracking=True,
        store=True,
        readonly=False,
        help="If enabled, this partner should be treated as blocked for payouts/approval.",
    )

    bank_verified = fields.Boolean(default=False, copy=False, tracking=True, store=True, readonly=False)
    bank_verified_on = fields.Datetime(string="Bank Verified On", copy=False, readonly=True)

    company_verified = fields.Boolean(default=False, copy=False, tracking=True, store=True, readonly=False)
    company_verified_on = fields.Datetime(string="Company Verified On", copy=False, readonly=True)

    vat_verified = fields.Boolean(default=False, copy=False, tracking=True, store=True, readonly=False)
    vat_verified_on = fields.Datetime(string="VAT Verified On", copy=False, readonly=True)

    # ----------------------------
    # Partner Attribution fields
    # ----------------------------
    partner_role = fields.Selection(
        selection=[
            ("ap", "Affiliate Partner"),
            ("lead", "Lead Partner"),
            ("sales_agent", "Sales Agent"),
            ("sales_partner", "Sales Partner (Buy–Sell)"),
        ],
        string="Partner Role",
        copy=False,
        tracking=True,
    )

    partner_state = fields.Selection(
        selection=[("draft", "Draft"), ("approved", "Approved")],
        string="Partner Status",
        default="draft",
        copy=False,
        tracking=True,
        store=True,
        readonly=True,
    )

    partner_code = fields.Char(string="Partner Code", copy=False, readonly=True, index=True, tracking=True)
    partner_uid = fields.Char(string="Partner ID", copy=False, readonly=True, index=True, tracking=True)

    _sql_constraints = [
        ("partner_code_unique", "unique(partner_code)", "Partner Code must be unique."),
        ("partner_uid_unique", "unique(partner_uid)", "Partner ID must be unique."),
    ]

    # ----------------------------
    # Commission (basic) - (NO DB COLUMN)
    # ----------------------------
    commission_rate = fields.Float(
        string="Commission Rate (%)",
        compute="_compute_commission_rate",
        inverse="_inverse_commission_rate",
        store=False,
        default=5.0,
        help="Commission percentage used later to generate vendor bills from posted customer invoices.",
    )

    def _compute_commission_rate(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for rec in self:
            if not rec.id:
                rec.commission_rate = 5.0
                continue
            val = ICP.get_param(f"partner_attribution_v1.commission_rate.partner_{rec.id}", default="5.0")
            try:
                rec.commission_rate = float(val)
            except Exception:
                rec.commission_rate = 5.0

    def _inverse_commission_rate(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for rec in self:
            if not rec.id:
                continue
            rate = rec.commission_rate or 0.0
            if rate < 0 or rate > 100:
                raise ValidationError(_("Commission Rate must be between 0 and 100."))
            ICP.set_param(f"partner_attribution_v1.commission_rate.partner_{rec.id}", str(rate))

    # ----------------------------
    # Portal URLs (computed only)
    # ----------------------------
    portal_invite_url = fields.Char(string="Portal Invite URL", compute="_compute_portal_invite_url", store=False, readonly=True)
    signup_url = fields.Char(string="Portal Signup URL", compute="_compute_signup_url", store=False, readonly=True)

    def _compute_signup_url(self):
        base_url = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        has_signup_token = "signup_token" in self._fields
        for rec in self:
            token = getattr(rec, "signup_token", False) if has_signup_token else False
            rec.signup_url = f"{base_url}/web/signup?token={token}" if (base_url and token) else False

    def _compute_portal_invite_url(self):
        for rec in self:
            rec.portal_invite_url = rec.signup_url or False

    def _require_auth_signup(self):
        if "signup_token" not in self._fields or not hasattr(self, "signup_prepare"):
            raise UserError(_(
                "Portal invite requires Odoo module 'auth_signup'.\n\n"
                "Fix:\n"
                "1) Apps → install: Signup (auth_signup)\n"
                "2) Add to your module __manifest__.py depends: ['auth_signup']\n"
                "3) Restart Odoo and upgrade your module."
            ))

    def action_generate_portal_invite_link(self):
        self.ensure_one()
        email = (self.email or "").strip().lower()
        if not email:
            raise UserError(_("This partner needs an Email to generate a portal invite link."))

        self._require_auth_signup()
        self.sudo().signup_prepare()

        token = getattr(self, "signup_token", False)
        if not token:
            raise UserError(_("Signup token was not generated. Please check auth_signup configuration."))

        base_url = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        if not base_url:
            raise UserError(_("Missing system parameter web.base.url. Please set it in Settings."))

        url = f"{base_url}/web/signup?token={token}"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Portal Invite Link"),
                "message": url,
                "sticky": True,
                "type": "success",
            }
        }

    # ----------------------------
    # Contract (basic) - render + attach
    # ----------------------------
    def action_generate_partner_contract(self):
        self.ensure_one()

        if self.partner_state != "approved":
            raise UserError(_("Only approved partners can generate a contract."))

        report = self.env.ref("partner_attribution_v1.action_report_partner_contract", raise_if_not_found=False)
        if not report:
            raise UserError(_("Partner Contract report is not configured."))

        return report.report_action(
            self,
            data={
                "partner_contract_today": fields.Date.context_today(self).strftime("%Y-%m-%d")
            }
        )

        # Pass a safe date string into report context (avoid QWeb 'fields' / 'format_datetime')
        today_str = fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        pdf, _ = self.env["ir.actions.report"].sudo().with_context(
            partner_contract_today=today_str
        )._render_qweb_pdf(report_xmlid, self.ids)

        filename = "Partner_Contract_%s.pdf" % (self.partner_code or self.id)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf),
            "mimetype": "application/pdf",
            "res_model": "res.partner",
            "res_id": self.id,
        })

        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    # ----------------------------
    # KYC Actions
    # ----------------------------
    def action_set_kyc_pending(self):
        self.write({"kyc_status": "pending"})

    def action_set_kyc_verified(self):
        self.write({"kyc_status": "verified", "kyc_verified_on": fields.Datetime.now(), "kyc_blocked": False})

    def action_set_kyc_complete(self):
        for partner in self:
            partner.write({
                "kyc_status": "complete",
                "kyc_verified_on": partner.kyc_verified_on or fields.Datetime.now(),
                "kyc_blocked": False,
            })

    def action_set_kyc_rejected(self):
        self.write({"kyc_status": "rejected", "kyc_verified_on": False})

    def action_set_kyc_blocked(self):
        self.write({"kyc_blocked": True})

    def action_set_kyc_unblocked(self):
        self.write({"kyc_blocked": False})

    def action_set_bank_verified(self):
        self.write({"bank_verified": True, "bank_verified_on": fields.Datetime.now()})

    def action_set_bank_unverified(self):
        self.write({"bank_verified": False, "bank_verified_on": False})

    def action_set_company_verified(self):
        self.write({"company_verified": True, "company_verified_on": fields.Datetime.now()})

    def action_set_company_unverified(self):
        self.write({"company_verified": False, "company_verified_on": False})

    def action_set_vat_verified(self):
        self.write({"vat_verified": True, "vat_verified_on": fields.Datetime.now()})

    def action_set_vat_unverified(self):
        self.write({"vat_verified": False, "vat_verified_on": False})

    # ----------------------------
    # Helpers
    # ----------------------------
    def _pick_and_cleanup_sequence(self, seq_code: str):
        Sequence = self.env["ir.sequence"].sudo()
        seqs = Sequence.search([
            ("code", "=", seq_code),
            ("active", "=", True),
            ("company_id", "in", [False, self.env.company.id]),
        ])
        if not seqs:
            return False

        if len(seqs) > 1:
            def score(s):
                next_num = getattr(s, "number_next_actual", s.number_next)
                return (
                    bool(s.company_id and s.company_id.id == self.env.company.id),
                    int(next_num or 0),
                    int(s.id or 0),
                )
            best = max(seqs, key=score)
            (seqs - best).write({"active": False})
            return best

        return seqs[0]

    def _next_sequence_or_raise(self, seq_code: str, label: str) -> str:
        seq = self._pick_and_cleanup_sequence(seq_code)
        if not seq:
            raise UserError(_(
                "%s sequence not found/misconfigured.\n"
                "Missing sequence code: %s\n\n"
                "Fix:\n"
                "1) Keep your module's ir_sequence.xml that creates this sequence, OR\n"
                "2) Create it manually: Settings > Technical > Sequences."
            ) % (label, seq_code))

        value = seq.next_by_id()
        if not value:
            raise UserError(_("%s sequence exists but could not generate a number.\nSequence code: %s") % (label, seq_code))
        return value

    def _is_unique_violation(self, err: Exception) -> bool:
        if psycopg2 and isinstance(err, psycopg2.Error) and getattr(err, "pgcode", None) == "23505":
            return True
        msg = (str(err) or "").lower()
        if "duplicate key value violates unique constraint" in msg:
            return True
        if "partner_code_unique" in msg or "partner_uid_unique" in msg:
            return True
        if "unique" in msg and "partner" in msg and ("code" in msg or "uid" in msg):
            return True
        return False

    def _ensure_partner_codes(self):
        UID_SEQ = "partner_attribution.partner_uid"
        CODE_SEQ = "partner_attribution.partner_code"

        for partner in self.sudo():
            if partner.partner_state != "approved":
                continue
            if not partner.partner_role:
                raise ValidationError(_("Please set Partner Role before approval."))
            if partner.partner_uid and partner.partner_code:
                continue

            last_err = None
            for _attempt in range(10):
                vals = {}
                if not partner.partner_uid:
                    vals["partner_uid"] = partner._next_sequence_or_raise(UID_SEQ, "Partner ID")
                if not partner.partner_code:
                    vals["partner_code"] = partner._next_sequence_or_raise(CODE_SEQ, "Partner Code")
                try:
                    with self.env.cr.savepoint():
                        partner.write(vals)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if self._is_unique_violation(e):
                        continue
                    raise

            if last_err:
                raise UserError(_(
                    "Could not generate a unique Partner ID/Code after multiple attempts.\n\n"
                    "Check sequences:\n"
                    "- partner_attribution.partner_uid\n"
                    "- partner_attribution.partner_code"
                ))

    def action_approve_partner(self):
        for partner in self.sudo():
            if partner.partner_state != "approved":
                if not partner.partner_role:
                    raise ValidationError(_("Please set Partner Role before approval."))
                partner.write({"partner_state": "approved"})
            partner._ensure_partner_codes()

    def action_reset_to_draft(self):
        self.sudo().write({"partner_state": "draft"})

    def write(self, vals):
        if "partner_uid" in vals:
            for p in self:
                if p.partner_uid and vals["partner_uid"] != p.partner_uid:
                    raise ValidationError(_("Partner ID is immutable once generated."))

        if "partner_code" in vals:
            for p in self:
                if p.partner_code and vals["partner_code"] != p.partner_code:
                    raise ValidationError(_("Partner Code is immutable once generated."))

        res = super().write(vals)

        if vals.get("partner_state") == "approved":
            self._ensure_partner_codes()

        return res