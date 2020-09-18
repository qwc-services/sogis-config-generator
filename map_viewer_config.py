from collections import OrderedDict
import json
from urllib.parse import urljoin

from sqlalchemy.orm import joinedload
from sqlalchemy.sql import text as sql_text

from permissions_query import PermissionsQuery
from service_config import ServiceConfig


class MapViewerConfig(ServiceConfig):
    """MapViewerServiceConfig class

    Generate Map Viewer service config and permissions.
    """

    # value for data_set_view.searchable if always searchable
    ALWAYS_SEARCHABLE = 2

    # lookup for edit field types:
    #     PostgreSQL data_type -> QWC2 edit field type
    EDIT_FIELD_TYPES = {
        'bigint': 'number',
        'boolean': 'boolean',
        'character varying': 'text',
        'date': 'date',
        'double precision': 'number',
        'integer': 'number',
        'numeric': 'number',
        'real': 'number',
        'smallint': 'number',
        'text': 'text',
        'timestamp with time zone': 'date',
        'timestamp without time zone': 'date',
        'uuid': 'text'
    }

    # lookup for edit geometry types:
    #     PostGIS geometry type -> QWC2 edit geometry type
    EDIT_GEOM_TYPES = {
        'POINT': 'Point',
        'MULTIPOINT': 'Point',
        'LINESTRING': 'LineString',
        'MULTILINESTRING': 'LineString',
        'POLYGON': 'Polygon',
        'MULTIPOLYGON': 'Polygon'
    }

    def __init__(self, config_models, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param Logger logger: Logger
        """
        super().__init__(
            'mapViewer',
            'https://github.com/qwc-services/qwc-map-viewer/raw/master/schemas/qwc-map-viewer.json',
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

        config['service'] = 'map-viewer'

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from ConfigDB
        session = self.config_models.session()

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        # collect resources from QWC2 config and ConfigDB
        resources['qwc2_config'] = self.qwc2_config(service_config)
        resources['qwc2_themes'] = self.qwc2_themes(service_config, session)

        session.close()

        return config

    def permissions(self, role):
        """Return service permissions for a role.

        :param str role: Role name
        """
        # NOTE: use ordered keys
        permissions = OrderedDict()

        # collect permissions from ConfigDB
        session = self.config_models.session()

        # NOTE: WMS service permissions collected by OGC service config
        permissions['wms_services'] = []
        permissions['background_layers'] = self.permitted_background_layers(
            role, session
        )
        # NOTE: edit permissions collected by Data service config
        permissions['data_datasets'] = []

        session.close()

        return permissions

    # service config

    def qwc2_config(self, service_config):
        """Collect QWC2 application configuration from config.json.

        :param obj service_config: Additional service config
        """
        # NOTE: use ordered keys
        qwc2_config = OrderedDict()

        # additional service config
        cfg_resources = service_config.get('resources', {})
        cfg_qwc2_config = cfg_resources.get('qwc2_config', {})

        # read QWC2 config.json
        config = OrderedDict()
        try:
            config_file = cfg_qwc2_config.get(
                'qwc2_config_file', 'config.json'
            )
            with open(config_file) as f:
                # parse config JSON with original order of keys
                config = json.load(f, object_pairs_hook=OrderedDict)
        except Exception as e:
            self.logger.error("Could not load QWC2 config.json:\n%s" % e)
            config['ERROR'] = str(e)

        # remove service URLs
        service_urls = [
            'permalinkServiceUrl',
            'elevationServiceUrl',
            'editServiceUrl',
            'dataproductServiceUrl',
            'searchServiceUrl',
            'searchDataServiceUrl',
            'authServiceUrl',
            'mapInfoService',
            'featureReportService',
            'landRegisterService',
            'cccConfigService',
            'plotInfoService'
        ]
        for service_url in service_urls:
            config.pop(service_url, None)

        # apply custom settings
        if 'wmsDpi' in cfg_qwc2_config:
            config['wmsDpi'] = cfg_qwc2_config['wmsDpi']
        if 'minResultsExanded' in cfg_qwc2_config:
            config['minResultsExanded'] = cfg_qwc2_config['minResultsExanded']

        qwc2_config['config'] = config

        return qwc2_config

    def qwc2_themes(self, service_config, session):
        """Collect QWC2 themes configuration from ConfigDB.

        :param obj service_config: Additional service config
        :param Session session: DB session
        """
        # NOTE: use ordered keys
        qwc2_themes = OrderedDict()

        # additional service config
        cfg_resources = service_config.get('resources', {})
        cfg_qwc2_themes = cfg_resources.get('qwc2_themes', {})

        # collect resources from ConfigDB
        themes = OrderedDict()
        themes['title'] = 'root'
        themes['items'] = self.themes_items(service_config, session)
        themes['subdirs'] = []
        themes['defaultTheme'] = cfg_qwc2_themes.get('default_theme')
        themes['backgroundLayers'] = self.background_layers(session)
        themes['defaultScales'] = cfg_qwc2_themes.get('default_scales', [
            4000000, 2000000, 1000000, 400000, 200000, 80000, 40000, 20000,
            10000, 8000, 6000, 4000, 2000, 1000, 500, 250, 100
        ])
        themes['defaultWMSVersion'] = cfg_qwc2_themes.get(
            'default_wms_version', '1.3.0'
        )
        themes['defaultPrintResolutions'] = cfg_qwc2_themes.get(
            'default_print_resolutions', [300]
        )
        themes['defaultPrintGrid'] = cfg_qwc2_themes.get(
            'default_print_grid', [
                {'s': 10000, 'x': 1000, 'y': 1000},
                {'s': 1000, 'x': 100, 'y': 100},
                {'s': 100, 'x': 10, 'y': 10}
            ]
        )

        qwc2_themes['themes'] = themes

        return qwc2_themes

    def themes_items(self, service_config, session):
        """Collect theme items from ConfigDB.

        :param obj service_config: Additional service config
        :param Session session: DB session
        """
        items = []

        # additional service config
        cfg_config = service_config.get('config', {})
        cfg_resources = service_config.get('resources', {})
        cfg_qwc2_themes = cfg_resources.get('qwc2_themes', {})

        ogc_service_url = cfg_config.get('ogc_service_url', '/ows/')

        # print layouts and labels
        default_print_layout = cfg_qwc2_themes.get('default_print_layout')
        print_layouts, print_label_config = self.print_layouts(
            default_print_layout, session
        )

        # search providers
        search_providers = [
            'coordinates',
            self.solr_search_provider(session)
        ]

        # default layer bbox
        layer_bbox = OrderedDict()
        layer_bbox['crs'] = cfg_qwc2_themes.get('default_crs', 'EPSG:2056')
        layer_bbox['bounds'] = cfg_qwc2_themes.get(
            'default_layer_bounds', [2590000, 1210000, 2650000, 1270000]
        )

        # collect maps
        Map = self.config_models.model('map')
        query = session.query(Map).distinct(Map.name)
        for map_obj in query.all():
            wms_wfs = map_obj.wms_wfs

            # extend default item
            item = self.default_item(cfg_qwc2_themes)
            item['id'] = map_obj.name
            item['name'] = map_obj.name
            item['title'] = map_obj.title
            item['wms_name'] = wms_wfs.name
            item['url'] = urljoin(ogc_service_url, wms_wfs.name)

            # parse map extent
            initial_bounds = [
                float(c) for c in map_obj.initial_extent.split(',')
            ]
            item['initialBbox']['bounds'] = initial_bounds

            # collect layers
            layers, drawing_order = self.map_layers(map_obj, layer_bbox)
            item['sublayers'] = layers
            item['drawingOrder'] = drawing_order

            background_layers = self.item_background_layers(session)
            if map_obj.background_layer:
                # set default background layer
                bg_name = map_obj.background_layer.qwc2_bg_layer_name
                for background_layer in background_layers:
                    if background_layer['name'] == bg_name:
                        background_layer['visibility'] = True
                        break
            item['backgroundLayers'] = background_layers

            item['print'] = print_layouts
            item['printLabelConfig'] = print_label_config
            item['searchProviders'] = search_providers
            item['editConfig'] = self.edit_config(session)

            if map_obj.thumbnail_image:
                item['thumbnail'] = (
                    "img/custommapthumbs/%s" % map_obj.thumbnail_image
                )

            # NOTE: temp map order for sorting
            item['map_order'] = map_obj.map_order

            items.append(item)

        # order by map order or title
        items.sort(key=lambda i: (i['map_order'], i['title']))

        # remove map_order
        for item in items:
            del item['map_order']

        return items

    def default_item(self, cfg_qwc2_themes):
        """Return theme item with default values.

        :param obj cfg_qwc2_themes: Additional QWC2 themes config
        """
        # NOTE: use ordered keys
        item = OrderedDict()
        item['id'] = None
        item['name'] = None
        item['title'] = None
        item['wms_name'] = None
        item['url'] = None

        attribution = OrderedDict()
        attribution['Title'] = cfg_qwc2_themes.get(
            'default_theme_attribution_title', ''
        )
        attribution['OnlineResource'] = cfg_qwc2_themes.get(
            'default_theme_attribution_online_resource', ''
        )
        item['attribution'] = attribution

        item['keywords'] = ''
        item['mapCrs'] = 'EPSG:2056'

        bbox = OrderedDict()
        bbox['crs'] = cfg_qwc2_themes.get('default_crs', 'EPSG:2056')
        bbox['bounds'] = cfg_qwc2_themes.get(
            'default_theme_item_bounds', [2590000, 1210000, 2650000, 1270000]
        )
        item['bbox'] = bbox

        initial_bbox = OrderedDict()
        initial_bbox['crs'] = cfg_qwc2_themes.get('default_crs', 'EPSG:2056')
        initial_bbox['bounds'] = cfg_qwc2_themes.get(
            'default_theme_item_bounds', [2590000, 1210000, 2650000, 1270000]
        )
        item['initialBbox'] = initial_bbox

        item['sublayers'] = []
        item['expanded'] = True
        item['drawingOrder'] = []
        item['backgroundLayers'] = []
        item['print'] = []
        item['printLabelConfig'] = {}
        item['searchProviders'] = []
        item['editConfig'] = None

        item['additionalMouseCrs'] = [
            'EPSG:21781', 'EPSG:2056'
        ]
        item['tiled'] = False
        item['availableFormats'] = cfg_qwc2_themes.get(
            'default_image_formats', ['image/jpeg', 'image/png']
        )
        item['skipEmptyFeatureAttributes'] = True
        item['infoFormats'] = [
            'text/plain',
            'text/html',
            'text/xml',
            'application/vnd.ogc.gml',
            'application/vnd.ogc.gml/3.1.1'
        ]
        item['thumbnail'] = 'img/mapthumbs/default.jpg'

        return item

    def print_layouts(self, default_print_layout, session):
        """Return QWC2 print layouts and labels from ConfigDB.

        :param str default_print_layout: Name of default print layout
        :param Session session: DB session
        """
        print_layouts = []
        print_label_config = {}

        TemplateQGIS = self.config_models.model('template_qgis')
        query = session.query(TemplateQGIS).order_by(TemplateQGIS.name)
        for template in query.all():
            # NOTE: use ordered keys
            print_layout = OrderedDict()
            print_layout['name'] = template.name

            map_cfg = OrderedDict()
            map_cfg['name'] = 'map0'
            map_cfg['width'] = template.map_width
            map_cfg['height'] = template.map_height
            print_layout['map'] = map_cfg

            if template.print_labels:
                # add print labels
                labels = []
                print_labels = template.print_labels.split(',')
                for label in print_labels:
                    # add label
                    labels.append(label)
                    # printLabelConfig
                    label_cfg = OrderedDict()
                    label_cfg['rows'] = 1
                    label_cfg['maxLength'] = 128
                    print_label_config[label] = label_cfg

                print_layout['labels'] = labels

            print_layout['default'] = (template.name == default_print_layout)

            print_layouts.append(print_layout)

        return print_layouts, print_label_config

    def solr_search_provider(self, session):
        """Return Solr search provider config with default search identifiers
        from ConfigDB.

        :param Session session: DB session
        """
        searches = ['foreground']
        facets = []

        # collect always searchable datasets
        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')
        query = session.query(DataSetEdit)
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
        )
        for dataset_edit in query.all():
            data_set_view = dataset_edit.data_set_view
            if data_set_view.searchable == self.ALWAYS_SEARCHABLE:
                facets.append(data_set_view.facet)

        # sort and add facets
        facets.sort()
        searches += facets

        # NOTE: use ordered keys
        cfg = OrderedDict()
        cfg['provider'] = 'solr'
        cfg['default'] = searches

        return cfg

    def map_layers(self, map_obj, layer_bbox):
        """Return theme item layers and drawing order for a map from ConfigDB.

        :param obj map_obj: Map object
        :param obj layer_bbox: Default layer extent
        """
        layers = []
        drawing_order = []

        for map_layer in map_obj.map_layers:
            ows_layer = map_layer.owslayer
            opacity = round(
                (100.0 - map_layer.layer_transparency)/100.0 * 255
            )
            res = self.collect_layers(
                ows_layer, opacity, map_layer.layer_active, layer_bbox
            )
            layers += res['layers']
            drawing_order += res['drawing_order']

        drawing_order.reverse()

        return layers, drawing_order

    def collect_layers(self, layer, opacity, visibility, layer_bbox):
        """Recursively collect layers for layer subtree from ConfigDB
        and return nested theme item sublayers and drawing order.

        :param obj layer: Group or Data layer object
        :param int opacity: Layer Opacity between [0..100]
        :param bool visibility: Whether layer is active
        :param obj layer_bbox: Default layer extent
        """
        layers = []
        drawing_order = []

        # NOTE: use ordered keys
        item_layer = OrderedDict()
        item_layer['name'] = layer.name
        if layer.title:
            item_layer['title'] = layer.title

        if layer.type == 'group' and not layer.facade:
            # group layer
            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sublayer
                res = self.collect_layers(
                    sublayer, opacity, visibility, layer_bbox
                )
                sublayers += res['layers']
                drawing_order += res['drawing_order']

            item_layer['sublayers'] = sublayers
            item_layer['expanded'] = True
        else:
            # data layer or facade group layer
            queryable = False
            display_field = None

            if layer.ows_metadata:
                # get abstract from layer metadata
                try:
                    # load JSON from ows_metadata
                    ows_metadata = json.loads(layer.ows_metadata)
                    if 'abstract' in ows_metadata:
                        item_layer['abstract'] = ows_metadata['abstract']
                except Exception as e:
                    self.logger.warning(
                        "Invalid JSON in ows_metadata of layer '%s':\n%s" %
                        (layer.name, e)
                    )

            item_layer['visibility'] = visibility

            if layer.type == 'data':
                # data layer
                data_set_view = layer.data_set_view
                data_source = data_set_view.data_set.data_source
                if data_source.connection_type == 'database':
                    # get any display field
                    for attr in data_set_view.attributes:
                        if attr.displayfield:
                            display_field = attr.alias or attr.name
                            break

            item_layer['queryable'] = self.layer_queryable(layer)
            if display_field:
                item_layer['displayField'] = display_field
            item_layer['opacity'] = opacity
            item_layer['bbox'] = layer_bbox

            drawing_order.append(layer.name)

        layers.append(item_layer)

        return {
            'layers': layers,
            'drawing_order': drawing_order
        }

    def layer_queryable(self, layer):
        """Recursively check whether a layer is queryable.

        :param obj layer: Group or Data layer object
        """
        queryable = False

        if layer.type == 'group':
            # group layer
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sublayer
                # group layer is queryable if any sublayer is queryable
                queryable |= self.layer_queryable(sublayer)
        else:
            # data layer
            data_set_view = layer.data_set_view
            data_source = data_set_view.data_set.data_source
            if data_source.connection_type == 'database':
                if data_set_view.attributes:
                    # make layer queryable if there are any attributes
                    queryable = True
            else:
                # raster data layers are always queryable
                queryable = True

        return queryable

    def item_background_layers(self, session):
        """Return background layers for item from ConfigDB.

        :param Session session: DB session
        """
        background_layers = []

        BackgroundLayer = self.config_models.model('background_layer')
        query = session.query(BackgroundLayer).order_by(BackgroundLayer.name)
        for layer in query.all():
            # NOTE: use ordered keys
            background_layer = OrderedDict()
            background_layer['name'] = layer.qwc2_bg_layer_name
            background_layer['printLayer'] = layer.name
            background_layers.append(background_layer)

        return background_layers

    def background_layers(self, session):
        """Return available background layers from ConfigDB.

        :param Session session: DB session
        """
        background_layers = []

        BackgroundLayer = self.config_models.model('background_layer')
        query = session.query(BackgroundLayer).order_by(BackgroundLayer.name)
        for layer in query.all():
            try:
                background_layer = json.loads(
                    layer.qwc2_bg_layer_config, object_pairs_hook=OrderedDict
                )
                if layer.thumbnail_image:
                    # set custom thumbnail
                    background_layer['thumbnail'] = (
                        "img/custommapthumbs/%s" %
                        layer.thumbnail_image
                    )
                background_layers.append(background_layer)
            except Exception as e:
                self.logger.warning(
                    "Could not load background layer '%s':\n%s" %
                    (layer.name, e)
                )

        return background_layers

    def edit_config(self, session):
        """Return edit config for all available edit layers.

        :param Session session: DB session
        """
        # NOTE: use ordered keys
        edit_config = OrderedDict()

        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')
        ResourcePermission = self.config_models.model('resource_permission')
        GDIResource = self.config_models.model('gdi_resource')

        # get IDs for edit datasets with any write permissions
        table_name = DataSetEdit.__table__.name
        query = session.query(ResourcePermission)\
            .join(ResourcePermission.resource) \
            .filter(GDIResource.table_name == table_name) \
            .filter(ResourcePermission.write) \
            .distinct(GDIResource.gdi_oid)
        # pluck resource IDs
        writable_ids = [p.gdi_oid_resource for p in query.all()]

        # get writable dataset edit configs and relations
        query = session.query(DataSetEdit) \
            .filter(DataSetEdit.gdi_oid.in_(writable_ids)) \
            .order_by(DataSetEdit.name)
        # eager load nested relations (except Attribute)
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
            .joinedload(DataSetView.data_set)
            .joinedload(DataSet.data_source)
        )

        for dataset_edit in query.all():
            data_set_view = dataset_edit.data_set_view
            data_set = data_set_view.data_set

            # layer title
            try:
                title = data_set_view.ows_layers[0].title
            except Exception as e:
                title = dataset_edit.name

            # parse schema and table name
            data_set_name = data_set.data_set_name
            parts = data_set_name.split('.')
            if len(parts) > 1:
                schema = parts[0]
                table_name = parts[1]
            else:
                schema = 'public'
                table_name = data_set_name

            fields = []
            geometry_type = None

            # database connection URL
            conn_str = data_set.data_source.connection

            # get attributes

            for attribute in data_set_view.attributes:
                # get data type and constraints
                attr_meta = self.permissions_query.attribute_metadata(
                    conn_str, schema, table_name, attribute.name
                )

                field = OrderedDict()
                field['id'] = attribute.name
                field['name'] = attribute.alias or attribute.name
                field['type'] = self.EDIT_FIELD_TYPES.get(
                    attr_meta['data_type'], 'text'
                )
                if attr_meta['constraints']:
                    # add any constraints
                    field['constraints'] = attr_meta['constraints']

                fields.append(field)

            # get geometry type

            # primary key if view
            primary_key = data_set.primary_key
            # geometry column if multiple
            geometry_column = data_set_view.geometry_column
            # PostGIS metadata
            pgmeta = self.permissions_query.postgis_metadata(
                conn_str, schema, table_name, geometry_column
            )
            if pgmeta.get('geometry_column'):
                geometry_type = pgmeta.get('geometry_type')
                if geometry_type not in self.EDIT_GEOM_TYPES:
                    # unsupported geometry type
                    self.logger.warning(
                        "Unsupported geometry type %s for editing %s.%s" %
                        (geometry_type, schema, table_name)
                    )
                    geometry_type = None
                else:
                    geometry_type = self.EDIT_GEOM_TYPES.get(geometry_type)

            if fields and geometry_type is not None:
                # add only datasets with attributes and geometry

                # NOTE: use ordered keys
                dataset = OrderedDict()
                dataset['editDataset'] = dataset_edit.name
                dataset['layerName'] = title
                dataset['fields'] = fields
                dataset['geomType'] = geometry_type

                edit_config[dataset_edit.name] = dataset

        if edit_config:
            return edit_config
        else:
            return None

    # permissions

    def permitted_background_layers(self, role, session):
        """Return permitted internal print layers for background layers from
        ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        background_layers = []

        # get IDs for permitted resources required for FeatureInfo
        table_names = [
            'background_layer'
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

        BackgroundLayer = self.config_models.model('background_layer')
        query = session.query(BackgroundLayer).order_by(BackgroundLayer.name)
        background_layers = [
            layer.qwc2_bg_layer_name for layer in query.all()
            if layer.gdi_oid in permitted_ids
        ]

        return background_layers

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
