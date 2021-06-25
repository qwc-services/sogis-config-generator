SO!MAP Config Generator
=======================

Generate JSON files for service configs and permissions from a SO!MAP ConfigDB, and write QGIS project files.

Can be run from command line or as a service.


Setup
-----

Create a config file `configGeneratorConfig.json` for the ConfigGenerator (see below).


Configuration
-------------

* [JSON schema](schemas/sogis-config-generator.json)

Example `configGeneratorConfig.json`:
```jsonc
{
  "$schema": "https://github.com/qwc-services/sogis-config-generator/raw/master/schemas/sogis-config-generator.json",
  "service": "config-generator",
  "config": {
    "config_db_url": "postgresql:///?service=soconfig_services",
    "config_path": "../docker/volumes/config/",
    "default_qgis_server_url": "http://localhost:8001/ows/"
  },
  "services": [
    {
      "name": "ogc",
      "config": {
        "default_ogc_server_url": "http://localhost:8001/ows/"
      },
      "resources": {
        "wms_services": [
          {
            "name": "somap",
            "online_resources": {
              "service": "https://geo.so.ch/ows/somap",
              "feature_info": "https://geo.so.ch/api/v1/featureinfo/somap",
              "legend": "https://geo.so.ch/api/v1/featureinfo/somap"
            }
          }
        ],
        "wfs_services": [
          {
            "name": "somap",
            "online_resource": "https://geo.so.ch/api/wfs"
          }
        ]
      }
    },
    {
      "name": "featureInfo",
      "config": {
        "default_qgis_server_url": "http://localhost:8001/ows/",
        "default_info_template": "<table>...</table>"
      }
    }
  ],
  "qgs_writer": {
    "project_output_dir": "../docker/volumes/qgs-resources/",
    "default_extent": [2590983, 1212806, 2646267, 1262755],
    "#default_raster_extent": [2590000, 1210000, 2650000, 1270000],
    "selection_color": [255, 255, 0, 255]
  },
  // optional metadata
  "metadata": {
    "db_url": "postgresql:///?service=metadb",
    "layers_sql": "SELECT layer_name, layer_ref, ...",
    "layer_dataproducts_sql": "SELECT ... WHERE layer.id = :layer_ref...",
    "metadata_templates": [
      {
        "label": "Metadata",
        "template": "<div>...</div>"
      }
    ]
  }
}
```

### Metadata

Metadata from a MetaDB may be collected by adding the optional `metadata` config block.

* `db_url`: Connection to MetaDB
* `layers_sql`: SQL for querying all layers. The unique columns `layer_name` and `layer_ref` are required for matching layer names and as reference for querying dataproducts.
* `layer_dataproducts_sql`: SQL for querying dataproducts of a layer, using `:layer_ref` as placeholder for layer reference
* `metadata_templates`: Templates for metadata entries added to service configs
  * `label`: Label for metadata entry
  * `template`: Jinja2 template for HTML metadata content
  **NOTE:** Templates are omitted if their rendered template is blank. This may be used to filter entries by checking e.g. if some value is present in the template.

Examples:

`layers_sql`:
```sql
SELECT
    descr.layer AS layer_name, -- required
    descr.id::text AS layer_ref, -- required
    descr.descr,
    descr.name_long,
    descr.ausk_fach,
    descr.ausk_gis,
    descr.abgtyp,
    descr.updated_at_date
FROM
    metadb.html_description descr
WHERE
    descr.dataprovider = 'postgres'
ORDER BY
    descr.layer;  
```

`layer_dataproducts_sql`:
```sql
SELECT
    product.id::text AS id,
    product.title,
    product.descr,
    product.uri
FROM
    metadb.product
    JOIN metadb.many_product_has_many_descr mtm
        ON mtm.id_product = product.id
    JOIN metadb.html_description descr
        ON descr.id = mtm.id_descr
WHERE
    descr.id = :layer_ref -- layer reference from layers query
    AND product.uri ILIKE '%.zip'
ORDER BY
    product.title;  
```

The following variables may be used in the metadata templates:
* `layer_name`: Layer name
* `layer_ref`: Layer reference for products query
* custom fields from layer query, e.g. `descr`, `name_long`, ...
* `_products`: List of dataproducts for this layer
  ```python
  _products = [
      {
          # custom fields from products query
          'id': "...",
          'title': "...",
          'descr': "...",
          'uri': "..."
          # ...
      }
  ]
  ```

Template for layer metadata:
```html
<ul>
  <li>Beschreibung: {{ descr | replace('\n', '<br>') }}</li>
  <li>Datenherr: {{ name_long }}</li>
  <li>Auskunft fachlich: {{ ausk_fach }}</li>
  <li>Auskunft GIS: {{ ausk_gis }}</li>
  <li>Abgabetyp: {{ abgtyp }}</li>
  <li>Letzte Änderung: {{ updated_at_date }}</li>
</ul>
```

Template for dataproduct links:
```html
{% if _products %}
    {% for product in _products %}
        <div>
            {{ product['title'] }}<br>
            <br>
            {{ product['descr'] }}<br>
            Metadateneintrag in geocat.ch: <a href="https://www.geocat.ch/geonetwork/srv/ger/catalog.search#/metadata/{{ product['id'] }}" target="_blank">Link</a><br>
            Dieses Datenprodukt herunterladen:<br>
            <a href="{{ product['uri'] }}" target="_blank">{{ product['uri'] }}</a>
        <div>
    {% endfor %}
{% endif %}
```
**NOTE:** This is rendered blank if `_products` is empty, so it will then be omitted.


Usage
-----

### Script

Show command options:

    python config_generator.py --help

Generate both service configs and permissions:

    python config_generator.py ./configGeneratorConfig.json all

Generate service config files:

    python config_generator.py ./configGeneratorConfig.json service_configs

Generate permissions file:

    python config_generator.py ./configGeneratorConfig.json permissions

Write QGIS project files:

    python config_generator.py ./configGeneratorConfig.json qgs

### Docker container

**NOTE:** Requires write permissions for config-generator docker user (`www-data`) in `config_path` and `project_output_dir` for writing service configs and permissions, and generating QGIS projects.

    cd ../docker
    docker-compose run config-generator /configGeneratorConfig.json all

### Service

Set the `CONFIG_GENERATOR_CONFIG` environment variable to the config file path (default: `/configGeneratorConfig.json`).

Base URL:

    http://localhost:5032/

Generate both service configs and permissions:

    curl -X POST "http://localhost:5032/generate_configs"

Write QGIS project files:

    curl -X POST "http://localhost:5032/update_qgs"


Development
-----------

Create a virtual environment:

    virtualenv --python=/usr/bin/python3 .venv

Activate virtual environment:

    source .venv/bin/activate

Install requirements:

    pip install -r requirements.txt

Run Test-DB and QGIS Server:

    cd ../docker && docker-compose up -d qgis-server

Generate service configs and permissions for Docker:

    python config_generator.py ../docker/configGeneratorConfig.json all

Write QGIS project files for Docker:

    python config_generator.py ../docker/configGeneratorConfig.json qgs

Start local service:

    CONFIG_GENERATOR_CONFIG=../docker/configGeneratorConfig.json python server.py
