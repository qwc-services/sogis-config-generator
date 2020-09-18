from collections import OrderedDict
import json
import os
import re

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.exc import ObjectDeletedError
from sqlalchemy.sql import text as sql_text
from xml.etree import ElementTree

from permissions_query import PermissionsQuery
from service_config import ServiceConfig
from service_lib.database import DatabaseEngine


class DataproductServiceConfig(ServiceConfig):
    """DataproductServiceConfig class

    Generate Dataproduct service config and permissions.
    """

    def __init__(self, config_models, generator_config, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param Logger logger: Logger
        """
        super().__init__(
            'dataproduct',
            'https://github.com/qwc-services/sogis-dataproduct-service/raw/master/schemas/sogis-dataproduct-service.json',
            logger
        )

        self.config_models = config_models
        self.generator_config = generator_config
        self.permissions_query = PermissionsQuery(config_models, logger)
        self.db_engine = DatabaseEngine()

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
        self.service_config = service_config.get('config', {})

        session = self.config_models.session()

        config['config'] = {}

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from ConfigDB

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        resources['dataproducts'] = self._dataproducts(session)

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

        permissions['dataproducts'] = self._dataproduct_permissions(
            role, self.session)

        return permissions

    # permissions

    def _dataproduct_permissions(self, role, session):
        """Collect dataproduct permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = set()

        OWSLayer = self.config_models.model('ows_layer')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')
        DataSource = self.config_models.model('data_source')

        # collect permitted resource IDs
        table_names = [
            OWSLayer.__table__.name,
            DataSetView.__table__.name,
            DataSet.__table__.name,
            DataSource.__table__.name
        ]
        resource_ids = self.permissions_query.resource_ids(
            table_names, role, session)

        # collect permissions for nested Group or Data layer objects
        # NOTE: do not filter here by permitted resource IDs,
        #       as Group layers do not have explicit permissions
        query = session.query(OWSLayer).order_by(OWSLayer.name)
        for ows_layer in query.all():
            self._collect_layer_permissions(
                ows_layer, resource_ids, permissions
            )

        # collect permissions for basic DataSets
        query = session.query(DataSetView).order_by(DataSetView.name) \
            .filter(DataSetView.gdi_oid.in_(resource_ids))
        # filter by DataSetViews without OWSLayers
        query = query.options(joinedload(DataSetView.ows_layers)) \
            .filter(~DataSetView.ows_layers.any())
        for data_set_view in query.all():
            permissions.add(data_set_view.name)

        return sorted(list(permissions), key=str.lower)

    def _collect_layer_permissions(self, ows_layer, permitted_ids,
                                   permissions):
        """Recursively collect layers for layer subtree from ConfigDB.

        :param obj ows_layer: Group or Data layer object
        :param set<int> permitted_ids: Set of permitted resource IDs
        :param permissions: Partial set of permitted DataProduct names
        """
        if ows_layer.type == 'group':
            # collect sub layers
            sublayers = []
            for group_layer in ows_layer.sub_layers:
                sub_layer = group_layer.sub_layer
                # update sub layer permissions
                self._collect_layer_permissions(
                    sub_layer, permitted_ids, permissions
                )
                if sub_layer.name in permissions:
                    sublayers.append(sub_layer.name)

            if sublayers:
                # add group if any sub layers permitted
                permissions.add(ows_layer.name)
        else:
            # data layer
            # NOTE: only checking data layer permissions,
            #       not for required resources
            if ows_layer.gdi_oid in permitted_ids:
                permissions.add(ows_layer.name)

    # service config

    def _dataproducts(self, session):
        """Collect dataproduct resources from ConfigDB.

        :param Session session: DB session
        """
        dataproducts = []

        # get WFS root layer
        wfs_root_layer_id = None
        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter_by(ows_type='WFS')
        wms_wfs = query.first()
        if wms_wfs is not None:
            wfs_root_layer_id = wms_wfs.gdi_oid_root_layer

        # collect Group or Data layer objects
        OWSLayer = self.config_models.model('ows_layer')
        query = session.query(OWSLayer).order_by(OWSLayer.name)
        # ignore WFS root layer
        query = query.filter(OWSLayer.gdi_oid != wfs_root_layer_id)
        for ows_layer in query.all():
            metadata = self._dataproduct_metadata(ows_layer, session)
            if len(metadata) > 0:
                dataproducts.append(metadata)

        # collect DataSetViews for basic DataSets
        DataSetView = self.config_models.model('data_set_view')
        query = session.query(DataSetView).order_by(DataSetView.name)
        # filter by DataSetViews without OWSLayers
        query = query.options(joinedload(DataSetView.ows_layers)) \
            .filter(~DataSetView.ows_layers.any())
        for data_set_view in query.all():
            metadata = self._basic_dataset_metadata(data_set_view, session)
            dataproducts.append(metadata)

        return dataproducts

    def _dataproduct_metadata(self, ows_layer, session):
        """Recursively collect metadata of a dataproduct.

        :param obj ows_layer: Group or Data layer object
        :param Session session: DB session
        """
        metadata = OrderedDict()

        # type
        sublayers = None
        data_set_view = None
        searchterms = []
        if ows_layer.type == 'group':
            if ows_layer.facade:
                dataproduct_type = 'facadelayer'
            else:
                dataproduct_type = 'layergroup'

            # collect sub layers
            sublayers = []
            for group_layer in ows_layer.sub_layers:
                sub_layer = group_layer.sub_layer
                sublayers.append(sub_layer.name)

            if not sublayers:
                self.logger.warning(
                    "Skipping ProductSet %s with empty sublayers"
                    % ows_layer.name
                )
                return metadata
        else:
            dataproduct_type = 'datasetview'
            # find matching DataSetView
            DataSetView = self.config_models.model('data_set_view')
            query = session.query(DataSetView).filter_by(name=ows_layer.name)
            data_set_view = query.first()

        try:
            contacts = self._dataproduct_contacts(ows_layer, session)
            datasource = self._dataproduct_datasource(ows_layer, session)
            wms_datasource = self._dataproduct_wms(ows_layer, session)
            ows_metadata = self._ows_metadata(ows_layer)
            description = ows_metadata.get('abstract')
        except ObjectDeletedError as e:
            self.logger.error("%s: %s" % (ows_layer.name, e))
            return metadata

        # qml
        qml = None
        if ows_layer.type == 'data':
            qml = ows_layer.client_qgs_style or ows_layer.qgs_style
            # embed any uploaded symbols in QML
            qml = self._update_qml(ows_layer.name, qml)

        metadata['identifier'] = ows_layer.name
        metadata['display'] = ows_layer.title
        metadata['type'] = dataproduct_type
        metadata['synonyms'] = self._split_values(ows_layer.synonyms)
        metadata['keywords'] = self._split_values(ows_layer.keywords)
        metadata['description'] = description or ''
        metadata['contacts'] = contacts
        metadata['wms_datasource'] = wms_datasource
        metadata['qml'] = qml
        metadata['sublayers'] = sublayers

        if data_set_view:
            if data_set_view.facet:
                metadata['searchterms'] = [data_set_view.facet]
                searchterms.append(data_set_view.facet)
        elif len(searchterms) > 0:
            metadata['searchterms'] = searchterms

        if wms_datasource:  # ?
            metadata.update(
                self._layer_display_infos(ows_layer, session))

        metadata.update(datasource)

        # Filter null entries
        filtered_metadata = OrderedDict()
        for k, v in metadata.items():
            if v is not None:
                filtered_metadata[k] = v

        return filtered_metadata

    def _layer_display_infos(self, ows_layer, session):
        """Return theme item layer infos from ConfigDB.

        :param obj ows_layer: Group or Data layer object
        """
        visible = True

        # get visibility from parent group_layer
        parents = ows_layer.parents
        if len(parents) > 0:
            group_layer = parents[0]
            visible = group_layer.layer_active

        queryable = False
        display_field = None
        if ows_layer.type == 'data':
            data_set_view = ows_layer.data_set_view
            data_source = data_set_view.data_set.data_source
            if data_source.connection_type == 'database':
                if data_set_view.attributes:
                    # make layer queryable if there are any attributes
                    queryable = True
                    # get any display field
                    for attr in data_set_view.attributes:
                        if attr.displayfield:
                            display_field = attr.name
                            break
            else:
                # raster data layers are always queryable
                queryable = True

        opacity = round(
            (100.0 - ows_layer.layer_transparency)/100.0 * 255
        )

        metadata = OrderedDict()
        metadata['visibility'] = visible
        metadata['queryable'] = queryable
        metadata['displayField'] = display_field
        metadata['opacity'] = opacity

        return metadata

    def _basic_dataset_metadata(self, data_set_view, session):
        """Collect metadata of a basic DataSet dataproduct.

        :param obj data_set_view: DataSetView object
        :param Session session: DB session
        """
        metadata = OrderedDict()

        contacts = self._basic_dataset_contacts(data_set_view, session)

        metadata['identifier'] = data_set_view.name
        metadata['display'] = data_set_view.data_set.data_set_name
        metadata['type'] = 'datasetview'
        metadata['description'] = data_set_view.description
        metadata['contacts'] = contacts
        metadata['datatype'] = 'table'

        if data_set_view.facet:
            metadata['searchterms'] = [data_set_view.facet]

        return metadata

    def _dataproduct_contacts(self, ows_layer, session):
        """Return contacts metadata for a dataproduct.

        :param obj ows_layer: Group or Data layer object
        :param Session session: DB session
        """
        # collect contacts for layer and related GDI resources
        gdi_oids = [ows_layer.gdi_oid]
        if ows_layer.type == 'data':
            # include data source
            gdi_oids.append(
                ows_layer.data_set_view.data_set.gdi_oid_data_source
            )

        return self._contacts(gdi_oids, session)

    def _basic_dataset_contacts(self, data_set_view, session):
        """Return contacts metadata for a basic DataSet dataproduct.

        :param obj data_set_view: DataSetView object
        :param Session session: DB session
        """
        # collect contacts for basic DataSet and related GDI resources
        gdi_oids = [
            data_set_view.gdi_oid, data_set_view.data_set.gdi_oid_data_source
        ]
        return self._contacts(gdi_oids, session)

    def _contacts(self, gdi_oids, session):
        """Return contacts metadata for a list of resource IDs.

        :param list[int] gdi_oids: List of GDI resource IDs
        :param Session session: DB session
        """
        contacts = []

        ResourceContact = self.config_models.model('resource_contact')
        Contact = self.config_models.model('contact')
        query = session.query(ResourceContact) \
            .filter(ResourceContact.gdi_oid_resource.in_(gdi_oids)) \
            .order_by(ResourceContact.id_contact_role)
        # eager load relations
        query = query.options(
            joinedload(ResourceContact.contact)
            .joinedload(Contact.organisation)
        )
        for res_contact in query.all():
            person = res_contact.contact
            person_data = OrderedDict()
            person_data['id'] = person.id
            person_data['name'] = person.name
            person_data['function'] = person.function
            person_data['email'] = person.email
            person_data['phone'] = person.phone
            person_data['street'] = person.street
            person_data['house_no'] = person.house_no
            person_data['zip'] = person.zip
            person_data['city'] = person.city
            person_data['country_code'] = person.country_code

            organisation_data = None
            organisation = person.organisation
            if organisation is not None:
                organisation_data = OrderedDict()
                organisation_data['id'] = organisation.id
                organisation_data['name'] = organisation.name
                organisation_data['unit'] = organisation.unit
                organisation_data['abbreviation'] = organisation.abbreviation
                organisation_data['street'] = organisation.street
                organisation_data['house_no'] = organisation.house_no
                organisation_data['zip'] = organisation.zip
                organisation_data['city'] = organisation.city
                organisation_data['country_code'] = organisation.country_code

                # Filter null entries
                filtered_data = OrderedDict()
                for k, v in organisation_data.items():
                    if v is not None:
                        filtered_data[k] = v
                organisation_data = filtered_data

            contact = OrderedDict()
            contact['person'] = person_data
            contact['organisation'] = organisation_data

            contacts.append(contact)

        return contacts

    def _dataproduct_datasource(self, ows_layer, session):
        """Return datasource metadata for a dataproduct.

        :param obj ows_layer: Group or Data layer object
        :param Session session: DB session
        """
        metadata = OrderedDict()

        if ows_layer.type == 'group':
            # group layer

            metadata['bbox'] = self.service_config.get('default_extent')
            metadata['crs'] = 'EPSG:2056'

            return metadata

        data_set = ows_layer.data_set_view.data_set
        data_source = data_set.data_source
        if data_source.connection_type == 'database':
            # vector DataSet

            # get table metadata
            postgis_datasource = None
            pg_metadata = self._dataset_info(
                data_source.gdi_oid, data_set.data_set_name
            )
            if 'error' not in pg_metadata:
                data_set_name = "%s.%s" % (
                    pg_metadata.get('schema'), pg_metadata.get('table')
                )

                primary_key = pg_metadata.get('primary_key')
                if primary_key is None:
                    # get primary key if view
                    primary_key = data_set.primary_key

                geom = {}
                if len(pg_metadata.get('geometry_columns')) > 1:
                    used_col = ows_layer.data_set_view.geometry_column
                    for geom_col in pg_metadata.get('geometry_columns'):
                        # get used geometry column if multiple
                        if geom_col.get('geometry_column') == used_col:
                            geom = geom_col
                            break
                elif len(pg_metadata.get('geometry_columns')) == 1:
                    # use sole geometry column
                    geom = pg_metadata.get('geometry_columns')[0]

                postgis_datasource = OrderedDict()
                postgis_datasource['dbconnection'] = data_source.connection
                postgis_datasource['data_set_name'] = data_set_name
                postgis_datasource['primary_key'] = primary_key
                postgis_datasource['geometry_field'] = geom.get('geometry_column')
                postgis_datasource['geometry_type'] = geom.get('geometry_type')
                postgis_datasource['srid'] = geom.get('srid')
            else:
                # show error message
                postgis_datasource = {
                    'error': pg_metadata.get('error')
                }

            metadata['bbox'] = self.service_config.get('default_extent')
            metadata['crs'] = 'EPSG:2056'
            metadata['datatype'] = 'vector'
            metadata['postgis_datasource'] = postgis_datasource
        else:
            # raster DataSet

            raster_datasource_pattern = self.service_config.get(
                'raster_datasource_pattern', '')
            raster_datasource_repl = self.service_config.get(
                'raster_datasource_repl', '')

            # modify connection dir
            connection = re.sub(
                raster_datasource_pattern, raster_datasource_repl,
                data_source.connection
            )
            # TODO: get srid
            srid = 2056
            metadata = OrderedDict()
            metadata['datatype'] = 'raster'
            raster_datasource = OrderedDict()
            raster_datasource['datasource'] = connection + data_set.data_set_name
            raster_datasource['srid'] = srid
            metadata['raster_datasource'] = raster_datasource

        return metadata

    def _dataset_info(self, data_source_id, table_name):
        """Return table metadata for a data_set.

        :param int data_source_id: data_source ID
        :param str table_name: Table name as "<schema>.<table>"
        """
        # NOTE: form field returns 'None' as string if not set
        if not table_name or table_name == 'None':
            # empty table name
            return None

        # parse schema and table name
        parts = table_name.split('.')
        if len(parts) > 1:
            schema = parts[0]
            table_name = parts[1]
        else:
            schema = 'public'

        return self._postgis_metadata(data_source_id, schema, table_name)

    def _postgis_metadata(self, data_source_id, schema, table_name):
        """Return primary key, geometry columns, types and srids
        from a PostGIS table.

        :param int data_source_id: data_source ID
        :param str schema: DB schema name
        :param str table_name: DB table name
        """
        metadata = {}

        try:
            engine = self._engine_for_data_source(data_source_id)
            if engine is None:
                return {
                    'error': "FEHLER: DataSource nicht gefunden"
                }

            # connect to data_source
            conn = engine.connect()

            # get primary key

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
            primary_key = None
            result = conn.execute(sql)
            for row in result:
                primary_key = row['attname']

            # get geometry column and srid

            # build query SQL
            sql = sql_text("""
                SELECT f_geometry_column, srid, type
                FROM geometry_columns
                WHERE f_table_schema = '{schema}' AND f_table_name = '{table}';
            """.format(schema=schema, table=table_name))

            # execute query
            geometry_columns = []
            result = conn.execute(sql)
            for row in result:
                geometry_columns.append({
                    'geometry_column': row['f_geometry_column'],
                    'geometry_type': row['type'],
                    'srid': row['srid']
                })

            # close database connection
            conn.close()

            metadata = {
                'schema': schema,
                'table': table_name,
                'primary_key': primary_key,
                'geometry_columns': geometry_columns
            }
        except OperationalError as e:
            self.logger.error(e.orig)
            return {
                'error': "OperationalError: %s" % e.orig
            }
        except ProgrammingError as e:
            self.logger.error(e.orig)
            return {
                'error': "ProgrammingError: %s" % e.orig
            }

        return metadata

    def _engine_for_data_source(self, data_source_id):
        """Return SQLAlchemy engine for a data_source.

        :param int data_source_id: data_source ID
        """
        engine = None

        # find data_source
        DataSource = self.config_models.model('data_source')
        session = self.config_models.session()
        query = session.query(DataSource) \
            .filter_by(gdi_oid=data_source_id)
        data_source = query.first()
        session.close()

        if data_source is not None:
            engine = self.db_engine.db_engine(data_source.connection)

        return engine

    def _dataproduct_wms(self, ows_layer, session):
        """Return any WMS datasource for a dataproduct.

        :param obj ows_layer: Group or Data layer object
        :param Session session: DB session
        """
        wms_datasource = None

        # get WMS root layer
        root_layer = None
        WmsWfs = self.config_models.model('wms_wfs')
        query = session.query(WmsWfs).filter_by(ows_type='WMS')
        # eager load relation
        query = query.options(
            joinedload(WmsWfs.root_layer)
        )
        wms_wfs = query.first()
        if wms_wfs is not None:
            root_layer = wms_wfs.root_layer

        if self._layer_in_ows(ows_layer, root_layer):
            wms_datasource = OrderedDict()
            wms_datasource['service_url'] = self.service_config.get('wms_service_url')
            wms_datasource['name'] = ows_layer.name

        return wms_datasource

    def _layer_in_ows(self, ows_layer, root_layer):
        """Recursively check if layer is a WMS layer.

        :param obj ows_layer: Group or Data layer object
        :param obj root_layer: WMS root layer
        """
        if root_layer is None:
            # no WMS root layer
            return False

        in_wms = False
        # get parent groups
        parents = [p.group for p in ows_layer.parents]
        for parent in parents:
            if parent.gdi_oid == root_layer.gdi_oid:
                # parent is WMS root layer
                in_wms = True
            else:
                # check if parent group is a WMS layer
                in_wms = in_wms or self._layer_in_ows(parent, root_layer)
            if in_wms:
                break

        return in_wms

    def _ows_metadata(self, layer):
        """Return ows_metadata for a layer.

        :param obj layer: Group or Data layer object
        """
        ows_metadata = {}

        if layer.ows_metadata:
            try:
                # load JSON from ows_metadata
                ows_metadata = json.loads(layer.ows_metadata)
            except ValueError as e:
                self.logger.warning(
                    "Invalid JSON in ows_metadata of layer %s: %s" %
                    (layer.name, e)
                )

        return ows_metadata

    def _split_values(self, value):
        """Split comma separated values into list.

        :param str value: Comma separated values
        """
        if value:
            return [s.strip() for s in value.split(',')]
        else:
            return []

    def _update_qml(self, identifier, qml):
        """Update QML with embedded symbols.

        param str identifer: Dataproduct ID
        param str qml: QML XML string
        """
        if not qml:
            return qml

        try:
            # parse XML
            root = ElementTree.fromstring(qml)

            # embed symbols
            self._embed_qml_symbols(root, 'SvgMarker', 'name')
            self._embed_qml_symbols(root, 'SVGFill', 'svgFile')
            self._embed_qml_symbols(root, 'RasterFill', 'imageFile')

            # return updated QML
            qml = ElementTree.tostring(
                root, encoding='utf-8', method='xml'
            )
            return qml.decode()
        except Exception as e:
            self.logger.warning(
                "Could not embed QML symbols for dataproduct '%s':\n%s"
                % (identifier, e)
            )
            return qml

    def _embed_qml_symbols(self, root, layer_class, prop_key):
        """Embed symbol resources as base64 in QML.

        :param xml.etree.ElementTree.Element root: XML root node
        :param str layer_class: Symbol layer class
        :param str prop_key: Symbol layer prop key for symbol path
        """
        qgs_resources_dir = self.service_config.get('qgs_resources_dir', '')
        for svgprop in root.findall(".//layer[@class='%s']/prop[@k='%s']" %
                                    (layer_class, prop_key)):
            symbol_path = svgprop.get('v')
            path = os.path.abspath(
                os.path.join(qgs_resources_dir, symbol_path)
            )

            # NOTE: assume symbols not included in ZIP are default symbols
            if os.path.exists(path):
                try:
                    # read symbol data and convert to base64
                    with open(path, 'rb') as f:
                        symbol_data = base64.b64encode(f.read())

                    # embed symbol in QML
                    svgprop.set('v', "base64:%s" % symbol_data.decode())
                    self.logger.info("Embed symbol in QML: %s" % symbol_path)
                except Exception as e:
                    self.logger.warning(
                        "Could not embed QML symbol %s:\n%s" % (symbol_path, e)
                    )

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
