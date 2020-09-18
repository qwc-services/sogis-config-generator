import os
from collections import OrderedDict

from service_config import ServiceConfig
from permissions_query import PermissionsQuery


class DocumentServiceConfig(ServiceConfig):
    """DocumentServiceConfig class

    Generate Document service config.
    """

    def __init__(self, config_models, logger):
        """Constructor

        :param Logger logger: Logger
        """
        super().__init__(
            'document',
            'https://github.com/qwc-services/qwc-document-service/raw/master/schemas/qwc-document-service.json',
            logger
        )
        self.config_models = config_models
        self.permissions_query = PermissionsQuery(config_models, logger)

    def config(self, service_config):
        """Return service config.

        :param obj service_config: Additional service config
        """
        config = super().config(service_config)

        session = self.config_models.session()

        resources = OrderedDict()
        resources['document_templates'] = self._document_templates(session)

        config['resources'] = resources
        return config

    def permissions(self, role):
        """Return service permissions for a role.

        :param str role: Role name
        """
        # NOTE: use ordered keys
        permissions = OrderedDict()

        # collect permissions from ConfigDB
        session = self.config_models.session()

        permissions['document_templates'] = \
            self._document_template_permissions(role, session)

        # collect feature reports
        session.close()

        return permissions

    def _document_templates(self, session):
        """Return document service resources.

        :param Session session: DB session
        """
        templates = []

        TemplateJasper = self.config_models.model('template_jasper')

        query = session.query(TemplateJasper).order_by(TemplateJasper.name)
        for template_obj in query.all():
            # remove .jrxml extension from filename
            report_filename = os.path.splitext(template_obj.report_filename)[0]
            resource = {
                'template': template_obj.name,
                'report_filename': report_filename
            }
            templates.append(resource)

        return templates

    def _document_template_permissions(self, role, session):
        """Collect template permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        TemplateJasper = self.config_models.model('template_jasper')

        resource_ids = self.permissions_query.resource_ids(
            ['template'], role, session)

        query = session.query(TemplateJasper).\
            order_by(TemplateJasper.name).\
            filter(TemplateJasper.gdi_oid.in_(resource_ids))
        for template_obj in query.all():
            permissions.append(template_obj.name)

        return permissions
