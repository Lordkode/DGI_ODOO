# -*- coding: utf-8 -*-
from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    """Écran d'édition des réglages FNE, stockés par société sur res.company
    (voir res_company.py) plutôt que dans ir.config_parameter, qui est global
    à toute la base et ne permettrait aucune isolation entre filiales."""
    _inherit = 'res.config.settings'

    fne_enabled = fields.Boolean(related='company_id.fne_enabled', readonly=False)
    fne_api_key = fields.Char(related='company_id.fne_api_key', readonly=False)
    fne_mode = fields.Selection(related='company_id.fne_mode', readonly=False)
    fne_auto_send = fields.Boolean(related='company_id.fne_auto_send', readonly=False)
    fne_test_url = fields.Char(related='company_id.fne_test_url', readonly=False)
    fne_prod_url = fields.Char(related='company_id.fne_prod_url', readonly=False)
    fne_point_de_vente = fields.Char(related='company_id.fne_point_de_vente', readonly=False)
    fne_establishment = fields.Char(related='company_id.fne_establishment', readonly=False)
    fne_footer = fields.Html(related='company_id.fne_footer', readonly=False)
    fne_external_api_key = fields.Char(related='company_id.fne_external_api_key', readonly=False)
