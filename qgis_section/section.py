# coding=utf-8

from qgis.core import * # unable to import QgsWKBTypes otherwize (quid?)
from qgis.gui import *

from PyQt4.QtCore import QObject, pyqtSignal

from shapely.geometry import Point, LineString
from shapely.wkt import loads
from shapely.ops import transform

from .helpers import projected_layer_to_original, projected_feature_to_original
from .layer import Layer
import numpy
import logging

class Section(QObject):
    changed = pyqtSignal(str, float)
    needs_redraw = pyqtSignal()
    # section_layer_modified = pyqtSignal(Layer)

    def __init__(self, id_="section", parent=None):
        QObject.__init__(self, parent)
        self.__line = None
        self.__id = id_
        self.__width = 0
        self.__z_scale = 1
        self.__projections = {}
        self.__enabled = True

        # in case of reload, or if a project is already opend with layers
        # that belong to this section
        self.__add_layers(QgsMapLayerRegistry.instance().mapLayers().values())

        # for project loading
        QgsMapLayerRegistry.instance().layersAdded.connect(self.__add_layers)
        QgsMapLayerRegistry.instance().layersWillBeRemoved.connect(self.__remove_layers)

    def unload(self):
        self.__remove_layers(self.__projections.keys())
        QgsMapLayerRegistry.instance().layersAdded.disconnect(self.__add_layers)
        QgsMapLayerRegistry.instance().layersWillBeRemoved.disconnect(self.__remove_layers)
        self.projections = {}

    def set_z_scale(self, scale):
        self.__z_scale = scale
        for sourceId in self.__projections:
            self.update_projections(sourceId)

    def update(self, wkt_line, width = 0):
        try:
            self.__line = loads(wkt_line.replace("Z", " Z"))
            self.__width = width
            # always reset z-scale when setting a new line
            self.__z_scale = 1.0
        except Exception, e:
            self.__line = None

        for sourceId in self.__projections:
            self.update_projections(sourceId)

        self.changed.emit(wkt_line, width)

    def project(self, qgs_geometry):
        return self._transform(qgs_geometry, self.project_point)

    def unproject(self, qgs_geometry):
        return self._transform(qgs_geometry, self.unproject_point)

    def _transform(self, qgs_geometry, point_transformation):
        """returns a transformed geometry"""
        #@todo use wkb to optimize ?
        geom = loads(qgs_geometry.exportToWkt().replace("Z", " Z"))
        return QgsGeometry.fromWkt(
                transform(
                    lambda x,y,z: point_transformation(x, y, z),
                    geom).wkt)

    def z_range(self, smin, smax):
        z_min = -float('inf')
        z_max = float('inf')
        for sourceId in self.__projections:
            for p in self.__projections[sourceId]['layers']:
                # min|max y of projected feature
                for feature in p.projected_layer.getFeatures():
                    bbox = feature.geometry().boundingBox()
                    if bbox.xMinimum() >= smin and bbox.xMaximum() <= smax:
                        z_min = max(z_min, bbox.yMinimum())
                        z_max = min(z_max, bbox.yMaximum())

        return (z_min / self.__z_scale, z_max / self.__z_scale)

    def project_point(self, x, y, z):
        # project a 3d point
        # x/y/z can be scalars or tuples
        if isinstance(x, tuple):
            _x = ()
            _y = ()
            _z = tuple((0 for i in range(0, len(x))))
            for i in range(0, len(x)):
                _x += (self.__line.project(Point(x[i], y[i])),)
                _y += (z[i]*self.__z_scale,)
            return (_x, _y, _z)
        else:
            _x = self.__line.project(Point(x, y))
            _y = z*self.__z_scale
            return (_x, _y, 0)

    def unproject_point(self, x, y, z):
        # 2d -> 3d transfomration
        # x/y/z can be scalars or tuples
        if isinstance(x, tuple):
            _x = ()
            _y = ()
            for i in range(0, len(x)):
                q = self.__line.interpolate(x[i])
                _x += (q.x, )
                _y += (q.y, )

            return (_x,
             _y, tuple((v/self.__z_scale for v in y)))
        else:
            q = self.__line.interpolate(x)
            return (q.x, q.y, y/self.__z_scale)

    def register_projection_layer(self, projection):
        sourceId = projection.source_layer.id()
        if not sourceId in self.__projections:
            self.__projections[sourceId] = {
                'needs_update_fn': lambda : self.update_projections(sourceId),
                'layers': []
            }
            # setup update logic
            projection.source_layer.featureAdded.connect(self.__projections[sourceId]['needs_update_fn'])
            projection.source_layer.editCommandEnded.connect(self.__projections[sourceId]['needs_update_fn'])
            projection.source_layer.editCommandEnded.connect(self.request_canvas_redraw)
            projection.source_layer.selectionChanged.connect(self.__synchronize_selection)

        self.__projections[sourceId]['layers'] += [projection]
        projection.projected_layer.beforeCommitChanges.connect(self.__propagateChangesToSourceLayer)
        projection.projected_layer.selectionChanged.connect(self.__synchronize_selection)
        self.changed.emit(self.__line.wkt if self.__line else None, self.__width)

    def update_projections(self, sourceId):
        if not self.__enabled:
            return
        logging.debug('update_projections {} {}!!!'.format(sourceId, len(self.__projections[sourceId]['layers'])))
        for p in self.__projections[sourceId]['layers']:
            p.apply(self, True)

    def unregister_projected_layer(self, layerId):
        for sourceId in self.__projections:
            sourceLayer = QgsMapLayerRegistry.instance().mapLayer(sourceId)

            # removal of source layer
            if sourceId == layerId:
                logging.debug('  > removing source layer {}'.format(layerId))
                sourceLayer.featureAdded.disconnect(self.__projections[sourceId]['needs_update_fn'])
                sourceLayer.editCommandEnded.disconnect(self.__projections[sourceId]['needs_update_fn'])
                sourceLayer.editCommandEnded.disconnect(self.request_canvas_redraw)
                sourceLayer.selectionChanged.disconnect(self.__synchronize_selection)
                projection_removed = []

                for p in self.__projections[sourceId]['layers']:
                    p.projected_layer.beforeCommitChanges.disconnect(self.__propagateChangesToSourceLayer)
                    p.projected_layer.selectionChanged.disconnect(self.__synchronize_selection)
                    projection_removed += [ p.projected_layer ]

                del self.__projections[sourceId]
                return projection_removed

            else:
                projections = self.__projections[sourceId]['layers']
                for p in projections:
                    if p.projected_layer.id() == layerId:
                        old_projections_count = len(self.__projections[sourceId]['layers'])
                        projection_removed = [ p.projected_layer ]
                        p.projected_layer.beforeCommitChanges.disconnect(self.__propagateChangesToSourceLayer)
                        p.projected_layer.selectionChanged.disconnect(self.__synchronize_selection)
                        self.__projections[sourceId]['layers'] = [p for p in projections if p.projected_layer.id() != layerId]

                        logging.debug('  > removed projection layer {} [{} projections old/new count = {}/{}]'.format(layerId, sourceId, old_projections_count, len(self.__projections[sourceId]['layers'])))

                        if len(self.__projections[sourceId]['layers']) == 0:
                            sourceLayer.featureAdded.disconnect(self.__projections[sourceId]['needs_update_fn'])
                            sourceLayer.editCommandEnded.disconnect(self.__projections[sourceId]['needs_update_fn'])
                            sourceLayer.selectionChanged.disconnect(self.__synchronize_selection)
                            del self.__projections[sourceId]

                        return projection_removed
        return []

    def __synchronize_selection(self, selected, deselected, clearAndSelect):
        source = self.sender()

        if source.id() in self.__projections:
            self.__synchronize_selection_source_proj(self.__projections[source.id()], selected, deselected)
        else:
            for s_id in self.__projections:
                for layer in self.__projections[s_id]['layers']:
                    if layer.projected_layer.id() == source.id():
                        layer.synchronize_selection_proj_to_source()
                        return


    def __synchronize_selection_source_proj(self, l, selected, deselected):
        # sync selected items from layer_from in [layers_to]
        if len(l['layers']) == 0:
            return

        source_layer = l['layers'][0].source_layer

        selected_ids = [f.attribute('id') for f in source_layer.selectedFeatures()]

        for layer in l['layers']:
            layer.synchronize_selection_source_to_proj(selected_ids)

    # Maintain section TreeView state
    def __add_layers(self, layers):
        for layer in layers:
            if hasattr(layer, 'customProperty') \
                    and layer.customProperty("section_id") is not None \
                    and layer.customProperty("section_id") == self.__id :
                source_layer = projected_layer_to_original(layer)
                if source_layer is not None:
                    l = Layer(source_layer, layer)
                    self.register_projection_layer(l)
                    l.apply(self, True)

    def __remove_layers(self, layer_ids):
        logging.debug('Removing layers {}'.format(layer_ids))
        for layer_id in layer_ids:
            projected_layers = self.unregister_projected_layer(layer_id)

    def __propagateChangesToSourceLayer(self):
        layer = self.sender()

        # todo: edition and section lines are tied because we need to unproject
        if not self.is_valid:
            return

        for sourceId in self.__projections:
            for p in self.__projections[sourceId]['layers']:
                if p.projected_layer.id() == layer.id():
                    p.propagateChangesToSourceLayer(self)
                    # self.section_layer_modified.emit(p)


        # Re-project all layer
        for sourceId in self.__projections:
            for p in self.__projections[sourceId]['layers']:
                p.apply(self, True)

        self.request_canvas_redraw()

    def request_canvas_redraw(self):
        self.needs_redraw.emit()

    def projections_of(self, layer_id):
        return self.__projections[layer_id]['layers'] if layer_id in self.__projections else []

    def disable(self):
        self.__enabled = False

    def enable(self):
        self.__enabled = True

    def __getattr__(self, name):
        if name == "line":
            return self.__line
        elif name == "width":
            return self.__width
        elif name == "id":
            return self.__id
        elif name == "is_valid":
            return self.line is not None
        elif name == "z_scale":
            return self.__z_scale
        elif name == "enabled":
            return self.__enabled
        raise AttributeError(name)
