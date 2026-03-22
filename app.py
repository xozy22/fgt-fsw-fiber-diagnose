import json
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory, Response

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder="static")


def get_fortigate_session(host, token, port=443):
    """Create a requests.Session with connection pooling for FortiGate API calls."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    s.verify = False
    # Increase connection pool for parallel requests
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    port_suffix = f":{port}" if port != 443 else ""
    return {
        "base_url": f"https://{host}{port_suffix}/api/v2",
        "session": s,
    }


def fgt_get(session, path, params=None):
    """Perform a GET request against the FortiGate API."""
    url = f"{session['base_url']}{path}"
    resp = session["session"].get(url, params=params, timeout=30)
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


def diagnose_single_host(host, token, vdom, port=443, progress_cb=None):
    """Diagnose a single FortiGate. Returns dict with switches or error."""
    session = get_fortigate_session(host, token, port)
    params = {}
    if vdom:
        params["vdom"] = vdom

    def emit(step, detail=""):
        if progress_cb:
            progress_cb(host, step, detail)

    try:
        emit("connect", "Verbinde und lade Switch-Daten...")

        # Step 1+2+3: Fetch status, transceivers and health-status in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_status = executor.submit(
                fgt_get, session, "/monitor/switch-controller/managed-switch/status", params
            )
            f_transceivers = executor.submit(
                fgt_get, session, "/monitor/switch-controller/managed-switch/transceivers", params
            )
            f_health = executor.submit(
                fgt_get, session, "/monitor/switch-controller/managed-switch/health-status", params
            )

        switches = f_status.result().get("results", [])
        transceivers = f_transceivers.result().get("results", [])

        emit("switches", f"{len(switches)} Switch(es), {len(transceivers)} Transceiver gefunden")

        # Build health lookup: switch_serial -> health data
        health_map = {}
        try:
            health_results = f_health.result().get("results", [])
            for h in health_results:
                serial = h.get("serial", "")
                if serial:
                    health_map[serial] = h
        except Exception:
            pass  # health-status may not be available

        # Build lookup: (switch_serial, port) -> transceiver info
        transceiver_map = {}
        for t in transceivers:
            key = (t.get("fortiswitch_id", ""), t.get("port", ""))
            transceiver_map[key] = t

        # Collect all fiber port fetch jobs across all switches
        all_fetch_jobs = []  # (switch_serial, port, transceiver_info)
        switch_info = {}  # switch_serial -> switch metadata

        for sw in switches:
            switch_serial = sw.get("serial", "")
            switch_info[switch_serial] = {
                "switch_id": sw.get("switch-id", sw.get("serial", "")),
                "name": sw.get("name", sw.get("switch-id", switch_serial)),
                "status": sw.get("status", ""),
                "os_version": sw.get("os_version", ""),
            }

            fiber_ports = [
                t.get("port") for t in transceivers
                if t.get("fortiswitch_id") == switch_serial
            ]
            for port in fiber_ports:
                all_fetch_jobs.append((switch_serial, port, transceiver_map.get((switch_serial, port), {})))

        total_ports = len(all_fetch_jobs)
        emit("tx-rx", f"Lese Tx/Rx Power für {total_ports} Port(s)...")

        # Fetch ALL tx-rx data across all switches in one parallel batch
        port_results = {}  # switch_serial -> [port_diagnostics]
        completed_ports = 0
        if all_fetch_jobs:
            with ThreadPoolExecutor(max_workers=min(16, len(all_fetch_jobs))) as executor:
                futures = {
                    executor.submit(
                        fetch_tx_rx, session, params, serial, port, tinfo
                    ): serial
                    for serial, port, tinfo in all_fetch_jobs
                }
                for future in as_completed(futures):
                    serial = futures[future]
                    port_results.setdefault(serial, []).append(future.result())
                    completed_ports += 1
                    if completed_ports % 2 == 0 or completed_ports == total_ports:
                        emit("tx-rx-progress", f"Tx/Rx: {completed_ports}/{total_ports} Ports")

        # Build results
        results = []
        for switch_serial, info in switch_info.items():
            port_diagnostics = port_results.get(switch_serial, [])
            if not port_diagnostics:
                continue

            port_diagnostics.sort(key=lambda p: p.get("port", ""))

            health = health_map.get(switch_serial, {})
            summary = health.get("summary", {})
            temp_data = summary.get("temperature", {})

            results.append({
                "switch_id": info["switch_id"],
                "serial": switch_serial,
                "name": info["name"],
                "status": info["status"],
                "os_version": info["os_version"],
                "temperature": temp_data.get("value"),
                "temperature_rating": temp_data.get("rating", ""),
                "ports": port_diagnostics,
            })

        emit("done", f"{len(results)} Switch(es) mit {total_ports} Fiber-Port(s)")
        return {"host": host, "switches": results}

    except requests.exceptions.ConnectTimeout:
        return {"host": host, "error": "Zeitüberschreitung beim Verbindungsaufbau.",
                "error_type": "timeout",
                "hint": "Host antwortet nicht rechtzeitig. Firewall-Regeln und Erreichbarkeit prüfen."}
    except requests.exceptions.ReadTimeout:
        return {"host": host, "error": "Zeitüberschreitung beim Lesen der API-Antwort.",
                "error_type": "timeout",
                "hint": "FortiGate antwortet zu langsam. Evtl. hohe Auslastung oder zu viele Switches."}
    except requests.exceptions.ConnectionError as e:
        err_str = str(e).lower()
        if "name or service not known" in err_str or "getaddrinfo failed" in err_str or "nodename nor servname" in err_str:
            return {"host": host, "error": f"DNS-Auflösung für '{host}' fehlgeschlagen.",
                    "error_type": "dns",
                    "hint": "Hostname prüfen. Ist der DNS-Server erreichbar?"}
        if "connection refused" in err_str or "errno 111" in err_str or "errno 10061" in err_str:
            return {"host": host, "error": f"Verbindung zu {host} abgelehnt.",
                    "error_type": "refused",
                    "hint": "HTTPS-Zugang (Port 443) auf der FortiGate aktiviert? Läuft der Management-Dienst?"}
        if "ssl" in err_str or "certificate" in err_str:
            return {"host": host, "error": f"SSL/TLS-Fehler bei Verbindung zu {host}.",
                    "error_type": "ssl",
                    "hint": "Zertifikatsproblem. Evtl. selbstsigniertes Zertifikat oder TLS-Version inkompatibel."}
        return {"host": host, "error": f"Verbindung zu {host} fehlgeschlagen.",
                "error_type": "connection",
                "hint": "Host erreichbar? Netzwerkverbindung und Firewall-Regeln prüfen."}
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 0
        if status_code == 401:
            return {"host": host, "error": "Authentifizierung fehlgeschlagen (HTTP 401).",
                    "error_type": "auth",
                    "hint": "API-Token ungültig oder abgelaufen. Neuen Token in der FortiGate erstellen."}
        if status_code == 403:
            return {"host": host, "error": "Zugriff verweigert (HTTP 403).",
                    "error_type": "permission",
                    "hint": "API-Token hat keine Berechtigung für switch-controller. Admin-Profil prüfen (Access Group: wifi)."}
        if status_code == 404:
            return {"host": host, "error": "API-Endpunkt nicht gefunden (HTTP 404).",
                    "error_type": "api",
                    "hint": "FortiOS-Version zu alt? Mindestens FortiOS 7.0 erforderlich. VDOM-Name korrekt?"}
        if status_code == 424:
            return {"host": host, "error": "Abhängigkeit fehlgeschlagen (HTTP 424).",
                    "error_type": "api",
                    "hint": "FortiLink ist möglicherweise nicht konfiguriert oder kein Switch verbunden."}
        if status_code == 500:
            return {"host": host, "error": "Interner FortiGate-Fehler (HTTP 500).",
                    "error_type": "server",
                    "hint": "FortiGate hat einen internen Fehler. Neustart oder CLI-Diagnose erforderlich."}
        return {"host": host, "error": f"FortiGate API Fehler (HTTP {status_code}).",
                "error_type": "api",
                "hint": f"Unerwarteter HTTP-Statuscode {status_code}. FortiGate-Logs prüfen."}
    except Exception as e:
        return {"host": host, "error": f"Unerwarteter Fehler: {e}",
                "error_type": "unknown",
                "hint": "Bitte Eingaben prüfen und erneut versuchen."}


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
    port = int(data.get("port", 443))

    if not host or not token:
        return jsonify({"error": "Host und API-Token sind erforderlich."}), 400

    result = diagnose_single_host(host, token, vdom, port)

    if "error" in result:
        return jsonify({"error": result["error"]}), 502

    return jsonify({"switches": result["switches"]})


@app.route("/api/diagnose-multi", methods=["POST"])
def diagnose_multi():
    """Diagnose multiple FortiGates in parallel with SSE progress."""
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
                int(h.get("port", 443)),
            ): h
            for h in host_list
            if h.get("host", "").strip() and h.get("token", "").strip()
        }
        for future in as_completed(futures):
            results.append(future.result())

    return jsonify({"results": results})


@app.route("/api/diagnose-stream", methods=["POST"])
def diagnose_stream():
    """Diagnose multiple FortiGates with SSE progress streaming."""
    data = request.get_json()
    host_list = data.get("hosts", [])

    if not host_list:
        return jsonify({"error": "Keine Hosts angegeben."}), 400

    valid_hosts = [
        h for h in host_list
        if h.get("host", "").strip() and h.get("token", "").strip()
    ]

    def generate():
        import queue
        import threading

        progress_queue = queue.Queue()

        def progress_cb(host, step, detail):
            progress_queue.put({"type": "progress", "host": host, "step": step, "detail": detail})

        def run_host(h):
            result = diagnose_single_host(
                h.get("host", "").strip(),
                h.get("token", "").strip(),
                h.get("vdom", "root").strip(),
                int(h.get("port", 443)),
                progress_cb=progress_cb,
            )
            progress_queue.put({"type": "result", "data": result})

        # Start all host diagnostics in parallel
        threads = []
        for h in valid_hosts:
            t = threading.Thread(target=run_host, args=(h,))
            t.start()
            threads.append(t)

        # Monitor progress and results
        results_received = 0
        total_hosts = len(valid_hosts)

        while results_received < total_hosts:
            try:
                msg = progress_queue.get(timeout=0.1)
                if msg["type"] == "progress":
                    yield f"data: {json.dumps(msg)}\n\n"
                elif msg["type"] == "result":
                    yield f"data: {json.dumps(msg)}\n\n"
                    results_received += 1
            except queue.Empty:
                continue

        # Wait for all threads to finish
        for t in threads:
            t.join()

        yield f"data: {json.dumps({'type': 'complete'})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/ping", methods=["POST"])
def ping_host():
    """Ping a host and return reachability info."""
    import subprocess
    import platform
    import socket
    import time

    data = request.get_json()
    host = data.get("host", "").strip()
    port = int(data.get("port", 443))

    if not host:
        return jsonify({"error": "Kein Host angegeben."}), 400

    result = {"host": host, "port": port, "dns": None, "ip": None, "ping": None, "https": None}

    # Step 1: DNS resolution
    try:
        ip = socket.gethostbyname(host)
        result["dns"] = {"ok": True, "ip": ip}
        result["ip"] = ip
    except socket.gaierror:
        result["dns"] = {"ok": False, "error": "DNS-Auflösung fehlgeschlagen"}
        return jsonify(result)

    # Step 2: ICMP Ping
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        timeout_param = "-w" if platform.system().lower() == "windows" else "-W"
        timeout_val = "2000" if platform.system().lower() == "windows" else "2"
        cmd = ["ping", param, "3", timeout_param, timeout_val, host]
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        elapsed = round((time.time() - start) * 1000)

        if proc.returncode == 0:
            # Extract avg RTT from output
            import re
            output = proc.stdout
            # Windows: "Minimum = 1ms, Maximum = 2ms, Mittelwert = 1ms"
            # Linux: "rtt min/avg/max/mdev = 0.5/1.0/1.5/0.3 ms"
            avg = None
            m = re.search(r"(?:Average|Mittelwert|avg)[^=]*=\s*(\d+)", output)
            if m:
                avg = int(m.group(1))
            else:
                m = re.search(r"min/avg/max/\S+\s*=\s*[\d.]+/([\d.]+)/", output)
                if m:
                    avg = round(float(m.group(1)))
            result["ping"] = {"ok": True, "rtt_ms": avg or elapsed // 3}
        else:
            result["ping"] = {"ok": False, "error": "Host antwortet nicht auf Ping (ICMP)"}
    except subprocess.TimeoutExpired:
        result["ping"] = {"ok": False, "error": "Ping Timeout"}
    except FileNotFoundError:
        result["ping"] = {"ok": False, "error": "Ping nicht verfügbar (Kommando nicht installiert)"}
    except Exception as e:
        result["ping"] = {"ok": False, "error": str(e)}

    # Step 3: HTTPS port check
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        start = time.time()
        sock.connect((result["ip"], port))
        elapsed = round((time.time() - start) * 1000)
        sock.close()
        result["https"] = {"ok": True, "rtt_ms": elapsed}
    except Exception:
        result["https"] = {"ok": False, "error": f"Port {port} nicht erreichbar"}

    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
