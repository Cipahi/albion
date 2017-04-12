# coding: utf-8

from qgis.core import QgsFeatureRequest, QgsGeometry
from shapely.geometry import Point
from shapely.ops import transform
from shapely.wkt import loads
from .qgis_hal import (insert_features_in_layer,
                       get_id,
                       create_new_feature,
                       clone_feature_with_geometry_transform,
                       get_feature_attribute_values,
                       feature_to_shapely_wkt)
from .graph_operations import compute_segment_geometry
from functools import partial


def __transform_geom(translation, geom):
    g = loads(geom.exportToWkt().replace("Z", " Z"))
    return QgsGeometry.fromWkt(
        transform(lambda x, y, z: (x + translation[0],
                                   y + translation[1],
                                   z + translation[2]),
                  g).wkt)


# Helper methods to manage fake generatrices
def create(section, source_layer, source_feature, link, translation):
    source_has_field_HoleID = source_layer.fields().fieldNameIndex(
        'HoleId') >= 0
    source_has_field_mine = source_layer.fields().fieldNameIndex(
        'mine') >= 0

    fake = clone_feature_with_geometry_transform(
        source_feature,
        partial(__transform_geom, translation))

    if source_has_field_HoleID:
        fake.setAttribute('HoleID', 'Fake')
    if source_has_field_mine:
        fake.setAttribute('mine', -1)
    fake.setAttribute('link', link)

    # we need to make sure that the newly created geometry is in the section
    buf = section.line.buffer(section.width, cap_style=2)
    centroid = fake.geometry().boundingBox().center()

    # max 10 step
    step = 10
    delta = -1.0 / float(step)

    while not Point(centroid.x(), centroid.y()).intersects(buf) and step > 0:
        fake.geometry().translate(
            translation[0] * delta, translation[1] * delta)
        centroid = fake.geometry().boundingBox().center()
        step = step - 1

    return fake


def insert(layer, feature):
    link = get_feature_attribute_values(layer, feature, 'link')
    insert_features_in_layer([feature], layer)
    return layer.getFeatures(
        QgsFeatureRequest().setFilterExpression(
            u'"link" = {0}'.format(link))).next()


def connect(subgraph, feature1, feature2, link, source_layer):
    segment = compute_segment_geometry(
        feature_to_shapely_wkt(feature1),
        feature_to_shapely_wkt(feature2))
    new_feature = create_new_feature(
        subgraph,
        segment.wkt,
        {
            'layer': get_id(source_layer),
            'start': get_id(feature1),
            'end': get_id(feature2),
            'link': link,
        })

    subgraph.beginEditCommand('subgraph update')
    subgraph.dataProvider().addFeatures([new_feature])
    subgraph.endEditCommand()
    subgraph.updateExtents()


def fake_generatrices(source_layer, layer):
    query = ''
    if source_layer.fields().fieldNameIndex('HoleId') >= 0:
        query = u"attribute($currentfeature, 'HoleId') = 'Fake' OR attribute($currentfeature, 'HoleId:Integer64(10,0)') = 'Fake'"
    elif source_layer.fields().fieldNameIndex('mine') >= 0:
        query = u"attribute($currentfeature, 'mine') = -1 OR attribute($currentfeature, 'mine:Integer64(10,0)') = -1"
    else:
        return None

    return layer.getFeatures(QgsFeatureRequest().setFilterExpression(query))
