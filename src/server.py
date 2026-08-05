from collections import OrderedDict
import os

from flask import Flask, json

from config_generator import ConfigGenerator


# Flask application
app = Flask(__name__)

# get path to ConfigGenerator config file
config_file = os.environ.get(
    'CONFIG_GENERATOR_CONFIG', '/configGeneratorConfig.json'
)


def config_generator():
    """Create a ConfigGenerator instance."""
    # read ConfigGenerator config file
    try:
        with open(config_file) as f:
            # parse config JSON with original order of keys
            config = json.load(f, object_pairs_hook=OrderedDict)
    except Exception as e:
        msg = "Error loading ConfigGenerator config:\n%s" % e
        app.logger.error(msg)
        raise Exception(msg)

    # create ConfigGenerator
    return ConfigGenerator(config, app.logger)


# routes
@app.route("/generate_configs", methods=['POST'])
def generate_configs():
    """Generate service configs and permissions."""
    try:
        # create ConfigGenerator
        generator = config_generator()
        generator.write_configs()
        generator.write_permissions()

        return {
            'message': "Finished writing service configs and permissions"
        }
    except Exception as e:
        return {
            'error': str(e)
        }


@app.route("/update_qgs", methods=['POST'])
def update_qgs():
    """Generate QGIS project files."""
    try:
        # create ConfigGenerator
        generator = config_generator()
        qgs_writer_log = generator.write_qgs()

        return {
            'message': "Finished writing QGIS project files",
            'log': qgs_writer_log
        }
    except Exception as e:
        return {
            'error': str(e)
        }


# local webserver
if __name__ == '__main__':
    print("Starting ConfigGenerator service...")
    app.run(host='localhost', port=5032, debug=True)
