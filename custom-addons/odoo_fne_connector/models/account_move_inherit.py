import logging
from logging import config
import requests
from odoo import models, api, fields, _
from odoo.exceptions import UserError
import time

_logger = logging.getLogger(__name__)

ALLOWED_TAXES = {
    'tva': 'TVA',
    'tvab': 'TVAB',
    'tvac': 'TVAC',
    'tvad': 'TVAD',
}

PAYMENT_METHOD_MAP = {
    'mobile_money': 'mobile-money',
    'espece': 'cash',
    'virement': 'transfer',
    'cheque': 'check',
    'deferred': 'deferred',
    'card': 'card',
}


def _clean_str(val):
    """Nettoie les chaînes pour éviter les caractères invalides."""
    if isinstance(val, str):
        return val.encode("utf-8", errors="ignore").decode("utf-8")
    return val

def _truncate(s, n):
    s = _clean_str(s) or ""
    return s[:n]

# ✅ ODOO 16 : account.invoice.line → account.move.line
class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'
    fne_item_id = fields.Char(string="Item ID FNE", copy=False)

# ✅ ODOO 16 : account.invoice → account.move
class AccountMove(models.Model):
    _inherit = 'account.move'

    fne_sent = fields.Boolean(string="Certifier la facture", default=False, copy=False)
    fne_reference_dgi = fields.Char(string="Référence DGI", readonly=True, copy=False)
    fne_verification_url = fields.Char(string="Lien vérification DGI", readonly=True, copy=False)
    invoice_id_from_fne = fields.Char(string="ID FNE", readonly=True, copy=False)
    fne_warning = fields.Boolean(string="Avertissement FNE", readonly=True, copy=False)
    fne_balance_sticker = fields.Integer(string="Solde sticker FNE", readonly=True, copy=False)
    fne_external_ref = fields.Char(
        string="Référence externe",
        copy=False,
        index=True,
        help="Identifiant fourni par le système externe qui a créé cette facture via l'API, utilisé pour éviter les doublons en cas de nouvel appel."
    )
    fne_mode = fields.Char(compute='_compute_fne_mode', string="Mode FNE")
    fne_company_enabled = fields.Boolean(
        related='company_id.fne_enabled',
        string="FNE activée (société)",
        help="Indique si la FNE est activée pour la société de cette facture."
    )
    modes_paiement = fields.Selection(
        selection=[
            ('mobile_money', 'Mobile Money'),
            ('espece', 'Espèces'),
            ('virement', 'Virement Bancaire'),
            ('cheque', 'Chèque'),
            ('deferred', 'A terme'),
            ('card', 'Carte Bancaire'),
        ],
        string="Methode de paiement",
        default='cheque',
        help="Sélectionnez le mode de paiement pour la facture."
    )
    def _compute_fne_mode(self):
        """Récupère dynamiquement le mode configuré sur la société de chaque facture."""
        for record in self:
            record.fne_mode = record.company_id.fne_mode or 'test'

    def _compute_custom_taxes(self):
        """Agrège les taxes non TVA (autres prélèvements) au niveau racine."""
        customs = {}
        for line in self.invoice_line_ids.filtered(lambda l: l.product_id):
            # ✅ ODOO 16 : invoice_line_tax_ids → tax_ids
            for tax in line.tax_ids:
                tg = (tax.tax_group_id and tax.tax_group_id.name or "")
                tg_normalized = ''.join(ch for ch in tg.upper() if ch.isalpha())
                if "TVA" in tg_normalized:
                    continue
                key = (tax.name, tax.amount)
                customs[key] = {"name": _truncate(tax.name, 50), "amount": float(tax.amount)}
        return list(customs.values())

    def _compute_currency_block(self):
        """foreignCurrency/rate si devise ≠ devise société."""
        if self.currency_id and self.currency_id != self.company_currency_id:
            rate = self.company_currency_id._get_conversion_rate(
                self.company_currency_id, self.currency_id, self.company_id,
                self.invoice_date or fields.Date.context_today(self)
            ) or 0.0
            return self.currency_id.name or "", float(rate)
        return "", 0.0

    def _get_fne_client_fields(self):
        """Construit les champs client communs (sale/purchase) et valide les champs obligatoires FNE."""
        partner = self.partner_id
        template = (partner.templateFne or 'b2c').upper()
        phone = partner.phone
        if not phone:
            raise UserError(_("Le numéro de téléphone du client '%s' est requis par la FNE.") % partner.name)
        if not partner.email:
            raise UserError(_("L'adresse e-mail du client '%s' est requise par la FNE.") % partner.name)
        return {
            'template': template,
            'clientCompanyName': partner.name or '',
            'clientPhone': phone,
            'clientEmail': partner.email,
        }

    def _prepare_payload_sale(self):
        self.ensure_one()
        fne_config = self._get_fne_config()
        client_fields = self._get_fne_client_fields()

        if client_fields['template'] == 'B2B' and not self.partner_id.vat:
            raise UserError(_(
                "Le NCC du client '%s' est requis pour une facture B2B.\n"
                "Veuillez le renseigner dans le champ TVA de la fiche client."
            ) % self.partner_id.name)

        payload = {
            "invoiceType": "sale",
            "paymentMethod": PAYMENT_METHOD_MAP.get(self.modes_paiement, 'cash'),
            "isRne": False,
            "pointOfSale": fne_config['point_de_vente'],
            "establishment": fne_config['establishment'],
            "footer": fne_config['footer'],
            "items": self._build_items(),
            **client_fields,
        }
        if client_fields['template'] == 'B2B':
            payload['clientNcc'] = self.partner_id.vat

        custom_taxes = self._compute_custom_taxes()
        if custom_taxes:
            payload['customTaxes'] = custom_taxes

        currency_name, currency_rate = self._compute_currency_block()
        if currency_name:
            payload['foreignCurrency'] = currency_name
            payload['foreignCurrencyRate'] = currency_rate

        return payload

    def _prepare_payload_purchase_agri(self):
        self.ensure_one()
        fne_config = self._get_fne_config()
        client_fields = self._get_fne_client_fields()

        return {
            "invoiceType": "purchase",
            "paymentMethod": PAYMENT_METHOD_MAP.get(self.modes_paiement, 'cash'),
            "template": client_fields['template'],
            "isRne": False,
            "clientCompanyName": client_fields['clientCompanyName'],
            "clientPhone": client_fields['clientPhone'],
            "clientEmail": client_fields['clientEmail'],
            "pointOfSale": fne_config['point_de_vente'],
            "establishment": fne_config['establishment'],
            "footer": fne_config['footer'],
            "items": self._build_items(),
        }

    def _build_items(self):
        try:
            regime_fiscal = self.partner_id.regimeFiscal
        except AttributeError:
            regime_fiscal = False
            _logger.warning("[FNE] Le champ regimeFiscal n'existe pas sur res.partner. Utilisant False par défaut.")

        taxe = ALLOWED_TAXES.get(regime_fiscal)
        _logger.info(f"[FNE] Régime fiscal pour {self.partner_id.name}: {regime_fiscal} -> Taxe: {taxe}")
        items = []
        for line in self.invoice_line_ids.filtered(lambda l: l.product_id):
            # ✅ ODOO 16 : self.type → self.move_type
            if self.move_type == 'out_invoice':
                _logger.info(f"[FNE] Traitement de la ligne {line.name} (Produit: {line.product_id.name}) pour le type {self.move_type}")
                taxes_list = [taxe] if taxe else []
                items.append({
                    "reference": _clean_str(line.product_id.default_code or ""),
                    "description": _truncate(line.name or line.product_id.display_name or "Ligne", 255),
                    "quantity": float(line.quantity or 0),
                    "amount": float(line.price_unit or 0),
                    "discount": float(line.discount or 0),
                    # ✅ ODOO 16 : line.uom_id → line.product_uom_id
                    "measurementUnit": _clean_str(line.product_uom_id and line.product_uom_id.name or "unités"),
                    "taxes": taxes_list,
                })
            elif self.move_type == 'in_invoice':
                items.append({
                    "reference": _clean_str(line.product_id.default_code or ""),
                    "description": _truncate(line.name or line.product_id.display_name or "Ligne", 255),
                    "quantity": float(line.quantity or 0),
                    "amount": float(line.price_unit or 0),
                    "discount": float(line.discount or 0),
                    "measurementUnit": _clean_str(line.product_uom_id and line.product_uom_id.name or "unités"),
                })
        return items

    def _get_fne_config(self):
        """Charge et valide la configuration FNE (clé API, mode, URL, point de vente)
        de la société de cette facture. Point d'entrée unique : c'est ici que se joue
        l'isolation multi-société, toute méthode qui parle à la DGI passe par elle."""
        company = self.company_id
        if not company.fne_enabled:
            raise UserError(_(
                "La FNE n'est pas activée pour la société '%s'.\n\n"
                "Activez-la dans Configuration > Paramètres > Section FNE si cette société "
                "doit certifier ses factures auprès de la DGI."
            ) % company.name)

        point_de_vente = (company.fne_point_de_vente or '').strip()
        establishment = (company.fne_establishment or '').strip()
        footer = (company.fne_footer or '').strip()
        api_key = (company.fne_api_key or '').strip()
        mode = (company.fne_mode or 'test').lower().strip()

        if not point_de_vente:
            raise UserError(_(
                "Le point de vente n'est pas configuré pour le FNE.\n\n"
                "Veuillez le renseigner dans:\n"
                "Configuration > Paramètres > Section FNE > Point de Vente"
            ))
        if not api_key:
            raise UserError(_(
                "La clé API FNE n'est pas configurée.\n\n"
                "Veuillez configurer le module FNE dans:\n"
                "Configuration > Paramètres > Section FNE"
            ))
        if not establishment:
            establishment = company.name or "ENTREPRISE"

        if mode == 'prod':
            base_url = company.fne_prod_url or 'https://www.services.fne.dgi.gouv.ci/ws'
        else:
            base_url = company.fne_test_url or 'http://54.247.95.108/ws'

        if not base_url or not base_url.strip():
            raise UserError(_(
                "L'URL de l'API FNE n'est pas configurée pour le mode '%s'.\n\n"
                "Veuillez configurer le module FNE dans:\n"
                "Configuration > Paramètres > Section FNE"
            ) % mode)

        base_url = base_url.strip()
        _logger.info("=" * 70)
        _logger.info("[FNE] Configuration chargée:")
        _logger.info(f"  - Mode: {mode}")
        _logger.info(f"  - API Key: {'*' * (len(api_key) - 4) + api_key[-4:] if len(api_key) > 4 else '***'}")
        _logger.info(f"  - Base URL: {base_url}")
        _logger.info("=" * 70)

        return {
            'headers': {
                'Authorization': f"Bearer {api_key}",
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            'base_url': base_url,
            'point_de_vente': point_de_vente,
            'establishment': establishment,
            'footer': footer,
        }

    def _post_refund_to_fne(self, headers, base_url):
        self.ensure_one()
        refund_move = self

        # Le lien natif Odoo (rempli automatiquement par le bouton "Avoir") est la source fiable.
        # On ne retombe sur le champ texte 'invoice_origin' que pour d'éventuels avoirs créés
        # autrement (import, saisie manuelle) où ce lien structuré ne serait pas renseigné.
        origin = refund_move.reversed_entry_id
        if not origin:
            if not refund_move.invoice_origin:
                raise UserError(_(
                    "Impossible de retrouver la facture d'origine de l'avoir %s : "
                    "ni le lien natif Odoo (reversed_entry_id), ni le champ 'Origin' ne sont renseignés."
                ) % refund_move.name)
            # ✅ ODOO 16 : account.invoice → account.move, number → name, type → move_type
            origin = self.env['account.move'].search([
                ('name', '=', refund_move.invoice_origin),
                ('move_type', 'in', ('out_invoice', 'in_invoice'))
            ], limit=1)

        if not origin:
            raise UserError(_("Facture d'origine introuvable (numéro %s) pour l'avoir %s") % (refund_move.invoice_origin, refund_move.name))

        if not origin.invoice_id_from_fne:
            raise UserError(_("ID FNE de la facture d'origine manquant pour l'avoir %s") % refund_move.name)

        _logger.info("[FNE] REFUND %s -> Recherche lignes originales de %s", refund_move.name, origin.name)

        items = []
        origin_lines_map = {
            line.product_id.id: line
            for line in origin.invoice_line_ids.filtered(lambda l: l.product_id and l.fne_item_id)
        }

        for line in refund_move.invoice_line_ids.filtered(lambda l: l.product_id):
            qty = abs(line.quantity or 0)
            if qty <= 0:
                continue

            orig_line = origin_lines_map.get(line.product_id.id)

            if not orig_line:
                raise UserError(_("Ligne d'origine introuvable ou ID FNE manquant pour le produit '%s' (avoir %s).") % (line.product_id.display_name or line.name or '', refund_move.name))

            fne_item_id = orig_line.fne_item_id

            if not fne_item_id:
                raise UserError(_("ID FNE de l'item d'origine manquant pour la ligne '%s' (avoir %s)") % (line.name or '', refund_move.name))

            items.append({"id": fne_item_id, "quantity": float(qty)})

        if not items:
            raise UserError(_("Aucune ligne valable pour le refund FNE (quantités nulles ou produits non mappés)."))

        endpoint = base_url.rstrip('/') + f"/external/invoices/{origin.invoice_id_from_fne}/refund"
        body = {"items": items}
        _logger.info("[FNE] REFUND %s ENDPOINT: %s body=%s", refund_move.name, endpoint, body)

        data = self._request_fne("POST", endpoint, headers, json_body=body)
        refund_move.fne_sent = True
        refund_move.fne_reference_dgi = data.get("reference") or False
        refund_move.fne_verification_url = data.get("token") or False
        refund_move.fne_warning = bool(data.get("warning"))
        refund_move.fne_balance_sticker = int(data.get("balance_sticker") or 0)
        _logger.info(f'[FNE] Avoir {refund_move.name} certifié avec succès. Réponse DGI : {data}')
        return data
    def _apply_sign_success(self, data):
        self.fne_sent = True
        self.fne_reference_dgi = data.get("reference") or False
        self.fne_verification_url = data.get("token") or False
        self.fne_warning = bool(data.get("warning"))
        self.fne_balance_sticker = int(data.get("balance_sticker") or 0)
        _logger.info(f'[FNE] Facture {self.name} certifiée avec succès. Réponse DGI : {data}')

        self.invoice_id_from_fne = data.get("id") or data.get("invoice", {}).get("id") or self.invoice_id_from_fne
        items = data.get("invoice", {}).get("items", [])
        
        # Mapping des ID d'items FNE aux lignes de facture Odoo par référence/produit (plus fiable)
        for fne_item in items:
            fne_item_id = fne_item.get("id")
            # Une implémentation plus robuste serait de mapper par la référence que vous avez envoyée
            # Mais en l'absence de référence unique, on se base sur l'ordre/description (moins fiable)
            # Puisque l'API retourne les items dans l'ordre, nous utilisons l'index comme fallback
            
            # Recherche de la ligne Odoo non-affichable
            try:
                line_index = items.index(fne_item)
                # Utiliser l'index pour trouver la ligne Odoo correspondante
                line = self.invoice_line_ids.filtered(lambda l: l.product_id)[line_index]
                if fne_item_id:
                    line.fne_item_id = fne_item_id
                else:
                    _logger.warning(f"[FNE] Aucun ID trouvé pour l’item {line_index} de la facture {self.name}")
            except IndexError:
                 _logger.warning(f"[FNE] Pas assez d’items retournés par la DGI pour mapper toutes les lignes de la facture {self.name}")
            except Exception as e:
                _logger.warning(f"[FNE] Erreur de mapping d'item FNE pour {self.name}: {e}")
                
    
    
    def _request_fne(self, method, url, headers, json_body=None, retries=2, timeout=30):
        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
                try:
                    data = resp.json()
                except ValueError:
                    data = {"raw_response": _truncate(resp.text, 200) } # Troncature pour les logs

                if resp.status_code in (200, 201):
                    return data
                if 500 <= resp.status_code < 600 and attempt < retries:
                    _logger.warning(f"[FNE] Tentative {attempt+1}/{retries+1} : Erreur 5xx. Nouvelle tentative dans {2 ** attempt}s.")
                    time.sleep(2 ** attempt)
                    continue
                raise UserError(_("FNE %s %s : %s - %s") % (method, url, resp.status_code, data))
            except requests.RequestException as e:
                last_err = e
                if attempt < retries:
                    _logger.warning(f"[FNE] Tentative {attempt+1}/{retries+1} : Erreur réseau. Nouvelle tentative dans {2 ** attempt}s.")
                    time.sleep(2 ** attempt)
                    continue
                raise UserError(_("Erreur réseau FNE %s %s : %s") % (method, url, str(e)))
        raise last_err # Devrait être atteint après la boucle
                
    def action_send_to_fne(self):
        fne_config = self._get_fne_config()
        headers = fne_config['headers']
        base_url = fne_config['base_url']
        endpoint_sign = base_url.rstrip('/') + "/external/invoices/sign"

        succeeded = []
        errors = []

        for inv in self:
            if inv.fne_sent:
                _logger.info("[FNE] %s déjà certifiée.", inv.name)
                continue

            if inv.state != 'posted':
                _logger.info("[FNE] %s ignorée : la facture n'est pas validée (état '%s').", inv.name, inv.state)
                continue

            if inv.move_type not in ('out_invoice', 'in_invoice', 'out_refund'):
                _logger.info("[FNE] %s ignorée (type %s non géré par l'envoi FNE).", inv.name, inv.move_type)
                continue

            try:
                if inv.move_type == 'out_invoice':
                    payload = inv._prepare_payload_sale()
                    _logger.info("[FNE] SIGN %s payload=%s", inv.name, payload)
                    data = inv._request_fne("POST", endpoint_sign, headers, json_body=payload)
                    inv._apply_sign_success(data)

                elif inv.move_type == 'in_invoice':
                    payload = inv._prepare_payload_purchase_agri()
                    _logger.info("[FNE] SIGN (purchase) %s payload=%s", inv.name, payload)
                    data = inv._request_fne("POST", endpoint_sign, headers, json_body=payload)
                    inv._apply_sign_success(data)

                elif inv.move_type == 'out_refund':
                    inv._post_refund_to_fne(headers, base_url)

                # Persiste immédiatement ce succès : si une facture suivante du lot échoue,
                # les certifications déjà obtenues auprès de la DGI ne doivent pas être perdues.
                self.env.cr.commit()
                succeeded.append(inv.name)

            except Exception as e:
                # Capturer le nom AVANT le rollback : après invalidate_all(), le cache est
                # vidé et relire un champ sur inv force une requête sur une transaction déjà
                # annulée, ce qui lève une MissingError qui masquerait l'erreur DGI d'origine.
                inv_name = inv.name
                message = e.args[0] if getattr(e, 'args', None) else str(e)
                self.env.cr.rollback()
                self.env.invalidate_all()
                _logger.exception("[FNE] Échec de certification pour %s", inv_name)
                errors.append(f"{inv_name} : {message}")

        if errors:
            raise UserError(_(
                "%(success)d facture(s) certifiée(s) avec succès.\n\n"
                "Échec pour %(fail)d facture(s) :\n%(details)s"
            ) % {
                'success': len(succeeded),
                'fail': len(errors),
                'details': '\n'.join(errors),
            })

    def action_open_fne_link(self):
        self.ensure_one()
        if self.fne_verification_url:
            return {
                'type': 'ir.actions.act_url',
                'target': 'new',
                'url': self.fne_verification_url,
            }
        else:
            raise UserError(_("Aucun lien de vérification DGI n'est disponible pour cette facture."))