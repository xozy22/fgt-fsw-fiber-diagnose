import os
import requests
import urllib3
from flask import Flask, request, jsonify, send_from_directory

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder="static")


def get_fortigate_session(host, token):
    """Create a session config for FortiGate API calls."""
    return {
        "base_url": f"https://{host}/api/v2",
        "headers": {"Authorization": f"Bearer {token}"},
        "verify": False,
    }


def fgt_get(session, path, params=None):
    """Perform a GET request against the FortiGate API."""
    url = f"{session['base_url']}{path}"
    resp = requests.get(
        url, headers=session["headers"], params=params, verify=session["verify"], timeout=30
    )
    resp.raise_for_status()
    return resp.json()


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/diagnose", methods=["POST"])
def diagnose():
    """Main endpoint: connects to FortiGate and retrieves fiber transceiver diagnostics."""
    data = request.get_json()
    host = data.get("host", "").strip()
    token = data.get("token", "").strip()
    vdom = data.get("vdom", "root").strip()

    if not host or not token:
        return jsonify({"error": "Host und API-Token sind erforderlich."}), 400

    session = get_fortigate_session(host, token)
    params = {}
    if vdom:
        params["vdom"] = vdom

    try:
        # Step 1: Get managed switch status (includes port details + transceiver info)
        status_resp = fgt_get(
            session, "/monitor/switch-controller/managed-switch/status", params
        )
        switches = status_resp.get("results", [])

        # Step 2: Get transceiver list for all switches
        transceiver_resp = fgt_get(
            session, "/monitor/switch-controller/managed-switch/transceivers", params
        )
        transceivers = transceiver_resp.get("results", [])

        # Build a lookup: (switch_id, port) -> transceiver info
        transceiver_map = {}
        for t in transceivers:
            key = (t.get("fortiswitch_id", ""), t.get("port", ""))
            transceiver_map[key] = t

        results = []

        for sw in switches:
            switch_id = sw.get("switch-id", sw.get("serial", ""))
            switch_serial = sw.get("serial", "")
            switch_name = sw.get("name", switch_id)
            switch_status = sw.get("status", "")
            os_version = sw.get("os_version", "")

            # Find fiber ports: ports that have a transceiver entry
            fiber_ports = []
            for t in transceivers:
                if t.get("fortiswitch_id") == switch_serial:
                    fiber_ports.append(t.get("port"))

            if not fiber_ports:
                continue

            # Step 3: Get Tx/Rx data for each fiber port
            port_diagnostics = []
            for port in fiber_ports:
                try:
                    tx_rx_params = {**params, "mkey": switch_serial, "port": port}
                    tx_rx_resp = fgt_get(
                        session,
                        "/monitor/switch-controller/managed-switch/tx-rx",
                        tx_rx_params,
                    )
                    tx_rx_data = tx_rx_resp.get("results", {})

                    transceiver_info = transceiver_map.get(
                        (switch_serial, port), {}
                    )

                    port_diagnostics.append(
                        {
                            "port": port,
                            "link_status": transceiver_info.get("status", "unknown"),
                            "type": transceiver_info.get("type", ""),
                            "vendor": transceiver_info.get("vendor", ""),
                            "part_number": transceiver_info.get(
                                "vendor_part_number", ""
                            ),
                            "serial_number": transceiver_info.get(
                                "vendor_serial_number", ""
                            ),
                            "tx_rx": tx_rx_data,
                        }
                    )
                except Exception as e:
                    port_diagnostics.append(
                        {
                            "port": port,
                            "error": str(e),
                        }
                    )

            results.append(
                {
                    "switch_id": switch_id,
                    "serial": switch_serial,
                    "name": switch_name,
                    "status": switch_status,
                    "os_version": os_version,
                    "ports": port_diagnostics,
                }
            )

        return jsonify({"switches": results})

    except requests.exceptions.ConnectionError:
        return jsonify({"error": f"Verbindung zu {host} fehlgeschlagen. Host erreichbar?"}), 502
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 401:
            return jsonify({"error": "Authentifizierung fehlgeschlagen. API-Token prüfen."}), 401
        if status == 403:
            return jsonify({"error": "Zugriff verweigert. Berechtigungen des API-Tokens prüfen."}), 403
        return jsonify({"error": f"FortiGate API Fehler: {e}"}), status or 500
    except Exception as e:
        return jsonify({"error": f"Unerwarteter Fehler: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
