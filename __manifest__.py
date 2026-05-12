# -*- coding: utf-8 -*-
{
    'name': 'Reza Field Service Buttons',
    'version': '19.0.1.0.0',
    'summary': 'Adds Order and Credit Note buttons to Field Service sub-tasks',
    'author': 'Reza',
    'category': 'Field Service',
    'depends': [
        'industry_fsm',
        'sale',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/project_task_views.xml',
        'views/liaise_worksheet_report.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'reza_field_service_buttons/static/src/js/list_delete_confirm.js',
            'reza_field_service_buttons/static/src/js/lightbox_image.js',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'OPL-1',
}