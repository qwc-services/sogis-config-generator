from sqlalchemy import MetaData, Table, Column, ForeignKey, \
    BigInteger, Boolean, Enum, Integer, LargeBinary, String, Text
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session, relationship, with_polymorphic


class ConfigModels():
    """ConfigModels class

    Provide SQLAlchemy ORM models for ConfigDB queries.
    """

    def __init__(self, config_db_engine):
        """Constructor

        :param Engine config_db_engine: Database engine for ConfigDB
        """
        self.engine = config_db_engine

        # init models
        self.base = None
        self.custom_models = {}
        self.init_models()

    def session(self):
        """Create a new session."""
        return Session(self.engine)

    def model(self, name):
        """Get SQLAlchemy model.

        :param str name: Table name of model
        """
        # get automap model or custom model
        return self.base.classes.get(name) or self.custom_models.get(name)

    def init_models(self):
        """Setup SQLAlchemy ORM models."""

        # Generate required models from ConfigDB using automap
        TABLES = [
            # gdi_knoten
            'data_source', 'data_set',
            'data_set_view', 'data_set_view_attributes',
            'map', 'map_layer',
            'background_layer',
            'wms_wfs',
            'data_set_search',
            'data_set_edit',
            'template_ows_layer', 'template_data_set',
            'service', 'service_module',
            'module', 'module_service',
            'transformation',
            # iam
            'user', 'group', 'role',
            'group_user', 'user_role', 'group_role',
            'resource_permission',
            # contacts
            'contact_role',
            # audit
            'logged_actions'
        ]
        # NOTE: some models and relations defined below

        def table_selector(table_name, meta_data):
            return (table_name in TABLES)

        metadata = MetaData()
        for schema in ['gdi_knoten', 'iam', 'contacts', 'audit']:
            metadata.reflect(self.engine, schema=schema, only=table_selector)
        Base = automap_base(metadata=metadata)

        # pre-declare model for 'gdi_resource' view
        class GDIResource(Base):
            __tablename__ = 'gdi_resource'
            __table_args__ = {'schema': 'gdi_knoten'}

            gdi_oid = Column(BigInteger, primary_key=True)
            table_name = Column(String)
            name = Column(String)
            description = Column(String)

            def __repr__(self):
                return "%s: %s" % (self.table_name, self.name)

        self.custom_models['gdi_resource'] = GDIResource

        # setup joined table inheritance for layers and group layers
        class OWSLayer(Base):
            """Abstract base class for OWSLayerData and OWSLayerGroup"""
            __tablename__ = 'ows_layer'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(BigInteger, primary_key=True)
            name = Column(String)
            description = Column(String)
            type = Column(Enum('data', 'group'))
            title = Column(String)
            legend_image = Column(LargeBinary)
            legend_filename = Column(String)
            ows_metadata = Column(String)

            # NOTE: explicitly define relation here and in subclass
            #       to avoid error 'property of that name exists on mapper'
            grouplayer_collection = relationship(
                'GroupLayer',
                back_populates='group'
            )

            parents = relationship(
                'GroupLayer',
                back_populates='sub_layer',
                # destroy GroupLayers on sub layer delete
                cascade="save-update, merge, delete"
            )

            __mapper_args__ = {
                'polymorphic_on': type
            }

        self.custom_models['ows_layer'] = OWSLayer

        class OWSLayerData(OWSLayer):
            __tablename__ = 'ows_layer_data'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(
                BigInteger,
                ForeignKey('gdi_knoten.ows_layer.gdi_oid'),
                primary_key=True
            )
            gdi_oid_data_set_view = Column(
                BigInteger, ForeignKey('gdi_knoten.data_set_view.gdi_oid')
            )
            qgs_style = Column(Text)
            uploaded_qml = Column(String)
            client_qgs_style = Column(Text)
            uploaded_client_qml = Column(String)

            data_set_view = relationship(
                'data_set_view'
            )

            __mapper_args__ = {
                'polymorphic_identity': 'data'
            }

        Base.classes['ows_layer_data'] = OWSLayerData

        class OWSLayerGroup(OWSLayer):
            __tablename__ = 'ows_layer_group'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(
                BigInteger,
                ForeignKey('gdi_knoten.ows_layer.gdi_oid'),
                primary_key=True
            )
            facade = Column(Boolean)

            # NOTE: explicitly define relation here and in superclass
            #       to avoid error 'property of that name exists on mapper'
            grouplayer_collection = relationship(
                'GroupLayer',
                order_by='GroupLayer.layer_order',
                back_populates='group'
            )

            # GroupLayer for children ordered by layer_order
            sub_layers = relationship(
                'GroupLayer',
                order_by='GroupLayer.layer_order',
                back_populates='group',
                # destroy GroupLayers on group layer delete
                cascade="save-update, merge, delete"
            )

            __mapper_args__ = {
                'polymorphic_identity': 'group'
            }

        Base.classes['ows_layer_group'] = OWSLayerGroup

        # many-to-many relation for nested layers
        class GroupLayer(Base):
            __tablename__ = 'group_layer'
            __table_args__ = {'schema': 'gdi_knoten'}

            id = Column(BigInteger, primary_key=True)
            gdi_oid_group_layer = Column(
                BigInteger,
                ForeignKey('gdi_knoten.ows_layer_group.gdi_oid')
            )
            gdi_oid_sub_layer = Column(
                BigInteger,
                ForeignKey('gdi_knoten.ows_layer.gdi_oid')
            )
            layer_active = Column(Boolean)
            layer_order = Column(Integer)

            group = relationship(
                'OWSLayerGroup',
                foreign_keys=[gdi_oid_group_layer]
            )
            sub_layer = relationship(
                'OWSLayer',
                foreign_keys=[gdi_oid_sub_layer]
            )

        Base.classes['group_layer'] = GroupLayer

        # setup joined table inheritance for templates
        class Template(Base):
            """Abstract base class for TemplateJasper, TemplateQGIS and
            TemplateInfo"""
            __tablename__ = 'template'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(BigInteger, primary_key=True)
            name = Column(String)
            description = Column(String)
            type = Column(Enum('jasper', 'qgis', 'info'))

            __mapper_args__ = {
                'polymorphic_on': type
            }

        self.custom_models['template'] = Template

        class TemplateJasper(Template):
            __tablename__ = 'template_jasper'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(
                BigInteger,
                ForeignKey('gdi_knoten.template.gdi_oid'),
                primary_key=True
            )
            report_filename = Column(String)
            uploaded_report = Column(String)

            # NOTE: explicitly define relation here
            #       to avoid error 'property of that name exists on mapper'
            owslayer_collection = relationship(
                'OWSLayer',
                order_by='OWSLayer.name',
                back_populates='template_jasper'
            )

            __mapper_args__ = {
                'polymorphic_identity': 'jasper'
            }

        Base.classes['template_jasper'] = TemplateJasper

        class TemplateQGIS(Template):
            __tablename__ = 'template_qgis'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(
                BigInteger,
                ForeignKey('gdi_knoten.template.gdi_oid'),
                primary_key=True
            )
            qgs_print_layout = Column(Text)
            uploaded_qpt = Column(String)
            map_width = Column(Integer)
            map_height = Column(Integer)
            print_labels = Column(String)

            __mapper_args__ = {
                'polymorphic_identity': 'qgis'
            }

        Base.classes['template_qgis'] = TemplateQGIS

        class TemplateInfo(Template):
            __tablename__ = 'template_info'
            __table_args__ = {'schema': 'gdi_knoten', 'extend_existing': True}

            gdi_oid = Column(
                BigInteger,
                ForeignKey('gdi_knoten.template.gdi_oid'),
                primary_key=True
            )
            info_template = Column(Text)
            template_filename = Column(Text)
            info_type = Column(Enum('sql', 'module', 'wms'))
            info_sql = Column(Text)
            info_module = Column(Text)

            __mapper_args__ = {
                'polymorphic_identity': 'info'
            }

        Base.classes['template_info'] = TemplateInfo

        # setup joined table inheritance for contacts
        class Contact(Base):
            """Abstract base class for Person and Organisation"""
            __tablename__ = 'contact'
            __table_args__ = {'schema': 'contacts', 'extend_existing': True}

            id = Column(BigInteger, primary_key=True)
            type = Column(Enum('person', 'organisation'))
            id_organisation = Column(BigInteger)
            name = Column(String)
            street = Column(String)
            house_no = Column(String)
            zip = Column(String)
            city = Column(String)
            country_code = Column(String)

            __mapper_args__ = {
                'polymorphic_on': type
            }

        self.custom_models['contact'] = Contact

        class Person(Contact):
            __tablename__ = 'person'
            __table_args__ = {'schema': 'contacts', 'extend_existing': True}

            id = Column(
                BigInteger,
                ForeignKey('contacts.contact.id'),
                primary_key=True
            )
            function = Column(String)
            email = Column(String)
            phone = Column(String)

            __mapper_args__ = {
                'polymorphic_identity': 'person'
            }

        Base.classes['person'] = Person

        class Organisation(Contact):
            __tablename__ = 'organisation'
            __table_args__ = {'schema': 'contacts', 'extend_existing': True}

            id = Column(
                BigInteger,
                ForeignKey('contacts.contact.id'),
                primary_key=True
            )
            unit = Column(String)
            abbreviation = Column(String)

            __mapper_args__ = {
                'polymorphic_identity': 'organisation'
            }

        Base.classes['organisation'] = Organisation

        # NOTE: declare ResourceContact here instead of using automap to avoid
        #       SQLAlchemy exception in Contact from multiple foreign keys
        class ResourceContact(Base):
            __tablename__ = 'resource_contact'
            __table_args__ = {'schema': 'contacts', 'extend_existing': True}

            id = Column(BigInteger, primary_key=True)
            id_contact_role = Column(
                BigInteger,
                ForeignKey('contacts.contact_role.id')
            )
            id_contact = Column(
                BigInteger,
                ForeignKey('contacts.contact.id')
            )
            gdi_oid_resource = Column(
                BigInteger,
                ForeignKey('gdi_knoten.gdi_resource.gdi_oid')
            )
            # create many-to-one relation from ResourceContact to GDIResource
            resource = relationship(
                GDIResource,
                foreign_keys=[gdi_oid_resource],
                primaryjoin=(
                    GDIResource.gdi_oid == gdi_oid_resource
                )
            )

        Base.classes['resource_contact'] = ResourceContact

        Base.prepare()

        self.base = Base

        # adjust models

        DataSetViewAttribute = Base.classes.data_set_view_attributes
        DataSetView = Base.classes.data_set_view
        # sorted attributes
        DataSetView.attributes = relationship(
            "data_set_view_attributes",
            order_by=DataSetViewAttribute.attribute_order
        )
        # OWS data layers
        DataSetView.ows_layers = relationship(
            OWSLayerData,
            # destroy layers on DataSetView delete
            cascade="save-update, merge, delete"
        )

        MapLayer = Base.classes.map_layer
        Map = Base.classes.map
        Map.map_layers = relationship(
            "map_layer",
            order_by=MapLayer.layer_order
        )

        WmsWfs = Base.classes.wms_wfs
        WmsWfs.root_layer = WmsWfs.owslayergroup

        # sorted Jasper template ows_layers
        TemplateJasper.ows_layers = relationship(
            OWSLayer,
            secondary='gdi_knoten.template_ows_layer',
            order_by=OWSLayer.name,
        )

        # sorted Jasper template data_sets
        DataSet = Base.classes.data_set
        TemplateJasper.data_sets = relationship(
            DataSet,
            secondary='gdi_knoten.template_data_set',
            order_by=DataSet.data_set_name,
            # avoid duplicate service_data_set DELETE on TemplateJasper delete
            viewonly=True
        )

        Module = Base.classes.module
        Service = Base.classes.service

        # create many-to-one relation from Service to GDIResource
        # NOTE: DataProducts are from data_set_view or ows_layer_group
        service_data_product = Table(
            'service_data_product', Base.metadata,
            Column('gdi_oid_service', BigInteger,
                   ForeignKey('gdi_knoten.service.gdi_oid')),
            Column('gdi_oid_data_product', BigInteger,
                   ForeignKey('gdi_knoten.gdi_resource.gdi_oid')),
            schema='gdi_knoten'
        )
        # sorted service data_products
        Service.sorted_data_products = relationship(
            GDIResource,
            secondary=service_data_product,
            order_by=GDIResource.name
        )

        # sorted service modules
        Service.modules = relationship(
            Module,
            secondary='gdi_knoten.service_module',
            order_by=Module.name
        )
        Service.module_collection = relationship(
            Module,
            secondary='gdi_knoten.service_module',
            order_by=Module.name,
            # avoid duplicate service_module DELETE on Service delete
            viewonly=True
        )

        # create many-to-one relation from Module to GDIResource
        # NOTE: DataProducts are from data_set_view or ows_layer_group
        module_data_product = Table(
            'module_data_product', Base.metadata,
            Column('gdi_oid_module', BigInteger,
                   ForeignKey('gdi_knoten.module.gdi_oid')),
            Column('gdi_oid_data_product', BigInteger,
                   ForeignKey('gdi_knoten.gdi_resource.gdi_oid')),
            schema='gdi_knoten'
        )
        # sorted module data_products
        Module.sorted_data_products = relationship(
            GDIResource,
            secondary=module_data_product,
            order_by=GDIResource.name
        )

        # sorted module services
        Module.services = relationship(
            Service,
            secondary='gdi_knoten.module_service',
            order_by=Service.name
        )
        Module.service_collection = relationship(
            Service,
            secondary='gdi_knoten.module_service',
            order_by=Service.name,
            # avoid duplicate module_service DELETE on Module delete
            viewonly=True
        )

        # many-to-many relation for Transformation data_set sources
        transformation_data_set_table = Table(
            'transformation_data_set', Base.metadata,
            Column('gdi_oid_transformation', BigInteger,
                   ForeignKey('gdi_knoten.transformation.gdi_oid')),
            Column('gdi_oid_data_set', BigInteger,
                   ForeignKey('gdi_knoten.data_set.gdi_oid')),
            schema='gdi_knoten'
        )

        # sorted Transformation data_set sources
        DataSet = Base.classes.data_set
        Transformation = Base.classes.transformation
        Transformation.source_data_sets = relationship(
            DataSet,
            secondary='gdi_knoten.transformation_data_set',
            order_by=DataSet.data_set_name
        )

        User = Base.classes.user
        Group = Base.classes.group
        Role = Base.classes.role

        # sorted user groups
        User.sorted_groups = relationship(
            Group,
            secondary='iam.group_user',
            order_by=Group.name,
            # avoid duplicate group DELETE on User delete
            viewonly=True
        )
        # sorted user roles
        User.sorted_roles = relationship(
            Role,
            secondary='iam.user_role',
            order_by=Role.name,
            # avoid duplicate role DELETE on User delete
            viewonly=True
        )

        # sorted group users
        Group.sorted_users = relationship(
            User,
            secondary='iam.group_user',
            order_by=User.name,
            # avoid duplicate user DELETE on Group delete
            viewonly=True
        )
        # sorted group roles
        Group.sorted_roles = relationship(
            Role,
            secondary='iam.group_role',
            order_by=Role.name,
            # avoid duplicate role DELETE on Group delete
            viewonly=True
        )

        # sorted role users
        Role.sorted_users = relationship(
            User,
            secondary='iam.user_role',
            order_by=User.name,
            # avoid duplicate user DELETE on Role delete
            viewonly=True
        )
        # sorted roles groups
        Role.sorted_groups = relationship(
            Group,
            secondary='iam.group_role',
            order_by=Group.name,
            # avoid duplicate group DELETE on Role delete
            viewonly=True
        )

        # create many-to-one relation from ResourcePermission to GDIResource
        ResourcePermission = Base.classes.resource_permission
        ResourcePermission.resource = relationship(
            GDIResource,
            foreign_keys=[ResourcePermission.gdi_oid_resource],
            primaryjoin=(
                GDIResource.gdi_oid == ResourcePermission.gdi_oid_resource
            )
        )

        # NOTE: workaround for SQLAlchemy exception from multiple foreign keys
        #       with automap:
        #           "Can't determine join between 'contact' and 'organisation';
        #           tables have more than one foreign key constraint
        #           relationship between them. Please specify the 'onclause' of
        #           this join explicitly."
        Contact.organisation = relationship(
            Organisation,
            foreign_keys=[Contact.id_organisation],
            primaryjoin=(
                Organisation.id == Contact.id_organisation
            )
        )

        # sorted organisation contacts
        Organisation.members = relationship(
            Contact,
            foreign_keys=[Contact.id_organisation],
            primaryjoin=(
                Organisation.id == Contact.id_organisation
            ),
            order_by='Contact.name'
        )
