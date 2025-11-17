# Introduction
This document serves for the documentation purposes of the WebMonitor Architecture.

# Architecture
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
WebMonitor follows a two-part architecture:
- Backend: A Cyphal node implemented using PyCyphal that monitors the CAN bus, reads/writes data to a MongoDB database, and handles register operations for Cyphal nodes based on requests.
- Frontend: A FastAPI application hosting a web interface on localhost. The **script.js** file renders dynamic tables from MongoDB data and enables register modification functionality.
![#Pic](WebMonitorArch.drawio.png)

As could be seen from this diagramm the application consists of 11 important modules, which are defining application functionality:
1. **run_monitor.py** - Entry script located in the repository root, used to run the application from source. It serves as a wrapper to initialize and start the WebMonitor application, including setting up the backend Cyphal node and launching the FastAPI frontend. Additionally, it can be used as a reference for building the application into an executable using **PyInstaller** by leveraging the **MonitorApp.spec** file.

Here concrete web adress can be specified.
```python
from frontend.web_app import app
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

2. Consists of **web_app.py** which renders **index.html** controlled by script.js and styled by **styles.css**. 
- **web_app.py** -  Module located in the **frontend** directory, used to define and run the FastAPI web application. It serves as the entry point for the web interface, handling HTTP requests, serving the **index.html** template, and providing API endpoints to interact with the backend (e.g., fetching node data from MongoDB and processing register modifications).

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # Allows requests from any domain
    allow_credentials=True, # Allows cookies, authorization headers, or TLS client certificates to be sent with requests.
    allow_methods=["*"],    # Permits all HTTP methods like GET, POST, PUT, DELETE, etc.
    allow_headers=["*"],    # Accepts any headers from incoming requests, like Authorization, Content-Type, or custom headers.
)
```
CORS allows web frontend and backend to communicate across different domains.

```python
client              = MongoClient('mongodb://user:wicon@localhost:27018/admin', serverSelectionTimeoutMS=5000)
    client.server_info()
    db                  = client['cyphal_database']
    nodes_collection    = db['cyphal_nodes']
    pub_sub_collection  = db['pub_sub_subjects']
    server_collection   = db['servers']
```
The **MongoClient** connects to a **MongoDB** instance running on **localhost:27018** with the username **user** and password **wicon**, using the admin database for authentication. The **serverSelectionTimeoutMS=5000** parameter sets a 5-second timeout for server selection, ensuring the application doesn’t hang indefinitely if the MongoDB instance is unavailable.

```python
@app.get("/", response_class=FileResponse)
def read_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    return FileResponse(index_path)
```
The **@app.get("/")** decorator specifies that this function handles HTTP GET requests to the root URL (e.g., http://localhost:8000/). This route serves as the entry point for the web interface, rendering the HTML template that includes the dynamic tables and interactive elements defined in script.js and styled with CSS from the static directory.

```python
# Helper function to convert ObjectId to string
def convert_id(document):
    document['_id'] = str(document['_id'])
    return document

@app.get("/nodes", response_class=JSONResponse)
def get_nodes():
    try:
        nodes = list(nodes_collection.find())
        if not nodes:
            raise HTTPException(status_code=404, detail="No nodes found")
        nodes = [convert_id(node) for node in nodes]
        return nodes
    except Exception as e:
        print(f"Error fetching nodes: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching nodes: {str(e)}")
```
The **nodes_collection.find()** method queries the **cyphal_nodes MongoDB collection** to fetch all node documents. The results are converted to a list. **convert_id** takes a MongoDB document (e.g., a node record from the cyphal_nodes collection) and converts its _id field, which is a MongoDB ObjectId object, into a string. This is necessary because ObjectId objects are not natively serializable to JSON, and the FastAPI JSONResponse requires string-compatible data. The same approach is used for **get_pub_sub_subjects** and **get_services** methods. The **get_node_registers** method differs by using the database to resolve **node_id** from **unique_id** before calling **scanner.get_registers(node_id)**, aligning with the Cyphal node’s register access mechanism in **scanner_node.py**. **set_node_register** uses the same strategy to set a specified register.

```python
@app.get("/nodes/uptime", response_class=JSONResponse)
def get_nodes_uptime():
    try:
        nodes = list(nodes_collection.find({}, {"_id": 1, "uptime": 1}))
        if not nodes:
            raise HTTPException(status_code=404, detail="No nodes found")
        uptime_updates = {str(node["_id"]): node["uptime"] for node in nodes}
        return uptime_updates
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching uptime: {str(e)}")
```
Here is only specific field **(uptime)** is fetched from the documents. Same logic for **get_subject_timestamp** method.

```python
@app.delete("/nodes/{id}", response_class=JSONResponse)
async def delete_node(id: str):
    try:
        result = nodes_collection.delete_one({"unique_id": id})
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail=f"Node not found with id {id}")
            
        return JSONResponse(
            content={"message": f"Node with id {id} deleted successfully."}, 
            status_code=200
        )
    except Exception as e:
        print(f"Error deleting node: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting node: {str(e)}")
```
This route allows manual removal of node data from the database. It is recommended to use this only for offline nodes, as the **ScannerNode (implemented in scanner_node.py)** continuously monitors the CAN bus and will re-add any active node to the database upon receiving a heartbeat message if no document exists, due to its lack of tracking deleted nodes (Bug or Feature?).

```python
#can.py
import subprocess

def list_can_interfaces():
    can_interfaces = []
    try:
        result = subprocess.run(['ip', 'link'], capture_output=True, text=True)
        output = result.stdout
        
        for line in output.splitlines():
            if 'can' in line:
                parts = line.split()
                if len(parts) > 1:
                    iface = parts[1].strip(':')
                    can_interfaces.append(iface)
    except subprocess.CalledProcessError as e:
        print(f"Error running ip link: {e}")
    
    return can_interfaces
```
```python
@app.get("/can-interfaces", response_class=JSONResponse)
def get_can_interfaces():
    interfaces = list_can_interfaces()
    print(f"Interfaces: {interfaces}")
    if not interfaces:
        raise HTTPException(status_code=404, detail="No CAN interfaces found")
    return interfaces
```
**get_can_interfaces** returns all available CAN intefaces using **can.py** module.

```python
class StartMonitoringRequest(BaseModel):
    can_interface: str
    bitrate: int

@app.post("/start-monitoring", response_class=JSONResponse)
async def start_monitoring_endpoint(request: StartMonitoringRequest):
    try:
        await monitor.start_monitoring(request.can_interface)  # Non-blocking
        print(f"Started monitoring with CAN interface: {request.can_interface}, bitrate: {request.bitrate} Kbps")
        return JSONResponse(
            content={"message": f"Monitoring started with interface {request.can_interface} at {request.bitrate} Kbps"},
            status_code=200
        )
    except Exception as e:
        print(f"Error starting monitoring: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error starting monitoring: {str(e)}")
```

 Handles HTTP POST requests to initiate CAN bus monitoring with a specified interface and bitrate (via a StartMonitoringRequest model), triggering the **monitor.start_monitoring** method. 
 
 **stop_monitoring_endpoint** method triggers **monitor.stop_monitoring**, **get_monitoring_status** checks the **monitor._monitor_task**.

 ```python
class Attribute(BaseModel):
    value: Any # Allow any type (parsed from JSON input)
    type: Optional[str] = None # Optional type field

class ServiceRequest(BaseModel):
    node_id: int
    unique_id: str
    service_type: str
    attributes: Dict[str, Attribute]

@app.post("/services/{service_id}/make_request", response_class=JSONResponse)
async def send_service_request(service_id: str, request: ServiceRequest):
    try:
        print(f"Processing service request: {request}")
        scanner = monitor.scanner_node
        if not scanner:
            raise HTTPException(status_code=503, detail="Monitoring is not running")
        response_str = await scanner.make_service_call(
            node_id=request.node_id,
            service_id=int(service_id),
            service_type=request.service_type,
            attributes={k: {"value": v.value, "type": v.type} for k, v in request.attributes.items()}
        )
        return JSONResponse(
            content={"response": response_str},
            status_code=200
        )
    except HTTPException as e:
        raise e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error processing service request: {str(e)}")
 ```

This route allows users to make service requests to any Cyphal node if its **node_id**, **unique_id**, **service_type**, and **service_id** are known, leveraging the **make_service_call** method from **scanner_node.py** to interact with the node’s services. Where service request attributes are presented by their values and types - for example reconstructed request could look this like:
```
{
  "node_id": 42,
  "unique_id": "a1b2c3...",
  "service_type": "dontpanic.SumService_1_0",
  "attributes": {
    "input1": {"value": 5, "type": "integer32"},
    "input2": {"value": 10, "type": "integer32"}
  }
}
```

- 2.1 **index.html** - Located in the **frontend/templates** directory, this HTML file serves as the main webpage template. It is rendered by **web_app.py** and provides the structure for the web interface, including placeholders for dynamic content (e.g., tables of nodes, subjects, and services) that are populated by **script.js**.

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cyphal Monitor</title>
    <link rel="icon" type="image/svg+xml" href="/static/images/can.png">
    <link rel="stylesheet" href="/static/css/style.css">
    <link href="https://unpkg.com/tabulator-tables@5.5.2/dist/css/tabulator.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css">
</head>
<body>
    <div id="controls-container">
        <div id="can-interface-panel">
            <label for="can-interface-dropdown">CAN Interfaces</label>
            <select id="can-interface-dropdown">
                <option value="">Loading...</option>
            </select>
        </div>
        <div id="can-bitrate-panel">
            <label for="can-bitrate-dropdown">Bitrate</label>
            <select id="can-bitrate-dropdown">
                <option value="1000000">1000 Kbps</option>
                <option value="800000">800 Kbps</option>
                <option value="500000">500 Kbps</option>
                <option value="250000">250 Kbps</option>
                <option value="125000">125 Kbps</option>
                <option value="100000">100 Kbps</option>
                <option value="50000">50 Kbps</option>
                <option value="20000">20 Kbps</option>
                <option value="10000">10 Kbps</option>
            </select>
        </div>
        <div class="wait-clock-container">
            <img id="wait-clock" src="/static/images/sand-clock-loader.gif" alt="Loading..." style="display: none;">
        </div>
        <div id="button-panel">
            <button id="start-button">Start</button>
            <button id="stop-button">Stop</button>
        </div>
        <div id="update-rate-panel">
            <label for="update-rate-slider">Update Rate</label>
            <input type="range" id="update-rate-slider" min="0" max="9" value="1" step="1">
            <span class="slider-value"></span>
            <select id="update-rate-select" style="display: none;">
                <option value="1000" selected>1 s</option>
                <option value="3000">3 s</option>
                <option value="5000">5 s</option>
                <option value="10000">10 s</option>
                <option value="30000">30 s</option>
                <option value="60000">1 m</option>
                <option value="180000">3 m</option>
                <option value="300000">5 m</option>
                <option value="600000">10 m</option>
                <option value="never">never</option>
            </select>
        </div>
        <div id="collapse-all-panel">
            <button id="collapse-all-button">Collapse All</button>
        </div>
    </div>
    <div id="nodeTable"></div>
    <script src="https://unpkg.com/tabulator-tables@5.5.2/dist/js/tabulator.min.js"></script>
    <script src="/static/js/script.js"></script>
</body>
</html>
```

Index.html is pretty self-explanatory with the only note:
```html
<script src="https://unpkg.com/tabulator-tables@5.5.2/dist/js/tabulator.min.js"></script>
```
The tabulator.min.js file provides the JavaScript API to create, configure, and manipulate [**Tabulator**](https://tabulator.info/) tables. In this application, **script.js** uses this library to initialize the table in the **\<div id="nodeTable">** element, populate it with data fetched from API endpoints (e.g., /nodes), and handle user interactions (e.g., sorting, filtering, or collapsing rows based on the "Collapse All" button).

- 2.2 **styles.css**: Located in the **frontend/static/css** directory, this CSS file styles the web interface defined in **index.html**. It controls the layout, colors, and responsiveness of the tables, buttons, and other elements, ensuring a user-friendly and visually consistent experience.

- 2.3 **script.js**: located in the **frontend/static/js** directory, forms the dynamic control layer of the **WebMonitor** web application. It leverages the [**Tabulator**](https://tabulator.info/)  library to create interactive tables and manages user interactions, API calls, and periodic data updates. The architecture is event-driven, centered around the main node table (**#nodeTable**), with supporting functions for sub-tables (e.g., registers, publishers, subscribers, servers), state persistence, and monitoring control. The code is organized to handle real-time data from the backend (via **web_app.py** API endpoints) and provide a responsive user experience.

```js
document.addEventListener("DOMContentLoaded", () => {
    const startButton = document.getElementById("start-button");
    const stopButton = document.getElementById("stop-button");
    const collapseAllButton = document.getElementById("collapse-all-button");

    restoreButtonState();

    // Event listeners with toggle logic
    startButton.addEventListener("click", () => toggleButtonState("start-button"));
    stopButton.addEventListener("click", () => toggleButtonState("stop-button"));
    if (collapseAllButton) {
        collapseAllButton.addEventListener("click", collapseAllSubTables);
    }
    fetchCanInterfaces();
});
```
The **DOMContentLoaded** event fires when the initial HTML document has been completely loaded and parsed, without waiting for stylesheets, images, or subframes to finish loading. This ensures the DOM elements (e.g., buttons, dropdowns) are accessible.

```js
function restoreButtonState() {
    // Function to collapse all expanded sub-tables in the main table
    function collapseAllSubTables() {
        // Select all rows within the main node table
        const rows = document.querySelectorAll("#nodeTable .tabulator-row");
        // Define the fields that may contain sub-tables
        const fields = ["publishers", "subscribers", "servers", "clients"];
        rows.forEach(row => {
            // Each row has a unique identifier used to manage sub-table states
            const uniqueId = row.getAttribute("data-unique-id");
            fields.forEach(field => {
                // Generate a key that ties a specific field to this row
                const key = `${uniqueId}-${field}`;
                // Find the container element holding the sub-table
                const holderEl = row.querySelector(`.sub-table-holder[data-field="${field}"]`);
                // Find the cell element that toggles expansion
                const cell = row.querySelector(`td.tabulator-cell[tabulator-field="${field}"]`);
                // Collapse the sub-table if it's currently visible
                if (holderEl && holderEl.style.display === "block") {
                    holderEl.style.display = "none";        // Hide the sub-table
                    clickedCells.delete(key);               // Remove tracking for this key
                    if (cell) cell.classList.remove("expanded"); // Remove visual indicator
                }
            });
        });
        // Save the updated state of collapsed cells
        saveClickedCells();
        console.log("All sub-tables collapsed");
    }
    // Call the collapse function to reset sub-table states
    collapseAllSubTables();
    // Fetch current monitoring status from the backend
    fetch('/status')
        .then(response => response.json())
        .then(data => {
            // Get references to the Start and Stop buttons
            const startButton = document.getElementById("start-button");
            const stopButton = document.getElementById("stop-button");

            if (data.is_monitoring) {
                // Monitoring is active: highlight Start and disable it
                startButton.classList.add("pressed");
                stopButton.classList.remove("pressed");
                startButton.disabled = true;
                stopButton.disabled = false;
                localStorage.setItem("pressedButton", "start-button");

                // Begin auto-refreshing data (function defined elsewhere)
                startPeriodicUpdates();
            } else {
                // Monitoring is inactive: highlight Stop and disable it
                stopButton.classList.add("pressed");
                startButton.classList.remove("pressed");
                stopButton.disabled = true;
                startButton.disabled = false;
                localStorage.setItem("pressedButton", "stop-button");

                // Stop data refresh
                stopPeriodicUpdates();
            }
        })
        .catch(error => {
            // If the status fetch fails, default to "stopped" state
            console.error('Error fetching status:', error);

            const startButton = document.getElementById("start-button");
            const stopButton = document.getElementById("stop-button");

            stopButton.classList.add("pressed");
            startButton.classList.remove("pressed");
            stopButton.disabled = true;
            startButton.disabled = false;
            localStorage.setItem("pressedButton", "stop-button");
        });
}

```
**restoreButtonState**: fetches the **/status** endpoint to determine if monitoring is active, setting the "Start" and "Stop" button states accordingly (e.g., disabling "Start" if monitoring is running). This connects to the backend to reflect the current monitoring state on the frontend side. Collapse any expanded sub-tables in a data grid.

```js
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
```
Collapse any visible sub-tables inside the grid (e.g., publishers, subscribers...), and clear their visual state and tracking.  Registers are managed via the **"Registers"** button in the **"Actions"** column of the main table, using **toggleRegistersSubTable** and **fetchAndUpdateRegistersSubTable**, rather than direct cell clicks like publishers, subscribers, and servers. The intention is to allow registers to remain expanded independently of other sub-tables.

```js
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
```
When a user expands sub-tables in the grid, their identifiers are stored in a Set called **clickedCells**. This Set is stored in the **browser's localStorage**, The restoration ensures the grid can reapply expansion to the correct sub-table elements as if the session never ended between reloads or page closing.

```js
async function startPeriodicUpdates() {
    // The startPeriodicUpdates function is responsible for initializing and managing periodic updates of the node table and its sub-tables based on real-time data from the backend. It leverages the Tabulator library to update the main table (#nodeTable) and visible sub-tables (publishers and subscribers) when changes are detected, with the update frequency controlled by a slider. 

    // Initialize the table instance by awaiting populateNodesTable, which sets up the main #nodeTable
    let tableInstance = await populateNodesTable();
    // Store the last hash of node data for change detection
    let lastNodesHash = "";
    // Store the last hash of subject data for change detection
    let lastSubjectsHash = "";
    // Hold the interval ID for periodic updates, initially null
    let intervalId = null;

    // Helper function to convert data to a JSON string for hashing
    const hashData = data => JSON.stringify(data);

    // Function to update the "Actions" column with Delete and Registers buttons for each row
    const updateActionsColumn = () => {
        // Exit if tableInstance is not available
        if (!tableInstance) return;
        // Iterate over each row in the table
        tableInstance.getRows().forEach(row => {
            const cell = row.getCell("actions"); // Get the "Actions" cell
            if (cell) {
                const rowData = row.getData(); // Get the row's data
                const isOffline = rowData.uptime === "Offline"; // Check if node is offline

                // Create a flex container for buttons
                const container = document.createElement("div");
                container.style.display = "flex";
                container.style.gap = "8px";
                container.style.justifyContent = "center";

                // Create Delete button
                const deleteButton = document.createElement("button");
                deleteButton.innerHTML = "Delete";
                deleteButton.className = "delete-button";
                deleteButton.disabled = !isOffline; // Enable only for offline nodes
                if (isOffline) {
                    deleteButton.addEventListener("click", async () => {
                        // Delete node from database and remove row on success
                        await deleteNodeFromDatabase(rowData.uniqueId);
                        row.delete();
                    });
                }

                // Create Registers button
                const registersButton = document.createElement("button");
                registersButton.innerHTML = "Registers";
                registersButton.className = "registers-button";
                registersButton.disabled = isOffline; // Enable only for online nodes
                if (!isOffline) {
                    registersButton.addEventListener("click", () => {
                        // Toggle the registers sub-table for this node
                        toggleRegistersSubTable(cell, rowData.uniqueId);
                    });
                }

                // Append buttons to container
                container.appendChild(deleteButton);
                container.appendChild(registersButton);

                // Clear and update the cell content with the new button container
                cell.getElement().innerHTML = "";
                cell.getElement().appendChild(container);
            }
        });
    };

    // Async function to perform the update logic
    const performUpdate = async () => {
        // Fetch the latest node data
        const nodes = await fetchInfo('/nodes');
        let subjects = [];
        try {
            // Fetch the latest subject data
            subjects = await fetchInfo('/pub-sub-subjects');
        } catch (error) {
            console.warn('No pub-sub subjects available, proceeding with empty subjects:', error);
        }

        // Create a map of nodes by unique_id
        const nodesMap = nodes.reduce((map, node) => {
            map[node.unique_id] = node;
            return map;
        }, {});
        // Create a map of subjects by subject_id
        const subjectsMap = subjects.reduce((map, subject) => {
            map[subject.subject_id] = subject;
            return map;
        }, {});

        // Generate hashes for change detection
        const newNodesHash = hashData(nodes);
        const newSubjectsHash = hashData(subjects);

        // Update main table if node data has changed
        if (newNodesHash !== lastNodesHash) {
            console.log('New node data detected, updating main table...');
            // Update global unique IDs array
            allUniqueIds = nodes.map(node => node.unique_id);
            // Update mapping of unique IDs to publishers
            uniqueIdToSubjectIds = nodes.reduce((map, node) => {
                map[node.unique_id] = node.publishers || [];
                return map;
            }, {});

            // Prepare updated table data
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

            // Update or add data to the table and handle deletions
            await tableInstance.updateOrAddData(updatedData);
            const currentIds = new Set(tableInstance.getData().map(row => row.uniqueId));
            const newIds = new Set(nodes.map(node => node.unique_id));
            const deletedIds = [...currentIds].filter(id => !newIds.has(id));
            if (deletedIds.length > 0) {
                await tableInstance.deleteRow(deletedIds);
                console.log('Deleted rows:', deletedIds);
            }
            // Refresh the Actions column
            updateActionsColumn();
            // Update the last hash
            lastNodesHash = newNodesHash;
            console.log('Main table updated at', new Date().toISOString());
        }

        // Update visible publisher/subscriber sub-tables if subject data has changed
        if (newSubjectsHash !== lastSubjectsHash && allUniqueIds.some(uniqueId => 
            (clickedCells.has(`${uniqueId}-publishers`) && isSubTableVisible(uniqueId, "publishers")) ||
            (clickedCells.has(`${uniqueId}-subscribers`) && isSubTableVisible(uniqueId, "subscribers"))
        )) {
            console.log('New subject data detected, updating visible Publishers and Subscribers sub-tables...');
            allUniqueIds.forEach(uniqueId => {
                // Update Publishers sub-table if visible
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

                // Update Subscribers sub-table if visible
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

    // Get DOM elements for update rate control
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

    // Function to start or stop the update interval
    const startInterval = (intervalValue) => {
        if (intervalId) {
            // Clear existing interval if it exists
            clearInterval(intervalId);
            intervalId = null;
        }

        if (intervalValue === "never") {
            console.log("Periodic updates stopped.");
            return;
        }

        // Start new interval with the specified milliseconds
        const ms = parseInt(intervalValue, 10);
        intervalId = setInterval(performUpdate, ms);
        console.log(`Periodic updates started with interval: ${ms}ms`);
    };

    // Function to update the slider display and trigger interval change
    const updateSliderDisplay = () => {
        const sliderIndex = parseInt(updateRateSlider.value, 10); // Get slider position
        const mapping = rateMapping[sliderIndex]; // Map to rate and display
        sliderValueDisplay.textContent = mapping.display; // Update displayed text
        updateRateSelect.value = mapping.value; // Sync hidden select

        // Trigger change event on hidden select to update interval
        const event = new Event('change', { bubbles: true });
        updateRateSelect.dispatchEvent(event);
    };

    // Initial setup
    updateSliderDisplay(); // Set initial display and select value
    startInterval(updateRateSelect.value); // Start initial interval

    // Listen for slider changes
    updateRateSlider.addEventListener("input", updateSliderDisplay);

    // Listen for changes on the hidden select (triggered by slider)
    updateRateSelect.addEventListener("change", (e) => {
        startInterval(e.target.value); // Adjust interval based on new value
    });
}
```

![Pic](Webmonitor.drawio.png)