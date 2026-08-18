# -*- coding: utf-8 -*-
{
    'name': 'APS Connector',
    'version': '18.0.1.10.0',
    'category': 'Manufacturing',
    'summary': 'Finite capacity scheduling for Odoo manufacturing — connect to APS 4 Manufacturing',
    'author': 'Avalah',
    'website': 'https://aps4mfg.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mrp',
        'sale_mrp',
        'stock',
        'purchase',
        'resource',
    ],
    'data': [
        'security/aps_security.xml',
        'security/ir.model.access.csv',
        'views/aps_sync_config_views.xml',
        'views/menu_views.xml',
        'data/aps_sync_config_data.xml',
    ],
    'images': ['static/description/banner.png'],
}
