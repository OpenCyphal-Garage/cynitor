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
                    if 'UP' in line:
                        can_interfaces.append(iface)
                    else:
                        print(f"CAN INTERFACE {iface} IS DOWN")
    except subprocess.CalledProcessError as e:
        print(f"Error running ip link: {e}")
    
    return can_interfaces