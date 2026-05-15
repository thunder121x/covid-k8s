import { useEffect, useMemo, useState } from "react";
import {
  MapContainer,
  Marker,
  Popup,
  TileLayer,
  ZoomControl,
} from "react-leaflet";
import L from "leaflet";
import RefreshIcon from "@mui/icons-material/Refresh";
import AirIcon from "@mui/icons-material/Air";
import SpeedIcon from "@mui/icons-material/Speed";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import PlaceIcon from "@mui/icons-material/Place";
import "leaflet/dist/leaflet.css";
import "./App.css";

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
const API_BASE_URL =
  configuredApiBaseUrl === undefined
    ? "/api"
    : configuredApiBaseUrl.replace(/\/$/, "");
const API_URL = `${API_BASE_URL}/aqi`;
const REFRESH_INTERVAL_MS = 30000;

const DISTRICT_LOCATIONS = {
  Bangkok: { lat: 13.7563, lng: 100.5018, label: "Bangkok" },
  "Pathum Wan": { lat: 13.7466, lng: 100.5347, label: "Pathum Wan" },
  "Din Daeng": { lat: 13.7698, lng: 100.5527, label: "Din Daeng" },
  Chatuchak: { lat: 13.815, lng: 100.56, label: "Chatuchak" },
  "Bang Na": { lat: 13.6682, lng: 100.614, label: "Bang Na" },
  "Lat Phrao": { lat: 13.8036, lng: 100.6075, label: "Lat Phrao" },
  "Bang Kapi": { lat: 13.7658, lng: 100.6478, label: "Bang Kapi" },
  Thonburi: { lat: 13.725, lng: 100.4851, label: "Thonburi" },
};

const SAMPLE_DATA = [
  { district: "Bangkok", pm25: 31.8, last_reading: new Date().toISOString() },
  { district: "Pathum Wan", pm25: 44.6, last_reading: new Date().toISOString() },
  { district: "Din Daeng", pm25: 59.1, last_reading: new Date().toISOString() },
  { district: "Chatuchak", pm25: 72.4, last_reading: new Date().toISOString() },
  { district: "Bang Na", pm25: 22.9, last_reading: new Date().toISOString() },
];

const LEVELS = [
  {
    max: 15,
    label: "Excellent",
    shortLabel: "Clean",
    tone: "good",
    color: "#22c55e",
    description: "Air is clear and comfortable for outdoor activity.",
  },
  {
    max: 25,
    label: "Good",
    shortLabel: "Good",
    tone: "fresh",
    color: "#84cc16",
    description: "Air quality is healthy for most people.",
  },
  {
    max: 37.5,
    label: "Moderate",
    shortLabel: "Moderate",
    tone: "watch",
    color: "#facc15",
    description: "Sensitive groups may want to watch conditions.",
  },
  {
    max: 75,
    label: "Unhealthy for sensitive groups",
    shortLabel: "Sensitive",
    tone: "warn",
    color: "#fb923c",
    description: "Children, older adults, and sensitive groups should reduce exposure.",
  },
  {
    max: Infinity,
    label: "Unhealthy",
    shortLabel: "High",
    tone: "danger",
    color: "#ef4444",
    description: "Limit outdoor activity and consider a mask outdoors.",
  },
];

function getLevel(pm25) {
  return LEVELS.find((level) => pm25 <= level.max) || LEVELS[LEVELS.length - 1];
}

function getFallbackLocation(district) {
  const seed = [...district].reduce((total, char) => total + char.charCodeAt(0), 0);
  const latOffset = ((seed % 15) - 7) * 0.009;
  const lngOffset = (((seed * 3) % 15) - 7) * 0.01;

  return {
    lat: 13.7563 + latOffset,
    lng: 100.5018 + lngOffset,
    label: district,
  };
}

function normalizeReading(reading) {
  const district = String(reading.district || "Bangkok");
  const location = DISTRICT_LOCATIONS[district] || getFallbackLocation(district);
  const pm25 = Number(reading.pm25 || 0);
  const level = getLevel(pm25);

  return {
    district,
    displayName: location.label,
    lat: location.lat,
    lng: location.lng,
    pm25,
    level,
    lastReading: reading.last_reading,
  };
}

function formatTime(value) {
  if (!value) {
    return "No timestamp";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return "No timestamp";
  }

  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function createPm25Icon(item) {
  const label = escapeHtml(item.displayName);
  const shortLabel = escapeHtml(item.level.shortLabel);

  return L.divIcon({
    className: "pm25PinRoot",
    iconSize: [112, 68],
    iconAnchor: [25, 56],
    popupAnchor: [0, -48],
    html: `
      <div class="pm25Pin" style="--pin-color: ${item.level.color}">
        <div class="pinNeedle">
          <span>${Math.round(item.pm25)}</span>
        </div>
        <div class="pinLabel">
          <strong>${label}</strong>
          <small>${shortLabel}</small>
        </div>
      </div>
    `,
  });
}

function App() {
  const [readings, setReadings] = useState([]);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");
  const [lastSync, setLastSync] = useState("");

  async function loadData() {
    setStatus((current) => (current === "live" ? "refreshing" : "loading"));
    setError("");

    try {
      const response = await fetch(API_URL, { cache: "no-store" });

      if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
      }

      const data = await response.json();

      if (Array.isArray(data) && data.length > 0) {
        setReadings(data);
        setStatus("live");
      } else {
        setReadings(SAMPLE_DATA);
        setStatus("empty");
      }
    } catch (nextError) {
      setReadings(SAMPLE_DATA);
      setStatus("offline");
      setError(nextError?.message || "Cannot connect to API");
    } finally {
      setLastSync(new Date().toISOString());
    }
  }

  useEffect(() => {
    const initialLoad = window.setTimeout(loadData, 0);

    const timer = window.setInterval(loadData, REFRESH_INTERVAL_MS);
    return () => {
      window.clearTimeout(initialLoad);
      window.clearInterval(timer);
    };
  }, []);

  const mapData = useMemo(() => {
    return readings.map(normalizeReading).sort((a, b) => b.pm25 - a.pm25);
  }, [readings]);

  const summary = useMemo(() => {
    if (!mapData.length) {
      return {
        average: 0,
        highest: null,
        lowest: null,
        level: getLevel(0),
      };
    }

    const total = mapData.reduce((sum, item) => sum + item.pm25, 0);
    const average = total / mapData.length;

    return {
      average,
      highest: mapData[0],
      lowest: mapData[mapData.length - 1],
      level: getLevel(average),
    };
  }, [mapData]);

  const statusText = {
    loading: "Connecting to API",
    refreshing: "Refreshing live data",
    live: "Live API",
    empty: "API online, sample map",
    offline: "Sample map",
  }[status];

  return (
    <main className="airMapApp">
      <MapContainer
        center={[13.7563, 100.5018]}
        zoom={11}
        minZoom={10}
        maxZoom={16}
        zoomControl={false}
        scrollWheelZoom
        className="mapCanvas"
      >
        <TileLayer
          attribution='&copy; <a href="https://carto.com/">Carto</a>'
          url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
        />
        <ZoomControl position="bottomright" />

        {mapData.map((item) => (
          <Marker
            key={item.district}
            position={[item.lat, item.lng]}
            icon={createPm25Icon(item)}
          >
            <Popup>
              <div className="popupCard">
                <div className="popupHeader">
                  <span style={{ backgroundColor: item.level.color }}>
                    <PlaceIcon fontSize="small" />
                  </span>
                  <div>
                    <h3>{item.displayName}</h3>
                    <p>{item.district}</p>
                  </div>
                </div>
                <div className="popupValue">
                  <strong>{item.pm25.toFixed(1)}</strong>
                  <span>ug/m3 PM2.5</span>
                </div>
                <div className="popupBadge" style={{ color: item.level.color }}>
                  {item.level.label}
                </div>
                <p className="popupDescription">{item.level.description}</p>
                <p className="popupTime">Updated {formatTime(item.lastReading)}</p>
              </div>
            </Popup>
          </Marker>
        ))}
      </MapContainer>

      <section className="topStatusBar">
        <div className="brandMark">
          <AirIcon />
        </div>
        <div className="brandCopy">
          <span>Guardian Air Map</span>
          <strong>Bangkok PM2.5 monitoring</strong>
        </div>
        <div className={`connectionPill ${status}`}>
          <span />
          {statusText}
        </div>
        <div className="statusMetric">
          <small>Average</small>
          <strong>{summary.average.toFixed(1)}</strong>
        </div>
        <div className="statusMetric">
          <small>Worst area</small>
          <strong>{summary.highest?.displayName || "-"}</strong>
        </div>
        <button className="iconButton" type="button" onClick={loadData} aria-label="Refresh map data">
          <RefreshIcon />
        </button>
      </section>

      <aside className="insightPanel">
        <div className="panelHero" style={{ "--level-color": summary.level.color }}>
          <div>
            <p>City average</p>
            <h1>{summary.average.toFixed(1)}</h1>
            <span>ug/m3 PM2.5</span>
          </div>
          <div className="airGauge">
            <SpeedIcon />
          </div>
        </div>

        <div className="levelCallout" style={{ borderColor: summary.level.color }}>
          <WarningAmberIcon />
          <div>
            <strong>{summary.level.label}</strong>
            <p>{summary.level.description}</p>
          </div>
        </div>

        <div className="miniGrid">
          <MetricCard label="Stations" value={mapData.length} />
          <MetricCard label="Highest" value={summary.highest ? summary.highest.pm25.toFixed(1) : "-"} />
          <MetricCard label="Lowest" value={summary.lowest ? summary.lowest.pm25.toFixed(1) : "-"} />
        </div>

        {error && <div className="apiNotice">{error}</div>}

        <div className="rankingHeader">
          <div>
            <p>Area ranking</p>
            <h2>Live PM2.5 pins</h2>
          </div>
          <span>Updated {formatTime(lastSync)}</span>
        </div>

        <div className="areaList">
          {mapData.map((item, index) => (
            <article className="areaRow" key={item.district}>
              <span className="rank">{index + 1}</span>
              <span className="qualityDot" style={{ backgroundColor: item.level.color }} />
              <div>
                <strong>{item.displayName}</strong>
                <small>{item.level.shortLabel}</small>
              </div>
              <b>{item.pm25.toFixed(1)}</b>
            </article>
          ))}
        </div>
      </aside>

      <section className="legendStrip">
        {LEVELS.map((level) => (
          <div className="legendItem" key={level.label}>
            <span style={{ backgroundColor: level.color }} />
            <strong>{level.shortLabel}</strong>
          </div>
        ))}
      </section>
    </main>
  );
}

function MetricCard({ label, value }) {
  return (
    <div className="metricCard">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default App;
