# WebMonitor

This repository contains a [Cyphal](https://opencyphal.org/) webapplication to monitor Cyphal nodes (and aslo change their registers) on a CAN-bus.

## Table of Contents
1. [About this repository](#about-this-repository)
2. [Getting started](#getting-started)
3. [Architecture](#architecture)
4. [Building and Running](#building-and-running)
5. [Steps to create a new combined package from scratch](#steps-to-create-a-new-combined-package-from-scratch)


## Getting started

The current repository looks like

```bash
monitor
├── backend
│   ├── database.py
│   ├── dsdl_messages (git submodule)
│   │   ├── c_compiled_messages
│   │   ├── c_generate.bash
│   │   ├── cleanup.bash
│   │   ├── dontpanic
│   │   ├── public_regulated_data_types
│   │   ├── python_compiled_messages
│   │   ├── python_generate.bash
│   │   └── README.md
│   ├── __init__.py
│   ├── main.py
│   ├── mongodb
│   │   ├── bin
│   ├── node_data_manager.py
│   ├── node_info.py
│   ├── python_compiled_messages
│   │   ├── dontpanic
│   │   ├── nunavut_support.py
│   │   ├── __pycache__
│   │   ├── reg
│   │   └── uavcan
│   └── scanner_node.py
├── frontend
│   ├── backend_control.py
│   ├── can.py
│   ├── mongodb_control.py
│   ├── requirements.txt
│   ├── static
│   │   ├── css
│   │   ├── images
│   │   └── js
│   ├── templates
│   │   └── index.html
│   └── web_app.py
├── __init__.py
├── MonitorApp.spec
├── README.md
├── WebMonitor.drawio
├── WebMonitor.drawio.png
├── WebMonitorArch.drawio
└── WebMonitorArch.drawio.png
```

The **dsdl_messages** directory is a git submodule, so ensure all submodules are initialized and updated:

```bash
git submodule update --init --remote --recursive
```
Install the required dependencies:
```bash
sudo apt install can-utils
pip3 install python-can
pip3 install 'pycyphal[transport-can-pythoncan,transport-serial,transport-udp]'
pip3 install uvicorn nunavut parsimonious pyinstaller
```
## Architecture
WebMonitor follows a two-part architecture:
- Backend: A Cyphal node implemented using PyCyphal that monitors the CAN bus, reads/writes data to a MongoDB database, and handles register operations for Cyphal nodes based on requests.
- Frontend: A FastAPI application hosting a web interface on localhost. The **script.js** file renders dynamic tables from MongoDB data and enables register modification functionality.
![Pic](WebMonitorArch.drawio.png)

## Building and Running

Once dependencies are installed, start the monitor node:
```bash
python3 monitor/run_monitor.py
```
Alternatively, build and run an executable:
```bash
pyinstaller monitor/MonitorApp.spec
```
**Note**: If you have previously run **run_monitor.py**, delete the **mongod.conf** file in the mongodb folder before building the executable. This prevents the socket from linking to an incorrect path, which could prevent MongoDB from starting.