"""Start Kept: python run.py  then open http://localhost:8000 (or the phone link it prints)."""

import os
import socket

from kept.web.app import create_app


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    ip = lan_ip()
    print("\n  Kept is running.")
    print(f"  On this computer:  http://localhost:{port}")
    if ip:
        print(f"  On your phone (same Wi-Fi):  http://{ip}:{port}")
    print("  Stop it with Ctrl+C.\n")
    create_app().run(host="0.0.0.0", port=port, debug=False)
