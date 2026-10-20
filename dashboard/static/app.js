const value = (item) => item === null || item === undefined ? "—" : String(item);

function renderDetails(id, fields) {
  const root = document.getElementById(id);
  root.replaceChildren();
  Object.entries(fields || {}).forEach(([key, item]) => {
    const term = document.createElement("dt");
    term.textContent = key.replaceAll("_", " ");
    const detail = document.createElement("dd");
    detail.textContent = value(item);
    if (item === true) detail.className = "active";
    root.append(term, detail);
  });
}

function renderHazards(hazards) {
  const root = document.getElementById("hazards");
  root.replaceChildren();
  if (!hazards.length) { root.textContent = "No hazards recorded."; return; }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Location</th><th>Magnitude</th><th>Confidence</th><th>Time</th></tr></thead>";
  const body = document.createElement("tbody");
  hazards.forEach((hazard) => {
    const row = document.createElement("tr");
    [
      `${hazard.latitude}, ${hazard.longitude}`,
      `${hazard.magnitude_g} g`,
      hazard.confidence,
      hazard.timestamp_utc,
    ].forEach((cell) => { const element = document.createElement("td"); element.textContent = value(cell); row.append(element); });
    body.append(row);
  });
  table.append(body); root.append(table);
}

async function refresh() {
  const connection = document.getElementById("connection-status");
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    renderDetails("drowsiness", state.drowsiness);
    renderDetails("gps", state.gps);
    renderDetails("geofence", state.geofence);
    renderDetails("alcohol", state.alcohol);
    renderDetails("relays", state.relays);
    renderHazards(state.hazards || []);
    document.getElementById("esp32").textContent = JSON.stringify(state.esp32 || {}, null, 2);
    connection.textContent = `Live state refreshed ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    connection.textContent = `Dashboard data unavailable: ${error.message}`;
  }
}

refresh();
setInterval(refresh, 1000);
