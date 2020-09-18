from collections import OrderedDict
from sqlalchemy.orm import joinedload

from service_config import ServiceConfig
from permissions_query import PermissionsQuery


class SearchServiceConfig(ServiceConfig):
    """SearchServiceConfig class

    Generate Search service config.
    """

    def __init__(self, config_models, logger):
        """Constructor

        :param Logger logger: Logger
        """
        super().__init__(
            'search',
            'https://github.com/qwc-services/qwc-fulltext-search-service/raw/master/schemas/qwc-search-service.json',
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
        resources['facets'] = self._facets(session)

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

        permissions['solr_facets'] = self._facet_permissions(
            role, session)

        # collect feature reports
        session.close()

        return permissions

    def _facets(self, session):
        """Return search service resources.

        :param str username: User name
        :param str group: Group name
        :param Session session: DB session
        """
        searches = [
            {
                'name': 'foreground',
                'filter_word': 'Karte',
                'default': True
            },
            {
                'name': 'background',
                'filter_word': 'Hintergrundkarte',
                'default': False
            }
        ]

        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')

        dataset_edit_ids = self.permissions_query.all_resource_ids(
            [DataSetEdit.__table__.name], session)

        query = session.query(DataSetEdit)
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
            # .joinedload(DataSetView.data_set)
        )
        # Da die 1:n Beziehung von DataSetView zu DataSet
        # zwar im DB-Schema vorgesehen, aber nur teilweise
        # umgesetzt ist (z.B. AGDI) wird vorläufig der
        # indizierte Product-Identifier in DataSetView.facet
        # gespeichert.
        facets = {}
        query = query.filter(DataSetEdit.gdi_oid.in_(dataset_edit_ids))
        for dataset_edit in query.all():
            data_set_view = dataset_edit.data_set_view
            if data_set_view.searchable != 0:
                feature_search = {
                    'name': data_set_view.facet,
                    # 'dataproduct_id': data_set_view.name,
                    'filter_word': data_set_view.filter_word,
                    'default': (data_set_view.searchable == 2)
                }
                if data_set_view.facet not in facets:
                    searches.append(feature_search)
                    facets[data_set_view.facet] = [feature_search]
                # Only add feature_search entry, if filter_word differs
                unique = True
                for entry in facets[data_set_view.facet]:
                    if entry['filter_word'] == data_set_view.filter_word:
                        unique = False
                if unique:
                    searches.append(feature_search)
                    facets[data_set_view.facet].append(feature_search)

        return searches

    def _facet_permissions(self, role, session):
        """Collect dataset_edit permissions from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        permissions = []

        if role == self.permissions_query.public_role():
            # add default public permissions
            permissions = ['foreground', 'background']

        DataSetEdit = self.config_models.model('data_set_edit')
        DataSetView = self.config_models.model('data_set_view')

        resource_ids = self.permissions_query.resource_ids(
            [DataSetEdit.__table__.name], role, session)

        query = session.query(DataSetEdit).\
            filter(DataSetEdit.gdi_oid.in_(resource_ids))
        query = query.options(
            joinedload(DataSetEdit.data_set_view)
            # .joinedload(DataSetView.data_set)
        )
        for dataset_edit in query.all():
            data_set_view = dataset_edit.data_set_view
            if data_set_view.searchable != 0:
                permissions.append(data_set_view.facet)

        return permissions
