#!/bin/bash

# script directory
SCRIPT_DIR="$( cd "$(dirname "${BASH_SOURCE[0]}")" ; pwd -P )"

# manually trans-compile messages
HEADER_OUTPUT_DIR="$SCRIPT_DIR/c_compiled_messages"
UAVCAN_DIR="$SCRIPT_DIR/public_regulated_data_types/uavcan"
REG_DIR="$SCRIPT_DIR/public_regulated_data_types/reg"
CUSTOM_DIR="$SCRIPT_DIR/dontpanic"

nnvg --target-language c --target-endianness=little --enable-serialization-asserts $REG_DIR --lookup-dir $UAVCAN_DIR --outdir $HEADER_OUTPUT_DIR > /dev/null 2>&1 || { echo "Error with nnvg command for reg";}
nnvg --target-language c --target-endianness=little --enable-serialization-asserts $UAVCAN_DIR --lookup-dir $REG_DIR --outdir $HEADER_OUTPUT_DIR > /dev/null 2>&1 || { echo "Error with nnvg command for uavcan";}
nnvg --target-language c --target-endianness=little --enable-serialization-asserts $CUSTOM_DIR --lookup-dir $REG_DIR --lookup-dir $UAVCAN_DIR --outdir $HEADER_OUTPUT_DIR 2>&1 || {
    echo "Error with nnvg command for dontpanic:"
    echo "$?"
}
