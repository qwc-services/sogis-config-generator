from collections import OrderedDict
import json
import os
import re

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.exc import ObjectDeletedError
from sqlalchemy.sql import text as sql_text

from service_lib.database import DatabaseEngine

from wmts_utils import get_wmts_layer_data


class ExternalLayers:

    def __init__(self, config_models, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param Logger logger: Logger
        """
        self.config_models = config_models
        self.logger = logger
        self.db_engine = DatabaseEngine()

    def layers(self):
        """Collect layer resources from ConfigDB.

        """
        #OWSLayer = self.config_models.model('ows_layer')
        OWSLayerData = self.config_models.model('ows_layer_data')
        DataSetView = self.config_models.model('data_set_view')
        DataSet = self.config_models.model('data_set')

        session = self.config_models.session()
        query = session.query(OWSLayerData)
        query = query.options(
            joinedload(OWSLayerData.data_set_view)
            .joinedload(DataSetView.data_set)
            .joinedload(DataSet.data_source)
        )
        for ows_layer in query.all():
            self._layer_datasource(ows_layer, session)

        session.close()

    def _layer_datasource(self, ows_layer, session):
        """Return datasource metadata for a layer.

        :param obj ows_layer: Group or Data layer object
        :param Session session: DB session
        """
        if ows_layer.type == 'group':
            return

        data_set = ows_layer.data_set_view.data_set
        self.logger.debug(data_set.data_set_name)
        data_source = data_set.data_source
        if data_source.connection_type == 'database':
            # vector DataSet
            pass
        elif data_source.connection_type == 'wms':
            # External WMS

            url = data_source.connection
            layername = data_set.data_set_name
            conn = "wms:%s#%s" % (url, layername)
            metadata = OrderedDict()
            metadata['datatype'] = 'raster'
            metadata['external_layer'] = {
                "name": conn,
                "type": "wms",
                "url": url,
                "params": {"LAYERS": layername},
                "infoFormats": ["text/plain"]
            }
        elif data_source.connection_type == 'wmts':
            # External WMTS

            url = data_source.connection
            layername = data_set.data_set_name
            conn = "wmts:%s#%s" % (url, layername)
            data = get_wmts_layer_data(self.logger, url, layername)
            metadata = OrderedDict()
            metadata['datatype'] = 'raster'
            metadata['external_layer'] = {
                "name": conn,
                "type": "wmts",
                "url": data["res_url"],
                "tileMatrixPrefix": "",
                "tileMatrixSet": data["tileMatrixSet"],
                "originX": data["origin"][0],
                "originY": data["origin"][1],
                "projection:": data["crs"],
                "resolutions": data["resolutions"],
                "tileSize": data["tile_size"],
                "abstract": data["abstract"]
            }
            self.logger.debug(metadata)
        else:
            # raster DataSet
            pass
