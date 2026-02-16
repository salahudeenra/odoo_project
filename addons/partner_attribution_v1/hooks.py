# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def _keep_best_sequence_and_archive_others(env, code: str):
    Sequence = env["ir.sequence"].sudo()

    seqs = Sequence.search([("code", "=", code), ("active", "=", True)])
    if len(seqs) <= 1:
        return

    def next_number(s):
        return getattr(s, "number_next_actual", s.number_next)

    # prefer company-specific, then highest next number, then highest id
    def score(s):
        return (
            1 if s.company_id else 0,
            next_number(s),
            s.id,
        )

    best = max(seqs, key=score)
    (seqs - best).write({"active": False})


def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})

    _keep_best_sequence_and_archive_others(env, "partner_attribution.partner_code")
    _keep_best_sequence_and_archive_others(env, "partner_attribution.partner_uid")