import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def fetch_tx_rx(session, params, switch_serial, port, transceiver_info):
    """Fetch Tx/Rx data for a single port. Designed to run in a thread."""
    try:
        tx_rx_params = {**params, "mkey": switch_serial, "port": port}
        tx_rx_resp = fgt_get(
            session,
            "/monitor/switch-controller/managed-switch/tx-rx",
            tx_rx_params,
        )
        tx_rx_data = tx_rx_resp.get("results", {})

        return {
            "port": port,
            "link_status": transceiver_info.get("status", "unknown"),
            "type": transceiver_info.get("type", ""),
            "vendor": transceiver_info.get("vendor", ""),
            "part_number": transceiver_info.get("vendor_part_number", ""),
            "serial_number": transceiver_info.get("vendor_serial_number", ""),
            "tx_rx": tx_rx_data,
        }
    except Exception as e:
        return {
            "port": port,
            "error": str(e),
        }


def diagnose_single_host(host, token, vdom):
    """Diagnose a single FortiGate. Returns dict with switches or error."""
    session = get_fortigate_session(host, token)
    params = {}
    if vdom:
        params["vdom"] = vdom

    try:
        # Step 1+2: Fetch status and transceivers in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            f_status = executor.submit(
                fgt_get, session, "/monitor/switch-controller/managed-switch/status", params
            )
            f_transceivers = executor.submit(
                fgt_get, session, "/monitor/switch-controller/managed-switch/transceivers", params
            )

        switches = f_status.result().get("results", [])
        transceivers = f_transceivers.result().get("results", [])

        # Build lookup: (switch_serial, port) -> transceiver info
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

            # Find fiber ports
            fiber_ports = [
                t.get("port") for t in transceivers
                if t.get("fortiswitch_id") == switch_serial
            ]

            if not fiber_ports:
                continue

            # Step 3: Fetch Tx/Rx for all ports in parallel
            port_diagnostics = []
            with ThreadPoolExecutor(max_workers=min(8, len(fiber_ports))) as executor:
                futures = {
                    executor.submit(
                        fetch_tx_rx, session, params, switch_serial, port,
                        transceiver_map.get((switch_serial, port), {})
                    ): port
                    for port in fiber_ports
                }
                for future in as_completed(futures):
                    port_diagnostics.append(future.result())

            # Sort ports by name for consistent ordering
            port_diagnostics.sort(key=lambda p: p.get("port", ""))

            results.append({
                "switch_id": switch_id,
                "serial": switch_serial,
                "name": switch_name,
                "status": switch_status,
                "os_version": os_version,
                "ports": port_diagnostics,
            })

        return {"host": host, "switches": results}

    except requests.exceptions.ConnectionError:
        return {"host": host, "error": f"Verbindung zu {host} fehlgeschlagen. Host erreichbar?"}
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 0
        if status_code == 401:
            return {"host": host, "error": "Authentifizierung fehlgeschlagen. API-Token prüfen."}
        if status_code == 403:
            return {"host": host, "error": "Zugriff verweigert. Berechtigungen des API-Tokens prüfen."}
        return {"host": host, "error": f"FortiGate API Fehler: {e}"}
    except Exception as e:
        return {"host": host, "error": f"Unerwarteter Fehler: {e}"}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/diagnose", methods=["POST"])
def diagnose():
    """Single host diagnose endpoint (kept for backwards compatibility)."""
    data = request.get_json()
    host = data.get("host", "").strip()
    token = data.get("token", "").strip()
    vdom = data.get("vdom", "root").strip()

    if not host or not token:
        return jsonify({"error": "Host und API-Token sind erforderlich."}), 400

    result = diagnose_single_host(host, token, vdom)

    if "error" in result:
        return jsonify({"error": result["error"]}), 502

    return jsonify({"switches": result["switches"]})


@app.route("/api/diagnose-multi", methods=["POST"])
def diagnose_multi():
    """Diagnose multiple FortiGates in parallel."""
    data = request.get_json()
    host_list = data.get("hosts", [])

    if not host_list:
        return jsonify({"error": "Keine Hosts angegeben."}), 400

    # Run all host diagnostics in parallel
    results = []
    with ThreadPoolExecutor(max_workers=min(10, len(host_list))) as executor:
        futures = {
            executor.submit(
                diagnose_single_host,
                h.get("host", "").strip(),
                h.get("token", "").strip(),
                h.get("vdom", "root").strip(),
            ): h
            for h in host_list
            if h.get("host", "").strip() and h.get("token", "").strip()
        }
        for future in as_completed(futures):
            results.append(future.result())

    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
