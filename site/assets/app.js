"use strict";

const FLIGHT_ORDER = ["SU1032", "SU1009"];
const MONTHS_SHORT = [
  "янв", "фев", "мар", "апр", "май", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек",
];
const WEEKDAYS_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];

function parseDay(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

function isoDay(value) {
  return value.toISOString().slice(0, 10);
}

function displayDays(rows, limit = 30) {
  if (!Array.isArray(rows) || rows.length === 0) return [];
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const dates = [...byDate.keys()].sort();
  const newest = parseDay(dates[dates.length - 1]);
  const oldestAvailable = parseDay(dates[0]);
  const lowerBound = new Date(newest);
  lowerBound.setUTCDate(lowerBound.getUTCDate() - (limit - 1));
  const oldest = oldestAvailable > lowerBound ? oldestAvailable : lowerBound;
  const result = [];

  for (let cursor = new Date(newest); cursor >= oldest; cursor.setUTCDate(cursor.getUTCDate() - 1)) {
    const key = isoDay(cursor);
    result.push(byDate.get(key) || { date: key });
  }
  return result;
}

function formatDay(value) {
  const date = parseDay(value);
  return {
    day: String(date.getUTCDate()).padStart(2, "0"),
    month: MONTHS_SHORT[date.getUTCMonth()],
    weekday: WEEKDAYS_SHORT[date.getUTCDay()],
  };
}

function formatTime(value, timeZone) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatDuration(minutes) {
  if (!Number.isFinite(minutes)) return "—";
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest} мин`;
  return `${hours} ч ${String(rest).padStart(2, "0")} мин`;
}

function formatAverage(value) {
  return Number.isFinite(value) ? `${value} мин` : "—";
}

function formatPercent(value) {
  if (!Number.isFinite(value)) return "—";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value)}%`;
}

function positiveDelay(record) {
  if (!record) return 0;
  const values = [record.departureDelayMinutes, record.arrivalDelayMinutes]
    .filter(Number.isFinite)
    .map((value) => Math.max(0, value));
  return values.length ? Math.max(...values) : 0;
}

function severity(record) {
  if (!record || record.status === "unknown") return "muted";
  if (record.status === "cancelled" || record.status === "diverted") return "critical";
  const delay = positiveDelay(record);
  if (delay >= 30) return "critical";
  if (delay >= 15) return "warning";
  return "normal";
}

function statusText(record) {
  if (!record) return "Нет данных";
  if (record.status === "cancelled") return "Отменён";
  if (record.status === "diverted") return "Перенаправлен";
  if (record.status === "unknown") return "Нет данных";
  if (record.status === "airborne") return "В пути";
  if (record.status === "scheduled") {
    const delay = positiveDelay(record);
    return delay >= 15 ? `Задержка ${formatDuration(delay)}` : "Ожидается";
  }
  const delay = positiveDelay(record);
  if (delay >= 15) return `Задержка ${formatDuration(delay)}`;
  return "Завершён";
}

function mainTime(actual, estimated, timeZone) {
  if (actual) return formatTime(actual, timeZone);
  if (estimated) return `~${formatTime(estimated, timeZone)}`;
  return "—";
}

function metric(label, actual, estimated, scheduled, timeZone) {
  return `
    <div class="metric">
      <span class="metric-label">${label}</span>
      <strong>${mainTime(actual, estimated, timeZone)}</strong>
      <small>план ${formatTime(scheduled, timeZone)}</small>
    </div>`;
}

function timelinePoint(label, airport, actual, estimated, scheduled, timeZone) {
  return `
    <div class="timeline-point">
      <span>${label} · ${airport}</span>
      <strong>${mainTime(actual, estimated, timeZone)}</strong>
      <small>план ${formatTime(scheduled, timeZone)}</small>
    </div>`;
}

function flightPanel(code, record, config) {
  const panelSeverity = severity(record);
  const route = `${config.departureAirport} → ${config.arrivalAirport}`;
  const duration = record ? record.durationMinutes : null;
  return `
    <article class="flight-panel severity-${panelSeverity}" aria-label="${code}, ${route}, ${statusText(record)}">
      <header class="flight-header">
        <div>
          <strong class="flight-code">${code}</strong>
          <span class="route">${route}</span>
        </div>
        <span class="status">${statusText(record)}</span>
      </header>

      <div class="metrics">
        ${metric(
          "Вылет",
          record?.actualDeparture,
          record?.estimatedDeparture,
          record?.scheduledDeparture,
          config.departureTimezone,
        )}
        ${metric(
          "Прилёт",
          record?.actualArrival,
          record?.estimatedArrival,
          record?.scheduledArrival,
          config.arrivalTimezone,
        )}
        <div class="metric duration-metric">
          <span class="metric-label">В пути</span>
          <strong>${formatDuration(duration)}</strong>
          <small>фактически</small>
        </div>
      </div>

      <div class="route-timeline" aria-hidden="true">
        ${timelinePoint(
          "вылет",
          config.departureAirport,
          record?.actualDeparture,
          record?.estimatedDeparture,
          record?.scheduledDeparture,
          config.departureTimezone,
        )}
        <div class="timeline-line"><span>${formatDuration(duration)}</span></div>
        ${timelinePoint(
          "прилёт",
          config.arrivalAirport,
          record?.actualArrival,
          record?.estimatedArrival,
          record?.scheduledArrival,
          config.arrivalTimezone,
        )}
      </div>
    </article>`;
}

function dayRow(row, flights) {
  const date = formatDay(row.date);
  return `
    <section class="day-row" data-date="${row.date}">
      <div class="left-flight">${flightPanel("SU1032", row.SU1032, flights.SU1032)}</div>
      <time class="day-date" datetime="${row.date}">
        <span class="weekday">${date.weekday}</span>
        <strong>${date.day}</strong>
        <span>${date.month}</span>
      </time>
      <div class="right-flight">${flightPanel("SU1009", row.SU1009, flights.SU1009)}</div>
    </section>`;
}

function statPanel(code, statistics, config) {
  const stats = statistics?.[code] || {};
  return `
    <section class="stat-panel" aria-label="Статистика ${code}">
      <header>
        <strong>${code}</strong>
        <span>${config.departureAirport} → ${config.arrivalAirport}</span>
      </header>
      <div class="stat-values">
        <div><span>ср. задержка вылета</span><strong>${formatAverage(stats.averageDepartureDelayMinutes)}</strong></div>
        <div><span>ср. задержка прилёта</span><strong>${formatAverage(stats.averageArrivalDelayMinutes)}</strong></div>
        <div><span>вероятность отмены</span><strong>${formatPercent(stats.cancellationProbabilityPercent)}</strong></div>
      </div>
    </section>`;
}

function renderStatistics(data) {
  document.getElementById("statistics").innerHTML = `
    <div class="statistics-title">Статистика</div>
    <div class="stats-left">${statPanel("SU1032", data.statistics, data.flights.SU1032)}</div>
    <div class="stats-right">${statPanel("SU1009", data.statistics, data.flights.SU1009)}</div>`;
}

function renderUpdatedAt(value) {
  const element = document.getElementById("updated-at");
  if (!value) {
    element.textContent = "Время обновления неизвестно";
    return;
  }
  const formatted = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
  element.textContent = `Обновлено ${formatted} МСК`;
}

async function start() {
  const list = document.getElementById("history-list");
  try {
    const response = await fetch("data/flights.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const rows = displayDays(data.days, 30);
    list.innerHTML = rows.map((row) => dayRow(row, data.flights)).join("");
    renderStatistics(data);
    renderUpdatedAt(data.updatedAt);
  } catch (error) {
    list.innerHTML = `<p class="load-error">Не удалось загрузить данные рейсов.</p>`;
    document.getElementById("updated-at").textContent = "Данные недоступны";
    console.error(error);
  }
}

start();
