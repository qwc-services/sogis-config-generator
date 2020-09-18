from collections import OrderedDict
from urllib.parse import urljoin

from sqlalchemy.orm import joinedload
from sqlalchemy.sql import text as sql_text

from permissions_query import PermissionsQuery
from service_config import ServiceConfig


class OGCServiceConfig(ServiceConfig):
    """OGCServiceConfig class

    Generate OGC service config and permissions.
    """

    def __init__(self, config_models, db_engine, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param DatabaseEngine db_engine: Database engine with DB connections
        :param Logger logger: Logger
        """
        super().__init__(
            'ogc',
            'https://github.com/qwc-services/qwc-ogc-service/raw/master/schemas/qwc-ogc-service.json',
            logger
        )

        self.config_models = config_models
        self.db_engine = db_engine
        self.permissions_query = PermissionsQuery(config_models, logger)

        # shared session and cached resources for collecting permissions
        self.session = None
        self.cached_resources = None

    def __del__(self):
        """Destructor"""
        if self.session:
            # close shared session
            self.session.close()

    def config(self, service_config):
        """Return service config.

        :param obj service_config: Additional service config
        """
        # get base config
        config = super().config(service_config)

        # additional service config
        cfg_config = service_config.get('config', {})
        if 'default_qgis_server_url' not in cfg_config:
            # use default QGIS server URL from ConfigGenerator config
            # if not set in service config
            qgis_server_url = service_config.get('defaults', {}).get(
                'qgis_server_url', 'http://localhost:8001/ows/'
            ).rstrip('/') + '/'
            cfg_config['default_qgis_server_url'] = qgis_server_url

        config['config'] = cfg_config

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from ConfigDB
        session = self.config_models.session()

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        resources['wms_services'] = self.wms_services(service_config, session)
        resources['wfs_services'] = self.wfs_services(service_config, session)

        session.close()

        return config

    def permissions(self, role):
        """Return service permissions for a role.

        :param str role: Role name
        """
        # NOTE: use ordered keys
        permissions = OrderedDict()

        # collect permissions from ConfigDB
        if self.session is None:
            # create shared session
            self.session = self.config_models.session()

            # NOTE: keep precached resources in memory while querying
            #       permissions
            self.cached_resources = self.precache_resources(self.session)

        permissions['wms_services'] = self.wms_permissions(role, self.session)
        permissions['wfs_services'] = self.wfs_permissions(role, self.session)

        return permissions

    # service config

    def wms_services(self, service_config, session):
        """Collect WMS Service resources from ConfigDB.

        :param obj service_config: Additional service config
        :param Session session: DB session
        """
        wms_services = []

        # additional service config
        cfg_config = service_config.get('config', {})
        cfg_resources = service_config.get('resources', {})
        cfg_wms_services = cfg_resources.get('wms_services', [])

        default_qgis_server_url = cfg_config.get(
            'default_qgis_server_url', 'http://localhost:8001/ows/'
        ).rstrip('/') + '/'

        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter(WmsWfs.ows_type == 'WMS')
        for wms in query.all():
            # find additional config for WMS service
            cfg_wms = {}
            for cfg in cfg_wms_services:
                if cfg.get('name') == wms.name:
                    cfg_wms = cfg
                    break

            # NOTE: use ordered keys
            wms_service = OrderedDict()
            wms_service['name'] = wms.name
            # set any online resources
            wms_service['online_resources'] = cfg_wms.get(
                'online_resources', {}
            )
            # collect WMS layers
            wms_service['root_layer'] = self.collect_wms_layers(
                wms.root_layer, False
            )
            # use separate QGIS project for printing
            wms_service['print_url'] = urljoin(
                default_qgis_server_url, "%s_print" % wms.name
            )
            wms_service['print_templates'] = self.print_templates(session)
            wms_service['internal_print_layers'] = self.print_layers(session)

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
            queryable = False

            data_source = layer.data_set_view.data_set.data_source
            if data_source.connection_type == 'database':
                # vector data layer
                # collect attribute names
                attributes = [
                    attr.name for attr in layer.data_set_view.attributes
                ]
                wms_layer['attributes'] = attributes

                # add geometry column
                attributes.append('geometry')

                # layer is queryable if there are any attributes
                queryable = len(attributes) > 0
            else:
                # raster data layers are always queryable
                queryable = True

            wms_layer['queryable'] = queryable

            if facade and layer.layer_transparency != 0:
                # add opacity to facade sublayers
                wms_layer['opacity'] = 100 - layer.layer_transparency

        return wms_layer

    def print_templates(self, session):
        """Return print templates from ConfigDB.

        :param Session session: DB session
        """
        TemplateQGIS = self.config_models.model('template_qgis')
        query = session.query(TemplateQGIS).order_by(TemplateQGIS.name)
        return [
            template.name for template in query.all()
        ]

    def print_layers(self, session):
        """Return internal print layers for background layers from ConfigDB.

        :param Session session: DB session
        """
        BackgroundLayer = self.config_models.model('background_layer')
        query = session.query(BackgroundLayer).order_by(BackgroundLayer.name)
        return [
            layer.name for layer in query.all()
        ]

    def wfs_services(self, service_config, session):
        """Collect WFS Service resources from ConfigDB.

        :param obj service_config: Additional service config
        :param Session session: DB session
        """
        wfs_services = []

        # additional service config
        cfg_config = service_config.get('config', {})
        cfg_resources = service_config.get('resources', {})
        cfg_wfs_services = cfg_resources.get('wfs_services', [])

        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter(WmsWfs.ows_type == 'WFS')
        for wfs in query.all():
            # find additional config for WFS service
            cfg_wfs = {}
            for cfg in cfg_wfs_services:
                if cfg.get('name') == wfs.name:
                    cfg_wfs = cfg
                    break

            # NOTE: use ordered keys
            wfs_service = OrderedDict()
            wfs_service['name'] = wfs.name
            # set any online resource
            wfs_service['online_resource'] = cfg_wfs.get('online_resource')
            # collect WFS layers
            wfs_service['layers'] = self.collect_wfs_layers(wfs.root_layer)

            wfs_services.append(wfs_service)

        return wfs_services

    def collect_wfs_layers(self, layer):
        """Recursively collect WFS layer info for layer subtree from ConfigDB
        and return flat WFS layer list.

        :param obj layer: Group or Data layer object
        """
        wfs_layers = []

        if layer.type == 'group':
            # group layer
            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sub layer
                wfs_layers += self.collect_wfs_layers(sublayer)

        else:
            # data layer
            data_source = layer.data_set_view.data_set.data_source
            if data_source.connection_type == 'database':
                # vector data layer
                # NOTE: use ordered keys
                wfs_layer = OrderedDict()
                wfs_layer['name'] = layer.name

                attributes = []

                # add primary key
                # NOTE: QGIS Server 3.10 returns incomplete FIDs if
                #       primary key property is excluded
                pkey = self.postgis_primary_key(layer.data_set_view)
                if pkey:
                    attributes.append(pkey)
                else:
                    self.logger.warning(
                        "Could not find primary key for layer '%s'" %
                        layer.name
                    )

                # collect attribute names
                attributes += [
                    attr.name for attr in layer.data_set_view.attributes
                ]

                # add geometry column
                attributes.append('geometry')

                wfs_layer['attributes'] = attributes

                wfs_layers.append(wfs_layer)

        return wfs_layers

    def postgis_primary_key(self, data_set_view):
        """Return primary key for a PostGIS DataSetView.

        :param obj data_set_view: DataSetView object
        """
        data_set = data_set_view.data_set

        if data_set.primary_key:
            # primary key if view
            return data_set.primary_key

        # database connection URL
        conn_str = data_set.data_source.connection

        # parse schema and table name
        data_set_name = data_set.data_set_name
        parts = data_set_name.split('.')
        if len(parts) > 1:
            schema = parts[0]
            table_name = parts[1]
        else:
            schema = 'public'
            table_name = data_set_name

        primary_key = None
        try:
            # connect to GeoDB
            conn = None
            engine = self.db_engine.db_engine(conn_str)
            conn = engine.connect()

            # build query SQL
            sql = sql_text("""
                SELECT a.attname
                FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid
                        AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = '{schema}.{table}'::regclass
                    AND i.indisprimary;
            """.format(schema=schema, table=table_name))

            # execute query
            result = conn.execute(sql)
            for row in result:
                primary_key = row['attname']
        except Exception as e:
            self.logger.error(
                "Could not get primary key of '%s':\n%s" % (data_set_name, e)
            )
        finally:
            if conn:
                # close database connection
                conn.close()

        return primary_key

    # permissions

    def wms_permissions(self, role, session):
        """Collect WMS Service permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        # get IDs for permitted resources required for WMS
        table_names = [
            'wms_wfs',
            'ows_layer',
            'data_set_view',
            'data_set',
            'data_source',
            'data_set_view_attributes',
            'background_layer',
            'template'
        ]
        # get IDs for role permissions
        permitted_ids = self.permissions_query.resource_ids(
            table_names, role, session
        )
        # get IDs for public permissions
        public_ids = self.permissions_query.resource_ids(
            table_names, self.permissions_query.public_role(), session
        )
        if role != self.permissions_query.public_role():
            # use only additional role permissions
            # as role permissions without public permissions
            permitted_ids = permitted_ids - public_ids

        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter(WmsWfs.ows_type == 'WMS')
        for wms in query.all():
            if wms.gdi_oid not in permitted_ids | public_ids:
                # WMS not permitted
                continue

            # NOTE: use ordered keys
            wms_permissions = OrderedDict()
            wms_permissions['name'] = wms.name

            # collect WMS layers
            layers = self.collect_wms_layer_permissions(
                wms.root_layer, permitted_ids, public_ids
            )
            # add internal print layers
            layers += self.permitted_print_layers(
                permitted_ids, session
            )
            wms_permissions['layers'] = layers

            # print templates
            print_templates = self.permitted_print_templates(
                permitted_ids, session
            )
            if print_templates:
                wms_permissions['print_templates'] = print_templates

            if layers or print_templates:
                permissions.append(wms_permissions)

        return permissions

    def collect_wms_layer_permissions(self, layer, permitted_ids, public_ids):
        """Recursively collect WMS layer permissions for a role for layer
        subtree from ConfigDB and return flat list of permitted WMS layers.

        :param obj layer: Group or Data layer object
        :param set<int> permitted_ids: Set of permitted resource IDs for role
        :param set<int> public_ids: Set of permitted resource IDs for
                                    public role
        """
        wms_layers = []

        # NOTE: use ordered keys
        wms_layer = OrderedDict()
        wms_layer['name'] = layer.name

        if layer.type == 'group':
            # group layer

            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sub layer
                sublayers += self.collect_wms_layer_permissions(
                    sublayer, permitted_ids, public_ids
                )

            if sublayers:
                # add group layer if any sub layers are permitted
                wms_layers.append(wms_layer)
                # add sub layers
                wms_layers += sublayers
        else:
            # data layer

            # check permissions for data layer and required resources
            data_set_view = layer.data_set_view
            data_set = data_set_view.data_set
            data_source = data_set.data_source
            if (
                layer.gdi_oid in permitted_ids and
                data_set_view.gdi_oid in permitted_ids and
                data_set.gdi_oid in permitted_ids and
                # NOTE: data_source permissions may be only public
                data_source.gdi_oid in permitted_ids | public_ids
            ):
                if data_source.connection_type == 'database':
                    # vector data layer
                    # collect attribute names
                    attributes = [
                        attr.name for attr in layer.data_set_view.attributes
                        if attr.gdi_oid in permitted_ids
                    ]
                    wms_layer['attributes'] = attributes

                    # add geometry column
                    attributes.append('geometry')

                # info template (NOTE: used in FeatureInfo service)
                if layer.templateinfo:
                    wms_layer['info_template'] = \
                        layer.templateinfo.gdi_oid in permitted_ids

                # add layer
                wms_layers.append(wms_layer)

            elif (
                layer.templateinfo and
                layer.gdi_oid in public_ids and
                data_set_view.gdi_oid in public_ids and
                data_set.gdi_oid in public_ids and
                data_source.gdi_oid in public_ids
            ):
                # public layer with potential restricted info template
                # or restricted feature report
                if (
                    layer.templateinfo and
                    layer.templateinfo.gdi_oid in permitted_ids
                ):
                    # public layer with restricted info template
                    wms_layer['info_template'] = True
                    # add layer
                    wms_layers.append(wms_layer)

        return wms_layers

    def permitted_print_layers(self, permitted_ids, session):
        """Return permitted internal print layers for background layers from
        ConfigDB.

        :param set<int> permitted_ids: Set of permitted resource IDs for role
        :param Session session: DB session
        """
        BackgroundLayer = self.config_models.model('background_layer')
        query = session.query(BackgroundLayer).order_by(BackgroundLayer.name)
        internal_print_layers = []
        for layer in query.all():
            if layer.gdi_oid in permitted_ids:
                # NOTE: use ordered keys
                wms_layer = OrderedDict()
                wms_layer['name'] = layer.name

                internal_print_layers.append(wms_layer)

        return internal_print_layers

    def permitted_print_templates(self, permitted_ids, session):
        """Return permitted print templates from ConfigDB.

        :param Session session: DB session
        """
        TemplateQGIS = self.config_models.model('template_qgis')
        query = session.query(TemplateQGIS).order_by(TemplateQGIS.name)
        return [
            template.name for template in query.all()
            if template.gdi_oid in permitted_ids
        ]

    def wfs_permissions(self, role, session):
        """Collect WFS Service permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        # get IDs for permitted resources required for WMS
        table_names = [
            'wms_wfs',
            'ows_layer',
            'data_set_view',
            'data_set',
            'data_source',
            'data_set_view_attributes'
        ]
        # get IDs for role permissions
        permitted_ids = self.permissions_query.resource_ids(
            table_names, role, session
        )
        # get IDs for public permissions
        public_ids = self.permissions_query.resource_ids(
            table_names, self.permissions_query.public_role(), session
        )
        if role != self.permissions_query.public_role():
            # use only additional role permissions
            # as role permissions without public permissions
            permitted_ids = permitted_ids - public_ids

        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter(WmsWfs.ows_type == 'WFS')
        for wfs in query.all():
            if wfs.gdi_oid not in permitted_ids | public_ids:
                # WFS not permitted
                continue

            # NOTE: use ordered keys
            wfs_permissions = OrderedDict()
            wfs_permissions['name'] = wfs.name

            # collect WMS layers
            layers = self.collect_wfs_layer_permissions(
                wfs.root_layer, permitted_ids, public_ids
            )
            if layers:
                wfs_permissions['layers'] = layers
                permissions.append(wfs_permissions)

        return permissions

    def collect_wfs_layer_permissions(self, layer, permitted_ids, public_ids):
        """Recursively collect WFS layer info for layer subtree from ConfigDB
        and return flat WFS layer list.

        :param obj layer: Group or Data layer object
        :param set<int> permitted_ids: Set of permitted resource IDs for role
        :param set<int> public_ids: Set of permitted resource IDs for
                                    public role
        """
        wfs_layers = []

        if layer.type == 'group':
            # group layer
            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sub layer
                wfs_layers += self.collect_wfs_layer_permissions(
                    sublayer, permitted_ids, public_ids
                )
        else:
            # data layer

            # check permissions for data layer and required resources
            data_set_view = layer.data_set_view
            data_set = data_set_view.data_set
            data_source = data_set.data_source
            if (
                layer.gdi_oid in permitted_ids and
                data_set_view.gdi_oid in permitted_ids and
                data_set.gdi_oid in permitted_ids and
                # NOTE: data_source permissions may be only public
                data_source.gdi_oid in permitted_ids | public_ids
            ):
                if data_source.connection_type == 'database':
                    # vector data layer
                    # NOTE: use ordered keys
                    wfs_layer = OrderedDict()
                    wfs_layer['name'] = layer.name

                    attributes = []

                    # add primary key
                    # NOTE: QGIS Server 3.10 returns incomplete FIDs if
                    #       primary key property is excluded
                    pkey = self.postgis_primary_key(layer.data_set_view)
                    if pkey:
                        attributes.append(pkey)
                    else:
                        self.logger.warning(
                            "Could not find primary key for layer '%s'" %
                            layer.name
                        )

                    # collect attribute names
                    attributes = [
                        attr.name for attr in layer.data_set_view.attributes
                        if attr.gdi_oid in permitted_ids
                    ]

                    # add geometry column
                    attributes.append('geometry')

                    wfs_layer['attributes'] = attributes

                    wfs_layers.append(wfs_layer)

        return wfs_layers

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
