from collections import OrderedDict

from sqlalchemy.sql import text as sql_text

from service_lib.database import DatabaseEngine


class PermissionsQuery:
    """PermissionsQuery base class

    Query permissions for GDI resources.
    """

    # name of public iam.role
    PUBLIC_ROLE_NAME = 'public'

    def __init__(self, config_models, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param Logger logger: Application logger
        """
        self.config_models = config_models
        self.logger = logger
        self.db_engine = DatabaseEngine()

    def public_role(self):
        """Return public role name."""
        return self.PUBLIC_ROLE_NAME

    def resource_ids(self, table_names, role, session):
        """Query permissions for multiple GDI resource types.

        Return set of permitted GDI resource IDs.

        :param list[str] table_names: List of table names for GDI resource types
        :param str role: Role name
        :param Session session: DB session
        """
        ResourcePermission = self.config_models.model('resource_permission')
        GDIResource = self.config_models.model('gdi_resource')

        # base query for all permissions of user
        query = self.role_permissions_query(role, session)

        # filter permissions by GDI resource types
        query = query.join(ResourcePermission.resource) \
            .filter(GDIResource.table_name.in_(table_names))

        # execute query and pluck resource IDs
        resource_ids = [p.gdi_oid_resource for p in query.all()]

        return set(resource_ids)

    def resource_permissions(self, table_name, resource_name, role, session):
        """Query permissions for a GDI resource type and optional name.

        Return resource permissions sorted by priority.

        :param str table_name: Table name for a GDI resource type
        :param str resource_name: optional GDI resource name (None for all)
        :param str role: Role name
        :param Session session: DB session
        """
        ResourcePermission = self.config_models.model('resource_permission')
        GDIResource = self.config_models.model('gdi_resource')

        # base query for all permissions of user
        query = self.role_permissions_query(role, session)

        # filter permissions by GDI resource types
        query = query.join(ResourcePermission.resource) \
            .filter(GDIResource.table_name == table_name)

        if resource_name is not None:
            # filter by resource name
            query = query.filter(GDIResource.name == resource_name)

        # order by priority
        query = query.order_by(ResourcePermission.priority.desc())

        # execute query and return results
        return query.all()

    def role_permissions_query(self, role, session):
        """Create base query for all permissions of a role.

        :param str role: Role name
        :param Session session: DB session
        """
        ResourcePermission = self.config_models.model('resource_permission')
        Role = self.config_models.model('role')

        # create query for permissions of role
        query = session.query(ResourcePermission). \
            join(ResourcePermission.role). \
            filter(Role.name == role)

        return query

    def all_resource_ids(self, table_names, session):
        """Query multiple GDI resource types.

        Return set of GDI resource IDs.

        :param list[str] table_names: List of table names for GDI resource types
        :param Session session: DB session
        """
        ResourcePermission = self.config_models.model('resource_permission')
        GDIResource = self.config_models.model('gdi_resource')

        # create query with any permissions
        query = session.query(ResourcePermission) \
            .join(ResourcePermission.resource) \
            .filter(GDIResource.table_name.in_(table_names))

        # execute query and pluck resource IDs
        resource_ids = [p.gdi_oid_resource for p in query.all()]

        return set(resource_ids)

    def postgis_metadata(self, conn_str, schema, table_name, geometry_column):
        """Return primary key, geometry column, type and srid from a PostGIS
        table.

        :param str conn_str: DB connection URL
        :param str schema: DB schema name
        :param str table_name: DB table name
        :param str geometry_column: optional geometry column name if not unique
        """
        info = {}

        conn = None
        try:
            # connect to GeoDB
            engine = self.db_engine.db_engine(conn_str)
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
            result = conn.execute(sql)
            for row in result:
                info['primary_key'] = row['attname']

            # get geometry column and srid

            # build query SQL
            where_clause = ""
            if geometry_column:
                # select specific geometry column
                where_clause = sql_text("""
                    AND f_geometry_column = '{geom_column}'
                """.format(geom_column=geometry_column))

            sql = sql_text("""
                SELECT f_geometry_column, srid, type
                FROM geometry_columns
                WHERE f_table_schema = '{schema}' AND f_table_name = '{table}'
                    {where_clause};
            """.format(
                schema=schema, table=table_name, where_clause=where_clause)
            )

            # execute query
            result = conn.execute(sql)
            for row in result:
                info['geometry_column'] = row['f_geometry_column']
                info['geometry_type'] = row['type']
                info['srid'] = row['srid']

            # close database connection
            conn.close()
        except Exception as e:
            self.logger.error(
                "Could not get PostGIS metadata for table '%s.%s':\n%s" %
                (schema, table_name, e)
            )
            if conn:
                conn.close()

        return info

    def attribute_metadata(self, conn_str, schema, table_name, column):
        """Return data type and any constraints for a table column.

        :param str conn_str: DB connection URL
        :param str schema: DB schema name
        :param str table_name: DB table name
        :param str column: Column name
        """
        data_type = 'text'
        # NOTE: use ordered keys
        constraints = OrderedDict()

        try:
            # connect to GeoDB
            geo_db = self.db_engine.db_engine(conn_str)
            conn = geo_db.connect()

            # build query SQL
            sql = sql_text("""
                SELECT data_type, character_maximum_length, numeric_precision,
                    numeric_scale
                FROM information_schema.columns
                WHERE table_schema = '{schema}' AND table_name = '{table}'
                    AND column_name = '{column}'
                ORDER BY ordinal_position;
            """.format(schema=schema, table=table_name, column=column))

            # execute query
            result = conn.execute(sql)
            for row in result:
                data_type = row['data_type']

                # constraints from data type
                if (data_type in ['character', 'character varying'] and
                        row['character_maximum_length']):
                    constraints['maxlength'] = row['character_maximum_length']
                elif data_type in ['double precision', 'real']:
                    # NOTE: use text field with pattern for floats
                    constraints['pattern'] = '[0-9]+([\\.,][0-9]+)?'
                elif data_type == 'numeric' and row['numeric_precision']:
                    step = pow(10, -row['numeric_scale'])
                    max_value = pow(
                        10, row['numeric_precision'] - row['numeric_scale']
                    ) - step
                    constraints['numeric_precision'] = row['numeric_precision']
                    constraints['numeric_scale'] = row['numeric_scale']
                    constraints['min'] = -max_value
                    constraints['max'] = max_value
                    constraints['step'] = step
                elif data_type == 'smallint':
                    constraints['min'] = -32768
                    constraints['max'] = 32767
                elif data_type == 'integer':
                    constraints['min'] = -2147483648
                    constraints['max'] = 2147483647
                elif data_type == 'bigint':
                    # NOTE: JSON/Javascript may reduce precision
                    constraints['min'] = -9223372036854775808
                    constraints['max'] = 9223372036854775807

            if not data_type:
                self.logger.warning(
                    "Could not find data type of column '%s' "
                    "of table '%s.%s'" % (column, schema, table_name)
                )
                data_type = 'text'

            # close database connection
            conn.close()
        except Exception as e:
            self.logger.error(
                "Could not get data type of column '%s' "
                "of table '%s.%s':\n%s" % (column, schema, table_name, e)
            )
            data_type = 'text'

        return {
            'data_type': data_type,
            'constraints': constraints
        }
