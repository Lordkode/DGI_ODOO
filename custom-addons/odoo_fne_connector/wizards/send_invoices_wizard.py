from odoo import models, fields, api, _
from odoo.exceptions import UserError

class FneSendWizard(models.TransientModel):
    _name = 'fne.send.wizard'
    _description = "Wizard d'envoi des factures à la DGI"

    invoice_ids = fields.Many2many(
    'account.move',
    'fne_wizard_invoices_rel',
    'wizard_id',
    'invoice_id',
    string="Factures à envoyer"
)

    exclude_invoice_ids = fields.Many2many(
    'account.move',
    'fne_wizard_exclude_invoices_rel',  # table différente
    'wizard_id',
    'invoice_id',
    string="Factures à exclure"
)


    def action_send_selected(self):
        factures_a_envoyer = self.invoice_ids - self.exclude_invoice_ids
        if not factures_a_envoyer:
            raise UserError(_("Aucune facture sélectionnée pour l'envoi."))

        for invoice in factures_a_envoyer:
            invoice.action_send_to_fne()
        return {'type': 'ir.actions.act_window_close'}
