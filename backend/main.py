#!/usr/bin/env python3

import asyncio
import logging
from .scanner_node import ScannerNode
from .node_data_manager import NodeDataManager

async def register_nodes(scanner: ScannerNode, registered_nodes_list: list, data_manager: NodeDataManager) -> None:
    try:
        for node in scanner.nodes:
            if node.has_appeared:
                if node.has_registered_ports and node.node_id not in registered_nodes_list and not node.has_disappeared:
                    registered_nodes_list.append(node.node_id)
                    data_manager.update_node_info(node)
                    # print(f"Node {node.node_id} information is added to the database")

                    dsdl_pub_messages, dsdl_srv_messages = await scanner.update_reg_list(node.node_id)
                    await scanner.add_subscriptions(node.node_id, dsdl_pub_messages)
                    server_info = await scanner.add_servers(node.node_id, dsdl_srv_messages)
                    if server_info:
                        data_manager.update_service_info(node.node_id, server_info) 

            if node.has_disappeared and node.node_id in registered_nodes_list:
                registered_nodes_list.remove(node.node_id)
                scanner.cleanup_subscriptions(node.node_id) 
                data_manager.update_node_id(node, node_id="not assigned")
                # print(f"Node {node.node_id} was removed from the node registration list")

    except Exception as e:
        print(f"Error in register_nodes: {str(e)}")
        raise

def update_registered_nodes(registered_nodes_list: list, data_manager: NodeDataManager) -> None:
    try:
        for node_id in registered_nodes_list:
            node = data_manager.all_nodes[node_id]
            data_manager.update_node_uptime(node)
            # print(f"Node {node_id} uptime is updated")

    except Exception as e:
        print(f"Error in update_registered_nodes: {str(e)}")
        raise

async def main_loop(set_scanner_node_callback=None):
    print("Initializing main loop...")
    try:
        scanner = ScannerNode()
        if set_scanner_node_callback:
            set_scanner_node_callback(scanner)
        print("ScannerNode initialized")
        data_manager = NodeDataManager(scanner.node, scanner.nodes, scanner.message_queue, batch_interval=1.0)
        print("NodeDataManager initialized")
        data_manager.start()
        print("NodeDataManager started")

        registered_nodes_list = []
        
        while True:
            await asyncio.sleep(1)
            await register_nodes(scanner, registered_nodes_list, data_manager)
            update_registered_nodes(registered_nodes_list, data_manager)
            
    except asyncio.CancelledError:
        print("Main loop cancelled, cleaning up...")
        data_manager.close()
        scanner.close()
        raise
    except Exception as e:
        print(f"Error in main_loop: {str(e)}")
        raise
    finally:
        print("Final cleanup...")
        data_manager.close()
        scanner.close()

if __name__ == "__main__":
    asyncio.run(main_loop())