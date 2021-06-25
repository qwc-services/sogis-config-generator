from collections import OrderedDict

from jinja2 import Template
from sqlalchemy.sql import text as sql_text

from service_lib.database import DatabaseEngine


class MetadataReader():
    """MetadataReader class

    Load metadata for layers from MetaDB.
    """

    def __init__(self, config, logger):
        """Constructor

        :param obj config: MetadataReader config
        :param Logger logger: Logger
        """
        self.logger = logger
        self.db_url = config.get('db_url')
        self.layers_sql = config.get('layers_sql', '')
        self.layer_dataproducts_sql = config.get('layer_dataproducts_sql', '')

        # prepare templates
        self.templates = []
        for template in config.get('metadata_templates', []):
            self.templates.append({
                'label': template.get('label'),
                'template': Template(template.get('template'))
            })

        """
        lookup for metadata by layer

        metadata = {
            <layer name>: {
                layer_name: <layer name>
                layer_ref: <layer reference for products query
                <custom fields from layers query>: ...
                ...
                _products: [
                    {
                        <custom fields from products query>: ...
                        ...
                    }
                ]
            }
        }
        """
        self.metadata = {}

    def load_metadata(self):
        """Load metadata for all layers from MetaDB."""
        if not self.db_url:
            # skip if no MetaDB in config
            self.logger.debug(
                "No MetaDB connection configured, skipping loading of metadata"
            )
            return

        try:
            # connect to MetaDB
            db_engine = DatabaseEngine().db_engine(self.db_url)
            conn = db_engine.connect()

            # build query SQL
            sql = sql_text(self.layers_sql)

            # execute query
            result = conn.execute(sql)
            for row in result:
                # check required columns
                if 'layer_name' not in row:
                    self.logger.error(
                        "Missing column 'layer_name' in layers query"
                    )
                    break
                if 'layer_ref' not in row:
                    self.logger.error(
                        "Missing column 'layer_ref' in layers query"
                    )
                    break

                # collect metadata
                layer_metadata = dict(row)
                layer_metadata['_products'] = self.load_products_metadata(
                    row['layer_ref'], conn
                )

                # render templates
                html_metadata = self.render_metadata_templates(layer_metadata)

                # add to metadata lookup
                self.metadata[row['layer_name']] = {
                    'metadata': layer_metadata,
                    'html_contents': html_metadata
                }

            # close database connection
            conn.close()
        except Exception as e:
            self.logger.error("Could not load metadata:\n%s" % e)

    def load_products_metadata(self, layer_ref, conn):
        """Load metadata for any dataproducts of a layer from MetaDB.

        :param str layer_ref: Unique layer reference value corresponding to
                              dataproducts query, e.g. layer ID or layer name
        :param sqlalchemy.engine.Connection conn: DB connection
        """
        products_metadata = []

        try:
            # build query SQL
            sql = sql_text(self.layer_dataproducts_sql)

            # execute query
            result = conn.execute(sql, layer_ref=layer_ref)
            for row in result:
                products_metadata.append(dict(row))

        except Exception as e:
            self.logger.error("Could not load product metadata:\n%s" % e)

        return products_metadata

    def render_metadata_templates(self, metadata):
        """Render metadata templates and return HTML contents for a layer.

        :param obj metadata: Raw layer metadata from query
        """
        html_contents = []

        for template in self.templates:
            try:
                # render template
                content = template['template'].render(**metadata)
                if content:
                    # add content if HTML is not blank

                    # NOTE: use ordered keys
                    html_content = OrderedDict()
                    html_content['label'] = template['label']
                    html_content['content'] = content

                    html_contents.append(html_content)
            except Exception as e:
                self.logger.error(
                    "Could not render metadata template for %s:\n%s",
                    (template['label'], e)
                )

        return html_contents

    def layer_metadata(self, layername):
        """Return raw metadata from query for a layer.

        :param str layername: Layer name
        """
        return self.metadata.get(layername, {}).get('metadata', {})

    def layer_html_metadata(self, layername):
        """Return metadata HTML contents for a layer
        for use in Map Viewer and Dataproduct Service configs.

        :param str layername: Layer name
        """
        return self.metadata.get(layername, {}).get('html_contents', [])
