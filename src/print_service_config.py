from collections import OrderedDict

from service_config import ServiceConfig
from permissions_query import PermissionsQuery


class PrintServiceConfig(ServiceConfig):
    """PrintServiceConfig class

    Generate Print service config.
    """

    def __init__(self, config_models, logger):
        """Constructor

        :param Logger logger: Logger
        """
        super().__init__(
            'print',
            'https://github.com/qwc-services/qwc-print-service/raw/master/schemas/qwc-print-service.json',
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
        resources['print_templates'] = self._print_templates(session)

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

        permissions['print_templates'] = self._print_template_permissions(
            role, session)

        # collect feature reports
        session.close()

        return permissions

    def _print_templates(self, session):
        """Return print service resources.

        :param Session session: DB session
        """
        templates = []

        TemplateQGIS = self.config_models.model('template_qgis')

        query = session.query(TemplateQGIS).order_by(TemplateQGIS.name)
        for template_obj in query.all():
            resource = {
                'template': template_obj.name
            }
            templates.append(resource)

        return templates

    def _print_template_permissions(self, role, session):
        """Collect template permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        TemplateQGIS = self.config_models.model('template_qgis')

        resource_ids = self.permissions_query.resource_ids(
            ['template'], role, session)

        query = session.query(TemplateQGIS).\
            order_by(TemplateQGIS.name).\
            filter(TemplateQGIS.gdi_oid.in_(resource_ids))
        for template_obj in query.all():
            permissions.append(template_obj.name)

        return permissions
