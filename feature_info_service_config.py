from collections import OrderedDict
import json

from sqlalchemy.orm import joinedload

from permissions_query import PermissionsQuery
from service_config import ServiceConfig


class FeatureInfoServiceConfig(ServiceConfig):
    """FeatureInfoServiceConfig class

    Generate FeatureInfo service config and permissions.
    """

    def __init__(self, config_models, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param Logger logger: Logger
        """
        super().__init__(
            'featureInfo',
            'https://github.com/qwc-services/qwc-feature-info-service/raw/master/schemas/qwc-feature-info-service.json',
            logger
        )

        self.config_models = config_models
        self.permissions_query = PermissionsQuery(config_models, logger)

    def config(self, service_config):
        """Return service config.

        :param obj service_config: Additional service config
        """
        # get base config
        config = super().config(service_config)
        config['service'] = 'feature-info'

        # additional service config
        cfg_config = service_config.get('config', {})
        if 'default_qgis_server_url' not in cfg_config:
            # use default QGIS server URL from ConfigGenerator config
            # if not set in service config
            qgis_server_url = service_config.get('defaults', {}).get(
                'qgis_server_url', 'http://localhost:8001/ows/'
            ).rstrip('/') + '/'
            cfg_config['default_qgis_server_url'] = qgis_server_url

        session = self.config_models.session()

        if 'default_info_template' not in cfg_config:
            # get any default info template
            TemplateInfo = self.config_models.model('template_info')
            query = session.query(TemplateInfo). \
                filter(TemplateInfo.name == 'default')
            default_template = query.first()
            if default_template:
                # NOTE: use HTML template only
                cfg_config['default_info_template'] = \
                    default_template.info_template

        config['config'] = cfg_config

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from ConfigDB

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        resources['wms_services'] = self.wms_services(service_config, session)

        session.close()

        return config

    def permissions(self, role):
        """Return service permissions for a role.

        :param str role: Role name
        """
        # NOTE: use ordered keys
        permissions = OrderedDict()

        # NOTE: WMS service permissions collected by OGC service config
        permissions['wms_services'] = []
        # NOTE: feature reports permissions collected by Document service config
        permissions['document_templates'] = []

        return permissions

    # service config

    def wms_services(self, service_config, session):
        """Collect WMS Service resources from ConfigDB.

        :param obj service_config: Additional service config
        :param Session session: DB session
        """
        wms_services = []

        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter(WmsWfs.ows_type == 'WMS')
        for wms in query.all():
            # NOTE: use ordered keys
            wms_service = OrderedDict()
            wms_service['name'] = wms.name
            # collect WMS layers
            wms_service['root_layer'] = self.collect_wms_layers(
                wms.root_layer, False
            )

            wms_services.append(wms_service)

        return wms_services

    def collect_wms_layers(self, layer, facade):
        """Recursively collect WMS layer info for layer subtree from ConfigDB
        and return nested WMS layers.

        :param obj layer: Group or Data layer object
        :param bool facade: Whether this is a facade sub layer
        """
        # NOTE: use ordered keys
        wms_layer = OrderedDict()

        wms_layer['name'] = layer.name
        if layer.type == 'group':
            wms_layer['type'] = 'layergroup'
        else:
            wms_layer['type'] = 'layer'
        if layer.title:
            wms_layer['title'] = layer.title

        if layer.type == 'group':
            # group layer

            # set facade if layer is a facade group or is a facade sub layer
            in_facade = layer.facade or facade

            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sub layer
                sublayers.append(self.collect_wms_layers(sublayer, in_facade))

            wms_layer['layers'] = sublayers

            if layer.facade:
                wms_layer['hide_sublayers'] = True
        else:
            # data layer

            # collect attributes
            attributes = []
            display_field = None
            for attr in layer.data_set_view.attributes:
                # NOTE: use ordered keys
                attribute = OrderedDict()
                attribute['name'] = attr.name

                # alias
                if attr.alias:
                    alias = attr.alias
                    json_aliases = None
                    try:
                        if alias.startswith('{'):
                            # parse JSON
                            json_config = json.loads(alias)
                            alias = (
                                json_config.get('alias', alias) or
                                attr.name
                            )
                            json_attrs = json_config.get('json_attrs', [])

                            json_aliases = []
                            for json_attr in json_attrs:
                                # NOTE: use ordered keys
                                json_alias = OrderedDict()
                                json_alias['name'] = json_attr.get('name')
                                json_alias['alias'] = json_attr.get('alias') \
                                    or json_attr.get('name')
                                json_aliases.append(json_alias)
                    except Exception as e:
                        self.logger.warning(
                            "Could not parse '%s' value in layer '%s' as JSON:"
                            " '%s'\n%s" %
                            (attr.name, layer.name, alias, e)
                        )

                    attribute['alias'] = alias
                    if json_aliases:
                        attribute['json_attribute_aliases'] = json_aliases

                # format
                if attr.format:
                    attribute['format'] = attr.format

                # display field
                if attr.displayfield:
                    display_field = attr.name

                attributes.append(attribute)

            data_source = layer.data_set_view.data_set.data_source
            if data_source.connection_type == 'database':
                # add geometry column
                attribute = OrderedDict()
                attribute['name'] = 'geometry'
                attributes.append(attribute)

            wms_layer['attributes'] = attributes

            # info template
            if layer.templateinfo:
                templateinfo = layer.templateinfo

                # NOTE: use ordered keys
                info_template = OrderedDict()
                info_template['type'] = templateinfo.info_type

                if templateinfo.info_type == 'wms':
                    # always use default QGIS server URL
                    pass
                elif templateinfo.info_type == 'sql':
                    if data_source.connection_type == 'database':
                        info_template['db_url'] = data_source.connection
                    info_template['sql'] = templateinfo.info_sql
                elif templateinfo.info_type == 'module':
                    info_template['module'] = templateinfo.info_module

                info_template['template'] = templateinfo.info_template

                wms_layer['info_template'] = info_template

            # display field
            if display_field:
                wms_layer['display_field'] = display_field

            # feature_report
            if layer.templatejasper:
                templatejasper = layer.templatejasper
                default_format = templatejasper.default_format or 'pdf'
                report = "%s.%s" % (templatejasper.name, default_format)
                wms_layer['feature_report'] = report

        return wms_layer

    # helpers

    def precache_resources(self, session):
        """Precache some resources using eager loaded relations to reduce the
        number of actual separate DB requests in later queries.

        NOTE: The lookup tables do not have to be actually used, but remain
        in memory, so SQLAlchemy will use the cached records.
        E.g. accessing ows_layer_data.data_set_view afterwards won't generate
        a separate DB query

        :param Session session: DB session
        """
        OWSLayerGroup = self.config_models.model('ows_layer_group')
        OWSLayerData = self.config_models.model('ows_layer_data')
        GroupLayer = self.config_models.model('group_layer')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')

        # precache OWSLayerData and eager load relations
        ows_layer_data_lookup = {}
        query = session.query(OWSLayerData)
        query = query.options(
            joinedload(OWSLayerData.data_set_view)
            .joinedload(DataSetView.data_set)
            .joinedload(DataSet.data_source)
        )
        for layer in query.all():
            ows_layer_data_lookup[layer.gdi_oid] = layer

        # precache DataSetView and eager load attributes
        query = session.query(DataSetView)
        query = query.options(
            joinedload(DataSetView.attributes)
        )
        data_set_view_lookup = {}
        for data_set_view in query.all():
            data_set_view_lookup[data_set_view.gdi_oid] = data_set_view

        # precache OWSLayerGroup and eager load sub layers
        query = session.query(OWSLayerGroup)
        query = query.options(
            joinedload(OWSLayerGroup.sub_layers)
            .joinedload(GroupLayer.sub_layer)
        )
        ows_layer_group_lookup = {}
        for group in query.all():
            ows_layer_group_lookup[group.gdi_oid] = group

        # NOTE: return precached resources so they stay in memory
        return {
            'ows_layer_data_lookup': ows_layer_data_lookup,
            'data_set_view_lookup': data_set_view_lookup,
            'ows_layer_group_lookup': ows_layer_group_lookup
        }
