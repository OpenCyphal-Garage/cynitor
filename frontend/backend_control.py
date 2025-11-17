# backend_control.py
#!/usr/bin/env python3

import os
import sys
import subprocess
import asyncio
import argparse
from .mongodb_control import MongoDBController


class MonitorController:
    def __init__(self, mongodb_ip="127.0.0.1", mongodb_port="27018", mongodb_db="cyphal_database",
                 mongodb_user="user", mongodb_admin="admin", mongodb_pwd="wicon"):
        """Initialize the monitor controller with MongoDB parameters."""
        self._monitor_task = None
        self.scanner_node = None
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        self.project_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mongodb_controller = MongoDBController(
            base_dir=self.project_directory,
            mongodb_ip=mongodb_ip,
            mongodb_port=mongodb_port,
            mongodb_db=mongodb_db,
            mongodb_user=mongodb_user,
            mongodb_admin=mongodb_admin,
            mongodb_pwd=mongodb_pwd
        )
        self._setup_paths()

    def _setup_paths(self):
        """Set up sys.path and compile DSDL messages."""
        base_dir = self.project_directory
        backend_dir = os.path.join(base_dir, "backend")
        sys.path.append(base_dir)
        sys.path.append(backend_dir)

        try:
            self._compile_dsdl_messages(backend_dir)
        except Exception as e:
            print(f"Compilation failed: {str(e)}")
            sys.exit(1)

        os.environ["PYCYPHAL_PATH"] = os.path.join(backend_dir, "python_compiled_messages")
        sys.path.append(os.environ["PYCYPHAL_PATH"])

    def _compile_dsdl_messages(self, backend_dir: str) -> None:
        """Compile DSDL messages into backend/python_compiled_messages/."""
        python_output_dir = os.path.join(backend_dir, "python_compiled_messages")
        uavcan_dir = os.path.join(backend_dir, "dsdl_messages", "public_regulated_data_types", "uavcan")
        reg_dir = os.path.join(backend_dir, "dsdl_messages", "public_regulated_data_types", "reg")
        custom_dir = os.path.join(backend_dir, "dsdl_messages", "dontpanic")

        for dir_path in [uavcan_dir, reg_dir, custom_dir]:
            if not os.path.isdir(dir_path):
                print(f"Error: Directory not found: {dir_path}")
                raise FileNotFoundError(f"Required DSDL directory missing: {dir_path}")

        os.makedirs(python_output_dir, exist_ok=True)

        commands = [
            ["nnvg", "--target-language", "py", reg_dir, "--lookup-dir", uavcan_dir, "--outdir", python_output_dir],
            ["nnvg", "--target-language", "py", uavcan_dir, "--lookup-dir", reg_dir, "--outdir", python_output_dir],
            ["nnvg", "--target-language", "py", "--enable-serialization-asserts", custom_dir,
             "--lookup-dir", reg_dir, "--lookup-dir", uavcan_dir, "--outdir", python_output_dir]
        ]

        for cmd in commands:
            try:
                result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(f"Command {' '.join(cmd)} executed successfully")
            except subprocess.CalledProcessError as e:
                print(f"Error compiling DSDL messages with command {' '.join(cmd)}: {e.stderr.decode()}")
                raise

        print("DSDL messages compiled successfully")

    def _setup_environment(self, can_interface: str) -> None:
        """Set up the environment for monitoring."""
        backend_dir = os.path.join(self.project_directory, "backend")
        os.environ["CYPHAL_PATH"] = f"{os.path.join(backend_dir, 'dsdl_messages', 'public_regulated_data_types')}:{os.environ.get('CYPHAL_PATH', '')}"
        os.environ["CYPHAL_PATH"] = f"{os.path.join(backend_dir, 'dsdl_messages', 'dontpanic')}:{os.environ['CYPHAL_PATH']}"
        os.environ["UAVCAN__CAN__IFACE"] = f"socketcan:{can_interface}"
        os.environ["UAVCAN__CAN__MTU"] = "8"

        try:
            node_id = subprocess.check_output(["yakut", "accommodate"]).decode().strip()
            os.environ["UAVCAN__NODE__ID"] = node_id
            print(f"Set UAVCAN__NODE__ID to {node_id}")
        except subprocess.CalledProcessError as e:
            print(f"Error running yakut accommodate: {e}")
            raise

    async def start_monitoring(self, can_interface: str) -> None:
        """Start the monitoring task in the background."""
        from backend.main import main_loop

        if self._monitor_task is not None and not self._monitor_task.done():
            print("Monitoring is already running")
            return

        self._setup_environment(can_interface)
        print(f"Starting monitoring with CAN interface: {can_interface}")
        try:
            def set_scanner_node(scanner):
                self.scanner_node = scanner
            self._monitor_task = asyncio.create_task(main_loop(set_scanner_node_callback=set_scanner_node))
            print("Monitoring task scheduled")
        except Exception as e:
            print(f"Error starting monitoring: {str(e)}")
            raise

    async def stop_monitoring(self) -> None:
        """Stop the running monitoring task and MongoDB server."""
        if self._monitor_task is None or self._monitor_task.done():
            print("No monitoring task is running")
        else:
            print("Stopping monitoring")
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                print("Monitoring task cancelled successfully")
            finally:
                self._monitor_task = None
                self.scanner_node = None

        await self.mongodb_controller.stop_mongodb_server()

    @staticmethod
    def parse_arguments():
        """Parse command-line arguments for MongoDB configuration."""
        parser = argparse.ArgumentParser(description="Monitor Controller with MongoDB")
        parser.add_argument("--ip", default="127.0.0.1", help="MongoDB IP address (default: 127.0.0.1)")
        parser.add_argument("--port", default="27018", help="MongoDB port (default: 27018)")
        parser.add_argument("--db", default="cyphal_database", help="MongoDB database name (default: cyphal_database)")
        parser.add_argument("--user", default="user", help="MongoDB username (default: user)")
        parser.add_argument("--pwd", default="wicon", help="MongoDB password (default: wicon)")
        return parser.parse_args()


if __name__ == "__main__":
    args = MonitorController.parse_arguments()
    controller = MonitorController(
        mongodb_ip=args.ip,
        mongodb_port=args.port,
        mongodb_db=args.db,
        mongodb_user=args.user,
        mongodb_pwd=args.pwd
    )
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(controller.start_monitoring("vcan0"))
    except KeyboardInterrupt:
        print("Received KeyboardInterrupt, shutting down...")
        loop.run_until_complete(controller.stop_monitoring())
    finally:
        if not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()