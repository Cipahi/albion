# coding: utf-8

from qgis.core import (QgsMapLayer,
                       QgsRectangle,
                       QgsGeometry,
                       QgsFeatureRequest,
                       QGis)
from qgis.gui import QgsMapTool

from PyQt4.QtCore import pyqtSignal
from shapely.geometry import LineString
from .qgis_hal import get_all_layers_with_property_set, get_id

import logging


class LineSelectTool(QgsMapTool):
    line_clicked = pyqtSignal(str)

    def __init__(self, canvas):
        QgsMapTool.__init__(self, canvas)
        self.canvas = canvas

    def canvasReleaseEvent(self, event):
        projections = get_all_layers_with_property_set('projected_layer')
        filtered = [p.customProperty('projected_layer')
                    for p in projections]
        filtered += [get_id(p) for p in projections]

        # Get the click
        radius = QgsMapTool.searchRadiusMU(self.canvas)
        for layer in self.canvas.layers():
            if get_id(layer) in filtered:
                continue

            layerPoint = self.toLayerCoordinates(layer, event.pos())
            rect = QgsRectangle(layerPoint.x() - radius,
                                layerPoint.y() - radius,
                                layerPoint.x() + radius,
                                layerPoint.y() + radius)
            rect_geom = QgsGeometry.fromRect(rect)
            if layer.type() == QgsMapLayer.VectorLayer and\
               layer.geometryType() == QGis.Line:
                for feat in layer.getFeatures(QgsFeatureRequest(rect)):
                    if feat.geometry().intersects(rect_geom) and\
                       feat.geometry().length() > 0:
                        logging.info('found line in {}'.format(layer.name()))
                        self.line_clicked.emit(
                            QgsGeometry.exportToWkt(feat.geometry()))
                        return
        # emit a small linestring in the x direction
        layerPoint = self.toMapCoordinates(event.pos())
        self.line_clicked.emit(
            LineString([(
                layerPoint.x()-radius, layerPoint.y()),
                (layerPoint.x()+radius, layerPoint.y())]).wkt)
