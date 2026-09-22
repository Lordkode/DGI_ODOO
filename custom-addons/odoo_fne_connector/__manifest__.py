{
    'name': 'FNE Connector (DGI) for Odoo 18',
    'version': '18.0.0.1',
    'sequence': 2,
    'summary': 'Connecteur FNE - Déversement des factures vers la DGI',
    'author': 'Neurones Technologies',
    'license': 'AGPL-3',
    'category': 'Accounting',
    'depends': ['account', 'base'],
    'data': [
        'security/ir.model.access.csv',
        'views/fne_actions.xml',
        # 'views/fne_menu.xml',
        'views/fne_config_views.xml',
        'views/fne_invoice_views.xml',
        'views/res_partner.xml',
        
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
