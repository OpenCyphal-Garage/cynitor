#!/usr/bin/env python3
# mongodb_control.py

import os
import sys
import subprocess
import time
import psutil
import tempfile


class MongoDBController:
    def __init__(self, base_dir, mongodb_ip="127.0.0.1", mongodb_port="27018", mongodb_db="cyphal_database",
                 mongodb_user="user", mongodb_admin="admin", mongodb_pwd="wicon"):
        """Initialize the MongoDB controller with configuration parameters."""
        self.mongodb_ip = mongodb_ip
        self.mongodb_port = mongodb_port
        self.mongodb_db = mongodb_db
        self.mongodb_user = mongodb_user
        self.mongodb_admin = mongodb_admin
        self.mongodb_pwd = mongodb_pwd
        self.mongod_process = None

        # Determine base directory for paths
        if getattr(sys, 'frozen', False):
            # Running in PyInstaller bundle
            self.base_dir = sys._MEIPASS
        else:
            # Running in development
            self.base_dir = base_dir
        print(f"Running from: {'bundle' if getattr(sys, 'frozen', False) else 'source'}")
        print(f"Base directory is: {self.base_dir}")

        # Set up paths
        self._setup_paths()
        
        # Determine path to mongod binary
        self.mongod_path = os.path.join(self.base_dir, "backend", "mongodb", "bin", "mongod")
        print(f"Looking for mongod at: {self.mongod_path}")
        if not os.path.exists(self.mongod_path):
            raise RuntimeError(f"mongod binary not found at {self.mongod_path}")

        if not os.path.exists(self.mongo_config_file):
            print("No existing mongo_config, creating new one")
            self.configure_mongodb_server()
        print(f"Using mongo_config at: {self.mongo_config_file}")
        self.start_mongodb_server()

    def _setup_paths(self):
        """Set up MongoDB directories and configuration file paths."""
        self.mongo_db_data_path = os.path.join(self.base_dir, "backend", "mongodb", "mongodb-data")
        self.mongo_db_socket_path = os.path.join(self.base_dir, "backend", "mongodb", "mongodb-socket")
        self.mongo_log_file = os.path.join(self.base_dir, "backend", "mongodb", "mongod.log")
        self.mongo_config_file = os.path.join(self.base_dir, "backend", "mongodb", "mongod.conf")
        
        # Create MongoDB directories with appropriate permissions
        os.makedirs(self.mongo_db_data_path, exist_ok=True)
        os.makedirs(self.mongo_db_socket_path, exist_ok=True)
        os.chmod(self.mongo_db_data_path, 0o755)
        os.chmod(self.mongo_db_socket_path, 0o755)

    def configure_mongodb_server(self):
        """Configure MongoDB server by creating mongod.conf, initializing users, and enabling authentication."""
        print(f"Creating MongoDB configuration file at {self.mongo_config_file}...")
        
        # Step 1: Create mongod.conf with authorization disabled
        config_content = f"""# MongoDB configuration file
storage:
  dbPath: {self.mongo_db_data_path}
systemLog:
  destination: file
  logAppend: true
  path: {self.mongo_log_file}
net:
  port: {self.mongodb_port}
  bindIp: {self.mongodb_ip}
  unixDomainSocket:
    enabled: true
    pathPrefix: {self.mongo_db_socket_path}
security:
  authorization: disabled
processManagement:
  timeZoneInfo: /usr/share/zoneinfo
"""
        try:
            with open(self.mongo_config_file, "w") as f:
                f.write(config_content)
            os.chmod(self.mongo_config_file, 0o644)
            print(f"Configuration file created: {self.mongo_config_file}")
        except Exception as e:
            print(f"Failed to create configuration file: {str(e)}")
            raise

        # Step 2: Start MongoDB without authentication
        print("Starting MongoDB without authentication to initialize users and database...")
        try:
            self.mongod_process = subprocess.Popen(
                [self.mongod_path, "--config", self.mongo_config_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            time.sleep(5)  # Wait for MongoDB to start

            if not psutil.pid_exists(self.mongod_process.pid):
                stdout, stderr = self.mongod_process.communicate()
                print(f"Failed to start MongoDB. Check logs in {self.mongo_log_file} for details.")
                print(f"mongod stdout: {stdout}")
                print(f"mongod stderr: {stderr}")
                raise RuntimeError("MongoDB failed to start")
        except Exception as e:
            print(f"Failed to start MongoDB for initialization: {str(e)}")
            raise

        # Step 3: Check if authentication is already enabled
        mongosh_path = os.path.join(os.path.dirname(self.mongod_path), "mongosh") if os.path.exists(os.path.join(os.path.dirname(self.mongod_path), "mongosh")) else "/usr/bin/mongosh"
        try:
            # Attempt to connect with authentication to check if users exist
            check_auth_cmd = [
                mongosh_path, "--host", self.mongodb_ip, "--port", self.mongodb_port,
                "-u", self.mongodb_admin, "-p", self.mongodb_pwd, "--authenticationDatabase", "admin",
                "--eval", f"db.getSiblingDB('admin').getUser('{self.mongodb_admin}')"
            ]
            result = subprocess.run(
                check_auth_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if result.returncode == 0:
                print("Authentication is already enabled and admin user exists. Skipping user creation.")
                # Shut down MongoDB to update config
                print("Shutting down MongoDB to enable authentication in config...")
                try:
                    subprocess.run(
                        [
                            mongosh_path, "--host", self.mongodb_ip, "--port", self.mongodb_port,
                            "-u", self.mongodb_admin, "-p", self.mongodb_pwd, "--authenticationDatabase", "admin",
                            "--eval", "db.getSiblingDB('admin').shutdownServer()"
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    self.mongod_process.wait(timeout=10)
                    self.mongod_process = None
                    print("MongoDB shut down successfully")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    print(f"Failed to shut down MongoDB gracefully: {str(e)}")
                    self._emergency_shutdown()
                return  # Exit early since users and auth are already set up
        except subprocess.CalledProcessError as e:
            print(f"Authentication check failed, proceeding to create users: {e.stderr}")

        # Step 4: Create database and users using mongosh
        print(f"Creating database '{self.mongodb_db}', user '{self.mongodb_user}', and admin user '{self.mongodb_admin}'...")
        mongosh_script = f"""
    // Create a sample collection to ensure the database is created
    db.getSiblingDB('{self.mongodb_db}').myCollection.insertOne({{ name: 'init' }});
    // Create admin user if it doesn't exist
    if (db.getSiblingDB('admin').getUser('{self.mongodb_admin}') == null) {{
        db.getSiblingDB('admin').createUser({{
            user: '{self.mongodb_admin}',
            pwd: '{self.mongodb_pwd}',
            roles: [ 'root' ]
        }});
        print('Admin user {self.mongodb_admin} created.');
    }} else {{
        print('Admin user {self.mongodb_admin} already exists.');
    }}
    // Create regular user if it doesn't exist
    if (db.getSiblingDB('admin').getUser('{self.mongodb_user}') == null) {{
        db.getSiblingDB('admin').createUser({{
            user: '{self.mongodb_user}',
            pwd: '{self.mongodb_pwd}',
            roles: [
                {{ role: 'readWrite', db: '{self.mongodb_db}' }},
                {{ role: 'dbAdmin', db: '{self.mongodb_db}' }}
            ]
        }});
        print('User {self.mongodb_user} created.');
    }} else {{
        print('User {self.mongodb_user} already exists.');
    }}
    """
        try:
            result = subprocess.run(
                [mongosh_path, "--host", self.mongodb_ip, "--port", self.mongodb_port, "--eval", mongosh_script],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            print(result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"Failed to create database or users: {e.stderr}")
            self._emergency_shutdown()
            raise RuntimeError("Failed to create database or users")

        # Step 5: Shut down MongoDB using mongosh
        print("Shutting down MongoDB to enable authentication...")
        try:
            subprocess.run(
                [
                    mongosh_path, "--host", self.mongodb_ip, "--port", self.mongodb_port,
                    "-u", self.mongodb_admin, "-p", self.mongodb_pwd, "--authenticationDatabase", "admin",
                    "--eval", "db.getSiblingDB('admin').shutdownServer()"
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self.mongod_process.wait(timeout=10)
            self.mongod_process = None
            print("MongoDB shut down successfully")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Failed to shut down MongoDB gracefully: {str(e)}")
            self._emergency_shutdown()

        # Step 6: Update configuration file to enable authentication
        print("Updating configuration file to enable authentication...")
        try:
            with open(self.mongo_config_file, "r") as f:
                config_content = f.read()
            config_content = config_content.replace("authorization: disabled", "authorization: enabled")
            with open(self.mongo_config_file, "w") as f:
                f.write(config_content)
            os.chmod(self.mongo_config_file, 0o644)
            print("Authentication enabled in configuration file")
        except Exception as e:
            print(f"Failed to update configuration file: {str(e)}")
            raise

    def _emergency_shutdown(self):
        """Forcefully terminate MongoDB process if it’s still running."""
        if self.mongod_process and psutil.pid_exists(self.mongod_process.pid):
            print("Forcing MongoDB process termination...")
            self.mongod_process.kill()
            self.mongod_process.wait(timeout=5)
            self.mongod_process = None

    def start_mongodb_server(self):
        """Start the MongoDB server with authentication."""
        print(f"Starting MongoDB with config: {self.mongo_config_file}")
        try:
            # Start mongod process
            self.mongod_process = subprocess.Popen(
                [self.mongod_path, "--config", self.mongo_config_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            # Wait briefly to allow MongoDB to start
            time.sleep(5)

            # Check if the process is still running
            if not psutil.pid_exists(self.mongod_process.pid):
                stdout, stderr = self.mongod_process.communicate()
                print(f"Failed to start MongoDB. Check logs in {self.mongo_log_file} for details.")
                print(f"mongod stdout: {stdout}")
                print(f"mongod stderr: {stderr}")
                raise RuntimeError("MongoDB failed to start")

            # Check if socket file exists
            socket_file = os.path.join(self.mongo_db_socket_path, f"mongodb-{self.mongodb_port}.sock")
            if not os.path.exists(socket_file):
                stdout, stderr = self.mongod_process.communicate()
                print(f"Socket file not created: {socket_file}. Check {self.mongo_log_file} for details.")
                print(f"mongod stdout: {stdout}")
                print(f"mongod stderr: {stderr}")
                raise RuntimeError(f"Socket file {socket_file} not created")

            print(f"MongoDB is running on {self.mongodb_ip}:{self.mongodb_port} with authentication.")
            print(f"Socket file created: {socket_file}")
            mongosh_path = os.path.join(os.path.dirname(self.mongod_path), "mongosh") if os.path.exists(os.path.join(os.path.dirname(self.mongod_path), "mongosh")) else "/usr/bin/mongosh"
            print(f"Connect using: {mongosh_path} --host {self.mongodb_ip} --port {self.mongodb_port} "
                  f"-u {self.mongodb_user} -p {self.mongodb_pwd} --authenticationDatabase admin")

        except Exception as e:
            print(f"Failed to start MongoDB: {str(e)}")
            self._emergency_shutdown()
            raise

    async def stop_mongodb_server(self):
        """Stop the MongoDB server."""
        if self.mongod_process and psutil.pid_exists(self.mongod_process.pid):
            print("Stopping MongoDB server...")
            self.mongod_process.terminate()
            try:
                self.mongod_process.wait(timeout=5)
                print("MongoDB server stopped successfully")
            except subprocess.TimeoutExpired:
                print("MongoDB server did not stop gracefully, forcing termination...")
                self.mongod_process.kill()
            self.mongod_process = None
        else:
            print("No MongoDB server is running")