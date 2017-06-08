# -*- coding: UTF-8 -*-

import numpy
from OpenGL.GL import *
from OpenGL.GL import shaders

from PyQt4.QtGui import *
from PyQt4.QtCore import *

from .utility import computeNormals
import psycopg2
from shapely import wkb

class Scene(QObject):
    
    changed = pyqtSignal()

    def __init__(self, conn_info, graph_id, texture_binder, parent=None):
        super(Scene, self).__init__(parent)
        self.__zScale = 1. # must be one here
        self.shaderNeedRecompile = True
        self.__textureBinder = texture_binder


        print "fetch collar"
        self.graph_id = graph_id
        self.conn_info = conn_info
        con = psycopg2.connect(self.conn_info)
        cur = con.cursor()
        cur.execute("""
            select st_3dextent(geom)
            from albion.collar
            """)

        ext = cur.fetchone()[0].replace('BOX3D(','').replace(')','').split(',')
        ext = [[float(c) for c in ext[0].split()],[float(c) for c in ext[1].split()]]
        self.extent = (ext[0][0], ext[0][1], ext[1][0], ext[1][1])


        self.center = QVector3D(
                .5*(ext[0][0]+ext[1][0]), 
                .5*(ext[0][1]+ext[1][1]),
                .5*(ext[0][2]+ext[1][2]))

        self.vtx = None
        self.idx = None
        self.nrml = None

        self.hvtx = None
        self.hidx = None
        self.hnrml = None

        self.__display_labels = False
        self.__labels = []

        self.__display_holes = False
        self.__holes = []

        print "done"


        self.setZscale(self.__zScale)

        if graph_id:

            print "fetch nodes"

            cur.execute("""
                select hole_id, st_x(geom), st_y(geom), st_z(geom)
                from (select hole_id, st_startpoint(geom) as geom from albion.node where graph_id='{}' ) as t
                """.format(graph_id))

            for id_, x, y, z in cur.fetchall():
                scene = QGraphicsScene()
                scene.setSceneRect(scene.itemsBoundingRect())
                scene.addText(id_)#, QFont('Arial', 32))
                image = QImage(scene.sceneRect().size().toSize(), QImage.Format_ARGB32)
                image.fill(Qt.transparent)
                painter = QPainter(image)
                image.save('/tmp/test.png')
                scene.render(painter)
                del painter
                scat = {'point': [x,y,z], 'image': image}
                scat['texture'] = self.__textureBinder(scat['image'])
                self.__labels.append(scat)


            self.__holes = []
            cur.execute("""
                select geom from albion.node where graph_id='{}'
                """.format(graph_id))
            for geom, in cur.fetchall():
                line = numpy.require(wkb.loads(geom, True).coords, numpy.float32, 'C')
                self.__holes.append(line)

            print "fetch sections"
            cur.execute("""
                select st_collectionhomogenize(st_collect(triangulation))
                from albion.section where graph_id='{}'
                """.format(graph_id))
            res = cur.fetchone()[0]
            if res:
                geom = wkb.loads(res, True)
                self.vtx = numpy.require(numpy.array([tri.exterior.coords[:-1] for tri in geom]).reshape((-1,3)), numpy.float32, 'C')
                self.vtx[:,2] *= self.__zScale
                self.idx = numpy.require(numpy.arange(len(self.vtx)).reshape((-1,3)), numpy.int32, 'C')
                self.nrml = computeNormals(self.vtx, self.idx)

            print "fetch surfaces"

            cur.execute("""
                select st_collectionhomogenize(st_collect(triangulation)) 
                from albion.volume
                """.format(graph_id))
            res = cur.fetchone()[0]
            if res:
                geom = wkb.loads(res, True)
                self.hvtx = numpy.require(numpy.array([tri.exterior.coords[:-1] for tri in geom]).reshape((-1,3)), numpy.float32, 'C')
                self.hvtx[:,2] *= self.__zScale
                self.hidx = numpy.require(numpy.arange(len(self.hvtx)).reshape((-1,3)), numpy.int32, 'C')
                self.hnrml = computeNormals(self.hvtx, self.hidx)

        con.close()
        print "done"


    def rendergl(self, leftv, upv, eye, height, context):

        glEnable(GL_DEPTH_TEST)
        glLightModelfv(GL_LIGHT_MODEL_TWO_SIDE, GL_TRUE)
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [1., 0., 0., 1.])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [1., 0., 0., 1.])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [1., 1., 1., 1.])
        glMaterialf(GL_FRONT_AND_BACK, GL_SHININESS, 50)

        if self.shaderNeedRecompile:
            self.compileShaders()

        glEnableClientState(GL_VERTEX_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glEnableClientState(GL_NORMAL_ARRAY)

        glVertexPointerf(self.vtx)
        glNormalPointerf(self.nrml)
        glDrawElementsui(GL_TRIANGLES, self.idx)


        glVertexPointerf(self.hvtx)
        glNormalPointerf(self.hnrml)
        glDrawElementsui(GL_TRIANGLES, self.hidx)

        if self.__display_holes:
            glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION,  [1., 0., 0., 1.])
            glDisableClientState(GL_NORMAL_ARRAY)
            for hole in self.__holes:
                glVertexPointerf(hole)
                glDrawArrays(GL_LINE_STRIP, 0, len(hole))

            glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION,  [0., 0., 0., 1.])

        # render labels
        if self.__display_labels:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glDisableClientState(GL_VERTEX_ARRAY)
            glDisableClientState(GL_NORMAL_ARRAY)
            glDisableClientState(GL_TEXTURE_COORD_ARRAY)
            glDisable(GL_LIGHTING)
            glDisable(GL_COLOR_MATERIAL)
            glDisable(GL_LIGHT0)
            glDisable(GL_DEPTH_TEST)
            glDisable(GL_TEXTURE_2D)
            for scatter in self.__labels:
                pt = scatter['point']
                point = QVector3D(pt[0], pt[1], pt[2])
                glColor4f(0, 0, 0, 1)
                glPointSize(4)
                glBegin(GL_POINTS)
                glVertex3f(point.x(), point.y(), point.z())
                glEnd()

            glEnable(GL_TEXTURE_2D)
            for scatter in self.__labels:
                pt = scatter['point']
                point = QVector3D(pt[0], pt[1], pt[2])
                dist = .8*(point-eye).length()/height
                w = dist*scatter['image'].width()
                h = dist*scatter['image'].height()
                glBindTexture(GL_TEXTURE_2D, scatter['texture'])
                glColor4f(1, 1, 1, 1);
                glBegin(GL_QUADS)
                glNormal3f(0, 0, 1)
                glTexCoord2f(0, 0)
                glVertex3f(point.x(), point.y(), point.z())
                point -= leftv*w
                glNormal3f(0, 0, 1)
                glTexCoord2f(1, 0)
                glVertex3f(point.x(), point.y(), point.z())
                point += upv*h
                glNormal3f(0, 0, 1)
                glTexCoord2f(1, 1)
                glVertex3f(point.x(), point.y(), point.z())
                point += leftv*w
                glNormal3f(0, 0, 1)
                glTexCoord2f(0, 1)
                glVertex3f(point.x(), point.y(), point.z())
                glEnd()
            glDisable(GL_TEXTURE_2D)


    def initializeGL(self, textureBinder=None):
        #for scatter in self.__labels:
        #    scatter['texture'] = self.__textureBinder(scatter['image']) \
        #            if not textureBinder else textureBinder(scatter['image'])

        self.compileShaders()

    def requireShaderRecompile(self):
        self.shaderNeedRecompile = True

    def compileShaders(self):
        self.shaderNeedRecompile = False

    def zScale(self):
        return self.__zScale

    def toggle_labels(self, state):
        self.__display_labels = bool(state)
        self.changed.emit()

    def toggle_holes(self, state):
        self.__display_holes = bool(state)
        self.changed.emit()

    def setZscale(self, scale):
        factor = float(scale)/self.__zScale

        if self.vtx is not None:
            self.vtx[:,2] *= factor
            self.nrml = computeNormals(self.vtx, self.idx)

        if self.hvtx is not None:
            self.hvtx[:,2] *= factor
            self.hnrml = computeNormals(self.hvtx, self.hidx)


        for scatter in self.__labels:
            scatter['point'][2] *= factor

        for h in range(len(self.__holes)):
            self.__holes[h][:,2] *= factor


        self.__zScale = scale

        self.changed.emit()
