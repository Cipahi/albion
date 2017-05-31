# coding: utf-8

import sys
import logging
import traceback

from qgis.core import (QgsVectorLayer,
                       QgsLayerTreeLayer,
                       QgsMapLayerRegistry,
                       QgsPluginLayerRegistry, 
                       QgsProject,
                       QgsLayerTreeLayer)

from qgis.gui import QgsMapToolEmitPoint

from PyQt4.QtCore import (Qt, QObject)
from PyQt4.QtGui import (QMenu, 
                         QToolBar, 
                         QInputDialog, 
                         QLineEdit, 
                         QFileDialog, 
                         QProgressBar,
                         QComboBox,
                         QApplication)

import psycopg2 



from .axis_layer import AxisLayer, AxisLayerType
from .action_state_helper import ActionStateHelper

from pglite import start_cluster, stop_cluster, init_cluster, check_cluster, cluster_params
import atexit
import os
import time

from .load import load_file
from .utils import icon

AXIS_LAYER_TYPE = AxisLayerType()
QgsPluginLayerRegistry.instance().addPluginLayerType(AXIS_LAYER_TYPE)


@atexit.register
def unload_axi_layer_type():
    QgsPluginLayerRegistry.instance().removePluginLayerType(
        AxisLayer.LAYER_TYPE)

class Plugin(QObject):
    def __init__(self, iface):
        QObject.__init__(self, None)
        FORMAT = '\033[30;100m%(created)-13s\033[0m \033[33m%(filename)-12s\033[0m:\033[34m%(lineno)4d\033[0m %(levelname)8s %(message)s' if sys.platform.find('linux')>= 0 else '%(created)13s %(filename)-12s:%(lineno)4d %(message)s'
        lvl = logging.INFO if sys.platform.find('linux')>= 0 else logging.CRITICAL
        logging.basicConfig(format=FORMAT, level=lvl)

        self.__iface = iface
        self.__project = None
        self.__menu = None
        self.__toolbar = None
        self.__click_tool = None
        self.__previous_tool = None
        self.__select_current_section_action = None
        self.__current_graph = QComboBox()
        self.__current_graph.setMinimumWidth(150)
        self.__axis_layer = None

        if not check_cluster():
            init_cluster()
        start_cluster()

    def initGui(self):
        self.__menu = QMenu("Albion")
        self.__menu.addAction('New &Project').triggered.connect(self.__new_project)
        self.__menu.addSeparator()
        self.__menu.addAction('Refresh Grid Cells')
        self.__menu.addSeparator()
        self.__menu.addAction('&Import Data').triggered.connect(self.__import_data)
        self.__menu.addAction('Compute &Mineralization')
        self.__menu.addSeparator()
        self.__menu.addAction('New &Graph').triggered.connect(self.__new_graph)
        self.__menu.addAction('&Fix Current Graph')
        self.__menu.addAction('Delete Graph').triggered.connect(self.__delete_graph)
        self.__menu.addSeparator()
        self.__menu.addAction('&Export Project')
        self.__menu.addAction('Import Project')
        self.__menu.addAction('Reset QGIS Project').triggered.connect(self.__reset_qgis_project)
        self.__menu.addAction('Export sections').triggered.connect(self.__export_sections)
        self.__menu.addAction('Export volume').triggered.connect(self.__export_volume)
        self.__menu.addSeparator()
        self.__menu.addAction('Auto graph').triggered.connect(self.__auto_graph)
        self.__menu.addAction('Extend all sections').triggered.connect(self.__extend_all_sections)
        self.__menu.addAction('Toggle axis').triggered.connect(self.__toggle_axis)
        
        self.__iface.mainWindow().menuBar().addMenu(self.__menu)

        self.__toolbar = QToolBar('Albion')
        self.__iface.mainWindow().addToolBar(self.__toolbar)
        self.__select_current_section_action = self.__toolbar.addAction(icon('select_line.svg'), 'select section')
        self.__select_current_section_action.setCheckable(True)
        self.__select_current_section_action.triggered.connect(self.__select_current_section)

        self.__toolbar.addAction(icon('previous_line.svg'), 'previous section').triggered.connect(self.__select_previous_section)
        self.__toolbar.addAction(icon('next_line.svg'), 'next section').triggered.connect(self.__select_next_section)
        self.__toolbar.addWidget(self.__current_graph)
        self.__toolbar.addAction(icon('auto_connect.svg'), 'auto connect').triggered.connect(self.__auto_connect)
        self.__toolbar.addAction(icon('auto_ceil_wall.svg'), 'auto ceil and wall').triggered.connect(self.__auto_ceil_wall)

        QgsProject.instance().readProject.connect(self.__qgis__project__loaded)
        self.__qgis__project__loaded() # case of reload

    def unload(self):
        self.__menu and self.__menu.setParent(None)
        self.__toolbar and self.__toolbar.setParent(None)
        stop_cluster()
        QgsProject.instance().readProject.disconnect(self.__qgis__project__loaded)

    def __qgis__project__loaded(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        self.__current_graph.clear()
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("select id from albion.graph")
        self.__current_graph.addItems([id_ for id_, in cur.fetchall()])
        con.close()

    def __reset_qgis_project(self):

        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return

        QgsMapLayerRegistry.instance().removeAllMapLayers()
   
        root = QgsProject.instance().layerTreeRoot()
        root.removeAllChildren()

        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        srid = QgsProject.instance().readEntry("albion", "srid", "")[0]
        
        for layer_name in reversed(['cell', 'formation', 'grid', 'hole', 
                'intersection_without_hole', 'collar', 'small_edge', 'close_point']):
            layer = QgsVectorLayer('{} sslmode=disable srid={} key="id" table="albion"."{}" (geom)'.format(conn_info, srid, layer_name), layer_name, 'postgres')
            QgsMapLayerRegistry.instance().addMapLayer(layer, False)
            node = QgsLayerTreeLayer(layer)
            root.addChildNode(node)
        
        layer = QgsVectorLayer('{} sslmode=disable srid={} key="id" table="(SELECT albion.current_section_id() as id, albion.current_section_geom() as geom)" (geom) sql='.format(conn_info, srid), 'current_section', 'postgres')
        QgsMapLayerRegistry.instance().addMapLayer(layer, False)
        node = QgsLayerTreeLayer(layer)
        root.addChildNode(node)

        section_group = root.insertGroup(0, "section")

        for layer_name in ['collar_section', 'formation_section', 'resistivity_section',
                'radiometry_section', 'node_section', 'edge_section']:
            layer = QgsVectorLayer('{} sslmode=disable srid={} key="id" table="albion"."{}" (geom)'.format(conn_info, srid, layer_name), layer_name, 'postgres')
            QgsMapLayerRegistry.instance().addMapLayer(layer, False)
            node = QgsLayerTreeLayer(layer)
            section_group.addChildNode(node)

         #self.__axis_layer = AxisLayer(self.__iface.mapCanvas().mapSettings().destinationCrs())
         #QgsMapLayerRegistry.instance().addMapLayer(self.__axis_layer)

        self.__iface.actionSaveProject().trigger()

    def __new_project(self):

        # @todo open dialog to configure project name and srid
        project_name, ok = QInputDialog.getText(self.__iface.mainWindow(),
                "Project name",
                 "Project name (no space, no caps, ascii only):", QLineEdit.Normal,
                 'test_project')
        if not ok:
            return

        srid, ok = QInputDialog.getText(self.__iface.mainWindow(),
                "Project SRID",
                 "Project SRID EPSG:", QLineEdit.Normal,
                 '32632')

        if not ok:
            return

        self.__iface.newProject()

        srid = int(srid)

        self.__iface.messageBar().pushInfo('Albion:', "creating project...")
       
        QgsProject.instance().writeEntry("albion", "project_name", project_name)
        QgsProject.instance().writeEntry("albion", "srid", srid)
        conn_info = "dbname={} {}".format(project_name, cluster_params())
        QgsProject.instance().writeEntry("albion", "conn_info", conn_info)

        con = psycopg2.connect("dbname=postgres {}".format(cluster_params()))
        cur = con.cursor()
        con.set_isolation_level(0)
        cur.execute("select pg_terminate_backend(pg_stat_activity.pid) \
                    from pg_stat_activity \
                    where pg_stat_activity.datname = '{}'".format(project_name))
        cur.execute("drop database if exists {}".format(project_name))
        cur.execute("create database {}".format(project_name))
        con.commit()
        con.close()
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("create extension postgis")
        cur.execute("create extension \"uuid-ossp\"")
        for file_ in ('_albion.sql', 'albion.sql'):
            for statement in open(os.path.join(os.path.dirname(__file__), file_)).read().split('\n;\n')[:-1]:
                cur.execute(statement.format(srid=srid))
        cur.execute("insert into albion.metadata(srid, snap_distance) select 32632, 2")
        con.commit()
        con.close()

        self.__reset_qgis_project()


    def __new_graph(self):

        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return

        graph, ok = QInputDialog.getText(self.__iface.mainWindow(),
                "Graph",
                 "Graph name:", QLineEdit.Normal,
                 'test_graph')

        if not ok:
            return

        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        srid = QgsProject.instance().readEntry("albion", "srid", "")[0]

        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("delete from albion.graph casacde where id='{}';".format(graph))
        cur.execute("insert into albion.graph(id) values ('{}');".format(graph))
        con.commit()
        con.close()

        self.__current_graph.addItem(graph)
        self.__current_graph.setCurrentIndex(self.__current_graph.findText(graph))

    def __delete_graph(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return

        graph, ok = QInputDialog.getText(self.__iface.mainWindow(),
                "Graph",
                 "Graph name:", QLineEdit.Normal,
                 '')

        if not ok:
            return

        for id_ in QgsMapLayerRegistry.instance().mapLayers():
            if id_.find(graph) != -1:
                QgsMapLayerRegistry.instance().removeMapLayer(id_)

        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        srid = QgsProject.instance().readEntry("albion", "srid", "")[0]

        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("delete from albion.graph casacde where id='{}';".format(graph))
        con.commit()
        con.close()
        self.__current_graph.removeItem(self.__current_graph.findText(graph))
        self.__refresh_layers()

    def __import_data(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return
        dir_ = QFileDialog.getExistingDirectory(None,
                        u"Data directory",
                        "" )
        if not dir_:
            return

        #@todo run the collar import, and then subprocess the rest to allow the user
        #      to edit the grid without waiting

        con = psycopg2.connect(QgsProject.instance().readEntry("albion", "conn_info", "")[0])
        cur = con.cursor()

        progressMessageBar = self.__iface.messageBar().createMessage("Loading {}...".format(dir_))
        progress = QProgressBar()
        progress.setMaximum(5)
        progress.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        progressMessageBar.layout().addWidget(progress)
        self.__iface.messageBar().pushWidget(progressMessageBar, self.__iface.messageBar().INFO)
        progress.setValue(0)
        for filename in os.listdir(dir_):
            if filename.find('collar') != -1:
                load_file(cur, os.path.join(dir_, filename))
                progress.setValue(1)

        for filename in os.listdir(dir_):
            if filename.find('devia') != -1:
                load_file(cur, os.path.join(dir_, filename))
                progress.setValue(2)

        for filename in os.listdir(dir_):
            if filename.find('formation') != -1 or filename.find('resi') != -1 or filename.find('avp') != -1:
                load_file(cur, os.path.join(dir_, filename))
                progress.setValue(progress.value() + 1)
        progress.setValue(6)
        self.__iface.messageBar().clearWidgets()

        con.commit()
        con.close()

        collar = QgsMapLayerRegistry.instance().mapLayersByName('collar')[0]
        collar.reload()
        collar.updateExtents()
        self.__iface.setActiveLayer(collar)
        QApplication.instance().processEvents()
        while self.__iface.mapCanvas().isDrawing():
            QApplication.instance().processEvents()
        self.__iface.zoomFull()

        self.__iface.actionSaveProject().trigger()

    def __select_current_section(self):
        #@todo switch behavior when in section view -> ortho
        self.__click_tool = QgsMapToolEmitPoint(self.__iface.mapCanvas())
        self.__iface.mapCanvas().setMapTool(self.__click_tool)
        self.__click_tool.canvasClicked.connect(self.__map_clicked)
        self.__select_current_section_action.setChecked(True)

    def __map_clicked(self, point, button):
        print(point, button)
        self.__select_current_section_action.setChecked(False)
        self.__click_tool.setParent(None)
        self.__click_tool = None

        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return
        

        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        srid = QgsProject.instance().readEntry("albion", "srid", "")[0]

        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("""update albion.metadata set current_section=(
                select id from albion.grid where st_dwithin(
                    geom, 'SRID={srid} ;POINT({x} {y})'::geometry, 5)
                limit 1
                )""".format(srid=srid, x=point.x(), y=point.y()))
        cur.execute("select st_extent(geom) from albion.collar_section")
        print('section extent ', cur.fetchone())
        con.commit()
        con.close()
        self.__refresh_layers()

    def __select_next_section(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("""update albion.metadata set current_section=albion.next_section()""")
        con.commit()
        con.close()
        self.__refresh_layers()

    def __select_previous_section(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]:
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("""update albion.metadata set current_section=albion.previous_section()""")
        con.commit()
        con.close()
        self.__refresh_layers()

    def __auto_connect(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0] \
                or not self.__current_graph.currentText():
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("""
                select albion.auto_connect('{}', albion.current_section_id())
                """.format(self.__current_graph.currentText()))
        con.commit()
        con.close()
        self.__refresh_layers()

    def __auto_ceil_wall(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0]\
                or not self.__current_graph.currentText():
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("""
                select albion.auto_ceil_and_wall('{}', albion.current_section_id())
                """.format(self.__current_graph.currentText()))
        con.commit()
        con.close()
        self.__refresh_layers()

    def __export_sections(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0] \
                or not self.__current_graph.currentText():
            return

        fil = QFileDialog.getSaveFileName(None,
                u"Export section",
                "",
                "Section files (*.obj, *.txt)")
        if not fil:
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()

        if fil[-4:] == '.txt':
            cur.execute("select albion.export_polygons('{}')".format(self.__current_graph.currentText()))
            open(fil, 'w').write(cur.fetchone()[0])
        elif fil[-4:] == '.obj':
            cur.execute("""
                select albion.to_obj(st_collectionhomogenize(st_collect(albion.triangulate_edge(ceil_, wall_)))) 
                from albion.edge 
                where graph_id='{}'
                """.format(self.__current_graph.currentText()))
            open(fil, 'w').write(cur.fetchone()[0])
        con.close()

    def __export_volume(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0] \
                or not self.__current_graph.currentText():
            return

        fil = QFileDialog.getSaveFileName(None,
                u"Export volume",
                "",
                "Surface files(*.obj)")
        if not fil:
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()

        if fil[-4:] == '.obj':
            progressMessageBar = self.__iface.messageBar().createMessage("Loading {}...".format(dir_))
            progress = QProgressBar()
            progress.setMaximum(7)
            progress.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
            progressMessageBar.layout().addWidget(progress)
            self.__iface.messageBar().pushWidget(progressMessageBar, self.__iface.messageBar().INFO)
            progress.setValue(0)
            cur.execute("refresh materialized  view albion.dense_grid")
            progress.setValue(1)
            cur.execute("refresh materialized  view albion.cell")
            progress.setValue(2)
            cur.execute("refresh materialized  view albion.triangle")
            progress.setValue(3)
            cur.execute("refresh materialized  view albion.projected_edge")
            progress.setValue(4)
            cur.execute("refresh materialized  view albion.cell_edge")
            progress.setValue(5)
            cur.execute("""
                select albion.to_obj(st_collectionhomogenize(st_collect(albion.elementary_volume('{}', id)))) 
                from albion.cell
                """.format(self.__current_graph.currentText()))
            progress.setValue(6)
            open(fil, 'w').write(cur.fetchone()[0])
            progress.setValue(7)
            self.__iface.messageBar().clearWidgets()
        con.commit()
        con.close()

    def __auto_graph(sel):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0] \
                or not self.__current_graph.currentText():
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("select albion.auto_graph('{}')".format(self.__current_graph.currentText()))
        con.commit()
        con.close()
        self.__refresh_layers()

    def __extend_all_sections(self):
        if not QgsProject.instance().readEntry("albion", "conn_info", "")[0] \
                or not self.__current_graph.currentText():
            return
        conn_info = QgsProject.instance().readEntry("albion", "conn_info", "")[0]
        con = psycopg2.connect(conn_info)
        cur = con.cursor()
        cur.execute("select albion.extend_to_interpolated('{}', id) from albion.grid".format(self.__current_graph.currentText()))
        con.commit()
        con.close()
        self.__refresh_layers()

    def __refresh_layers(self):
        for layer in self.__iface.mapCanvas().layers():
            layer.triggerRepaint()
        

    def __toggle_axis(self):
        if self.__axis_layer:
            pass
            QgsMapLayerRegistry.instance().removeMapLayer(self.__axis_layer.id())
            self.__axis_layer = None
        else:
            self.__axis_layer = AxisLayer(self.__iface.mapCanvas().mapSettings().destinationCrs())
            QgsMapLayerRegistry.instance().addMapLayer(self.__axis_layer)

