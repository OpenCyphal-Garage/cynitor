#!/bin/bash

rm -rf ./include/*

# Check if pip3 is installed
if command -v pip3 &> /dev/null
then
    echo "pip3 is installed."
else
    echo "pip3 is not installed. Please install pip3."
    exit 1
fi

# Check if nunavut is installed
if pip3 freeze | grep -q "nunavut"; then
    echo "nunavut is installed."
else    
    echo "nunavut is not installed. Please install nunavut using <pip3 install -U nunavut>."
    exit 1
fi

#
C_OUTPUT_DIR="include"
# Run nnvg (nunavut) commands
nnvg --target-language c --enable-serialization-asserts public_regulated_data_types/reg --lookup-dir public_regulated_data_types/uavcan --outdir $C_OUTPUT_DIR > /dev/null 2>&1 || { echo "Error with nnvg command for reg"; exit 1; }
nnvg --target-language c --enable-serialization-asserts public_regulated_data_types/uavcan --lookup-dir public_regulated_data_types/reg --outdir $C_OUTPUT_DIR > /dev/null 2>&1 || { echo "Error with nnvg command for uavcan"; exit 1; }
nnvg --target-language c --enable-serialization-asserts custom_messages --lookup-dir public_regulated_data_types/reg --lookup-dir public_regulated_data_types/uavcan --outdir $C_OUTPUT_DIR  > /dev/null 2>&1 || { echo "Error with nnvg command for reg"; exit 1; }