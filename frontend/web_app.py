#web_app.py
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from bson import ObjectId
from typing import Any, Dict, Optional
from pydantic import BaseModel

from .backend_control import MonitorController 
from .can import list_can_interfaces


app = FastAPI()

monitor = MonitorController()


# Access to all scripts in the static folder
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change this to specific origins in production for security
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers
)


try:
    client              = MongoClient('mongodb://user:wicon@localhost:27018/admin', serverSelectionTimeoutMS=5000)
    client.server_info()
    db                  = client['cyphal_database']
    nodes_collection    = db['cyphal_nodes']
    pub_sub_collection  = db['pub_sub_subjects']
    server_collection   = db['servers']

    client.server_info()  # This will raise an exception if the connection fails
    print("Successfully connected to MongoDB")

except Exception as e:
    print(f"Failed to connect to MongoDB: {e}")
    raise e



@app.get("/", response_class=FileResponse)
def read_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    return FileResponse(index_path)


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
    
@app.get("/pub-sub-subjects", response_class=JSONResponse)
def get_pub_sub_subjects():
    try:
        subjects = list(pub_sub_collection.find())  # Convert cursor to list
        if not subjects:
            raise HTTPException(status_code=404, detail="No pub_sub_subjects found")
        subjects = [convert_id(subject) for subject in subjects]
        return subjects
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching pub_sub_subjects: {str(e)}")
    
@app.get("/services", response_class=JSONResponse)
def get_services():
    try:
        services = list(server_collection.find())  # Assuming services are stored in server_collection
        if not services:
            raise HTTPException(status_code=404, detail="No services found")
        services = [convert_id(service) for service in services]
        return services
    except Exception as e:
        print(f"Error fetching services: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching services: {str(e)}")

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
    
@app.get("/pub-sub-subjects/timestamp", response_class=JSONResponse)
def get_subject_timestamp():
    try:
        timestamps = list(pub_sub_collection.find({}, {"subject_id": 1, "timestamp": 1}))
        if not timestamps:
            raise HTTPException(status_code=404, detail="No timestamps found")
        timestamp_updates = {str(subject["subject_id"]): subject["timestamp"] for subject in timestamps}
        return timestamp_updates
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching uptime: {str(e)}")
    
class NodeUpdate(BaseModel):
    node_name: str

@app.put("/nodes/{id}", response_class=JSONResponse)
async def update_node(id: str, node: NodeUpdate):
    try:
        object_id = ObjectId(id)
        result = nodes_collection.update_one({"_id": object_id}, {"$set": node.model_dump()})
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"Node not found with id {id}")
        
        return JSONResponse(
            content={"message": f"Node with id {id} updated successfully."}, 
            status_code=200
        )
    except Exception as e:
        print(f"Error updating node: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating node: {str(e)}")


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
    
@app.get("/can-interfaces", response_class=JSONResponse)
def get_can_interfaces():
    interfaces = list_can_interfaces()
    print(f"Interfaces: {interfaces}")
    if not interfaces:
        raise HTTPException(status_code=404, detail="No CAN interfaces found")
    return interfaces

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

@app.post("/stop-monitoring", response_class=JSONResponse)
async def stop_monitoring_endpoint():
    try:
        await monitor.stop_monitoring()
        print("Stopped monitoring")
        return JSONResponse(
            content={"message": "Monitoring stopped"},
            status_code=200
        )
    except Exception as e:
        print(f"Error stopping monitoring: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error stopping monitoring: {str(e)}")
    
@app.get("/status", response_class=JSONResponse)
async def get_monitoring_status():
    """Return the current monitoring state."""
    try:
        is_running = monitor._monitor_task is not None and not monitor._monitor_task.done()
        return JSONResponse(
            content={"is_monitoring": is_running},
            status_code=200
        )
    except Exception as e:
        print(f"Error getting status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting status: {str(e)}")
    

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

        # # Validate node_id and unique_id match
        # node = nodes_collection.find_one({"node_id": request.node_id, "unique_id": request.unique_id})
        # if not node:
        #     raise HTTPException(status_code=404, detail=f"Node with id {request.node_id} and unique_id {request.unique_id} not found")

        # # Validate service exists and belongs to the node
        # service = server_collection.find_one({
        #     f"node_service_map.{request.node_id}": int(service_id),
        #     "service_type": request.service_type
        # })
        # if not service:
        #     raise HTTPException(status_code=404, detail=f"Service {service_id} with type {request.service_type} not found for node {request.node_id}")

        # # Validate attributes
        # if not request.attributes:
        #     raise HTTPException(status_code=400, detail="No attributes provided for the service request")

        # Call service using ScannerNode
        response_str = await scanner.make_service_call(
            node_id=request.node_id,
            service_id=int(service_id),
            service_type=request.service_type,
            attributes={k: {"value": v.value, "type": v.type} for k, v in request.attributes.items()}
        )

        # # Store the response in server_collection
        # server_collection.update_one(
        #     {"_id": service["_id"]},
        #     {
        #         "$set": {
        #             "last_request": {
        #                 "node_id": request.node_id,
        #                 "unique_id": request.unique_id,
        #                 "service_id": int(service_id),
        #                 "service_type": request.service_type,
        #                 "attributes": request.attributes,
        #                 "response": response_str,
        #                 "timestamp": datetime.datetime.utcnow()
        #             }
        #         }
        #     },
        #     upsert=False
        # )

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

@app.get("/nodes/{unique_id}/registers", response_class=JSONResponse)
async def get_node_registers(unique_id: str):
    try:
        scanner = monitor.scanner_node
        if not scanner:
            raise HTTPException(status_code=503, detail="Monitoring is not running")

        # Fetch node from database to get node_id
        node = nodes_collection.find_one({"unique_id": unique_id})
        if not node:
            raise HTTPException(status_code=404, detail=f"Node with unique_id {unique_id} not found")

        node_id = node["node_id"]
        # Call scanner_node to fetch registers directly
        # Assuming scanner_node has a get_registers method
        registers = await scanner.get_registers(node_id=node_id)

        if not registers:
            raise HTTPException(status_code=404, detail=f"No registers found for node {unique_id}")

        return JSONResponse(
            content=registers,
            status_code=200
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error fetching registers for node {unique_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching registers: {str(e)}")

class RegisterUpdate(BaseModel):
    value: str
    type: str

@app.put("/nodes/{unique_id}/registers/{register_name}", response_class=JSONResponse)
async def set_node_register(unique_id: str, register_name: str, register_update: RegisterUpdate):
    try:
        scanner = monitor.scanner_node
        if not scanner:
            raise HTTPException(status_code=503, detail="Monitoring is not running")

        # Fetch node from database to get node_id
        node = nodes_collection.find_one({"unique_id": unique_id})
        if not node:
            raise HTTPException(status_code=404, detail=f"Node with unique_id {unique_id} not found")

        node_id = node["node_id"]
        # Call scanner_node to set the register value
        # Assuming scanner_node has a set_register method
        updated_value = await scanner.set_register(
            node_id=node_id,
            register_name=register_name,
            value=register_update.value,
            type=register_update.type
        )

        if updated_value is None:
            raise HTTPException(status_code=400, detail=f"Failed to set register {register_name}")

        return JSONResponse(
            content={"value": updated_value, "message": f"Register {register_name} set successfully"},
            status_code=200
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error setting register {register_name} for node {unique_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error setting register: {str(e)}")

# To run the server, use the following command:
# uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload