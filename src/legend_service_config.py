from collections import OrderedDict
import os

from sqlalchemy.orm import joinedload

from permissions_query import PermissionsQuery
from service_config import ServiceConfig


class LegendServiceConfig(ServiceConfig):
    """LegendServiceConfig class

    Generate Legend service config and permissions.
    """

    def __init__(self, config_models, generator_config, logger):
        """Constructor

        :param ConfigModels config_models: Helper for ORM models
        :param obj generator_config: ConfigGenerator config
        :param Logger logger: Logger
        """
        super().__init__(
            'legend',
            'https://github.com/qwc-services/qwc-legend-service/raw/master/schemas/qwc-legend-service.json',
            logger
        )

        self.config_models = config_models
        self.permissions_query = PermissionsQuery(config_models, logger)

        # get path for writing custom legend images from ConfigGenerator config
        self.legend_images_output_path = generator_config.get(
            'legend_images_path', '/tmp'
        )

    def config(self, service_config):
        """Return service config.

        :param obj service_config: Additional service config
        """
        # get base config
        config = super().config(service_config)

        # additional service config
        if 'default_qgis_server_url' not in config['config']:
            # use default QGIS server URL from ConfigGenerator config
            # if not set in service config
            qgis_server_url = service_config.get('defaults', {}).get(
                'qgis_server_url', 'http://localhost:8001/ows/'
            ).rstrip('/') + '/'
            config['config']['default_qgis_server_url'] = qgis_server_url

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from ConfigDB
        session = self.config_models.session()

        # NOTE: keep precached resources in memory while querying resources
        cached_resources = self.precache_resources(session)

        resources['wms_services'] = self.wms_services(session)

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

        return permissions

    # service config

    def wms_services(self, session):
        """Collect WMS Service resources from ConfigDB.

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
            # group layer

            # set facade if layer is a facade group or is a facade sub layer
            in_facade = layer.facade or facade

            sublayers = []
            for group_layer in layer.sub_layers:
                sublayer = group_layer.sub_layer
                # recursively collect sub layer
                sublayers.append(self.collect_wms_layers(sublayer, in_facade))

            wms_layer['type'] = 'layergroup'
            wms_layer['layers'] = sublayers

            if layer.facade:
                wms_layer['hide_sublayers'] = True

                if layer.legend_image:
                    # custom legend image
                    wms_layer['legend_image'] = self.save_legend_image(layer)
        else:
            # data layer
            wms_layer['type'] = 'layer'
            if layer.legend_image:
                # custom legend image
                wms_layer['legend_image'] = self.save_legend_image(layer)

        return wms_layer

    def save_legend_image(self, layer):
        """Save custom legend image file for a layer and
        return final legend image filename.

        :param obj layer: Group or Data layer object
        """
        legend_filename = None
        if layer.legend_image:
            # NOTE: assume legend image filenames are unique
            legend_filename = layer.legend_filename
            if legend_filename is None:
                # fallback to <layername>.png
                legend_filename = "%s.png" % layer.name
            try:
                # save legend image as file
                legend_path = os.path.join(
                    self.legend_images_output_path, legend_filename
                )
                self.logger.info(
                    "Saving custom legend image '%s' for layer '%s'" %
                    (legend_path, layer.name)
                )
                with open(legend_path, 'wb') as f:
                    f.write(layer.legend_image)
            except UnicodeEncodeError as e:
                self.logger.warning(
                    "Could not write legend image for layer '%s':\n%s" %
                    (layer.name, e)
                )

                # show stack trace
                import traceback
                self.logger.error(traceback.format_exc())

                # show data around error position
                start = max(e.start - 30, 0)
                end = e.start + 30
                self.logger.warning(
                    "data at pos %s-%s:\n... %s ..." %
                    (start, end, e.object[start:end])
                )
            except Exception as e:
                self.logger.warning(
                    "Could not write legend image for layer '%s':\n%s" %
                    (layer.name, e)
                )

        return legend_filename

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
