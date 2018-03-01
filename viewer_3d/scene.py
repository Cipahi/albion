# -*- coding: UTF-8 -*-

import numpy
from OpenGL.GL import *
from OpenGL.GL import shaders

from PyQt4.QtGui import *
from PyQt4.QtCore import *

from .utility import computeNormals
from shapely import wkb

class Scene(QObject):
    
    def __del__(self):
        pass

    def __init__(self, project, param, texture_binder, parent=None):
        super(Scene, self).__init__(parent)
        self.__textureBinder = texture_binder
        self.__old_param = dict(param)
        #{
        #        "label": False,
        #        "node": False,
        #        "edge": False,
        #        "volume": False,
        #        "error": False,
        #        "section": False,
        #        "z_scale": 1,
        #        "graph_id": "330"
        #        }
        self.__param = param

        self.__project = project

        with project.connect() as con:
            cur = con.cursor()
            cur.execute("""
                select st_3dextent(geom)
                from albion.collar
                """)

            ext = cur.fetchone()[0].replace('BOX3D(','').replace(')','').split(',')
            ext = [[float(c) for c in ext[0].split()],[float(c) for c in ext[1].split()]]
            self.__offset = -numpy.array((
                    .5*(ext[0][0]+ext[1][0]), 
                    .5*(ext[0][1]+ext[1][1]),
                    .5*(ext[0][2]+ext[1][2])))

            self.extent = (
                    ext[0][0] + self.__offset[0], ext[0][1] + self.__offset[1], 
                    ext[1][0] + self.__offset[0], ext[1][1] + self.__offset[1])

            self.center = QVector3D(0, 0, 0)

        self.vtx = {
                "node":None,
                "edge":None,
                "section":None,
                "volume":None,
                "error":None}
        self.idx = {
                "node":None,
                "edge":None,
                "section":None,
                "volume":None,
                "error":None}

        self.idx_to_id_map = {
                "node":{},
                "edge":{}}
        self.pick_color = {
                "node":None,
                "edge":None}
        self.nrml = {
                "volume":None,
                "error":None}

        self.__labels = []

        self.highlighted_idx = {
                "node": None,
                "edge": None}

        self.shaders = None

        for layer in ['node', 'edge', 'volume', 'section', 'error']:
            self.update(layer)

    def compileShaders(self):
        vertex_shader = shaders.compileShader("""
            #extension GL_OES_standard_derivatives : enable
            varying vec3 N;
            varying vec3 v;
            varying vec3 vBC;

            void main(void)
            {

                v = vec3(gl_ModelViewMatrix * gl_Vertex);       
                N = normalize(gl_NormalMatrix * gl_Normal);
                vBC = gl_Color.xyz;
                gl_Position = gl_ModelViewProjectionMatrix * gl_Vertex;
            }
            """, GL_VERTEX_SHADER)

        fragment_shader = shaders.compileShader("""
            varying vec3 N;
            varying vec3 v;
            varying vec3 vBC;
            float edgeFactor(){
                vec3 d = fwidth(vBC);
                vec3 a3 = smoothstep(vec3(0.0), d, vBC);
                return min(min(a3.x, a3.y), a3.z);
            }

            void main(void)
            {
                vec3 L = normalize(gl_LightSource[0].position.xyz - v);   
                vec4 Idiff = gl_FrontLightProduct[0].diffuse * max(dot(N,L), 0.);  
                Idiff = clamp(Idiff, 0.0, 1.0); 

                if (Idiff==vec4(0.))
                {
                    Idiff = vec4(1., 0., 0., 0.) * max(dot(-N,L), 0.);  
                    //Idiff = vec4(1., 0., 0., 0.);
                    Idiff = clamp(Idiff, 0.0, 1.0); 
                }

                gl_FragColor.rgb = mix(vec3(0.0), Idiff.xyz, edgeFactor());

                //if(any(lessThan(vBC, vec3(0.02)))){
                //    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
                //}
                //else{
                //    gl_FragColor = Idiff;
                //}
                //gl_FragColor = vec4(vBC.xyz, 1);//Idiff;
                //gl_FragColor = Idiff;
            }
            """, GL_FRAGMENT_SHADER)

        self.shaders = shaders.compileProgram(vertex_shader, fragment_shader)


    def highlight(self, layer, color):
        idx = 0
        for b in color[:3]:
            idx = idx * 256 + int(b)
        if idx < len(self.idx[layer]):
            self.highlighted_idx[layer] = idx
            return self.idx_to_id_map[layer][idx]
        else:
            self.highlighted_idx[layer] = None
            return None

    def delete_highlighted(self, layer):
        if self.highlighted_idx[layer] in  self.idx_to_id_map[layer]:
            print("delete", layer, self.idx_to_id_map[layer][self.highlighted_idx[layer]])
            with self.__project.connect() as con:
                cur = con.cursor()
                if layer == "edge":
                    cur.execute("""
                        delete from albion.edge where id='{}'
                    """.format( self.idx_to_id_map[layer][self.highlighted_idx[layer]]))
                    con.commit()

    def add_edge(self, start, end):
        with self.__project.connect() as con:
            cur = con.cursor()
            cur.execute("""
                insert into albion.edge(start_, end_, graph_id) values('{}', '{}', '{}')
            """.format(start, end, self.__param["graph_id"]))
            con.commit()
        print("add edge", start, end)

    def pickrendergl(self, layer):
        glDisable(GL_COLOR_MATERIAL)
        glDisable(GL_LIGHTING)
        glDisable(GL_TEXTURE_2D)
        glEnableClientState(GL_VERTEX_ARRAY)
        glEnableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_NORMAL_ARRAY)
        if self.__param[layer]:
            if self.__param[layer] != self.__old_param[layer]:
                self.update(layer)
            glLineWidth(8)
            glColorPointer(4, GL_UNSIGNED_BYTE, 0, self.pick_color[layer])
            glVertexPointerf(self.vtx[layer])
            glDrawElementsui(GL_LINES, self.idx[layer])
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)

    def rendergl(self, leftv, upv, eye, height, context):

        glDisable(GL_TEXTURE_2D)
        glEnable(GL_DEPTH_TEST)
        glEnableClientState(GL_VERTEX_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glEnableClientState(GL_COLOR_ARRAY)
        glEnableClientState(GL_NORMAL_ARRAY)

        if self.__param["graph_id"] != self.__old_param["graph_id"]:
            self.setGraph(self.__param["graph_id"])

        if self.__param["z_scale"] != self.__old_param["z_scale"]:
            self.setZscale(self.__param["z_scale"])

        glEnable(GL_LIGHTING)
        glEnable(GL_LIGHT0)
        glLightModelfv(GL_LIGHT_MODEL_TWO_SIDE, GL_TRUE)

        if not self.shaders:
            self.compileShaders()
        glUseProgram(self.shaders)

        for layer in ['volume', 'error']:
            if self.__param[layer]:
                if self.__param[layer] != self.__old_param[layer]:
                    self.update(layer)
                if len(self.vtx[layer]):
                    texcoord = numpy.array([((255,0,0),(0,255,0),(0,0,255))]*len(self.vtx[layer]), dtype=numpy.uint8)
                    glVertexPointerf(self.vtx[layer])
                    glColorPointer(3, GL_UNSIGNED_BYTE, 0, texcoord)
                    glNormalPointerf(self.nrml[layer])
                    glDrawElementsui(GL_TRIANGLES, self.idx[layer])
                    #glLineWidth(1)
                    #glColor4f(0,0,0,1)
                    #glDrawElementsui(GL_LINE_LOOP, self.idx[layer])
        glDisableClientState(GL_COLOR_ARRAY)

        glUseProgram(0)

        glDisable(GL_COLOR_MATERIAL)
        glDisable(GL_LIGHTING)
        glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        color = {'node':[0.,0.,0.,1.], 
                 'edge':[0.,0.,.7,1.]}
        for layer in ['node', 'edge']:
            if self.__param[layer]:
                if self.__param[layer] != self.__old_param[layer]:
                    self.update(layer)
                glLineWidth(2)
                glColor4f(*color[layer])
                if len(self.vtx[layer]):
                    glVertexPointerf(self.vtx[layer])
                    glDrawElementsui(GL_LINES, self.idx[layer])
                    glDisableClientState(GL_COLOR_ARRAY)
                    if self.highlighted_idx[layer] is not None:
                        glLineWidth(6)
                        a = numpy.array(self.idx[layer][self.highlighted_idx[layer]])
                        glDrawElementsui(GL_LINES, a)
        
        # current section, highlight nodes
        glDisable(GL_DEPTH_TEST)
        glLineWidth(2)
        glPointSize(3)
        glColor4f(1., 1., 0., 1.)
        if self.__param['section'] != self.__old_param['section']:
            self.update('section')
        if len(self.vtx['section']):
            glVertexPointerf(self.vtx['section'])
            glDrawElementsui(GL_LINES, self.idx['section'])
            glDrawArrays(GL_POINTS, 0, len(self.vtx['section']))
        

        


        glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION,  [0., 0., 0., 1.])

        # render labels
        if self.__param['label']:
            if self.__param['label'] != self.__old_param['label']:
                self.update('label')
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
                #glColor4f(0, 0, 0, 1)
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
                #glColor4f(1, 1, 1, 1);
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

    def update(self, layer):

        with self.__project.connect() as con:
            cur = con.cursor()
            if layer=='label':
                self.__labels = []
                cur.execute("""
                    select hole_id, st_x(geom), st_y(geom), st_z(geom)
                    from (select hole_id, st_startpoint(geom) as geom from albion.node where graph_id='{}' ) as t
                    """.format(self.__param["graph_id"]))
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
                    scat = {'point': [x+self.__offset[0], y+self.__offset[1], (z+self.__offset[2])*self.__param["z_scale"]], 'image': image}
                    scat['texture'] = self.__textureBinder(scat['image'])
                    self.__labels.append(scat)

            elif layer=='node':
                cur.execute("""
                    select array_agg(id), coalesce(st_collect(geom), 'GEOMETRYCOLLECTION EMPTY'::geometry) from albion.node where graph_id='{}'
                    """.format(self.__param["graph_id"]))
                res = cur.fetchone()
                lines = wkb.loads(res[1], True)
                lines_ids = res[0]
                vtx = []
                idx = []
                colors = []
                for line, id_ in zip(lines, lines_ids):
                    elt = len(idx)
                    self.idx_to_id_map[layer][elt] = id_
                    colors += [(elt >> 16 & 0xff, elt >>  8 & 0xff, elt >>  0 & 0xff, 255)]*len(line.coords)
                    idx += [(i, i+1) for i in range(len(vtx), len(vtx)+len(line.coords)-1)]
                    vtx += list(line.coords)
                self.vtx[layer] = numpy.array(vtx, dtype=numpy.float32)
                if len(vtx):
                    self.vtx[layer] += self.__offset
                    self.vtx[layer][:,2] *= self.__param["z_scale"]
                self.idx[layer] = numpy.array(idx, dtype=numpy.int32)
                self.pick_color[layer] = numpy.array(colors, dtype=numpy.uint8)

            elif layer=='section':

                cur.execute("""
                    select coalesce(st_collect(n.geom), 'GEOMETRYCOLLECTION EMPTY'::geometry)
                    from albion.section as s
                    join albion.collar as c on st_intersects(s.geom, c.geom)
                    join albion.hole as h on h.collar_id=c.id
                    join albion.node as n on n.hole_id=h.id
                    where n.graph_id='{}'
                    """.format(self.__param["graph_id"])
                    )
                lines = wkb.loads(cur.fetchone()[0], True)
                vtx = []
                idx = []
                for line in lines:
                    idx += [(i, i+1) for i in range(len(vtx), len(vtx)+len(line.coords)-1)]
                    vtx += list(line.coords)
                vtx = numpy.array(vtx, dtype=numpy.float32)
                if len(vtx):
                    vtx += self.__offset
                    vtx[:,2] *= self.__param['z_scale']
                self.vtx[layer] = vtx
                self.idx[layer] = numpy.array(idx, dtype=numpy.int32)

            elif layer=='edge':
                cur.execute("""
                    select array_agg(id), coalesce(st_collect(geom), 'GEOMETRYCOLLECTION EMPTY'::geometry) from albion.edge where graph_id='{}'
                    """.format(self.__param["graph_id"]))
                res = cur.fetchone()
                lines = wkb.loads(res[1], True)
                lines_ids = res[0]
                vtx = []
                idx = []
                colors = []
                for line, id_ in zip(lines, lines_ids):
                    new_idx = [(i, i+1) for i in range(len(vtx), len(vtx)+len(line.coords)-1)]
                    elt = len(idx)
                    self.idx_to_id_map[layer][elt] = id_
                    colors += [(elt >> 16 & 0xff, elt >>  8 & 0xff, elt >>  0 & 0xff, 255)]*len(line.coords)
                    idx += [(i, i+1) for i in range(len(vtx), len(vtx)+len(line.coords)-1)]
                    vtx += list(line.coords)
                self.vtx[layer] = numpy.array(vtx, dtype=numpy.float32)
                if len(vtx):
                    self.vtx[layer] += self.__offset
                    self.vtx[layer][:,2] *= self.__param["z_scale"]
                self.idx[layer] = numpy.array(idx, dtype=numpy.int32)
                self.pick_color[layer] = numpy.array(colors, dtype=numpy.uint8)
            
            elif layer=='volume':
                cur.execute("""
                    select albion.volume_union(st_collectionhomogenize(coalesce(st_collect(triangulation), 'GEOMETRYCOLLECTION EMPTY'::geometry)))
                    from albion.volume
                    where graph_id='{}'
                    and albion.is_closed_volume(triangulation)
                    """.format(self.__param["graph_id"]))
                geom = wkb.loads(cur.fetchone()[0], True)
                self.vtx[layer] = numpy.require(numpy.array([tri.exterior.coords[:-1] for tri in geom]).reshape((-1,3)), numpy.float32, 'C')
                if len(self.vtx[layer]):
                    self.vtx[layer] += self.__offset
                    self.vtx[layer][:,2] *= self.__param["z_scale"]
                self.idx[layer] = numpy.require(numpy.arange(len(self.vtx[layer])).reshape((-1,3)), numpy.int32, 'C')
                self.nrml[layer] = computeNormals(self.vtx[layer], self.idx[layer])

            elif layer=='error':
                cur.execute("""
                    select st_collectionhomogenize(coalesce(st_collect(triangulation), 'GEOMETRYCOLLECTION EMPTY'::geometry))
                    from albion.volume
                    where graph_id='{}'
                    and not albion.is_closed_volume(triangulation)
                    """.format(self.__param["graph_id"]))
                geom = wkb.loads(cur.fetchone()[0], True)
                self.vtx[layer] = numpy.require(numpy.array([tri.exterior.coords[:-1] for tri in geom]).reshape((-1,3)), numpy.float32, 'C')
                if len(self.vtx[layer]):
                    self.vtx[layer] += self.__offset
                    self.vtx[layer][:,2] *= self.__param["z_scale"]
                self.idx[layer] = numpy.require(numpy.arange(len(self.vtx[layer])).reshape((-1,3)), numpy.int32, 'C')
                self.nrml[layer] = computeNormals(self.vtx[layer], self.idx[layer])

            self.__old_param[layer] = self.__param[layer]

    def setGraph(self, graph_id):
        for layer in ['node', 'edge', 'volume', 'section', 'error']:
            self.update(layer)
        self.__old_param["graph_id"] = graph_id


    def setZscale(self, scale):
        factor = float(scale)/self.__old_param["z_scale"]

        for layer in ['node', 'edge', 'volume', 'section', 'error']:
            if self.vtx[layer] is not None:
                self.vtx[layer][:,2] *= factor
                if layer in ['volume', 'error']:
                    self.nrml[layer] = computeNormals(self.vtx[layer], self.idx[layer])

        for scatter in self.__labels:
            scatter['point'][2] *= factor

        self.__old_param["z_scale"] = scale

