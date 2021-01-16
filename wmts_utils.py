import re
import requests
from xml.dom.minidom import parseString
from qwc_services_core.cache import ExpiringDict

capabilites_cache = ExpiringDict()

def getFirstElementByTagName(parent, name):
    try:
        return parent.getElementsByTagName(name)[0]
    except:
        return None

def get_wms_layer_data(logger, capabilites_url, layer_name):

    global capabilites_cache

    if not capabilites_cache.lookup(capabilites_url):
        response = requests.get(capabilites_url + "?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0")
        if response.status_code != requests.codes.ok:
            logger.error(
                "Could not download WMS capabilities from %s:\n%s" %
                (capabilites_url, response.text))
            return {}

        capabilites_cache.set(capabilites_url, response.text)

    doc = parseString(capabilites_cache.lookup(capabilites_url)["value"])
    contents = getFirstElementByTagName(doc, "WMS_Capabilities")
    layers = contents.getElementsByTagName("Layer")
    targetLayer = None
    for layer in layers:
        name = getFirstElementByTagName(layer, "Name")
        if name and name.firstChild.nodeValue == layer_name:
            targetLayer = layer
            break

    if not targetLayer:
        return {
            "abstract": ""
        }

    abstract = getFirstElementByTagName(targetLayer, "Abstract")
    return {
        "abstract": abstract.firstChild.nodeValue if abstract else ""
    }


def get_wmts_layer_data(logger, capabilites_url, layer_name):

    global capabilites_cache

    if not capabilites_cache.lookup(capabilites_url):
        response = requests.get(capabilites_url)
        if response.status_code != requests.codes.ok:
            logger.error(
                "Could not download WMTS capabilities from %s:\n%s" %
                (capabilites_url, response.text))
            return {}

        capabilites_cache.set(capabilites_url, response.text)

    doc = parseString(capabilites_cache.lookup(capabilites_url)["value"])
    contents = doc.getElementsByTagName("Contents")[0]
    tileMatrixSetMap = {}
    for child in contents.childNodes:
        if child.nodeName == "TileMatrixSet":
            tileMatrixSet = child
            identifier = tileMatrixSet \
                .getElementsByTagName("ows:Identifier")[0] \
                .firstChild.nodeValue
            crs = tileMatrixSet \
                .getElementsByTagName("ows:SupportedCRS")[0] \
                .firstChild.nodeValue.replace("urn:ogc:def:crs:", "")
            tileMatrices = tileMatrixSet.getElementsByTagName("TileMatrix")
            if tileMatrices:
                origin = list(map(float, re.split(r"\s+", tileMatrices[0] \
                    .getElementsByTagName("TopLeftCorner")[0] \
                    .firstChild.nodeValue)))

                tile_size = [
                    tileMatrices[0].getElementsByTagName("TileWidth")[0] \
                        .firstChild.nodeValue,
                    tileMatrices[0].getElementsByTagName("TileHeight")[0] \
                        .firstChild.nodeValue
                ]

                # 0.00028: assumed pixel width in meters, as per WMTS standard
                resolutions = list(map(lambda node: float(
                    node.getElementsByTagName("ScaleDenominator")[0] \
                    .firstChild.nodeValue) * 0.00028, tileMatrices
                ))

                tileMatrixSetMap[identifier] = {
                    "name": identifier,
                    "crs": crs,
                    "origin": origin,
                    "resolutions": resolutions,
                    "tile_size": tile_size
                }

    targetLayer = None
    layers = contents.getElementsByTagName("Layer")
    for entry in layers:
        if entry.getElementsByTagName("ows:Identifier")[0] \
            .firstChild.nodeValue == layer_name:
            targetLayer = entry
            break

    if not targetLayer:
        logger.error(
                "Could not find layer %s in WMTS capabilities %s" %
                (layer_name, capabilites_url))

    targetTileMatrixSet = None
    tileMatrixSets = targetLayer \
        .getElementsByTagName("TileMatrixSetLink")[0] \
        .getElementsByTagName("TileMatrixSet")
    for tileMatrixSet in tileMatrixSets:
        if tileMatrixSet.firstChild.nodeValue in tileMatrixSetMap:
            targetTileMatrixSet = tileMatrixSetMap[tileMatrixSet.firstChild.nodeValue]
            # Use EPSG:2056 tile matrix if possible
            if targetTileMatrixSet["crs"] == "EPSG:2056":
                break

    style = targetLayer.getElementsByTagName("Style")[0] \
        .getElementsByTagName("ows:Identifier")[0].firstChild.nodeValue

    dim_ident = targetLayer.getElementsByTagName("Dimension")[0] \
        .getElementsByTagName("ows:Identifier")[0].firstChild.nodeValue
    dim_value = targetLayer.getElementsByTagName("Dimension")[0] \
        .getElementsByTagName("Value")[0].firstChild.nodeValue

    res_url = targetLayer.getElementsByTagName("ResourceURL")[0] \
        .getAttribute("template").replace("{%s}" % dim_ident, dim_value)

    abstract = getFirstElementByTagName(targetLayer, "ows:Abstract")

    return {
        "crs": targetTileMatrixSet["crs"],
        "layer_name": layer_name,
        "style": style,
        "origin": targetTileMatrixSet["origin"],
        "tile_size": targetTileMatrixSet["tile_size"],
        "tileMatrixSet": targetTileMatrixSet["name"],
        "resolutions": targetTileMatrixSet["resolutions"],
        "capabilites_url": capabilites_url,
        "dim_ident": dim_ident,
        "dim_value": dim_value,
        "res_url": res_url,
        "abstract": abstract.firstChild.nodeValue if abstract else ""
    }
