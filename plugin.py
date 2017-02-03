# coding=utf-8

from qgis.core import *
from qgis.gui import *

from PyQt4.QtCore import Qt, pyqtSignal, QObject, QVariant, QTimer
from PyQt4.QtGui import QDockWidget, QMenu, QColor, QToolBar, QDialog, QIcon, QCursor, QMainWindow, QProgressDialog, QPixmap, QFileDialog, QLineEdit, QLabel, QMessageBox, QComboBox

import os, traceback
import math
import numpy as np
from operator import xor

from .qgis_section.main_window import MainWindow
from .qgis_section.section import Section
from .qgis_section.section_tools import SelectionTool
from .qgis_section.helpers import projected_layer_to_original, projected_feature_to_original
from .qgis_section.action_state_helper import ActionStateHelper
from .qgis_section.layer import Layer

from shapely.wkt import loads
from shapely.geometry import Point, LineString

from .create_layer_widget import CreateLayerWidget
from .graph_edit_tool import GraphEditTool
from .polygon_section_layer import PolygonLayerProjection
from .graph import to_volume, to_surface, extract_paths
from .viewer_3d.viewer_3d import Viewer3D

from .fake_generatrice import create as fg_create
from .fake_generatrice import insert as fg_insert
from .fake_generatrice import connect as fg_connect
from .fake_generatrice import fake_generatrices as fg_fake_generatrices

import numpy as np
import logging

def icon(name):
    return QIcon(os.path.join(os.path.dirname(__file__), 'icons', name))

class GraphLayerHelper(QObject):
    graph_layer_tagged = pyqtSignal(QgsVectorLayer)

    def __init__(self, custom_property):
        QObject.__init__(self)
        self.graphLayer = None
        self.custom_property = custom_property

    def add_to_toolbar(self, iface, toolbar, icon_name):
        self.iface = iface
        self.action = toolbar.addAction(icon(icon_name), self.__tooltip())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.__on_click)

    def lookup(self, layers):
        for layer in layers:
            if layer.customProperty(self.custom_property):
                self.__tag_layer(layer)

    def layer(self):
        return self.graphLayer

    def layer_is_projection(self, layer):
        if layer is None or self.graphLayer is None:
            return False
        return layer.customProperty("projected_layer") == self.graphLayer.id()

    def __tooltip(self):
        if self.graphLayer:
            return "'{}' layer is '{}'".format(self.custom_property, self.graphLayer.name())
        else:
            return "No '{}' layer defined".format(self.custom_property)

    def __tag_layer(self, layer):
        self.graphLayer = layer
        self.graphLayer.setCustomProperty(self.custom_property, True)
        self.action.setChecked(True)
        self.action.setToolTip(self.__tooltip())
        self.graph_layer_tagged.emit(self.graphLayer)

    def __untag_layer(self):
        self.graphLayer.removeCustomProperty(self.custom_property)
        self.graphLayer = None
        self.graph_layer_tagged.emit(self.graphLayer)
        self.action.setToolTip(self.__tooltip())
        self.action.setChecked(False)

    def __on_click(self):
        if not self.action.isChecked():
            self.__untag_layer()
            return

        # mark active layer as the graph layer
        layer = self.iface.mapCanvas().currentLayer()

        if layer is None: return
        if not isinstance(layer, QgsVectorLayer):   return
        if not (layer.geometryType() == QGis.Line): return
        if layer.fieldNameIndex("start") == -1:     return
        if layer.fieldNameIndex("end") == -1:       return
        if layer.fieldNameIndex("layer") == -1:     return

        self.__tag_layer(layer)

class DataToolbar(QToolBar):
    def __init__(self, iface, section, viewer3d, graphLayerHelper, subGraphLayerHelper):
        QToolBar.__init__(self)
        self.__iface = iface
        self.__section = section;
        self.__logger = iface.messageBar()
        self.viewer3d = viewer3d
        self.mapCanvas = iface.mapCanvas()

        self.addAction(icon('1_add_layer.svg'), 'create line layer').triggered.connect(self.__add_layer)
        self.addAction(icon('1_add_layer.svg'), 'create layer from csv').triggered.connect(self.__import_csv )

        self.graphLayerHelper = graphLayerHelper
        self.subGraphLayerHelper = subGraphLayerHelper
        self.graphLayerHelper.add_to_toolbar(iface, self, '3_tag_layer_graph.svg')
        self.subGraphLayerHelper.add_to_toolbar(iface, self, '4_tag_layer_sous_graph.svg')

        QgsMapLayerRegistry.instance().layersAdded.connect(self.add_layers)

        ex = self.addAction(icon('5_export_polygons.svg'), 'Export polygons (graph)')
        ex.triggered.connect(lambda c: self.__export_polygons(self.graphLayerHelper.layer()))
        ActionStateHelper(ex).add_is_enabled_test(lambda action: self.__export_polygons_precondition_check(self.graphLayerHelper.layer())).update_state()

        ex2 = self.addAction(icon('5b_export_polygons_sous_graphes.svg'), 'Export polygons (subgraph)')
        ex2.triggered.connect(lambda c: self.__export_polygons(self.subGraphLayerHelper.layer()))
        ActionStateHelper(ex2).add_is_enabled_test(lambda action: self.__export_polygons_precondition_check(self.subGraphLayerHelper.layer())).update_state()


        self.addAction(icon('6_export_volume.svg'), 'Build volume').triggered.connect(self.__build_volume)
        self.__section.toolbar.projected_layer_created.connect(self.__add_polygon_layer)

    def cleanup(self):
        self.__section.toolbar.projected_layer_created.disconnect(self.__add_polygon_layer)
        self.__section = None
        QgsMapLayerRegistry.instance().layersAdded.disconnect(self.add_layers)

    def __export_polygons_precondition_check(self, graphLayer):
        layer = self.mapCanvas.currentLayer()
        if layer is None:
            return (False, "No active layer")
        if graphLayer is None:
            return (False, "No graph layer defined")
        if not layer.customProperty("session_id") is None:
            return (False, "Select a non-projected layer")
        if not layer.isSpatial():
            return (False, "Selected layer has no geometry")
        if layer.featureCount() == 0:
            return (False, "Selected layer has no features")
        return (True, "")

    def create_projected_layer(self, layer, section_id):
        if layer is None:
            return

        section = QgsVectorLayer(
            "{geomType}?crs={crs}&index=yes".format(
                geomType={
                    QGis.Point:"Point",
                    QGis.Line:"LineString",
                    QGis.Polygon:"Polygon"
                    }[layer.geometryType()],
                crs=self.__iface.mapCanvas().mapSettings().destinationCrs().authid()
                ), layer.name() + "_export", "memory")
        section.setCustomProperty("section_id", section_id)
        section.setCustomProperty("projected_layer", layer.id())

        # cpy attributes structure
        section.dataProvider().addAttributes([layer.fields().field(f) for f in range(layer.fields().count())])
        section.updateFields()

        # cpy style
        section.setRendererV2(layer.rendererV2().clone())
        return section

    def __export_polygons(self, graphLayer):
        file = QFileDialog.getSaveFileName(self, "Save polygon-csv export to...")
        if len(file) == 0:
            return

        layer = self.mapCanvas.currentLayer()

        polygons = self.export_polygons_impl(graphLayer, layer)

        out_file = open(file, 'w')
        for index in range(0, len(polygons)):
            vertices = polygons[index]

            for i in range(0, len(vertices), 2):
                v = vertices[i]
                out_file.write('{};{};{};{}\n'.format(index, v[0], v[1], v[2]))

            for i in range(len(vertices)-1, 0, -2):
                v = vertices[i]
                out_file.write('{};{};{};{}\n'.format(index, v[0], v[1], v[2]))

            # last but not least: close the polygon
            v = vertices[0]
            out_file.write('{};{};{};{}\n'.format(index, v[0], v[1], v[2]))

        QMessageBox().information(self, 'Export', 'Wrote {} polygon(s)'.format(len(polygons)))

        out_file.close()



    def __export_polygons_for_one_section_line(self, section, graph_section_layer, scratch_projection, gen_ids, fakes_id, request, lid, generatrice_layer):
        # project graph features in scratch_projection layer using current section line
        graph_section_layer.apply(section, True)

        # export for real
        if scratch_projection.featureCount() == 0:
            return []

        connections = [[] for id_ in gen_ids]

        # browse edges
        for edge in scratch_projection.getFeatures(): #request):
            if edge.attribute('layer')  != lid:
                continue
            e1 = edge.attribute('start')
            e2 = edge.attribute('end')

            connections[gen_ids.index(e1)] += [e2]
            connections[gen_ids.index(e2)] += [e1]

        # export graph
        paths = extract_paths(gen_ids, fakes_id, connections)

        if paths == None or len(paths) == 0:
            logging.warning('No path found ({})'.format(request.filterExpression().expression()))
            return []

        logging.info('Found {} paths: {}'.format(len(paths), paths))


        result = []
        for path in paths:
            edges = []
            vertices = []

            for v in path:
                p = generatrice_layer.getFeatures(QgsFeatureRequest(v)).next()
                v = loads(p.geometry().exportToWkt().replace('Z', ' Z'))
                logging.debug(p.geometry().exportToWkt())

                vertices += [[ v.coords[0][0], v.coords[0][1], v.coords[0][2] ]]
                vertices += [[ v.coords[1][0], v.coords[1][1], v.coords[1][2] ]]

            if len(vertices) > 0:
                result += [vertices]

        return result

    def export_polygons_impl(self, graph_layer, sections_layer, section_param = None):
        result = []

        try:
            section = section_param if section_param else Section("dummy")

            # build a scratch (temporary) layer to hold graph_layer features projections
            scratch_projection = self.create_projected_layer(graph_layer, section.id)
            # QgsMapLayerRegistry.instance().addMapLayer(scratch_projection, False)

            # associate graph_layer to its projection layer
            graph_section_layer = Layer(graph_layer, scratch_projection)

            line_width = float(self.__section.toolbar.buffer_width.text())

            logging.info('Start polygon export')

            # read unique layers (of generating lines) that are connected in the graph
            layers = graph_layer.uniqueValues(graph_layer.fields().fieldNameIndex('layer'))

            for lid in layers:
                logging.info('Processing layer {}'.format(lid))
                generatrice_layer = QgsMapLayerRegistry.instance().mapLayer(lid)
                gen_ids = generatrice_layer.allFeatureIds()
                fakes = fg_fake_generatrices(generatrice_layer, generatrice_layer)
                fakes_id = [f.id() for f in fakes]

                # a valid path starts and ends on a fake generatrice, so skip this layer
                # if there aren't any fakes
                if len(fakes_id) == 0:
                    logging.warning('No fake generatrices in {}'.format(generatrice_layer.id()))
                    continue

                request = QgsFeatureRequest().setFilterExpression(u"'layer' = '{0}'".format(lid))

                if section_param is None:
                    # for each section line
                    for feature in sections_layer.getFeatures():
                        logging.info('Processing section {}'.format(feature.id()))
                        wkt_line = QgsGeometry.exportToWkt(feature.geometry())
                        section.update(wkt_line, line_width) # todo

                        # export for real
                        result += self.__export_polygons_for_one_section_line(section, graph_section_layer, scratch_projection, gen_ids, fakes_id, request, lid, generatrice_layer)
                else:
                    # export for real
                    result += self.__export_polygons_for_one_section_line(section, graph_section_layer, scratch_projection, gen_ids, fakes_id, request, lid, generatrice_layer)

        except Exception as e:
            logging.error(e)
        finally:
            if section_param is None:
                section.unload()
            # QgsMapLayerRegistry.instance().removeMapLayer(scratch_projection.id())
            print "DONE DONE"
            return result



    def __build_volume(self):
        pass
        # logging.info('build volume')
        # layer = self.mapCanvas.currentLayer()
        # if layer is None:
        #     logging.warning("No active layer")
        #     return

        # nodes = []
        # indices = {}
        # edges = []

        # def addVertice(geom):
        #     v = loads(geom.geometry().exportToWkt().replace('Z', ' Z'))
        #     return [[ list(v.coords[0]), list(v.coords[1]) ]]

        # graphLayer = self.graphLayerHelper.layer()

        # if graphLayer is None:
        #     return

        # section = Section()
        # for section_line in layer.getFeatures():
        #     section.update(section_line.geometry().exportToWkt(), float(self.__section.toolbar.buffer_width.text()))
        #     buf = section.line.buffer(section.width, cap_style=2)

        #     for edge in graphLayer.getFeatures():
        #         centroid = edge.geometry().boundingBox().center()
        #         if not Point(centroid.x(), centroid.y()).intersects(buf):
        #             continue


        #         layer = QgsMapLayerRegistry.instance().mapLayer(edge.attribute("layer"))
        #         start = layer.getFeatures(QgsFeatureRequest(edge.attribute("start"))).next()
        #         end = layer.getFeatures(QgsFeatureRequest(edge.attribute("end"))).next()

        #         if not(start.id() in indices):
        #             indices[start.id()] = len(nodes)
        #             nodes += addVertice(start)

        #         if not(end.id() in indices):
        #             indices[end.id()] = len(nodes)
        #             nodes += addVertice(end)

        #         edges += [ (indices[start.id()], indices[end.id()]) ]

        # volumes, vertices = to_volume(np.array(nodes), edges)

        # self.viewer3d.updateVolume(vertices, volumes)

        # self.updateGraph(None, None, True)

    def updateGraph(self, section_layers, scale_z = 1.0):
        graphLayer = self.graphLayerHelper.layer()

        if graphLayer is None:
            return

        def centroid(l):
            return [0.5*(l.coords[0][i]+l.coords[1][i]) for i in range(0, 3)]

        section_line_buffer = self.__section.section.line.buffer(self.__section.section.width, cap_style=2) if self.__section.section.is_valid else None
        graph_vertices = []
        graph_indices = []
        highlight_indices = []

        for edge in graphLayer.getFeatures():
            try:
                layer = QgsMapLayerRegistry.instance().mapLayer(edge.attribute("layer"))
                segment = []
                segment += [ layer.getFeatures(QgsFeatureRequest(edge.attribute("start"))).next() ]
                segment += [ layer.getFeatures(QgsFeatureRequest(edge.attribute("end"))).next() ]

                highlighted = not (section_line_buffer is None)

                for v in segment:
                    a = v.geometry().exportToWkt().replace("Z", " Z")
                    pv = loads(a)
                    c = centroid(pv)

                    if not (section_line_buffer is None):
                        if not (Point(c[0], c[1]).intersects(section_line_buffer)):
                            highlighted = False

                    graph_vertices += [ c ]

                l = len(graph_vertices)
                if 2 <= l:
                    graph_indices += [l - 2, l - 1]

                if highlighted:
                    highlight_indices += [ len(graph_vertices) - 2, len(graph_vertices) - 1 ]
            except Exception as e:
                pass


        # print graph_vertices, graph_indices
        self.viewer3d.updateGraph(graph_vertices, graph_indices, highlight_indices)



        if len(self.viewer3d.polygons_vertices) == 0:
            logging.info('Rebuild polygons!')
            self.viewer3d.polygons_colors = []
            for layer in section_layers:
                color = [1, 0, 0, 1] if section_layers.index(layer) == 0 else [0, 0, 1, 1]
                if not layer is None:
                    v = self.export_polygons_impl(graphLayer, layer)
                    self.viewer3d.polygons_vertices += v

                    for i in range(0, len(v)):
                        self.viewer3d.polygons_colors += [color]


        # if not section_line_buffer is None:
        #     polygon = PolygonLayerProjection.buildPolygon(self.__section.section, graphLayer, section_line_buffer, with_projection=False)

        #     vertices = []
        #     indices = []

        #     for geom in polygon.geoms:
        #         for c in geom.exterior.coord:
        #             indices += len(vertices)
        #             vertices += [c]

        #     print polygon

        self.viewer3d.scale_z = scale_z

        self.viewer3d.updateGL()

    def __create_polygon_projected_layer(self, layer):
        polygon_layer = QgsVectorLayer(
            "Polygon?crs={crs}&index=yes".format(
                crs=self.mapCanvas.mapSettings().destinationCrs().authid()
                ), layer.name() + "_polygon", "memory")

        polygon_layer.setReadOnly(True)

        # cpy attributes structure
        polygon_layer.dataProvider().addAttributes([layer.fields().field(f) for f in range(layer.fields().count())])
        polygon_layer.updateFields()
        # cpy style
        polygon_layer.setRendererV2(QgsSingleSymbolRendererV2(QgsFillSymbolV2()))
        return polygon_layer

    def __add_polygon_layer(self, layer, projected):
        if layer is None:
            return

        if not(layer is self.graphLayerHelper.layer() or layer is self.subGraphLayerHelper.layer()):
            return


        polygon_layer = self.__create_polygon_projected_layer(layer)

        section_id = projected.customProperty("section_id")
        # Do not tag as projected_layer here, so it's not added twice
        polygon_layer.setCustomProperty("polygon_projected_layer", layer.id())
        polygon_layer.setCustomProperty("section_id", section_id)

        QgsMapLayerRegistry.instance().addMapLayer(polygon_layer)

        group = self.__iface.layerTreeView().layerTreeModel().rootGroup().findGroup(section_id)
        assert not(group is None)
        group.addLayer(polygon_layer)
        logging.debug('register polygon!')
        self.__section.section.register_projection_layer(PolygonLayerProjection(layer, polygon_layer, self))

    def add_layers(self, layers):
        self.graphLayerHelper.lookup(layers)
        self.subGraphLayerHelper.lookup(layers)

        for layer in layers:
            if hasattr(layer, 'customProperty') \
                    and layer.customProperty("section_id") is not None \
                    and layer.customProperty("section_id") == self.__section.section.id :
                source_layer = projected_layer_to_original(layer, "polygon_projected_layer")
                if source_layer is not None:
                    l = PolygonLayerProjection(source_layer, layer, self)
                    self.__section.section.register_projection_layer(l)
                    l.apply(self.__section.section, True)

    def __add_layer(self):
        # popup selection widget
        CreateLayerWidget(self.__logger).exec_()

    def __import_csv(self):
        data_layer = self.mapCanvas.currentLayer()
        if data_layer is None:
            return

        dialog = QProgressDialog("Importing features", "Cancel", 0, data_layer.featureCount(), self)
        self.importer = ConvertDataLayer(data_layer, dialog)
        dialog.finished.connect(self.__reset_import)
        dialog.finished.connect(self.__reset_import)
        self.importer.tick()

    def __reset_import(self, value):
        logging.warning('finished', value)
        self.importer = None


    def __toggle_edit_graph(self, checked):
        if checked:
            self.edit_graph_tool.activate();
            self.previousTool = self.__section.canvas.mapTool()
            self.__section.canvas.setMapTool(self.edit_graph_tool)
        else:
            self.edit_graph_tool.deactivate();
            self.__section.canvas.setMapTool(self.previousTool)

class Plugin():
    def __init__(self, iface):
        logging.basicConfig(level=logging.DEBUG)

        self.__iface = iface

    def cleanup_data(self):
        if self.graphLayerHelper.layer() is None:
            return

        self.graphLayerHelper.layer().beginEditCommand('edges geom')
        # Store invalid graph elements for removal
        edge_removed = []
        for edge in self.graphLayerHelper.layer().getFeatures():
            try:
                lid = edge.attribute("layer")
                layer = QgsMapLayerRegistry.instance().mapLayer(lid)
                featA = layer.getFeatures(QgsFeatureRequest(edge.attribute("start"))).next()
                featB = layer.getFeatures(QgsFeatureRequest(edge.attribute("end"))).next()

                # update geometry
                self.graphLayerHelper.layer().dataProvider().changeGeometryValues({edge.id(): QgsGeometry.fromWkt(GraphEditTool.segmentGeometry(featA, featB).wkt)})

            except Exception as e:
                print e
                # invalid data -> removing
                edge_removed += [ edge.id() ]

        self.graphLayerHelper.layer().endEditCommand()

        if edge_removed:
            res = QMessageBox().information(self.toolbar, 'Graph cleanup', 'Will remove {} graph edge(s)'.format(len(edge_removed)), QMessageBox.Ok | QMessageBox.Cancel)

            if res == QMessageBox.Ok:
                self.graphLayerHelper.layer().beginEditCommand('edges cleanup')
                self.graphLayerHelper.layer().dataProvider().deleteFeatures(edge_removed)
                self.graphLayerHelper.layer().endEditCommand()

    def graph_layer_tagged(self, graph):
        self.edit_graph_tool.set_graph_layer(graph)

    def __update_3d_combo(self, layers):
        rpix = QPixmap(100,100)
        rpix.fill(QColor("red"))
        bpix = QPixmap(100,100)
        bpix.fill(QColor("blue"))

        red = QIcon(rpix)
        blue = QIcon(bpix)

        for combo in self.viewer3d_combo:
            for layer in layers:
                if not layer.customProperty('section_id') is None:
                    continue
                if layer == self.graphLayerHelper.layer() or layer == self.subGraphLayerHelper.layer():
                    continue
                if not layer.isSpatial():
                    continue

                combo.addItem(red if self.viewer3d_combo.index(combo) == 0 else blue, layer.name(), layer.id())


    def display_graph_polygons(self):
        section_layers = []
        for combo in self.viewer3d_combo:
            lid = combo.itemData(combo.currentIndex())
            section_layers += [QgsMapLayerRegistry.instance().mapLayer(lid)]

        self.toolbar.updateGraph(section_layers, float(self.viewer3d_scale_z.text()))

    def on_graph_modified(self):
        logging.info('on_graph_modified')
        self.viewer3d.polygons_vertices = []
        self.display_graph_polygons()
        self.__section_main.canvas.refresh()
        self.__iface.mapCanvas().refresh()

    def initGui(self):
        self.__section_main = MainWindow(self.__iface, 'section')
        self.__dock = QDockWidget('Section')
        self.__dock.setWidget(self.__section_main)

        # self.__legend_dock = QDockWidget('Section Legend')
        # self.__legend_dock.setWidget(self.__section_main.tree_view)

        self.viewer3d = Viewer3D()

        self.graphLayerHelper = GraphLayerHelper("graph_layer")
        self.subGraphLayerHelper = GraphLayerHelper("sub_graph_layer")

        self.toolbar = DataToolbar(self.__iface, self.__section_main, self.viewer3d, self.graphLayerHelper, self.subGraphLayerHelper)
        self.edit_graph_tool = GraphEditTool(self.__section_main.canvas)
        self.select_graph_tool = SelectionTool(self.__section_main.canvas)

        self.__section_main.toolbar.line_clicked.connect(self.edit_graph_tool._reset)
        self.__section_main.toolbar.line_clicked.connect(self.display_graph_polygons)
        self.edit_graph_tool.graph_modified.connect(self.on_graph_modified)
        self.graphLayerHelper.graph_layer_tagged.connect(self.graph_layer_tagged)


        self.toolbar.addAction('Clean graph').triggered.connect(self.cleanup_data)

        # in case we are reloading
        self.toolbar.add_layers(QgsMapLayerRegistry.instance().mapLayers().values())

        # self.__section_main.section.section_layer_modified.connect(self.__update_graphs_geometry)
        self.__iface.addToolBar(self.toolbar)
        self.viewer3d_dock = QDockWidget('3d View')
        self.viewer3d_window = QMainWindow(None)
        self.viewer3d_window.setWindowFlags(Qt.Widget)
        self.viewer3d_toolbar = QToolBar()
        self.viewer3d_window.addToolBar(Qt.TopToolBarArea, self.viewer3d_toolbar)
        self.viewer3d_window.setCentralWidget(self.viewer3d)
        self.viewer3d_dock.setWidget(self.viewer3d_window)
        self.viewer3d_scale_z = QLineEdit("3.0")
        self.viewer3d_scale_z.setMaximumWidth(50)

        self.viewer3d_combo = [QComboBox(), QComboBox()]
        for combo in self.viewer3d_combo:
            combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            combo.addItem('-', None)
            self.viewer3d_toolbar.addWidget(combo)

        self.viewer3d_toolbar.addWidget(self.viewer3d_scale_z)
        self.viewer3d_toolbar.addAction(QgsApplication.getThemeIcon('/mActionDraw.svg'), 'refresh').triggered.connect(self.on_graph_modified)

        QgsMapLayerRegistry.instance().layersAdded.connect(self.__update_3d_combo)
        self.__update_3d_combo(QgsMapLayerRegistry.instance().mapLayers().values())


        self.__iface.addDockWidget(Qt.BottomDockWidgetArea, self.viewer3d_dock)

        section_actions = self.__section_main.canvas.build_default_section_actions()
        section_actions += [
            None,
            { 'icon': icon('10_edit_graph.svg'), 'label': 'edit graph layer', 'tool': self.edit_graph_tool, 'precondition': lambda action: self.__toggle_edit_graph_precondition_check() },
            { 'icon': icon('12_add_graph.svg'), 'label': 'create subgraph', 'clicked': self.__create_subgraph, 'precondition': lambda action: self.__create_subgraph_precondition_check() },
            { 'icon': icon('11_add_generatrices.svg'), 'label': 'add generatrices', 'clicked': self.__add_generatrices, 'precondition': lambda action: self.__add_generatrices_precondition_check() },
            { 'icon': icon('13_maj_graph.svg'), 'label': 'update graphs geom', 'clicked': self.__update_graphs_geometry, 'precondition': lambda action: self.__update_graphs_geometry_precondition_check() },
            None,
            { 'label': 'reset subgraph|gen.', 'clicked': self.__reset_subgraph, 'precondition': lambda action: self.__reset_subgraph_precondition_check() },

        ]

        self.generatrice_distance = QLineEdit("25")
        self.generatrice_distance.setMaximumWidth(50)
        self.__section_main.toolbar.addWidget(QLabel("Generatrice dist.:"))
        self.__section_main.toolbar.addWidget(self.generatrice_distance)

        self.__section_main.canvas.add_section_actions_to_toolbar(section_actions, self.__section_main.toolbar)

        self.__iface.addDockWidget(Qt.BottomDockWidgetArea, self.__dock)
        # self.__iface.addDockWidget(Qt.LeftDockWidgetArea, self.__legend_dock)

    def unload(self):
        # self.__section_main.section.section_layer_modified.disconnect(self.__update_graphs_geometry)
        self.__section_main.toolbar.line_clicked.disconnect(self.edit_graph_tool._reset)
        self.__section_main.toolbar.line_clicked.disconnect(self.display_graph_polygons)
        QgsMapLayerRegistry.instance().layersAdded.disconnect(self.__update_3d_combo)

        self.__dock.setWidget(None)
        # self.__legend_dock.setWidget(None)
        self.__iface.removeDockWidget(self.__dock)
        # self.__iface.removeDockWidget(self.__legend_dock)
        self.__section_main.unload()
        self.toolbar.setParent(None)
        self.toolbar.cleanup()
        self.toolbar = None
        self.viewer3d_dock.setParent(None)
        self.__section_main = None

    def __reset_subgraph_precondition_check(self):
        if self.subGraphLayerHelper.layer() is None:
            return (False, 'Missing subgraph')
        if not self.__section_main.section.is_valid:
            return (False, "No active section")
        return (True, "")

    def __reset_subgraph(self):
        # remove everything in subgraph for this section
        subgraph = self.subGraphLayerHelper.layer()
        projected_subgraph = filter(lambda l: (not isinstance(l, PolygonLayerProjection)), self.__section_main.section.projections_of(subgraph.id()))[0].projected_layer

        to_remove = []
        for segment in projected_subgraph.getFeatures():
            logging.debug('FOUND SEGMENT {}'.format(segment.id()))
            to_remove += [projected_feature_to_original(subgraph, segment).id()]

        logging.debug('REMOVE: {}'.format(to_remove))
        if len(to_remove) > 0:
            subgraph.dataProvider().deleteFeatures(to_remove)
            self.__section_main.section.update_projections(subgraph.id())
            self.__section_main.section.request_canvas_redraw()


        layer = self.__iface.mapCanvas().currentLayer()
        if layer is None:
            return

        if layer.customProperty("section_id") is None:
            return

        # if active layer is a projection try to remove fake generatrice
        source = projected_layer_to_original(layer)

        fakes = fg_fake_generatrices(source, layer)
        to_remove = []
        for f in fakes:
            logging.debug('FOUND GENERATRICE {}'.format(f.id()))
            to_remove += [projected_feature_to_original(source, f).id()]

        logging.debug('REMOVE2: {}'.format(to_remove))
        if len(to_remove) > 0:
            source.dataProvider().deleteFeatures(to_remove)
            self.__section_main.section.update_projections(source.id())
            self.__section_main.section.request_canvas_redraw()



    def __update_graphs_geometry_precondition_check(self):
        if not self.__section_main.section.is_valid:
            return (False, "No active section")
        return (True, "")

    def __update_graphs_geometry(self):

        targets = [self.graphLayerHelper.layer(), self.subGraphLayerHelper.layer()]

        for target in targets:
            if target is None:
                continue

            attr = ['start', 'end'] if target.fields().fieldNameIndex('start') >= 0 else ['start:Integer64(10,0)', 'end:Integer64(10,0)']

            target.beginEditCommand('update segment geom')

            for segment in target.getFeatures():
                layer_id =  segment.attribute('layer')
                layer = QgsMapLayerRegistry.instance().mapLayer(layer_id)
                featA = layer.getFeatures(QgsFeatureRequest(segment.attribute(attr[0]))).next()
                featB = layer.getFeatures(QgsFeatureRequest(segment.attribute(attr[1]))).next()
                target.dataProvider().changeGeometryValues({segment.id(): QgsGeometry.fromWkt(GraphEditTool.segmentGeometry(featA, featB).wkt)})

            target.endEditCommand()
            target.updateExtents()

    # def __update_graphs_geometry(self, layer):
    #     if not self.__section_main.section.is_valid:
    #         return
    #     edit = layer.projected_layer.editBuffer()
    #     if edit is None:
    #         return
    #     print ">>>>>>> {} will commit changes".format(layer.projected_layer.id())

    #     targets = [self.graphLayerHelper.layer(), self.subGraphLayerHelper.layer()]

    #     for id_ in edit.changedGeometries():
    #         f = layer.projected_layer.getFeatures(QgsFeatureRequest(id_)).next()
    #         print f, f.id()
    #         print f.attributes()
    #         f.setFields(layer.projected_layer.fields(), False)
    #         print layer.projected_layer.fields().allAttributesList()
    #         print f.attributes()
    #         my_id = f.attribute('id') if layer.projected_layer.fields().fieldNameIndex('id') >= 0 else f.attribute('id:Integer64(10,0)')
    #         query = u"attribute($currentfeature, 'start') = {} OR attribute($currentfeature, 'end') = {}".format(my_id, my_id)

    #         for target in targets:
    #             if target is None:
    #                 continue
    #             target.beginEditCommand('update segment geom')

    #             # lookup every segment with start|end == i
    #             segments = target.getFeatures(QgsFeatureRequest().setFilterExpression(query))

    #             print 'ICI >'
    #             print 'query', query
    #             for segment in segments:
    #                 print target.id(), segment
    #                 featA = layer.getFeatures(QgsFeatureRequest(segment.attribute('start'))).next()
    #                 featB = layer.getFeatures(QgsFeatureRequest(segment.attribute('end'))).next()

    #                 layer.changeGeometry(segment.id(), QgsGeometry.fromWkt(GraphEditTool.segmentGeometry(featA, featB).wkt))
    #             print 'ICI <'

    #             target.endEditCommand()
    #             target.updateExtents()

    def __toggle_edit_graph_precondition_check(self):
        if not self.__section_main.section.is_valid:
            return (False, "No active section line")
        if self.graphLayerHelper.layer() is None:
            return (False, "No graph layer")
        layer = self.__iface.mapCanvas().currentLayer()
        if layer is None:
            self.edit_graph_tool._reset()
            return (False, "No active layer")
        if layer.customProperty("section_id") is None:
            self.edit_graph_tool._reset()
            return (False, "Active layer must be a projection")

        return (True, "")

    def __add_generatrices_precondition_check(self):
        layer = self.__iface.mapCanvas().currentLayer()

        if layer is None:
            return (False, "No active layer")
        if not self.__section_main.section.is_valid:
            return (False, "No active section line")
        source_layer = projected_layer_to_original(layer)
        if source_layer is None:
            return (False, "Active layer must be a projection")
        if self.graphLayerHelper.layer() is None and self.subGraphLayerHelper.layer() is None:
            return (False, "No (sub)graph layer")
        return (True, "")

    def __create_subgraph_precondition_check(self):
        if not self.__section_main.section.is_valid:
            return (False, "No active section line")
        graphLayer = self.graphLayerHelper.layer()
        if self.graphLayerHelper.layer() is None:
            return (False, "No graph layer")
        if self.subGraphLayerHelper.layer() is None:
            return (False, "No subgraph layer")
        proj = self.__iface.mapCanvas().currentLayer()
        if proj is None:
            return (False, "No active layer")
        if proj.customProperty("section_id") != self.__section_main.section.id:
            return (False, "Active layer isn't a projection of section")

        projected_graph = filter(lambda l: (not isinstance(l, PolygonLayerProjection)), self.__section_main.section.projections_of(graphLayer.id()))[0]
        if projected_graph is None:
            return (False, "Missing graph projection")

        # current layer = mineralised
        source_layer = projected_layer_to_original(proj)
        if source_layer is None:
            return (False, "Active layer isn't a projection of section")
        return (True, "")

    def __add_generatrices(self):
        try:
            # disable updates for 2 reasons:
            #  - perf
            #  - projected layer content won't change during update
            self.__section_main.section.disable()

            if not self.graphLayerHelper.layer() is None:
                logging.info('Add generatrices for graph')
                self.__add_generatrices_impl(self.graphLayerHelper.layer())

            if not self.subGraphLayerHelper.layer() is None:
                logging.info('Add generatrices for subgraph')
                self.__add_generatrices_impl(self.subGraphLayerHelper.layer())

        finally:
            self.__section_main.section.enable()
            if not self.graphLayerHelper.layer() is None:
                self.__section_main.section.update_projections(self.graphLayerHelper.layer().id())
            if not self.subGraphLayerHelper.layer() is None:
                self.__section_main.section.update_projections(self.subGraphLayerHelper.layer().id())

            layer = self.__iface.mapCanvas().currentLayer()
            source_layer = projected_layer_to_original(layer)
            self.__section_main.section.update_projections(source_layer.id())

            self.on_graph_modified()


    def __add_generatrices_impl(self, graph):
        layer = self.__iface.mapCanvas().currentLayer()

        if layer is None or not self.__section_main.section.is_valid:
            return

        source_layer = projected_layer_to_original(layer)
        if source_layer is None:
            return

        projected_graph = filter(lambda l: (not isinstance(l, PolygonLayerProjection)), self.__section_main.section.projections_of(graph.id()))[0].projected_layer

        ids = graph.uniqueValues(graph.fieldNameIndex('id'))
        my_id = (max(ids) if len(ids) > 0 else 0) + 1

        ids = source_layer.uniqueValues(source_layer.fieldNameIndex('id'))
        my_fake_id = (max(ids) if len(ids) > 0 else 0) + 1

        has_field_HoleID = layer.fields().fieldNameIndex("HoleID") >= 0
        has_field_mine = layer.fields().fieldNameIndex("mine") >= 0
        has_field_mine_str = layer.fields().fieldNameIndex("mine:Integer64(10,0)") >= 0

        distance = float(self.generatrice_distance.text())
        # Compute fake generatrice translation
        a = self.__section_main.section.unproject_point(distance, 0, 0)
        b = self.__section_main.section.unproject_point(0, 0, 0)
        translation_vec = tuple([a[i]-b[i] for i in range(0, 2)])

        query = QgsFeatureRequest().setFilterExpression (u'"layer" = "{0}"'.format(source_layer.id()))

        graph_attr = ['start', 'end'] if projected_graph.fields().fieldNameIndex('start') >= 0 else ['start:Integer64(10,0)', 'end:Integer64(10,0)']


        # First get a list of source features ids
        # so if we modify
        interesting_source_features = []
        centroids = []
        for feature in layer.getFeatures():
            if has_field_HoleID and feature.attribute("HoleID") == "Fake":
                continue
            if has_field_mine and feature.attribute("mine") == -1:
                continue
            if has_field_mine_str and feature.attribute("mine:Integer64(10,0)") == -1:
                continue

            interesting_source_features += [projected_feature_to_original(source_layer, feature)]
            centroids += [feature.geometry().centroid().asPoint()]


        graph.beginEditCommand('update edges')

        # Browse features in projected layer
        for source_feature in interesting_source_features:
            feature_idx = interesting_source_features.index(source_feature)
            source_id = source_feature.id()
            connected_edges = {'L':[], 'R':[]}
            logging.debug('bla {}'.format(source_id))


            # Lookup all edges connected to this feature
            for edge in projected_graph.getFeatures():
                if edge.attribute('layer') != source_layer.id():
                    continue
                # logging.debug('attr {}'.format(edge.attributes()))

                if edge.attribute(graph_attr[0]) == source_id or edge.attribute(graph_attr[1]) == source_id:
                    # edge is connected, check direction
                    centroid = centroids[feature_idx]
                    p = edge_center = edge.geometry().centroid().asPoint()
                    # p = self.__section_main.section.project_point(edge_center.x(), edge_center.y(), 0)

                    # if feature is to the left of the projected edge
                    if centroid.x() < p[0]:
                        connected_edges['R'] += [edge]
                        logging.debug('R edges += {}'.format(edge.id()))
                    else:
                        connected_edges['L'] += [edge]
                        logging.debug('L edges += {}'.format(edge.id()))

            logging.debug('connected {}|{}'.format(len(connected_edges['L']), len(connected_edges['R'])))
            # Now that we know all connected edges, we can create fake generatrices...

            # If this feature is connected on one side only -> add the missing generatrice on the other side
            if xor(len(connected_edges['L']) == 0, len(connected_edges['R']) == 0):
                missing_side = 1.0 if len(connected_edges['R']) == 0 else -1.0

                generatrice = fg_create(self.__section_main.section, source_layer, source_feature, my_fake_id, translation_vec, missing_side)
                # Read back feature to get proper id()
                fake_feature = fg_insert(source_layer, generatrice)
                # Add link in subgraph
                fg_connect(graph, source_feature, fake_feature, my_id, source_layer)

                my_fake_id = my_fake_id + 1
                my_id = my_id + 1
            elif len(connected_edges['L']) == 0 and len(connected_edges['R']) == 0 and source_id in source_layer.selectedFeaturesIds():
                for d in [-1.0, 1.0]:
                    generatrice = fg_create(self.__section_main.section, source_layer, source_feature, my_fake_id, translation_vec, d)
                    # Read back feature to get proper id()
                    fake_feature = fg_insert(source_layer, generatrice)
                    # Add link in subgraph
                    fg_connect(graph, source_feature, fake_feature, my_id, source_layer)
                    my_fake_id = my_fake_id + 1
                    my_id = my_id + 1

            # If this feature is connected to N (> 1) elements on 1 side -> add 1 fake generatrices
            for side in connected_edges:

                if len(connected_edges[side]) > 1:
                    missing_side = 1.0 if side == 'R' else -1.0
                    logging.debug('jambe pantalon {}'.format(missing_side))
                    # Hardcode 60cm fake generatrice distance
                    scale_factor = 0.6 / distance
                    generatrice = fg_create(self.__section_main.section, source_layer, source_feature, my_fake_id, [d * scale_factor for d in translation_vec], missing_side)
                    fake_feature = fg_insert(source_layer, generatrice)
                    fg_connect(graph, source_feature, fake_feature, my_id, source_layer)

                    logging.debug('added fake feature {}|{}'.format(fake_feature.id(), my_fake_id))
                    my_fake_id = my_fake_id + 1
                    my_id = my_id + 1


                    # Modify existing edges
                    for edge in connected_edges[side]:
                        attr = edge.attributes()

                        for field in range(0, len(graph_attr)):
                            index = edge.fieldNameIndex(graph_attr[field])

                            if attr[index] == source_feature.id():
                                index2 = edge.fieldNameIndex(graph_attr[1 - field])
                                other = attr[index2]

                                logging.debug('replace {} -> {}'.format(attr[index], fake_feature.id()))
                                fg_connect(graph, fake_feature, source_layer.getFeatures(QgsFeatureRequest(other)).next(), my_id, source_layer)

                        my_id = my_id + 1

                    logging.debug('Remove deprecated links {}'.format([projected_feature_to_original(graph, f).id() for f in connected_edges[side]]))
                    graph.dataProvider().deleteFeatures([projected_feature_to_original(graph, f).id() for f in connected_edges[side]])
        graph.endEditCommand()



    def __create_subgraph(self):
        graphLayer = self.graphLayerHelper.layer()
        subGraphLayer = self.subGraphLayerHelper.layer()
        proj = self.__iface.mapCanvas().currentLayer()

        if proj is None or graphLayer is None or subGraphLayer is None:
            return

        if not self.__section_main.section.is_valid:
            return

        if proj.customProperty("section_id") != self.__section_main.section.id:
            return

        projected_graph = filter(lambda l: (not isinstance(l, PolygonLayerProjection)), self.__section_main.section.projections_of(graphLayer.id()))[0]

        # current layer = mineralised
        source_layer = projected_layer_to_original(proj)

        logging.debug(source_layer)
        if source_layer is None or projected_graph is None:
            return

        features = []

        ids = subGraphLayer.uniqueValues(subGraphLayer.fieldNameIndex('id'))
        my_id = (max(ids) if len(ids) > 0 else 0) + 1



        # for each selected edge of the graph
        for edge in projected_graph.projected_layer.getFeatures():
            edge.setFields(graphLayer.fields(), False)
            layer = QgsMapLayerRegistry.instance().mapLayer(edge.attribute("layer"))
            start = layer.getFeatures(QgsFeatureRequest(edge.attribute("start"))).next()
            end = layer.getFeatures(QgsFeatureRequest(edge.attribute("end"))).next()


            # select all features of source_layer intersecting 'start'
            s = source_layer.getFeatures(QgsFeatureRequest(start.geometry().boundingBox()))
            # select all features of source_layer intersecting 'end'
            e = source_layer.getFeatures(QgsFeatureRequest(end.geometry().boundingBox()))

            for a in s:
                e = source_layer.getFeatures(QgsFeatureRequest(end.geometry().boundingBox()))
                for b in e:
                    req = QgsFeatureRequest().setFilterExpression (u'"start" = {0} AND "end" = {1}'.format(a.id(), b.id()))
                    # don't recreate an existing link
                    if len(list(graphLayer.getFeatures(req))) > 0:
                        continue

                    features += [ GraphEditTool.createSegmentEdge(a, b, my_id, subGraphLayer.fields(), source_layer.id()) ]
                    my_id = my_id + 1

        if len(features) > 0:
            subGraphLayer.beginEditCommand('subgraph creation')
            subGraphLayer.dataProvider().addFeatures(features)
            subGraphLayer.endEditCommand()
            subGraphLayer.updateExtents()


class ConvertDataLayer():
    def __init__(self, data_layer, dialog):
        self.dialog = dialog
        self.data_layer = data_layer
        self.new_layer = QgsVectorLayer(
            "LineString?&index=yes".format(
                data_layer.crs().authid()
                ), data_layer.name(), "memory")

        fields = [data_layer.fields().field(f) for f in range(data_layer.fields().count())]
        fields += [QgsField("id", QVariant.Int)]
        self.new_layer.dataProvider().addAttributes(fields)
        self.new_layer.updateFields()

        self.my_id = 0
        self.features = data_layer.getFeatures()
        QgsMapLayerRegistry.instance().addMapLayer(self.new_layer)


    def tick(self):
        logging.debug('TICK')
        features = []
        for f in self.features:
            p1 = (f.attribute('From X'), f.attribute('From Y'), f.attribute('From Z'))
            p2 = (f.attribute('To X'), f.attribute('To Y'), f.attribute('To Z'))
            geom = LineString([p1, p2])
            new_feature = QgsFeature()
            new_feature.setGeometry(QgsGeometry.fromWkt(geom.wkt.replace(' Z', 'Z')))

            attrs = f.attributes()
            attrs += [self.my_id]
            new_feature.setAttributes(attrs)
            self.my_id = self.my_id + 1
            features += [new_feature]

            self.dialog.setValue(self.my_id)

            if len(features) == 1000:
                break

        self.new_layer.beginEditCommand('layer creation')
        self.new_layer.dataProvider().addFeatures(features)
        self.new_layer.endEditCommand()
        self.new_layer.updateExtents()

        if self.dialog.wasCanceled():
            pass
        elif self.features.isClosed():
            pass
        else:
            self.timer = QTimer.singleShot(0, self.tick)
