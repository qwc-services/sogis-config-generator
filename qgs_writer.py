import html
import os
import re
from urllib.parse import urlparse, parse_qs
import uuid

from flask import json, jsonify
from jinja2 import Template
from xml.dom.minidom import parseString

from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import text as sql_text


class LoggerHelper:
    """LoggerHelper class

    Show and collect log entries.
    """

    LEVEL_DEBUG = 'debug'
    LEVEL_INFO = 'info'
    LEVEL_WARNING = 'warning'
    LEVEL_ERROR = 'error'

    def __init__(self, logger):
        """Constructor

        :param Logger logger: Logger
        """
        self.logger = logger
        self.logs = []

    def clear(self):
        """Clear log entries."""
        self.logs = []

    def log_entries(self):
        """Return log entries."""
        return self.logs

    def debug(self, msg):
        """Show debug log entry.

        :param str msg: Log message
        """
        self.logger.debug(msg)
        # do not collect debug entries

    def info(self, msg):
        """Add info log entry.

        :param str msg: Log message
        """
        self.logger.info(msg)
        self.add_log_entry(msg, self.LEVEL_INFO)

    def warning(self, msg):
        """Add warning log entry.

        :param str msg: Log message
        """
        self.logger.warning(msg)
        self.add_log_entry(msg, self.LEVEL_WARNING)

    def error(self, msg):
        """Add error log entry.

        :param str msg: Log message
        """
        self.logger.error(msg)
        self.add_log_entry(msg, self.LEVEL_ERROR)

    def add_log_entry(self, msg, level):
        """Append log entry with level.

        :param str msg: Log message
        :param str level: Log level
        """
        self.logs.append({
            'msg': msg,
            'level': level
        })


class QGSWriter:
    """QGSWriter class

    Generate QGIS projects from WMS and layers config.
    """

    def __init__(self, config, config_models, db_engine, logger):
        """Constructor

        :param obj config: QGSWriter config
        :param ConfigModels config_models: Helper for ORM models
        :param DatabaseEngine db_engine: Database engine with DB connections
        :param Logger logger: Logger
        """
        self.config_models = config_models
        self.db_engine = db_engine
        self.logger = LoggerHelper(logger)

        # get config settings
        self.project_output_dir = config.get('project_output_dir', '/tmp/')
        self.default_extent = config.get(
            'default_extent', [2590983, 1212806, 2646267, 1262755]
        )
        self.default_raster_extent = config.get('default_raster_extent', None)
        self.selection_color = config.get(
            'selection_color', [255, 255, 0, 255]
        )
        self.wms_service_url = config.get('wms_service_url', '')
        self.wfs_service_url = config.get('wfs_service_url', '')

        # load default styles
        self.default_styles = {
            "point": self.load_template('qgs/point.qml'),
            "linestring": self.load_template('qgs/linestring.qml'),
            "polygon": self.load_template('qgs/polygon.qml'),
            "raster": self.load_template('qgs/raster.qml')
        }

    def load_template(self, path):
        """Load contents of QGIS template file.

        :param str path: Path to template file
        """
        template = None
        try:
            # get absolute path to template
            path = os.path.abspath(
                os.path.join(os.path.dirname(__file__), path)
            )
            with open(path) as f:
                template = f.read()
        except Exception as e:
            print("Error loading template file '%s':\n%s" % (path, e))

        return template

    def parse_qml_style(self, xml, attributes=[]):
        doc = parseString(xml)
        qgis = doc.getElementsByTagName("qgis")[0]
        attr = " ".join(['%s="%s"' % entry for entry in filter(
            lambda entry: entry[0] != "version", qgis.attributes.items())])

        # update aliases
        if attributes:
            aliases = qgis.getElementsByTagName("aliases")
            if not aliases:
                # create <aliases>
                aliases = doc.createElement("aliases")
            else:
                # get <aliases>
                aliases = aliases[0]

            # remove existing aliases
            for alias in list(aliases.childNodes):
                aliases.removeChild(alias)

            # add aliases from layer config
            for i, attribute in enumerate(attributes):
                # get alias from from alias data
                attr_alias = attribute.alias
                try:
                    if attr_alias.startswith('{'):
                        # parse JSON
                        json_config = json.loads(attr_alias)
                        attr_alias = json_config.get('alias', attr_alias)
                except Exception as e:
                    self.logger.warning(
                        "Could not parse value as JSON: '%s'\n%s" %
                        (attr_alias, e)
                    )

                alias = doc.createElement("alias")
                alias.setAttribute('field', attribute.name)
                alias.setAttribute('index', str(i))
                alias.setAttribute('name', attr_alias)
                aliases.appendChild(alias)

        style = "".join([node.toxml() for node in qgis.childNodes])
        return {"attr": attr, "style": style}

    def collect_layers(self, layer, vectorlayerids, is_wms=True):
        """Recursively collect layer info for layer subtree from ConfigDB.

        param obj layer: Group or Data layer object
        param list[int] vectorlayerids: List of vector layer IDs
        param bool is_wms: True for WMS, False for WFS
        """

        if layer.type == 'group':
            return {
                "type": "group",
                "name": layer.name,
                "title": layer.title,
                "items": [
                    self.collect_layers(group_layer.sub_layer, vectorlayerids, is_wms)
                    for group_layer in layer.sub_layers
                ]
            }
        else:
            # FIXME Hardcoded
            CONNTYPE_DB = 'database'
            CONNTYPE_FILE = 'directory'

            data_source = layer.data_set_view.data_set.data_source
            conn_type = data_source.connection_type
            layerid = str(uuid.uuid4())

            if conn_type == CONNTYPE_DB:
                # provider and layer type
                # FIXME Hardcoded
                # FIXME DB connections are assumed to be vector layers
                provider = "postgres"
                layer_type = "vector"

                # check provider
                if not data_source.connection.startswith('postgresql:'):
                    self.logger.error(
                        "Unsupported DB provider for %s: %s" %
                        (layer.name, data_source.connection)
                    )
                    return {}

                # connect to data_source
                engine = self.db_engine.db_engine(data_source.connection)
                try:
                    geoconn = engine.connect()
                except OperationalError as e:
                    self.logger.error(
                        "DB connection failed for %s: %s\n%s" %
                        (layer.name, data_source.connection, e.orig)
                    )
                    return {}

                # Dataset and overridden geometry column
                dataset = layer.data_set_view.data_set.data_set_name
                dataset_parts = ["public", *dataset.split(".")][-2:]
                geometry_column = layer.data_set_view.geometry_column

                # geometry column, geometry type and srid from database geometry_columns table
                where_clause = ""
                if geometry_column:
                    where_clause = "AND f_geometry_column = :geomColumn"

                sql = sql_text("""
                    SELECT f_geometry_column, srid, type
                    FROM geometry_columns
                    WHERE f_table_schema = :schema AND f_table_name = :table {where_clause}
                    LIMIT 1;
                """.format(where_clause=where_clause))

                result = geoconn.execute(
                    sql, schema=dataset_parts[0], table=dataset_parts[1], geomColumn=geometry_column).first()
                if not result:
                    self.logger.error(
                        "Unable to determine geometry properties for %s" %
                        layer.name
                    )
                    geoconn.close()
                    return {}

                geometry_column = geometry_column or result.f_geometry_column
                srid = result.srid
                geometry_type = result.type

                # get primary key for views from data set
                primary_key = layer.data_set_view.data_set.primary_key
                if not primary_key:
                    # get primary key for regular tables from metadata
                    sql = sql_text("""
                        SELECT a.attname
                        FROM pg_index i
                            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
                        WHERE i.indrelid = '{schema}.{table}'::regclass
                            AND i.indisprimary;
                    """.format(
                        schema=dataset_parts[0], table=dataset_parts[1])
                    )

                    try:
                        primary_key = geoconn.execute(sql).first()['attname']
                    except TypeError:
                        self.logger.error(
                            "Unable to determine primary_key of "
                            "table %s for %s" % (dataset, layer.name)
                        )
                        geoconn.close()
                        return {}

                # parse database connection string
                parts = urlparse(data_source.connection)
                db_connection = ''
                if '?service=' in data_source.connection:
                    # PostgreSQL connection service
                    service = parse_qs(parts.query).get('service')[0]
                    if service == 'sogis_services':
                        # use sogis_webmapping instead of sogis_services
                        service = 'sogis_webmapping'
                    db_connection = "service='{service}'".format(
                        service=service
                    )
                else:
                    # full PostgreSQL connection
                    host = parts.hostname
                    port = parts.port
                    dbname = parts.path[1:]
                    user = parts.username
                    password = parts.password
                    db_connection = (
                        "dbname='{dbname}' host={host} port={port}".format(
                            dbname=dbname, host=host, port=port
                        )
                    )
                    if user:
                        db_connection += (
                            " user='{user}' password='{password}'".format(
                                user=user, password=password
                            )
                        )

                # QGS data source
                datasource = (
                    "{db_connection} sslmode=disable key='{pkey}' srid={srid} "
                    "type={geometry_type} table=\"{schema}\".\"{table}\" "
                    "({geometry_column}) sql=".format(
                        db_connection=db_connection, pkey=primary_key,
                        srid=srid, geometry_type=geometry_type,
                        schema=dataset_parts[0], table=dataset_parts[1],
                        geometry_column=geometry_column
                    )
                )

                # Extent
                extent = self.default_extent
                sql = sql_text("""
                    WITH extent AS (SELECT ST_Extent("{geometry_column}") AS extent FROM {dataset})
                    SELECT ST_XMIN(extent) AS xmin, ST_YMIN(extent) AS ymin, ST_XMAX(extent) AS xmax, ST_YMAX(extent) AS ymax
                    FROM extent""".format(geometry_column=geometry_column, dataset=dataset)
                               )
                try:
                    result = geoconn.execute(sql).first()
                except:
                    self.logger.error(
                        "Unable to determine extent of table %s for %s" %
                        (dataset, layer.name)
                    )
                    geoconn.close()
                    return {}

                if result:
                    extent = [
                        float(result.xmin or self.default_extent[0]),
                        float(result.ymin or self.default_extent[1]),
                        float(result.xmax or self.default_extent[2]),
                        float(result.ymax or self.default_extent[3])
                    ]

                # close connection
                geoconn.close()

                # Style
                try:
                    qml = self.parse_qml_style(
                        layer.qgs_style, layer.data_set_view.attributes
                    )
                except:
                    singletype = re.sub('^multi', "", geometry_type.lower())
                    if is_wms:
                        self.logger.warning(
                            "Falling back to default style for %s" % layer.name
                        )
                    qml = self.parse_qml_style(
                        self.default_styles[singletype],
                        layer.data_set_view.attributes
                    )

                # MapLayer attributes
                attributes = "geometry=\"%s\" " % (geometry_type) + qml["attr"]

                vectorlayerids.append(layerid)

            elif conn_type == CONNTYPE_FILE:
                # FIXME Hardcoded
                # FIXME File connections are assumed to be raster layers
                # FIXME Extent hardcoded to canton extent
                provider = "gdal"
                layer_type = "raster"
                data_source_dir = layer.data_set_view.data_set \
                    .data_source.connection
                dataset = layer.data_set_view.data_set.data_set_name

                extent = None
                if self.default_raster_extent is not None:
                    # set fixed raster layer extent
                    extent = self.default_raster_extent
                # else computed on-the-fly by QGIS Server

                # QGS data source
                datasource = os.path.join(data_source_dir, dataset)

                # Style
                try:
                    qml = self.parse_qml_style(layer.qgs_style)
                except:
                    if is_wms:
                        self.logger.debug(
                            "Falling back to default style for %s" % layer.name
                        )
                    qml = self.parse_qml_style(self.default_styles['raster'])

                # MapLayer attributes
                attributes = qml["attr"]
            else:
                return {}

            # Result
            return {
                "type": "layer",
                "name": html.escape(layer.name),
                "title": html.escape(layer.title),
                "id": layerid,
                "layertype": layer_type,
                "attributes": attributes,
                "provider": provider,
                "datasource": html.escape(datasource),
                "style": qml["style"],
                "mapTip": "",
                "extent": extent
            }

    def update_qgs(self):
        self.logger.clear()

        session = self.config_models.session()

        # Parse layertree from db
        wmswfs_model = self.config_models.model('wms_wfs')

        # WMS layertree
        query = session.query(wmswfs_model).filter_by(ows_type="WMS")
        wms = query.first()
        if wms is None:
            session.close()
            self.logger.error("WMS Service does not exist")
            return self.logger.log_entries()

        wms_root_layer = wms.root_layer
        wmsvectorlayerids = []
        wmslayertree = self.collect_layers(
            wms_root_layer, wmsvectorlayerids)

        query = session.query(wmswfs_model).filter_by(ows_type="WFS")

        # WFS layertree
        wfs = query.first()
        if wfs is None:
            session.close()
            self.logger.error("WFS Service does not exist")
            return self.logger.log_entries()

        wfs_root_layer = wfs.root_layer
        wfsvectorlayerids = []
        wfslayertree = self.collect_layers(
            wfs_root_layer, wfsvectorlayerids, False)

        # Collect composers from db
        template_qgis_model = self.config_models.model('template_qgis')
        composers = [entry.qgs_print_layout for entry in session.query(
            template_qgis_model).all()]

        # Collect print layers from db
        qml = self.parse_qml_style(self.default_styles['raster'])
        bg_layertree = {
            "type": "group",
            "name": "background_layers",
            "items": []
        }

        background_layer_model = self.config_models.model('background_layer')
        for entry in session.query(background_layer_model).all():
            # FIXME Extent hardcoded to canton extent
            bg_layertree["items"].append({
                "type": "layer",
                "name": html.escape(entry.name),
                "title": html.escape(entry.name),
                "id": str(uuid.uuid4()),
                "layertype": "raster",
                "attributes": qml["attr"],
                "provider": "wms",
                "datasource": html.escape(entry.qgis_datasource),
                "style": qml["style"],
                "mapTip": "",
                "extent": self.default_extent
            })

        # Render project
        qgs_template = Template(self.load_template('qgs/service.qgs'))
        dataset = wms.name

        ows_metadata = {}
        if wms.ows_metadata:
            try:
                # load JSON from ows_metadata
                ows_metadata = json.loads(wms.ows_metadata)
            except ValueError as e:
                msg = "Ungültiges JSON in wms_wfs.ows_metadata: %s" % e
                self.logger.error(msg)

        wms_service_title = ows_metadata.get('service_title', '')
        wms_service_abstract = ows_metadata.get('service_abstract', '')
        keywords = ows_metadata.get('keywords')
        if keywords:
            wms_keywords = [keyword.strip() for keyword in keywords.split(',')]
        else:
            wms_keywords = None
        wms_contact_person = ows_metadata.get('contact_person', '')
        wms_contact_organization = ows_metadata.get('contact_organization', '')
        wms_contact_position = ows_metadata.get('contact_position', '')
        wms_contact_phone = ows_metadata.get('contact_phone', '')
        wms_contact_mail = ows_metadata.get('contact_mail', '')
        wms_fees = ows_metadata.get('fees', '')
        wms_access_constraints = ows_metadata.get('access_constraints', '')
        wms_root_title = ows_metadata.get('wms_root_title', '')
        crs_list = ows_metadata.get('crs_list')
        if crs_list:
            wms_crs_list = [crs.strip() for crs in crs_list.split(',')]
        else:
            wms_crs_list = ['EPSG:2056']
        wms_extent = ows_metadata.get('wms_extent')
        if wms_extent:
            wms_extent = [float(coord) for coord in wms_extent.split(',')]

        # Write WMS project
        filename = "%s.qgs" % dataset
        qgs_path = os.path.join(self.project_output_dir, filename)
        with open(qgs_path, 'w', encoding='utf-8') as f:

            binding = {
                'wms_service_title': html.escape(wms_service_title),
                'wms_service_abstract': html.escape(wms_service_abstract),
                'wms_keywords': wms_keywords,
                'wms_url': html.escape(self.wms_service_url),
                'wms_contact_person': html.escape(wms_contact_person),
                'wms_contact_organization': html.escape(
                    wms_contact_organization
                ),
                'wms_contact_position': html.escape(wms_contact_position),
                'wms_contact_phone': html.escape(wms_contact_phone),
                'wms_contact_mail': html.escape(wms_contact_mail),
                'wms_fees': html.escape(wms_fees),
                'wms_access_constraints': html.escape(wms_access_constraints),
                'wms_root_name': html.escape(wmslayertree['name']),
                'wms_root_title': html.escape(wms_root_title),
                'wms_crs_list': wms_crs_list,
                'wms_extent': wms_extent,
                'layertree': wmslayertree['items'],
                'composers': [],
                'selection_color': self.selection_color
            }
            qgs = qgs_template.render(**binding)

            f.write(qgs)
            self.logger.debug("Wrote %s" % os.path.abspath(qgs_path))

        # Write print project
        filename = "%s_print.qgs" % dataset
        qgs_path = os.path.join(self.project_output_dir, filename)
        with open(qgs_path, 'w', encoding='utf-8') as f:

            binding = {
                'wms_abstract': html.escape(wms.description),
                'wms_url': html.escape(self.wms_service_url),
                'wms_root_name': html.escape(wmslayertree['name']),
                'layertree': wmslayertree['items'] + [bg_layertree],
                'composers': composers,
                'selection_color': self.selection_color
            }
            qgs = qgs_template.render(**binding)

            f.write(qgs)
            self.logger.debug("Wrote %s" % os.path.abspath(qgs_path))

        # Write WFS project
        filename = "%s_wfs.qgs" % dataset
        qgs_path = os.path.join(self.project_output_dir, filename)
        with open(qgs_path, 'w', encoding='utf-8') as f:

            binding = {
                'wms_service_title': html.escape(wms_service_title),
                'wms_service_abstract': html.escape(wms_service_abstract),
                'wms_keywords': wms_keywords,
                'wms_fees': html.escape(wms_fees),
                'wms_access_constraints': html.escape(wms_access_constraints),
                'layertree': wfslayertree['items'],
                'composers': [],
                'wfs_layers': wfsvectorlayerids,
                'wfs_url': html.escape(self.wfs_service_url),
                'selection_color': self.selection_color
            }
            qgs = qgs_template.render(**binding)

            f.write(qgs)
            self.logger.debug("Wrote %s" % os.path.abspath(qgs_path))

        session.close()

        return self.logger.log_entries()
