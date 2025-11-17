# DSDL Messages

DSDL (Data Structure Description Language) for CAN (Controller Area Network) standardizes the way data is structured and described within CAN messages. It provides a definition for data types and serialization methods, ensuring consistent and efficient communication between CAN devices by defining how data should be packed into messages. 

This repository contains those definitions and some scripts to generate `.h`-files and `.py`-files to include in a C/C++ or python project.


# Table of Contents
1. [Getting started](#getting-started)
2. [Generate Files](#generate-files)


## Getting started

The current repository looks like

```shell
dsdl_messages
.
├── c_compiled_messages
├── c_generate.bash
├── cleanup.sh
├── dontpanic
├── public_regulated_data_types
├── python_compiled_messages
├── python_generate.bash
└── README.md
``` 

where the `public_regulated_data_types` is a git-submodule referencing [Cyphal's official DSDL files](https://github.com/OpenCyphal/public_regulated_data_types) and `dontpanic` which contains our own message definitions.


## Generate Files

Install the dependencies:

```shell
sudo apt install python3
sudo apt install python3-pip
pip3 install -U nunavut
```

and run 

```shell
bash c_generate.bash
```

to generate

```shell
dsdl_messages
    ├── cleanup.sh
    ├── dontpanic
    ├── c_generate.sh
    ├── c_compiled_messages
    │   ├── dontpanic
    │   │   ├── MessageType_1_0.h
    │   │   ├── ...
    │   │   └── SubjectIDAllocation_1_0.h
    │   ├── nunavut
    │   │   └── support
    │   ├── reg
    │   │   └── udral
    │   └── uavcan
    │       ├── diagnostic
    │       ├── ...
    │       └── time
```

the `.h`-files you can include in your C/C++ project contained in `dsdl_messages/c_compiled_messages` and


```shell
bash python_generate.bash
```

to generate


```shell
.
├── cleanup.sh
├── dontpanic
├── c_generate.sh
├── c_compiled_messages
├── public_regulated_data_types
├── python_compiled_messages
│   ├── custom_messages
│   ├── __init__.py
│   ├── nunavut_support.py
│   ├── reg
│   │   ├── __init__.py
│   │   └── udral
│   └── uavcan
│       ├── diagnostic
│       ├── __init__.py
│       ├── internet
│       ├── ...
│       └── time
``` 


the `.py`-files you can include in your python project contained in `dsdl_messages/python_compiled_messages`. Clean up the generated files with

```shell
bash cleanup.bash
```