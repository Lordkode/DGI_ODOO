from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    fne_enabled = fields.Boolean(
        string="FNE activée",
        default=False,
        help="Active le connecteur FNE (DGI) pour cette société. Les autres sociétés "
             "conservent le comportement standard d'Odoo : aucun bouton, champ ou appel DGI."
    )
    fne_api_key = fields.Char(string="Clé API FNE")
    fne_mode = fields.Selection(
        [('test', 'Test'), ('prod', 'Production')],
        string="Mode FNE",
        default='test',
    )
    fne_auto_send = fields.Boolean(string="Envoi automatique après validation", default=False)
    fne_test_url = fields.Char(string="URL Test", default="http://54.247.95.108/ws")
    fne_prod_url = fields.Char(string="URL Production", default="https://www.services.fne.dgi.gouv.ci/ws")
    fne_point_de_vente = fields.Char(string="Point de Vente")
    fne_establishment = fields.Char(string="Établissement")
    fne_footer = fields.Html(string="Pied de page FNE", default="<p>Merci pour votre confiance</p>")
    fne_external_api_key = fields.Char(
        string="Clé API - Création externe de factures",
        help="Clé que le système externe doit fournir (header X-Api-Key) pour créer des "
             "factures via /fne/external/invoices pour cette société."
    )
