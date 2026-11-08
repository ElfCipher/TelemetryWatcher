(() => {
  "use strict";

  const MAX_POINTS = 180;
  const HISTORY_WINDOW_SEC = 900;
  const HISTORY_STEP_SEC = 5;
  const PROCESS_LIMIT = 10;

  const token = new URLSearchParams(window.location.search).get("token");
  const state = {
    charts: { load: null, net: null },
    lastPoint: null,
    lastNet: null,
    ws: null,
    reconnectAttempt: 0,
    reconnectTimer: null,
    toastTimer: null,
    hasConnected: false,
  };

  const $ = (id) => document.getElementById(id);

  const isFiniteNumber = (value) => Number.isFinite(Number(value));

  function num(value, digits = 1) {
    if (value === null || value === undefined || value === "") return "—";
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
  }

  function numberOrNull(value) {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatBytes(value, digits = 0) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let amount = parsed;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount.toFixed(index === 0 ? 0 : digits)} ${units[index]}`;
  }

  function timeLabel(timestamp) {
    const date = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString("ru-RU", { hour12: false });
  }

  function apiFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetch(path, { ...options, headers });
  }

  function showToast(message) {
    const toast = $("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
  }

  function setStatus(mode) {
    const badge = $("connection");
    const text = $("connection-text");
    if (!badge || !text) return;
    badge.className = `badge badge-${mode}`;
    text.textContent =
      { online: "онлайн", connecting: "подключение", offline: "нет связи" }[mode] ||
      mode;
  }

  function chartOptions({ min, max, unit = "" } = {}) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(7, 13, 25, 0.96)",
          borderColor: "rgba(148, 163, 184, 0.25)",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#cbd5e1",
          padding: 10,
          callbacks: {
            label(context) {
              const value = context.parsed.y;
              if (value === null || value === undefined) {
                return `${context.dataset.label}: —`;
              }
              return `${context.dataset.label}: ${Number(value).toFixed(1)}${unit}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: "#64748b", maxTicksLimit: 6, maxRotation: 0 },
          grid: { color: "rgba(148, 163, 184, 0.06)", drawTicks: false },
          border: { display: false },
        },
        y: {
          min,
          max,
          beginAtZero: min === 0,
          ticks: { color: "#64748b", maxTicksLimit: 5 },
          grid: { color: "rgba(148, 163, 184, 0.08)" },
          border: { display: false },
        },
      },
    };
  }

  function makeChart(canvas, datasets, options) {
    return new Chart(canvas, {
      type: "line",
      data: { labels: [], datasets },
      options,
    });
  }

  function createCharts() {
    if (typeof Chart === "undefined") {
      showToast("Chart.js не загружен — графики недоступны");
      return;
    }
    Chart.defaults.color = "#94a3b8";
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

    const line = (label, color, fill = false) => ({
      label,
      data: [],
      borderColor: color,
      backgroundColor: fill ? `${color}22` : "transparent",
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 3,
      tension: 0.3,
      fill,
      spanGaps: true,
    });

    state.charts.load = makeChart(
      $("load-chart"),
      [
        line("CPU", "#38bdf8", true),
        line("RAM", "#a78bfa"),
        line("Темп.", "#fb7185"),
      ],
      chartOptions({ min: 0, max: 100, unit: "%" })
    );

    state.charts.net = makeChart(
      $("net-chart"),
      [line("Приём", "#34d399", true), line("Отдача", "#fbbf24")],
      chartOptions({ min: 0, unit: " КБ/с" })
    );
  }

  function appendPoint(chart, label, values) {
    if (!chart) return;
    chart.data.labels.push(label);
    chart.data.datasets.forEach((dataset, index) => {
      dataset.data.push(values[index] ?? null);
    });
    while (chart.data.labels.length > MAX_POINTS) {
      chart.data.labels.shift();
      chart.data.datasets.forEach((dataset) => dataset.data.shift());
    }
    chart.update("none");
  }

  function networkRates(previous, current) {
    if (!previous) return { sent: 0, recv: 0 };
    const delta = Number(current.timestamp) - Number(previous.timestamp);
    if (!(delta > 0)) return { sent: 0, recv: 0 };
    const previousNet = previous.network || {};
    const currentNet = current.network || {};
    return {
      sent: Math.max(
        0,
        ((currentNet.bytes_sent || 0) - (previousNet.bytes_sent || 0)) /
          delta /
          1024
      ),
      recv: Math.max(
        0,
        ((currentNet.bytes_recv || 0) - (previousNet.bytes_recv || 0)) /
          delta /
          1024
      ),
    };
  }

  function renderProcesses(processes) {
    const body = $("processes-body");
    if (!body) return;
    const items = Array.isArray(processes)
      ? processes.slice(0, PROCESS_LIMIT)
      : [];
    if (items.length === 0) {
      body.innerHTML =
        '<tr><td colspan="4" class="empty">Нет данных</td></tr>';
      return;
    }
    body.replaceChildren();
    for (const process of items) {
      const row = document.createElement("tr");

      const nameCell = document.createElement("td");
      nameCell.className = "proc-name";
      nameCell.textContent = process.name || "?";
      nameCell.title = process.name || "";
      row.appendChild(nameCell);

      const pidCell = document.createElement("td");
      pidCell.className = "num";
      pidCell.textContent = process.pid ?? "—";
      row.appendChild(pidCell);

      const cpuCell = document.createElement("td");
      cpuCell.className = "num";
      const meter = document.createElement("span");
      meter.className = "meter";
      const bar = document.createElement("span");
      const cpuPercent = Number(process.cpu_percent) || 0;
      bar.style.width = `${Math.min(100, Math.max(0, cpuPercent))}%`;
      meter.appendChild(bar);
      cpuCell.appendChild(meter);
      cpuCell.appendChild(
        document.createTextNode(`${num(process.cpu_percent)}%`)
      );
      row.appendChild(cpuCell);

      const memCell = document.createElement("td");
      memCell.className = "num";
      memCell.textContent = `${num(process.memory_percent)}%`;
      row.appendChild(memCell);

      body.appendChild(row);
    }
  }

  function applyPoint(point) {
    if (!point || typeof point.timestamp !== "number") return;
    state.lastPoint = point;
    const cpu = point.cpu || {};
    const memory = point.memory || {};
    const disk = point.disk || {};
    const network = point.network || {};
    const rates = networkRates(state.lastNet, point);
    state.lastNet = {
      timestamp: point.timestamp,
      network: {
        bytes_sent: Number(network.bytes_sent) || 0,
        bytes_recv: Number(network.bytes_recv) || 0,
      },
    };

    $("cpu-value").textContent = `${num(cpu.percent)}%`;
    const cores = Array.isArray(cpu.per_cpu) ? cpu.per_cpu.length : 0;
    const load = Array.isArray(cpu.load_avg)
      ? cpu.load_avg.map((value) => num(value, 2)).join(" · ")
      : "—";
    $("cpu-sub").textContent = cores ? `${cores} ядер · LA ${load}` : `LA ${load}`;
    $("cpu-more").replaceChildren();
    for (const core of cpu.per_cpu) {
      const li = document.createElement('li');
      console.log(core);
      li.textContent = `${num(core)}`; 
      $("cpu-more").appendChild(li);
    }

    const temperature = point.temperature_c;
    $("temp-value").textContent =
      temperature === null || temperature === undefined
        ? "—"
        : `${num(temperature)}°C`;
    $("temp-sub").textContent =
      temperature === null || temperature === undefined
        ? "датчик недоступен"
        : "SoC";

    $("mem-value").textContent = `${num(memory.percent)}%`;
    $("mem-sub").textContent = `${num(memory.available, 0)} свободно из ${num(
      memory.total,
      0
    )} МБ`;

    $("disk-value").textContent = `${num(disk.percent)}%`;
    $("disk-sub").textContent = `${num(disk.used, 0)} / ${num(
      disk.total,
      0
    )} МБ`;

    $("rx-value").textContent = `${num(rates.recv, 1)} КБ/с`;
    $("rx-sub").textContent = `всего ${formatBytes(network.bytes_recv)}`;
    $("tx-value").textContent = `${num(rates.sent, 1)} КБ/с`;
    $("tx-sub").textContent = `всего ${formatBytes(network.bytes_sent)}`;

    const label = timeLabel(point.timestamp);
    appendPoint(state.charts.load, label, [
      numberOrNull(cpu.percent),
      numberOrNull(memory.percent),
      numberOrNull(temperature),
    ]);
    appendPoint(state.charts.net, label, [rates.recv, rates.sent]);

    renderProcesses(point.top_processes);
    $("processes-updated").textContent = `обновлено ${label}`;
    $("footer-updated").textContent = `последняя точка: ${label}`;
  }

  async function loadHistory() {
    const end = Date.now() / 1000;
    const start = end - HISTORY_WINDOW_SEC;
    try {
      const response = await apiFetch(
        `/api/v1/metrics/history?start=${start.toFixed(3)}&end=${end.toFixed(
          3
        )}&step=${HISTORY_STEP_SEC}`
      );
      if (!response.ok) return;
      const points = await response.json();
      if (!Array.isArray(points) || points.length === 0) return;
      const load = state.charts.load;
      const net = state.charts.net;
      if (!load || !net) return;

      load.data.labels = [];
      load.data.datasets.forEach((dataset) => {
        dataset.data = [];
      });
      net.data.labels = [];
      net.data.datasets.forEach((dataset) => {
        dataset.data = [];
      });

      let previous = null;
      for (const point of points) {
        const label = timeLabel(point.timestamp);
        load.data.labels.push(label);
        load.data.datasets[0].data.push(numberOrNull((point.cpu || {}).percent));
        load.data.datasets[1].data.push(
          numberOrNull((point.memory || {}).percent)
        );
        load.data.datasets[2].data.push(
          numberOrNull(point.temperature_c)
        );

        const rates = networkRates(previous, point);
        net.data.labels.push(label);
        net.data.datasets[0].data.push(rates.recv);
        net.data.datasets[1].data.push(rates.sent);
        previous = point;
      }
      state.lastNet = previous
        ? {
            timestamp: previous.timestamp,
            network: {
              bytes_sent: Number((previous.network || {}).bytes_sent) || 0,
              bytes_recv: Number((previous.network || {}).bytes_recv) || 0,
            },
          }
        : null;
      load.update("none");
      net.update("none");
    } catch (error) {
      console.warn("History request failed:", error);
    }
  }

  function syncIntervalSelect(value) {
    const select = $("interval-select");
    if (!select) return;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    const exists = Array.from(select.options).some(
      (option) => Number(option.value) === numeric
    );
    if (!exists) {
      const option = document.createElement("option");
      option.value = String(numeric);
      option.textContent = `${numeric} с`;
      select.appendChild(option);
    }
    select.value = String(numeric);
  }

  async function loadConfig() {
    try {
      const response = await apiFetch("/api/v1/config");
      if (!response.ok) return;
      const config = await response.json();
      if (config.interval_sec) syncIntervalSelect(config.interval_sec);
      if (config.buffer_size) {
        $("footer-meta").textContent = `буфер: ${config.buffer_size} точек · v${
          config.version || ""
        }`;
      }
    } catch (error) {
      console.warn("Config request failed:", error);
    }
  }

  async function changeInterval(value) {
    try {
      const response = await apiFetch("/api/v1/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interval_sec: Number(value) }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Не удалось изменить интервал");
      }
      syncIntervalSelect(data.interval_sec);
      showToast(`Интервал сбора: ${data.interval_sec} с`);
    } catch (error) {
      showToast(error.message || "Ошибка изменения интервала");
    }
  }

  function connect() {
    if (
      state.ws &&
      (state.ws.readyState === WebSocket.OPEN ||
        state.ws.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    setStatus("connecting");

    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const query = token ? `?token=${encodeURIComponent(token)}` : "";
    let socket;
    try {
      socket = new WebSocket(
        `${protocol}://${window.location.host}/ws/metrics${query}`
      );
    } catch (error) {
      scheduleReconnect();
      return;
    }
    state.ws = socket;

    socket.onopen = () => {
      state.reconnectAttempt = 0;
      setStatus("online");
      if (state.hasConnected) {
        // Catch up on points missed while the socket was down.
        loadHistory();
      }
      state.hasConnected = true;
    };
    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (message && typeof message.timestamp === "number") {
        applyPoint(message);
        return;
      }
      if (message && message.status === "error") {
        showToast(message.error || "Ошибка команды");
        return;
      }
      if (
        message &&
        message.status === "ok" &&
        typeof message.interval_sec === "number"
      ) {
        syncIntervalSelect(message.interval_sec);
      }
    };
    socket.onerror = () => {
      try {
        socket.close();
      } catch (error) {
        /* ignore */
      }
    };
    socket.onclose = () => {
      state.ws = null;
      setStatus("offline");
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    const delay = Math.min(
      15000,
      1000 * Math.pow(2, Math.min(state.reconnectAttempt, 4))
    );
    state.reconnectAttempt += 1;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      connect();
    }, delay);
  }

  function setLocalInput(input, date) {
    const pad = (value) => String(value).padStart(2, "0");
    input.value = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
      date.getDate()
    )}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function initExportRange() {
    const end = new Date();
    const start = new Date(end.getTime() - 60 * 60 * 1000);
    setLocalInput($("export-start"), start);
    setLocalInput($("export-end"), end);
  }

  function unixFromInput(input) {
    if (!input || !input.value) return NaN;
    return new Date(input.value).getTime() / 1000;
  }

  async function downloadExport(format) {
    const start = unixFromInput($("export-start"));
    const end = unixFromInput($("export-end"));
    if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) {
      showToast("Укажите корректный диапазон дат");
      return;
    }
    const button = $(format === "csv" ? "export-csv" : "export-json");
    if (button) button.disabled = true;
    try {
      const response = await apiFetch(
        `/api/v1/export?start=${start.toFixed(3)}&end=${end.toFixed(
          3
        )}&format=${format}`
      );
      if (!response.ok) {
        let detail = "Ошибка экспорта";
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (error) {
          /* ignore */
        }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = /filename="?([^";]+)"?/i.exec(disposition);
      const filename = match
        ? match[1]
        : `telemetry_${Math.round(start)}_${Math.round(end)}.${format}`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast(`Экспорт: ${filename}`);
    } catch (error) {
      showToast(error.message || "Ошибка экспорта");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function init() {
    $("host-label").textContent = window.location.host;
    initExportRange();
    createCharts();
    $("interval-select").addEventListener("change", (event) => {
      changeInterval(event.target.value);
    });
    $("export-csv").addEventListener("click", () => downloadExport("csv"));
    $("export-json").addEventListener("click", () => downloadExport("json"));
    await Promise.allSettled([loadConfig(), loadHistory()]);
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
