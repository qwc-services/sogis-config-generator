from collections import OrderedDict
from sqlalchemy.orm import joinedload

from service_config import ServiceConfig
from permissions_query import PermissionsQuery


class DataServiceConfig(ServiceConfig):
    """DataServiceConfig class

    Generate Data service config.
    """

    def __init__(self, config_models, logger):
        """Constructor

        :param Logger logger: Logger
        """
        super().__init__(
            'data',
            'https://github.com/qwc-services/qwc-data-service/raw/master/schemas/qwc-data-service.json',
            logger
        )
        self.config_models = config_models
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
        config = super().config(service_config)

        session = self.config_models.session()

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        resources = OrderedDict()
        resources['datasets'] = self._datasets(session)

        config['resources'] = resources
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

        permissions['data_datasets'] = self._dataset_permissions(
            role, self.session
        )

        return permissions

    def _datasets(self, session):
        """Return data service resources.

        :param Session session: DB session
        """
        datasets = []

        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')
        DataSource = self.config_models.model('data_source')
        Attribute = self.config_models.model('data_set_view_attributes')

        # get dataset edit configs and relations
        query = session.query(DataSetEdit).order_by(DataSetEdit.name)
        # eager load nested relations (except Attribute)
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
            .joinedload(DataSetView.data_set)
            .joinedload(DataSet.data_source)
        )

        for dataset_edit in query.all():
            data_set_view = dataset_edit.data_set_view
            data_set = data_set_view.data_set
            data_source = data_set.data_source

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

            # primary key if view
            primary_key = data_set.primary_key

            # geometry column if multiple
            geometry_column = data_set_view.geometry_column

            pgmeta = self.permissions_query.postgis_metadata(
                conn_str, schema, table_name, geometry_column
            )
            # -> {'primary_key': 'id', 'geometry_column': 'geom',
            #     'geometry_type': 'POLYGON', 'srid': 2056}

            if not pgmeta:
                # could not get PostGIS metadata
                continue

            # get attributes
            attributes = []
            for attribute in data_set_view.attributes:
                # get data type and constraints
                attr_meta = self.permissions_query.attribute_metadata(
                    conn_str, schema, table_name, attribute.name
                )

                # NOTE: use ordered keys
                field = OrderedDict()
                field['name'] = attribute.name
                field['data_type'] = attr_meta['data_type']
                if attr_meta['constraints']:
                    # add any constraints
                    field['constraints'] = attr_meta['constraints']

                attributes.append(field)

            # NOTE: use ordered keys
            dataset = OrderedDict()
            dataset['name'] = dataset_edit.name
            dataset['db_url'] = conn_str
            dataset['schema'] = schema
            dataset['table_name'] = table_name
            dataset['primary_key'] = primary_key or pgmeta.get('primary_key')
            dataset['fields'] = attributes

            if pgmeta.get('geometry_column'):
                # NOTE: use ordered keys
                geometry = OrderedDict()
                geometry['geometry_column'] = pgmeta['geometry_column']
                geometry['geometry_type'] = pgmeta['geometry_type']
                geometry['srid'] = pgmeta['srid']

                dataset['geometry'] = geometry

            datasets.append(dataset)

        return datasets

    def _dataset_permissions(self, role, session):
        """Collect edit dataset permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        # get IDs for permitted resources required for editing
        table_names = [
            'data_set_edit',
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
        # combined role and public permissions
        role_and_public_ids = permitted_ids | public_ids
        if role != self.permissions_query.public_role():
            # use only additional role permissions
            # as role permissions without public permissions
            permitted_ids = permitted_ids - public_ids

        # collect write permissions with highest priority for all edit datasets
        writeable_datasets = set()
        edit_permissions = self.permissions_query.resource_permissions(
            'data_set_edit', None, role, session
        )
        for edit_permission in edit_permissions:
            if (
                edit_permission.write
                and edit_permission.resource.name not in writeable_datasets
            ):
                writeable_datasets.add(edit_permission.resource.name)

        DataSetEdit = self.config_models.model('data_set_edit')
        query = session.query(DataSetEdit).order_by(DataSetEdit.name)
        for dataset_edit in query.all():
            # check permissions for edit dataset and required resources
            data_set_view = dataset_edit.data_set_view
            data_set = data_set_view.data_set
            data_source = data_set.data_source
            if not (
                dataset_edit.gdi_oid in role_and_public_ids and
                data_set_view.gdi_oid in role_and_public_ids and
                data_set.gdi_oid in role_and_public_ids and
                data_source.gdi_oid in role_and_public_ids
            ):
                # edit dataset not permitted
                continue

            # NOTE: use ordered keys
            dataset_permissions = OrderedDict()
            dataset_permissions['name'] = dataset_edit.name

            # collect attribute names
            attributes = []
            attributes = [
                attr.name for attr in data_set_view.attributes
                if attr.gdi_oid in permitted_ids
            ]
            dataset_permissions['attributes'] = attributes

            # get CRUD permissions for edit permission
            writable = dataset_edit.name in writeable_datasets

            dataset_permissions['writable'] = writable
            dataset_permissions['creatable'] = writable
            dataset_permissions['readable'] = True
            dataset_permissions['updatable'] = writable
            dataset_permissions['deletable'] = writable

            if attributes or writable:
                # only add additional permissions
                permissions.append(dataset_permissions)

        return permissions

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
        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')

        # precache DataSetEdit and eager load relations
        data_set_edit_lookup = {}
        query = session.query(DataSetEdit)
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
            .joinedload(DataSetView.data_set)
            .joinedload(DataSet.data_source)
        )
        for data_set_edit in query.all():
            data_set_edit_lookup[data_set_edit.gdi_oid] = data_set_edit

        # precache DataSetView and eager load attributes
        query = session.query(DataSetView)
        query = query.options(
            joinedload(DataSetView.attributes)
        )
        data_set_view_lookup = {}
        for data_set_view in query.all():
            data_set_view_lookup[data_set_view.gdi_oid] = data_set_view

        # NOTE: return precached resources so they stay in memory
        return {
            'data_set_edit_lookup': data_set_edit_lookup,
            'data_set_view_lookup': data_set_view_lookup
        }
