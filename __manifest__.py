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
    ],
    'installable': True,
    'application': False,
    'license': 'OPL-1',
}
