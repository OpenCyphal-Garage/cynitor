let allUniqueIds = [];
const clickedCells = new Set();
let uniqueIdToSubjectIds = {};

async function fetchInfo(endpoint) {
    try {
        const response = await fetch(endpoint);
        if (!response.ok) throw new Error(`Error fetching ${endpoint}: ${response.statusText}`);
        return await response.json();
    } catch (error) {
        console.error(`Error fetching ${endpoint}:`, error);
        throw error;
    }
}

async function fetchAndUpdateRegistersSubTable(rowElement, rowData, uniqueId) {
    console.log(`Fetching and updating registers for ${uniqueId}`);
    try {
        // Fetch new register data
        const response = await fetch(`/nodes/${uniqueId}/registers`);
        if (!response.ok) throw new Error(`Error fetching registers: ${response.statusText}`);
        const registersData = await response.json();
        
        // Prepare updated data
        const updatedData = registersData.map((register, index) => ({
            register_name: register.register_name || "N/A",
            value: register.value || "N/A",
            type: register.type || "N/A",
            access: register.access || "N/A",
            isFirstRow: true
        }));

        // Check if sub-table holder exists
        const holderEl = rowElement.querySelector(`.sub-table-holder[data-field="registers"]`);
        if (holderEl) {
            // Find the existing sub-table element
            const subTableEl = holderEl.querySelector("div");
            if (subTableEl) {
                const subTable = Tabulator.findTable(subTableEl)[0];
                if (subTable) {
                    // Update existing sub-table with new data
                    subTable.replaceData(updatedData);
                    console.log(`Registers sub-table for uniqueId: ${uniqueId} updated at`, new Date().toISOString());
                } else {
                    // No Tabulator instance found, create new sub-table
                    console.log(`No Tabulator instance found for ${uniqueId}, creating new sub-table`);
                    createRegistersSubTable(rowElement, rowData, registersData);
                }
            } else {
                // Holder exists but no sub-table element, create new sub-table
                console.log(`No sub-table element found for ${uniqueId}, creating new sub-table`);
                createRegistersSubTable(rowElement, rowData, registersData);
            }
        } else {
            // No holder exists, create new sub-table
            console.log(`No sub-table holder found for ${uniqueId}, creating new sub-table`);
            createRegistersSubTable(rowElement, rowData, registersData);
        }

        // Ensure sub-table is visible and marked as expanded
        const holderElUpdated = rowElement.querySelector(`.sub-table-holder[data-field="registers"]`);
        if (holderElUpdated) {
            holderElUpdated.style.display = "block";
            const cell = rowElement.querySelector(`td.tabulator-cell[tabulator-field="actions"]`);
            if (cell) cell.classList.add("expanded");
            clickedCells.add(`${uniqueId}-registers`);
            saveClickedCells();
        }
    } catch (error) {
        console.error(`Error fetching registers for ${uniqueId}:`, error);
        alert(`Failed to load registers: ${error.message}`);
    }
}

function createRegistersSubTable(rowElement, data, registersData) {
    let holderEl = rowElement.querySelector(`.sub-table-holder[data-field="registers"]`);
    
    // If holder exists, clear its contents to avoid duplicates
    if (holderEl) {
        console.log(`Clearing existing sub-table holder for registers in uniqueId: ${data.uniqueId}`);
        holderEl.innerHTML = "";
    } else {
        // Create new holder if it doesn't exist
        holderEl = document.createElement("div");
        holderEl.className = "sub-table-holder";
        holderEl.setAttribute("data-field", "registers");
        holderEl.style.cssText = "padding: 10px; border-top: 1px solid #333; box-sizing: border-box; display: block;";
        rowElement.appendChild(holderEl);
    }

    // Add Refresh button above the table
    const refreshButton = document.createElement("button");
    refreshButton.innerHTML = "Refresh";
    refreshButton.className = "refresh-button";
    refreshButton.addEventListener("click", async () => {
        refreshButton.disabled = true;
        refreshButton.innerHTML = "Refreshing...";
        try {
            await fetchAndUpdateRegistersSubTable(rowElement, data, data.uniqueId);
        } catch (error) {
            console.error(`Error refreshing registers for ${data.uniqueId}:`, error);
            alert(`Failed to refresh registers: ${error.message}`);
        } finally {
            refreshButton.disabled = false;
            refreshButton.innerHTML = "Refresh";
        }
    });
    holderEl.appendChild(refreshButton);

    // Create sub-table element
    const subTableEl = document.createElement("div");
    holderEl.appendChild(subTableEl);

    const flattenedData = registersData.map((register, index) => ({
        register_name: register.register_name || "N/A",
        value: register.value || "N/A",
        type: register.type || "N/A",
        access: register.access || "N/A",
        isFirstRow: true,
        new_value: "" // Initialize new_value field
    }));

    const table = new Tabulator(subTableEl, {
        layout: "fitColumns",
        data: flattenedData,
        columns: [
            { title: "Register Name", field: "register_name", sorter: "string", maxWidth: 450, responsive: 0, headerMenu },
            { title: "Value", field: "value", sorter: "string", maxWidth: 450, responsive: 0 },
            { title: "Type", field: "type", sorter: "string", maxWidth: 150, responsive: 1 },
            { title: "Access", field: "access", sorter: "string", maxWidth: 150, responsive: 1 },
            { 
                title: "New Value", 
                field: "new_value", 
                sorter: "string", 
                maxWidth: 200, 
                responsive: 0,
                editor: "input", 
                editorParams: {
                    elementAttributes: { type: "text" } // Allow text input
                }
            },
            {
                title: "Set Register", 
                field: "set_register",
                headerSort: false,
                maxWidth: 120,
                responsive: 0,
                hozAlign: "center",
                formatter: (cell) => {
                    const rowData = cell.getRow().getData();
                    const button = document.createElement("button");
                    button.innerHTML = "Set";
                    button.className = "set-button";
                    button.disabled = rowData.access === "read-only"; // Disable for read-only
                    button.style.cursor = rowData.access === "read-only" ? "not-allowed" : "pointer";
                    button.addEventListener("click", async () => {
                        if (!button.disabled && rowData.new_value) {
                            button.disabled = true;
                            button.innerHTML = "Setting...";
                            try {
                                const response = await fetch(`/nodes/${data.uniqueId}/registers/${rowData.register_name}`, {
                                    method: 'PUT',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ value: rowData.new_value, type: rowData.type })
                                });
                                if (!response.ok) throw new Error(`Error setting register: ${response.statusText}`);
                                const result = await response.json();
                                cell.getRow().update({ value: result.value }); // Update displayed value
                                console.log(`Register ${rowData.register_name} set to ${rowData.new_value} at`, new Date().toISOString());
                            } catch (error) {
                                console.error(`Error setting register ${rowData.register_name}:`, error);
                                alert(`Failed to set register: ${error.message}`);
                            } finally {
                                button.disabled = false;
                                button.innerHTML = "Set";
                            }
                        } else if (!rowData.new_value) {
                            alert("Please enter a new value before setting the register.");
                        }
                    });
                    return button;
                }
            }
        ],
        rowFormatter: subRow => {
            subRow.getElement().classList.add("register-row");
        },
        height: "auto",
        cellEdited: (cell) => {
            // Ensure new_value is updated in the row data
            const rowData = cell.getRow().getData();
            console.log(`New value edited for ${rowData.register_name}: ${cell.getValue()}`);
        }
    });
}

async function toggleRegistersSubTable(cell, uniqueId) {
    const row = cell.getRow();
    const rowElement = row.getElement();
    const rowData = row.getData();
    const holderEl = rowElement.querySelector(`.sub-table-holder[data-field="registers"]`);
    const key = `${uniqueId}-registers`;

    // Check if node is offline
    if (rowData.uptime === "Offline") {
        console.log(`Cannot toggle registers for offline node ${uniqueId}`);
        alert("Cannot toggle registers for an offline node");
        return;
    }

    if (holderEl) {
        // Toggle visibility
        holderEl.style.display = holderEl.style.display === "none" ? "block" : "none";
        cell.getElement().classList.toggle("expanded", holderEl.style.display === "block");
        clickedCells[holderEl.style.display === "block" ? "add" : "delete"](key);
        saveClickedCells();
    } else {
        // Create empty sub-table holder and fetch data
        console.log(`Creating sub-table holder and fetching registers for ${uniqueId}`);
        const holderEl = document.createElement("div");
        holderEl.className = "sub-table-holder";
        holderEl.setAttribute("data-field", "registers");
        holderEl.style.cssText = "padding: 10px; border-top: 1px solid #333; box-sizing: border-box; display: none;";
        const subTableEl = document.createElement("div");
        holderEl.appendChild(subTableEl);
        rowElement.appendChild(holderEl);
        // Fetch and populate the sub-table
        await fetchAndUpdateRegistersSubTable(rowElement, rowData, uniqueId);
    }
}

function saveClickedCells() {
    const clickedArray = Array.from(clickedCells);
    localStorage.setItem("clickedCells", JSON.stringify(clickedArray));
    console.log('Saved to localStorage:', clickedArray);
}

function loadClickedCells() {
    const clickedArray = JSON.parse(localStorage.getItem("clickedCells") || "[]");
    console.log('Loaded from localStorage:', clickedArray);
    clickedArray.forEach(key => clickedCells.add(key));
    return clickedCells;
}

async function updateNodeInDatabase(node) {
    try {
        const response = await fetch(`/nodes/${node.uniqueId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node_name: node.nodeName })
        });
        if (!response.ok) throw new Error(`Error updating node: ${response.statusText}`);
        const updatedNode = await response.json();
        console.log('Node updated successfully:', updatedNode);
    } catch (error) {
        console.error('Error updating node:', error);
    }
}

async function deleteNodeFromDatabase(uniqueId) {
    try {
        const response = await fetch(`/nodes/${uniqueId}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        });
        if (!response.ok) throw new Error(`Error deleting node: ${response.statusText}`);
        console.log('Node deleted successfully');
    } catch (error) {
        console.error('Error deleting node:', error);
    }
}

async function sendServiceRequest(nodeId, uniqueId, serviceId, serviceType, attributes) {
    try {
        const payload = {
            node_id: nodeId,
            unique_id: String(uniqueId),
            service_type: serviceType,
            attributes: attributes // { "a": { value: "2", type: "uavcan.si.unit.electric_current.Scalar_1_0" }, ... }
        };
        console.log(`Sending service request for service ${serviceId}:`, JSON.stringify(payload, null, 2));
        const response = await fetch(`/services/${serviceId}/make_request`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!response.ok) {
            const errorData = await response.json();
            console.error(`Error response for service ${serviceId}:`, errorData);
            throw new Error(errorData.detail || `Request failed with status ${response.status}`);
        }
        const responseData = await response.json();
        console.log(`Service request successful for node ${nodeId}, service ${serviceId}, type ${serviceType}:`, responseData);
        return responseData;
    } catch (error) {
        console.error(`Error sending request for service ${serviceId}:`, error);
        throw error;
    }
}
// Placeholder function to update the response column (to be implemented later)
async function updateResponseDisplay(serviceId, responseData) {
    console.log(`Placeholder: Updating response for service ${serviceId} with data:`, responseData);
    // Return the response to be used in the table
    return responseData.response || "N/A";
}

const headerMenu = function () {
    const menu = [];
    const columns = this.getColumns();
    for (let column of columns) {
        let icon = document.createElement("i");
        icon.classList.add("fas", column.isVisible() ? "fa-check-square" : "fa-square");
        let label = document.createElement("span");
        let title = document.createElement("span");
        title.textContent = " " + column.getDefinition().title;
        label.appendChild(icon);
        label.appendChild(title);
        menu.push({
            label: label,
            action: function (e) {
                e.stopPropagation();
                column.toggle();
                icon.classList.toggle("fa-check-square", column.isVisible());
                icon.classList.toggle("fa-square", !column.isVisible());
            }
        });
    }
    return menu;
};

function createPublishersSubTable(rowElement, data, subjectsMap) {
    const holderEl = document.createElement("div");
    holderEl.className = "sub-table-holder";
    holderEl.setAttribute("data-field", "publishers");
    holderEl.style.cssText = "padding: 10px; border-top: 1px solid #333; box-sizing: border-box; display: block;";

    const subTableEl = document.createElement("div");
    holderEl.appendChild(subTableEl);
    rowElement.appendChild(holderEl);

    const publishersString = data.publishers || "";
    const matchingSubjects = Object.keys(subjectsMap).filter(subject => publishersString.includes(subject));

    const flattenedData = matchingSubjects.reduce((acc, subjectId) => {
        const subject = subjectsMap[subjectId];
        if (subject.attributes && subject.attributes.length > 0) {
            subject.attributes.forEach((attr, index) => {
                acc.push({
                    subject_id: subject.subject_id,
                    isFirstRow: index === 0,
                    attribute: attr.attribute,
                    value: attr.value,
                    unit: attr.unit || "N/A",
                    message_type: subject.message_type,
                    rate: subject.rate,
                    timestamp: subject.timestamp
                });
            });
        } else {
            acc.push({
                subject_id: subject.subject_id,
                isFirstRow: true,
                attribute: "N/A",
                value: "N/A",
                unit: "N/A",
                message_type: subject.message_type,
                rate: subject.rate,
                timestamp: subject.timestamp
            });
        }
        return acc;
    }, []);

    new Tabulator(subTableEl, {
        layout: "fitColumns",
        data: flattenedData,
        columns: [
            { title: "Publisher ID", field: "subject_id", sorter: "number", hozAlign: "center", maxWidth: 160, responsive: 0, headerMenu,
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Message Type", field: "message_type", maxWidth: 450, responsive: 1, sorter: "string",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Attributes", field: "attribute", headerSort: false, maxWidth: 150, responsive: 1 },
            { title: "Values", field: "value", headerSort: false, responsive: 0, hozAlign: "left" },
            { title: "Units", field: "unit", headerSort: false, responsive: 1 },
            { title: "Rate (Hz)", field: "rate", headerSort: false, maxWidth: 150, responsive: 1, headerHozAlign: "center", hozAlign: "center",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Timestamp", field: "timestamp", headerSort: false, responsive: 1,
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" }
        ],
        rowFormatter: subRow => {
            if (!subRow.getData().isFirstRow) subRow.getElement().classList.add("grouped-row");
        },
        height: "auto",
    });
}

function createSubscribersSubTable(rowElement, data, subjectsMap) {
    const holderEl = document.createElement("div");
    holderEl.className = "sub-table-holder";
    holderEl.setAttribute("data-field", "subscribers");
    holderEl.style.cssText = "padding: 10px; border-top: 1px solid #333; box-sizing: border-box; display: block;";

    const subTableEl = document.createElement("div");
    holderEl.appendChild(subTableEl);
    rowElement.appendChild(holderEl);

    const subscribersString = data.subscribers || "";
    const matchingSubjects = Object.keys(subjectsMap).filter(subject => subscribersString.includes(subject));

    const flattenedData = matchingSubjects.reduce((acc, subjectId) => {
        const subject = subjectsMap[subjectId];
        if (subject.attributes && subject.attributes.length > 0) {
            subject.attributes.forEach((attr, index) => {
                acc.push({
                    subject_id: subject.subject_id,
                    isFirstRow: index === 0,
                    attribute: attr.attribute,
                    value: attr.value,
                    unit: attr.unit || "N/A",
                    message_type: subject.message_type,
                    rate: subject.rate,
                    timestamp: subject.timestamp
                });
            });
        } else {
            acc.push({
                subject_id: subject.subject_id,
                isFirstRow: true,
                attribute: "N/A",
                value: "N/A",
                unit: "N/A",
                message_type: subject.message_type,
                rate: subject.rate,
                timestamp: subject.timestamp
            });
        }
        return acc;
    }, []);

    new Tabulator(subTableEl, {
        layout: "fitColumns",
        data: flattenedData,
        columns: [
            { title: "Subscriber ID", field: "subject_id", sorter: "number", hozAlign: "center", maxWidth: 160, responsive: 0, headerMenu,
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Message Type", field: "message_type", maxWidth: 450, responsive: 1, sorter: "string",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Attributes", field: "attribute", headerSort: false, maxWidth: 150, responsive: 1 },
            { title: "Values", field: "value", headerSort: false, responsive: 0, hozAlign: "left" },
            { title: "Units", field: "unit", headerSort: false, responsive: 1 },
            { title: "Rate (Hz)", field: "rate", headerSort: false, maxWidth: 150, responsive: 1, headerHozAlign: "center", hozAlign: "center",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" },
            { title: "Timestamp", field: "timestamp", headerSort: false, responsive: 1,
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" }
        ],
        rowFormatter: subRow => {
            if (!subRow.getData().isFirstRow) subRow.getElement().classList.add("grouped-row");
        },
        height: "auto",
    });
}

function createServersSubTable(rowElement, data, serversMap) {
    const holderEl = document.createElement("div");
    holderEl.className = "sub-table-holder";
    holderEl.setAttribute("data-field", "servers");
    holderEl.style.cssText = "padding: 10px; border-top: 1px solid #333; box-sizing: border-box; display: block;";

    const subTableEl = document.createElement("div");
    holderEl.appendChild(subTableEl);
    rowElement.appendChild(holderEl);

    const uniqueId = data.uniqueId;
    const serverData = serversMap[uniqueId] || [];

    // Check if a Tabulator instance already exists for this subTableEl
    const existingTable = Tabulator.findTable(subTableEl)[0];
    if (existingTable) {
        console.log(`Sub-table for servers (uniqueId: ${uniqueId}) already exists, updating data`);
        existingTable.replaceData(serverData.reduce((acc, server) => {
            // Check if attributes exist and have entries
            if (server.attributes && typeof server.attributes === 'object' && Object.keys(server.attributes).length > 0) {
                Object.entries(server.attributes).forEach(([attribute, type], attrIndex) => {
                    acc.push({
                        service_id: server.service_id,
                        isFirstRow: attrIndex === 0,
                        attribute: attribute,
                        type: type,
                        unit: "",
                        service_type: server.service_type,
                        response: ""
                    });
                });
            } else {
                acc.push({
                    service_id: server.service_id,
                    isFirstRow: true,
                    attribute: "N/A",
                    type: "N/A",
                    unit: "",
                    service_type: server.service_type,
                    response: ""
                });
            }
            return acc;
        }, []));
        return;
    }

    const flattenedData = serverData.reduce((acc, server) => {
        // Check if attributes exist and have entries
        if (server.attributes && typeof server.attributes === 'object' && Object.keys(server.attributes).length > 0) {
            Object.entries(server.attributes).forEach(([attribute, type], attrIndex) => {
                acc.push({
                    service_id: server.service_id,
                    isFirstRow: attrIndex === 0,
                    attribute: attribute,
                    type: type,
                    unit: "",
                    service_type: server.service_type,
                    response: ""
                });
            });
        } else {
            acc.push({
                service_id: server.service_id,
                isFirstRow: true,
                attribute: "N/A",
                type: "N/A",
                unit: "",
                service_type: server.service_type,
                response: ""
            });
        }
        return acc;
    }, []);

    new Tabulator(subTableEl, {
        layout: "fitColumns",
        data: flattenedData,
        columns: [
            { 
                title: "Service ID", 
                field: "service_id",
                sorter: "number", 
                hozAlign: "center", 
                maxWidth: 160, 
                responsive: 0, 
                headerMenu,
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" 
            },
            { 
                title: "Service Type", 
                field: "service_type",
                maxWidth: 200, 
                responsive: 0, 
                sorter: "string",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : "" 
            },
            { 
                title: "Attributes", 
                field: "attribute",
                headerSort: false, 
                maxWidth: 300, 
                responsive: 0 
            },
            { 
                title: "Attribute types", 
                field: "type",
                headerSort: false,
                maxWidth: 300,  
                responsive: 1, 
                hozAlign: "left"
            },
            { 
                title: "Request attribute values", 
                field: "unit",
                headerSort: false,
                maxWidth: 300, 
                responsive: 1,
                editor: "input"
            },
            {
                title: "", 
                field: "request",
                headerSort: false, 
                maxWidth: 100, 
                responsive: 0, 
                hozAlign: "center",
                formatter: (cell) => {
                    if (!cell.getRow().getData().isFirstRow) return "";
                    const button = document.createElement("button");
                    button.innerHTML = "Request";
                    button.className = "request-button";
                    button.addEventListener("click", async () => {
                        const rowData = cell.getRow().getData();
                        const serviceId = rowData.service_id;
                        const serviceType = rowData.service_type;
                        const nodeId = data.nodeId; // From parent row data
                        const uniqueId = data.uniqueId; // From parent row data
                        const table = cell.getTable();
                        // Collect all attributes for this service
                        let serviceRows = table.getRows().filter(row => 
                            row.getData().service_id === serviceId
                        );
                        const attributes = serviceRows.reduce((acc, row) => {
                            const data = row.getData();
                            if (data.attribute !== "N/A" && data.unit !== "" && data.unit !== undefined) {
                                acc[data.attribute] = {
                                    value: String(data.unit),
                                    type: data.type
                                };
                            }
                            return acc;
                        }, {});
                        console.log(`Request button clicked for service ${serviceId}:`, {
                            nodeId,
                            uniqueId,
                            serviceId,
                            serviceType,
                            attributes,
                            serviceRows: serviceRows.map(row => row.getData())
                        });
                        try {
                            const responseData = await sendServiceRequest(nodeId, uniqueId, serviceId, serviceType, attributes);
                            const responseText = await updateResponseDisplay(serviceId, responseData);
                            // Re-fetch rows to ensure they still exist
                            serviceRows = table.getRows().filter(row => 
                                row.getData().service_id === serviceId
                            );
                            if (serviceRows.length === 0) {
                                console.warn(`No rows found for service ${serviceId} after request`);
                                return;
                            }
                            // Update only the first row with the response
                            const firstRow = serviceRows.find(row => row.getData().isFirstRow);
                            if (firstRow) {
                                firstRow.update({ response: responseText });
                            } else {
                                console.warn(`First row not found for service ${serviceId}`);
                            }
                        } catch (error) {
                            console.error(`Error sending request for service ${serviceId}:`, error);
                            alert(`Failed to make a request. Error: ${error.message}`);
                        }
                    });
                    return button;
                }
            },
            { 
                title: "Response", 
                field: "response",
                headerSort: false, 
                responsive: 1, 
                hozAlign: "left",
                formatter: cell => cell.getRow().getData().isFirstRow ? cell.getValue() : ""
            }
        ],
        rowFormatter: subRow => {
            if (!subRow.getData().isFirstRow) subRow.getElement().classList.add("grouped-row");
        },
        height: "auto",
    });
}

async function populateNodesTable() {
    try {
        // Fetch nodes (required)
        const nodes = await fetchInfo('/nodes');
        
        // Fetch subjects and servers (optional)
        let subjects = [];
        let servers = [];
        try {
            subjects = await fetchInfo('/pub-sub-subjects');
        } catch (error) {
            console.warn('No pub-sub subjects available, proceeding with empty subjects:', error);
        }
        try {
            servers = await fetchInfo('/services');
        } catch (error) {
            console.warn('No services available, proceeding with empty servers:', error);
        }

        allUniqueIds = nodes.map(node => node.unique_id);
        console.log('All unique IDs:', allUniqueIds);

        const subjectsMap = subjects.reduce((map, subject) => {
            map[subject.subject_id] = subject;
            return map;
        }, {});

        const nodesMap = nodes.reduce((map, node) => {
        // Map node_id to unique_id
        map[node.node_id] = node.unique_id;
        return map;
        }, {});
        
        const uniqueIdToServersMap = servers.reduce((map, server) => {
        // Iterate over each node_id in node_service_map
        Object.keys(server.node_service_map).forEach(node_id => {
            // Get the unique_id from nodesMap
            const unique_id = nodesMap[node_id];
            if (unique_id) { // Ensure the node_id exists in nodesMap
            // Initialize an array for the unique_id if it doesn't exist
                if (!map[unique_id]) {
                    map[unique_id] = [];
                }
                // Add the service with its service_id from node_service_map
                map[unique_id].push({
                    service_type: server.service_type,
                    attributes: server.attributes,
                    service_id: server.node_service_map[node_id]
                });
            }
        });
        return map;
        }, {});

        uniqueIdToSubjectIds = nodes.reduce((map, node) => {
            map[node.unique_id] = {
                publishers: node.publishers || [],
                subscribers: node.subscribers || [],
                servers: node.servers || []
            };
            console.log(map);
            return map;
        }, {});

        loadClickedCells();
        console.log('Initialized clickedCells:', Array.from(clickedCells));

        const tableData = nodes.map(node => ({
            uniqueId: node.unique_id,
            nodeId: node.node_id,
            nodeName: node.node_name,
            uptime: node.uptime,
            publishers: node.publishers.join(", "),
            subscribers: node.subscribers.join(", "),
            servers: node.servers.join(", "),
            clients: node.clients.join(", "),
            detailsData: {
                publishers: node.publishers,
                subscribers: node.subscribers,
                servers: node.servers,
                clients: node.clients,
            },
        }));

        console.log('Creating Tabulator table...');
        const table = new Tabulator("#nodeTable", {
            index: "uniqueId",
            data: tableData,
            layout: "fitColumns",
            responsiveLayout: "hide",
            pagination: "local",
            paginationSize: 10,
            height: "auto",
            tooltips: true,
            rowFormatter: function(row) {
                const data = row.getData();
                const rowElement = row.getElement();
                rowElement.setAttribute("data-unique-id", data.uniqueId);

                ["publishers", "subscribers", "servers", "clients"].forEach(field => {
                    const key = `${data.uniqueId}-${field}`;
                    if (clickedCells.has(key) && !rowElement.querySelector(`.sub-table-holder[data-field="${field}"]`)) {
                        console.log(`Expanding ${field} for ${data.uniqueId} during rowFormatter`);
                        if (field === "publishers") {
                            createPublishersSubTable(rowElement, data, subjectsMap);
                        } else if (field === "subscribers") {
                            createSubscribersSubTable(rowElement, data, subjectsMap);
                        } else if (field === "servers") {
                            createServersSubTable(rowElement, data, uniqueIdToServersMap);
                        }
                        const cell = rowElement.querySelector(`td.tabulator-cell[tabulator-field="${field}"]`);
                        if (cell) cell.classList.add("expanded");
                    }
                });
            },
            columns: [
                { title: "Node ID", field: "nodeId", sorter: "number", headerFilter: "input", hozAlign: "center", maxWidth: 120, responsive: 0, headerMenu },
                { title: "Unique ID", field: "uniqueId", sorter: "string", headerFilter: "input", width: 120, responsive: 2 },
                { title: "Node Name", field: "nodeName", sorter: "string", headerFilter: "input", editor: "input", maxWidth: 400,
                    cellEdited: async cell => await updateNodeInDatabase(cell.getRow().getData()) },
                { title: "Uptime", field: "uptime", sorter: "string", headerFilter: "input", maxWidth: 120, responsive: 2 },
                { title: "Publishers", field: "publishers", sorter: "string", headerFilter: "input", responsive: 1, cssClass: "clickable-cell",
                    cellClick: (e, cell) => toggleSubTable(cell, "publishers", createPublishersSubTable, subjectsMap) },
                { title: "Subscribers", field: "subscribers", sorter: "string", headerFilter: "input", responsive: 2, cssClass: "clickable-cell",
                    cellClick: (e, cell) => toggleSubTable(cell, "subscribers", createSubscribersSubTable, subjectsMap) },
                { title: "Servers", field: "servers", sorter: "string", headerFilter: "input", responsive: 1, cssClass: "clickable-cell",
                    cellClick: (e, cell) => toggleSubTable(cell, "servers", createServersSubTable, uniqueIdToServersMap) },
                { title: "Clients", field: "clients", sorter: "number", headerFilter: "number", responsive: 2},
                {
                    title: "Actions",
                    field: "actions",
                    responsive: 0,
                    headerSort: false,
                    formatter: cell => {
                        const rowData = cell.getRow().getData();
                        const isOffline = rowData.uptime === "Offline";

                        // Create container for buttons
                        const container = document.createElement("div");
                        container.style.display = "flex";
                        container.style.gap = "8px"; // Space between buttons
                        container.style.justifyContent = "center";

                        // Delete Button
                        const deleteButton = document.createElement("button");
                        deleteButton.innerHTML = "Delete";
                        deleteButton.className = "delete-button";
                        deleteButton.disabled = !isOffline;
                        if (isOffline) {
                            deleteButton.addEventListener("click", async () => {
                                await deleteNodeFromDatabase(rowData.uniqueId);
                                cell.getRow().delete();
                            });
                        }

                        // Registers Button
                        const registersButton = document.createElement("button");
                        registersButton.innerHTML = "Registers";
                        registersButton.className = "registers-button";
                        registersButton.disabled = isOffline; // Enabled only for active nodes
                        if (!isOffline) {
                            registersButton.addEventListener("click", () => {
                                toggleRegistersSubTable(cell, rowData.uniqueId);
                            });
                        }

                        // Append both buttons to container
                        container.appendChild(deleteButton);
                        container.appendChild(registersButton);

                        return container;
                    },
                    width: 160, // Increased width to accommodate two buttons
                    hozAlign: "center"
                }
            ]
        });

        console.log('Table object created:', table);
        return table;

    } catch (error) {
        console.error('Error loading nodes:', error);
        return null;
    }
}

function toggleSubTable(cell, field, createSubTableFn, subjectsMap) {
    const row = cell.getRow();
    const rowElement = row.getElement();
    const holderEl = rowElement.querySelector(`.sub-table-holder[data-field="${field}"]`);
    const uniqueId = row.getData().uniqueId;
    const key = `${uniqueId}-${field}`;

    if (holderEl) {
        holderEl.style.display = holderEl.style.display === "none" ? "block" : "none";
        cell.getElement().classList.toggle("expanded", holderEl.style.display === "block");
        clickedCells[holderEl.style.display === "block" ? "add" : "delete"](key);
    } else {
        console.log(`Expanding ${field} for ${uniqueId} on click`);
        createSubTableFn(rowElement, row.getData(), subjectsMap);
        clickedCells.add(key);
        cell.getElement().classList.add("expanded");
    }
    saveClickedCells();
}

function getSubTableByDom(uniqueId, field) {
    const rowElement = document.querySelector(`#nodeTable .tabulator-row[data-unique-id="${uniqueId}"]`);
    if (!rowElement) return null;
    const subTableEl = rowElement.querySelector(`.sub-table-holder[data-field="${field}"] > div`);
    if (!subTableEl) return null;
    return Tabulator.findTable(subTableEl)[0] || null;
}

function isSubTableVisible(uniqueId, field) {
    const rowElement = document.querySelector(`#nodeTable .tabulator-row[data-unique-id="${uniqueId}"]`);
    if (!rowElement) return false;
    const holderEl = rowElement.querySelector(`.sub-table-holder[data-field="${field}"]`);
    return holderEl && holderEl.style.display === "block";
}

async function startPeriodicUpdates() {
    let tableInstance = await populateNodesTable();
    let lastNodesHash = "";
    let lastSubjectsHash = "";
    let intervalId = null;

    const hashData = data => JSON.stringify(data);

    const updateActionsColumn = () => {
    if (!tableInstance) return;
    tableInstance.getRows().forEach(row => {
        const cell = row.getCell("actions");
        if (cell) {
            const rowData = row.getData();
            const isOffline = rowData.uptime === "Offline";

            // Create container for buttons
            const container = document.createElement("div");
            container.style.display = "flex";
            container.style.gap = "8px";
            container.style.justifyContent = "center";

            // Delete Button
            const deleteButton = document.createElement("button");
            deleteButton.innerHTML = "Delete";
            deleteButton.className = "delete-button";
            deleteButton.disabled = !isOffline;
            if (isOffline) {
                deleteButton.addEventListener("click", async () => {
                    await deleteNodeFromDatabase(rowData.uniqueId);
                    row.delete();
                });
            }

            // Registers Button
            const registersButton = document.createElement("button");
            registersButton.innerHTML = "Registers";
            registersButton.className = "registers-button";
            registersButton.disabled = isOffline;
            if (!isOffline) {
                    registersButton.addEventListener("click", () => {
                        toggleRegistersSubTable(cell, rowData.uniqueId);
                    });
                }

            // Append buttons to container
            container.appendChild(deleteButton);
            container.appendChild(registersButton);

            // Update cell content
            cell.getElement().innerHTML = "";
            cell.getElement().appendChild(container);
        }
    });
};

    const performUpdate = async () => {
        const nodes = await fetchInfo('/nodes');
        let subjects = [];
        try {
            subjects = await fetchInfo('/pub-sub-subjects');
        } catch (error) {
            console.warn('No pub-sub subjects available, proceeding with empty subjects:', error);
        }
    
        const nodesMap = nodes.reduce((map, node) => {
            map[node.unique_id] = node;
            return map;
        }, {});
        const subjectsMap = subjects.reduce((map, subject) => {
            map[subject.subject_id] = subject;
            return map;
        }, {});
    
        const newNodesHash = hashData(nodes);
        const newSubjectsHash = hashData(subjects);
    
        if (newNodesHash !== lastNodesHash) {
            console.log('New node data detected, updating main table...');
            allUniqueIds = nodes.map(node => node.unique_id);
            uniqueIdToSubjectIds = nodes.reduce((map, node) => {
                map[node.unique_id] = node.publishers || [];
                return map;
            }, {});
    
            const updatedData = nodes.map(node => ({
                uniqueId: node.unique_id,
                nodeId: node.node_id,
                nodeName: node.node_name,
                uptime: node.uptime,
                publishers: node.publishers.join(", "),
                subscribers: node.subscribers.join(", "),
                servers: node.servers.join(", "),
                clients: node.clients.join(", "),
                detailsData: {
                    publishers: node.publishers,
                    subscribers: node.subscribers,
                    servers: node.servers,
                    clients: node.clients,
                },
            }));
    
            await tableInstance.updateOrAddData(updatedData);
            const currentIds = new Set(tableInstance.getData().map(row => row.uniqueId));
            const newIds = new Set(nodes.map(node => node.unique_id));
            const deletedIds = [...currentIds].filter(id => !newIds.has(id));
            if (deletedIds.length > 0) {
                await tableInstance.deleteRow(deletedIds);
                console.log('Deleted rows:', deletedIds);
            }
            updateActionsColumn();
            lastNodesHash = newNodesHash;
            console.log('Main table updated at', new Date().toISOString());
        }
    
        if (newSubjectsHash !== lastSubjectsHash && allUniqueIds.some(uniqueId => 
            (clickedCells.has(`${uniqueId}-publishers`) && isSubTableVisible(uniqueId, "publishers")) ||
            (clickedCells.has(`${uniqueId}-subscribers`) && isSubTableVisible(uniqueId, "subscribers"))
        )) {
            console.log('New subject data detected, updating visible Publishers and Subscribers sub-tables...');
            allUniqueIds.forEach(uniqueId => {
                // Update Publishers sub-tables
                const publisherKey = `${uniqueId}-publishers`;
                if (clickedCells.has(publisherKey) && isSubTableVisible(uniqueId, "publishers")) {
                    const subTable = getSubTableByDom(uniqueId, "publishers");
                    if (subTable) {
                        const publishersString = uniqueIdToSubjectIds[uniqueId].join(", ") || "";
                        const matchingSubjects = Object.keys(subjectsMap).filter(subject => publishersString.includes(subject));
                        const updatedData = matchingSubjects.reduce((acc, subjectId) => {
                            const subject = subjectsMap[subjectId];
                            if (subject.attributes && subject.attributes.length > 0) {
                                subject.attributes.forEach((attr, index) => {
                                    acc.push({
                                        subject_id: subject.subject_id,
                                        isFirstRow: index === 0,
                                        attribute: attr.attribute,
                                        value: attr.value,
                                        unit: attr.unit || "N/A",
                                        message_type: subject.message_type,
                                        rate: subject.rate,
                                        timestamp: subject.timestamp
                                    });
                                });
                            } else {
                                acc.push({
                                    subject_id: subject.subject_id,
                                    isFirstRow: true,
                                    attribute: "N/A",
                                    value: "N/A",
                                    unit: "N/A",
                                    message_type: subject.message_type,
                                    rate: subject.rate,
                                    timestamp: subject.timestamp
                                });
                            }
                            return acc;
                        }, []);
                        subTable.replaceData(updatedData);
                        console.log(`Sub-table for publishers (uniqueId: ${uniqueId}) updated at`, new Date().toISOString());
                    }
                }
    
                // Update Subscribers sub-tables
                const subscriberKey = `${uniqueId}-subscribers`;
                if (clickedCells.has(subscriberKey) && isSubTableVisible(uniqueId, "subscribers")) {
                    const subTable = getSubTableByDom(uniqueId, "subscribers");
                    if (subTable) {
                        const subscribersString = nodesMap[uniqueId].subscribers.join(", ") || "";
                        const matchingSubjects = Object.keys(subjectsMap).filter(subject => subscribersString.includes(subject));
                        const updatedData = matchingSubjects.reduce((acc, subjectId) => {
                            const subject = subjectsMap[subjectId];
                            if (subject.attributes && subject.attributes.length > 0) {
                                subject.attributes.forEach((attr, index) => {
                                    acc.push({
                                        subject_id: subject.subject_id,
                                        isFirstRow: index === 0,
                                        attribute: attr.attribute,
                                        value: attr.value,
                                        unit: attr.unit || "N/A",
                                        message_type: subject.message_type,
                                        rate: subject.rate,
                                        timestamp: subject.timestamp
                                    });
                                });
                            } else {
                                acc.push({
                                    subject_id: subject.subject_id,
                                    isFirstRow: true,
                                    attribute: "N/A",
                                    value: "N/A",
                                    unit: "N/A",
                                    message_type: subject.message_type,
                                    rate: subject.rate,
                                    timestamp: subject.timestamp
                                });
                            }
                            return acc;
                        }, []);
                        subTable.replaceData(updatedData);
                        console.log(`Sub-table for subscribers (uniqueId: ${uniqueId}) updated at`, new Date().toISOString());
                    }
                }
            });
            lastSubjectsHash = newSubjectsHash;
        } else if (newNodesHash === lastNodesHash && newSubjectsHash === lastSubjectsHash) {
            console.log('No changes detected in nodes or subjects.');
        }
    };

    const updateRateSlider = document.getElementById("update-rate-slider");
    const updateRateSelect = document.getElementById("update-rate-select");
    const sliderValueDisplay = document.querySelector(".slider-value");

    // Mapping of slider values to update rates and display text
    const rateMapping = [
        { value: "1000", display: "1 s" },
        { value: "3000", display: "3 s" },
        { value: "5000", display: "5 s" },
        { value: "10000", display: "10 s" },
        { value: "30000", display: "30 s" },
        { value: "60000", display: "1 m" },
        { value: "180000", display: "3 m" },
        { value: "300000", display: "5 m" },
        { value: "600000", display: "10 m" },
        { value: "never", display: "never" }
    ];

    const startInterval = (intervalValue) => {
        if (intervalId) {
            clearInterval(intervalId);
            intervalId = null;
        }

        if (intervalValue === "never") {
            console.log("Periodic updates stopped.");
            return;
        }

        const ms = parseInt(intervalValue, 10);
        intervalId = setInterval(performUpdate, ms);
        console.log(`Periodic updates started with interval: ${ms}ms`);
    };

    // Update the display and hidden select when the slider changes
    const updateSliderDisplay = () => {
        const sliderIndex = parseInt(updateRateSlider.value, 10);
        const mapping = rateMapping[sliderIndex];
        sliderValueDisplay.textContent = mapping.display;
        updateRateSelect.value = mapping.value;

        // Trigger the change event on the hidden select to update the interval
        const event = new Event('change', { bubbles: true });
        updateRateSelect.dispatchEvent(event);
    };

    // Initial setup
    updateSliderDisplay();
    startInterval(updateRateSelect.value);

    // Listen for slider changes
    updateRateSlider.addEventListener("input", updateSliderDisplay);

    // Listen for changes on the hidden select (triggered by slider)
    updateRateSelect.addEventListener("change", (e) => {
        startInterval(e.target.value);
    });
}

// Stop periodic updates
function stopPeriodicUpdates() {
    if (updateInterval) {
        clearInterval(updateInterval);
        updateInterval = null;
        console.log("Periodic updates stopped");
    }
}

async function fetchCanInterfaces() {
    try {
        let response = await fetch("/can-interfaces");
        if (!response.ok) throw new Error("No CAN interfaces found");

        let data = await response.json();
        let dropdown = document.getElementById("can-interface-dropdown");
        
        dropdown.innerHTML = "";
        data.forEach(iface => {
            let option = document.createElement("option");
            option.value = iface;
            option.textContent = iface;
            dropdown.appendChild(option);
        });
    } catch (error) {
        console.error("Error fetching CAN interfaces:", error);
    }
}

// Reload the page
function reloadPage() {
    if (isBackendReady) {
        window.location.reload();
    } else {
        console.log("Cannot reload: backend not ready yet");
    }
}

async function startMonitoring() {
    try {
        const canInterface = document.getElementById("can-interface-dropdown").value;
        const bitrate = document.getElementById("can-bitrate-dropdown").value;

        if (!canInterface) {
            alert("Please select a CAN interface");
            return;
        }
        showWaitClock(); // Show clock if monitoring is active
        const response = await fetch("/start-monitoring", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                can_interface: canInterface,
                bitrate: parseInt(bitrate)
            })
        });

        if (!response.ok) {
            throw new Error("Failed to start monitoring");
        }

        const result = await response.json();
        startPeriodicUpdates();
        checkBackendReadiness();
        console.log("Monitoring started:", result.message);
    } catch (error) {
        console.error("Error starting monitoring:", error);
        alert("Failed to start monitoring: " + error.message);
    }
}

// Stop monitoring with API call
async function stopMonitoring() {
    try {
        const response = await fetch("/stop-monitoring", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });

        if (!response.ok) {
            throw new Error("Failed to stop monitoring");
        }

        const result = await response.json();
        stopPeriodicUpdates(); // Stop updates after successful stop
        isBackendReady = false;
        hideWaitClock();
        const reloadButton = document.getElementById("reload-button");
        if (reloadButton) reloadButton.disabled = true;
        console.log("Monitoring stopped:", result.message);
    } catch (error) {
        console.error("Error stopping monitoring:", error);
        // Only alert if the backend stop failed, not client-side issues
        if (!error.message.includes("updateInterval")) {
            alert("Failed to stop monitoring: " + error.message);
        }
    }
}

// Toggle button states and disable/enable accordingly
function toggleButtonState(buttonId) {
    const startButton = document.getElementById("start-button");
    const stopButton = document.getElementById("stop-button");

    if (buttonId === "start-button" && !startButton.classList.contains("pressed")) {
        startButton.classList.add("pressed");
        stopButton.classList.remove("pressed");
        startButton.disabled = true;  // Disable Start when pressed
        stopButton.disabled = false;  // Enable Stop
        localStorage.setItem("pressedButton", "start-button");
        startMonitoring(); // API call and start updates
    } else if (buttonId === "stop-button" && !stopButton.classList.contains("pressed")) {
        stopButton.classList.add("pressed");
        startButton.classList.remove("pressed");
        stopButton.disabled = true;   // Disable Stop when pressed
        startButton.disabled = false; // Enable Start
        localStorage.setItem("pressedButton", "stop-button");
        stopMonitoring(); // API call and stop updates
    }
    // If the button is already pressed, do nothing (effectively blocked)
}

// Restore button state based on backend status
function restoreButtonState() {function collapseAllSubTables() {
    // Get all rows in the main table
    const rows = document.querySelectorAll("#nodeTable .tabulator-row");
    const fields = ["publishers", "subscribers", "servers", "clients"];

    rows.forEach(row => {
        const uniqueId = row.getAttribute("data-unique-id");
        fields.forEach(field => {
            const key = `${uniqueId}-${field}`;
            const holderEl = row.querySelector(`.sub-table-holder[data-field="${field}"]`);
            const cell = row.querySelector(`td.tabulator-cell[tabulator-field="${field}"]`);

            if (holderEl && holderEl.style.display === "block") {
                holderEl.style.display = "none";
                clickedCells.delete(key);
                if (cell) cell.classList.remove("expanded");
            }
        });
    });

    // Save the updated clickedCells state
    saveClickedCells();
    console.log("All sub-tables collapsed");
}
    fetch('/status')
        .then(response => response.json())
        .then(data => {
            const startButton = document.getElementById("start-button");
            const stopButton = document.getElementById("stop-button");

            if (data.is_monitoring) {
                startButton.classList.add("pressed");
                stopButton.classList.remove("pressed");
                startButton.disabled = true;  // Disable Start if running
                stopButton.disabled = false;  // Enable Stop
                localStorage.setItem("pressedButton", "start-button");
                startPeriodicUpdates(); // Start updates if monitoring is active
            } else {
                stopButton.classList.add("pressed");
                startButton.classList.remove("pressed");
                stopButton.disabled = true;   // Disable Stop if not running
                startButton.disabled = false; // Enable Start
                localStorage.setItem("pressedButton", "stop-button");
                stopPeriodicUpdates(); // Ensure updates are off if not running
            }
        })
        .catch(error => {
            console.error('Error fetching status:', error);
            // Fallback to stopped state on error
            const startButton = document.getElementById("start-button");
            const stopButton = document.getElementById("stop-button");
            stopButton.classList.add("pressed");
            startButton.classList.remove("pressed");
            stopButton.disabled = true;
            startButton.disabled = false;
            localStorage.setItem("pressedButton", "stop-button");
        });
}

let isBackendReady = false; // Track when nodes and pub-sub are ready


// Check if backend is ready (nodes and pub-sub available)
function checkBackendReadiness() {
    const reloadButton = document.getElementById("reload-button");
    if (reloadButton) reloadButton.disabled = true;

    const checkInterval = setInterval(() => {
        fetch('/nodes')
            .then(res => ({ url: '/nodes', status: res.status }))
            .then(result => {
                if (result.status === 200) {
                    clearInterval(checkInterval);
                    isBackendReady = true;
                    console.log("Backend is ready: nodes available");
                    if (reloadButton) reloadButton.disabled = false;
                    hideWaitClock();
                    reloadPage(); // Uncomment if auto-reload is desired
                } else {
                    console.log("Waiting for nodes:", result);
                }
            })
            .catch(error => console.error('Error checking backend readiness:', error));
    }, 1000);
}

// Show the wait clock GIF
function showWaitClock() {
    const waitClock = document.getElementById("wait-clock");
    if (waitClock) waitClock.style.display = "block";
}

// Hide the wait clock GIF
function hideWaitClock() {
    const waitClock = document.getElementById("wait-clock");
    if (waitClock) waitClock.style.display = "none";
}

function collapseAllSubTables() {
    const rows = document.querySelectorAll("#nodeTable .tabulator-row");
    const fields = ["publishers", "subscribers", "servers", "clients", "registers"];

    rows.forEach(row => {
        const uniqueId = row.getAttribute("data-unique-id");
        fields.forEach(field => {
            const key = `${uniqueId}-${field}`;
            const holderEl = row.querySelector(`.sub-table-holder[data-field="${field}"]`);
            // Only query for cell if field is not "registers"
            const cell = field !== "registers" ? 
                row.querySelector(`td.tabulator-cell[tabulator-field="${field}"]`) : null;

            if (holderEl && holderEl.style.display === "block") {
                holderEl.style.display = "none";
                clickedCells.delete(key);
                if (cell) cell.classList.remove("expanded");
            }
        });
    });

    saveClickedCells();
    console.log("All sub-tables collapsed");
}



document.addEventListener("DOMContentLoaded", () => {
      
    const startButton = document.getElementById("start-button");
    const stopButton = document.getElementById("stop-button");
    
    const collapseAllButton = document.getElementById("collapse-all-button"); // New button

    // Restore state without API calls
    restoreButtonState();

    // Event listeners with toggle logic
    startButton.addEventListener("click", () => toggleButtonState("start-button"));
    stopButton.addEventListener("click", () => toggleButtonState("stop-button"));
    if (collapseAllButton) {
        collapseAllButton.addEventListener("click", collapseAllSubTables);
    }
    fetchCanInterfaces();
});