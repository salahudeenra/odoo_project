# -*- coding: utf-8 -*-
from odoo import models


class PartnerInquiryWorkflowPatch(models.Model):
    _inherit = "partner.attribution.inquiry"

    def action_submit(self):
        if hasattr(super(), "action_submit"):
            return super().action_submit()

        # Fallback: minimal behavior
        for rec in self:
            if rec.state == "draft":
                rec.state = "submitted"
        return True

    def action_accept(self):
        if hasattr(super(), "action_accept"):
            return super().action_accept()

        # Fallback: minimal behavior
        for rec in self:
            if rec.state in ("draft", "submitted"):
                rec.state = "accepted"
        return True

    def action_reject(self):
        if hasattr(super(), "action_reject"):
            return super().action_reject()

        for rec in self:
            rec.state = "rejected"
        return True