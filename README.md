Build 3D geological model from wells information
################################################

This plugin provides tools to create 3D geological models in QGIS.

**Note** 
  - The created layers are memory layers, the data are not saved, you should use the "save as" qgis function if you plan to store the layer as a shapefile or spatialite layer, you can also cpy/paste the layer feature in a postgis layer that has the right structure. This is intended to maximize flexibility of the source data format.

Quick dev install
=================

You have to install the following dependencies:

  - OpenGL for Python (python-opengl)
  - QtOpenGL for Python (python-qt4-gl)
  - shapely
  - pytest, hypothesis and mock (for running tests only)

Clone the repo in a `albion` directory. Add the directory containing `albion` to your PYTHONPATH environment variable.

```sh
export PYTHONPATH=$PWD/..
```

Install plugin:
```sh
python -m albion.package -i
```

Run tests:
```sh
pytest
```


Process
=======

- Create project (no caps, no space, no accents)
- Import data directory
- Create the grid
- Create a graph
- Set graph nodes (cpy/paste features)


Interface
=========

- Albion Menu: 
    - new project (create db and load layers)
    - --
    - import data
    - compute mineralization (adds table and layer)
    - --
    - new graph (create table and views and neww layers)
    - clean graph
    - --
    - export project (`pg_dump`)
    - import project (`pg_restore`)

- Albion Toolbar:
    - toggle section/top
    - current graph
    - grid line from points
    - select current section (either on top view, or orthogonal in section view)
    - previous section
    - next section
    - end distance
    - auto-connect
    - auto-wall and ceil 

notes on 3D:
- togle visibility
- dynamic update on changes
