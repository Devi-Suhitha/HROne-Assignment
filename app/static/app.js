const pageSize = 20;
const employeePageSize = 100;
let attendancePage = 1;
let attendanceTotal = 0;
let employeePage = 1;
let employeeTotal = 0;
let toastTimer;

const $ = (selector) => document.querySelector(selector);

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
}

function apiErrorMessage(payload, status) {
  const detail = payload && payload.detail;
  if (Array.isArray(detail)) {
    return detail.map((issue) => {
      const location = Array.isArray(issue.loc) ? `${issue.loc.map(String).join(".")}: ` : "";
      return `${location}${issue.msg || "Invalid value"}`;
    }).join("; ");
  }
  if (typeof detail === "string") return detail;
  return `Request failed (${status}). Please try again.`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) throw new Error(`Request failed (${response.status}).`);
  }
  if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
  return payload;
}

function setLoading(button, loading, label) {
  if (loading) {
    button.dataset.originalText = button.textContent;
    button.textContent = label || "Loading…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
    delete button.dataset.originalText;
  }
}

async function withLoading(button, action, label) {
  setLoading(button, true, label);
  try {
    await action();
  } catch (error) {
    showToast(error.message || "Something went wrong.", true);
  } finally {
    setLoading(button, false);
  }
}

function addCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}

function emptyRow(body, columnCount, message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = columnCount;
  cell.className = "empty-state";
  cell.textContent = message;
  row.append(cell);
  body.replaceChildren(row);
}

function formatDateTime(epoch) {
  if (epoch === null || epoch === undefined) return "—";
  const date = new Date(epoch);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function currentIstDate() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function setTodayDefaults() {
  const today = currentIstDate();
  const month = today.slice(0, 7);
  $("#today-label").textContent = new Intl.DateTimeFormat(undefined, {
    dateStyle: "full",
    timeZone: "Asia/Kolkata",
  }).format(new Date(`${today}T12:00:00+05:30`));
  document.querySelectorAll('input[type="month"]').forEach((input) => { input.value = month; });
  const [year, monthNumber, day] = today.split("-").map(Number);
  const from = new Date(Date.UTC(year, monthNumber - 1, day - 6)).toISOString().slice(0, 10);
  $("#trend-form [name='from']").value = from;
  $("#trend-form [name='to']").value = today;
  $("#attendance-filter [name='date_from']").value = today;
  $("#attendance-filter [name='date_to']").value = today;
}

async function loadDashboard() {
  const date = currentIstDate();
  const [employees, attendance] = await Promise.all([
    api("/employees?page=1&page_size=1"),
    api(`/attendance?date_from=${encodeURIComponent(date)}&date_to=${encodeURIComponent(date)}&page=1&page_size=100`),
  ]);
  const complete = attendance.items.length === attendance.total;
  const present = complete ? attendance.items.filter((item) => ["PRESENT", "WFH", "ON_DUTY"].includes(item.status)).length : "—";
  const late = complete ? attendance.items.filter((item) => item.late_minutes > 0).length : "—";
  $("#stat-employees").textContent = employees.total;
  $("#stat-present").textContent = present;
  $("#stat-late").textContent = late;
  $("#stat-records").textContent = attendance.total;
  $("#stat-present").nextElementSibling.textContent = complete ? "Presence-status records" : "More than 100 records";
  $("#stat-late").nextElementSibling.textContent = complete ? "Records with late minutes" : "More than 100 records";
}

async function loadEmployees() {
  const body = $("#employees-body");
  emptyRow(body, 5, "Loading employees…");
  let result;
  try {
    result = await api(`/employees?page=${employeePage}&page_size=${employeePageSize}`);
  } catch (error) {
    emptyRow(body, 5, error.message || "Unable to load employees.");
    throw error;
  }
  employeeTotal = result.total;
  employeePage = result.page;
  $("#employee-count").textContent = `${result.total} employee${result.total === 1 ? "" : "s"}`;
  $("#employees-page").textContent = `Page ${result.page} of ${Math.max(1, Math.ceil(result.total / employeePageSize))}`;
  $("#employees-prev").disabled = result.page <= 1;
  $("#employees-next").disabled = result.page * employeePageSize >= result.total;
  if (!result.items.length) {
    emptyRow(body, 5, "No employees yet. Add your first employee above.");
    return;
  }
  body.replaceChildren();
  result.items.forEach((employee) => {
    const row = document.createElement("tr");
    const person = document.createElement("td");
    const identity = document.createElement("div");
    identity.className = "person-cell";
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = employee.name.split(/\s+/).map((part) => part[0]).slice(0, 2).join("").toUpperCase();
    const details = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = employee.name;
    const email = document.createElement("small");
    email.className = "muted";
    email.textContent = employee.email;
    details.append(name, document.createElement("br"), email);
    identity.append(avatar, details);
    person.append(identity);
    row.append(person);
    addCell(row, employee.emp_code);
    addCell(row, employee.department);
    addCell(row, `${employee.shift_start} – ${employee.shift_end}`);
    addCell(row, employee.joined_on);
    body.append(row);
  });
}

function attendanceQuery(page) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  new FormData($("#attendance-filter")).forEach((value, key) => {
    if (value) params.set(key, value);
  });
  return params;
}

async function loadAttendance() {
  const body = $("#attendance-body");
  emptyRow(body, 7, "Loading attendance…");
  let result;
  try {
    result = await api(`/attendance?${attendanceQuery(attendancePage)}`);
  } catch (error) {
    emptyRow(body, 7, error.message || "Unable to load attendance.");
    throw error;
  }
  attendanceTotal = result.total;
  attendancePage = result.page;
  $("#attendance-count").textContent = `${result.total} record${result.total === 1 ? "" : "s"} found`;
  $("#attendance-page").textContent = `Page ${result.page} of ${Math.max(1, Math.ceil(result.total / pageSize))}`;
  $("#attendance-prev").disabled = result.page <= 1;
  $("#attendance-next").disabled = result.page * pageSize >= result.total;
  if (!result.items.length) {
    emptyRow(body, 7, "No attendance records match these filters.");
    return;
  }
  body.replaceChildren();
  result.items.forEach((record) => {
    const row = document.createElement("tr");
    addCell(row, record.date);
    addCell(row, record.emp_code);
    const status = addCell(row, record.status);
    status.className = `status-cell status-${record.status.toLowerCase()}`;
    addCell(row, formatDateTime(record.punch_in));
    addCell(row, formatDateTime(record.punch_out));
    addCell(row, record.work_hours === null ? "—" : Number(record.work_hours).toFixed(2));
    addCell(row, `${record.late_minutes} min`);
    body.append(row);
  });
}

function reportMessage(container, message) {
  container.replaceChildren();
  container.className = "report-result muted";
  container.textContent = message;
}

function addMetric(container, label, value) {
  const metric = document.createElement("div");
  metric.className = "metric";
  const caption = document.createElement("small");
  caption.textContent = label;
  const amount = document.createElement("strong");
  amount.textContent = value === null || value === undefined ? "—" : String(value);
  metric.append(caption, amount);
  container.append(metric);
}

function renderMetrics(container, fields) {
  container.replaceChildren();
  container.className = "report-result";
  const grid = document.createElement("div");
  grid.className = "metric-grid";
  fields.forEach(([label, value]) => addMetric(grid, label, value));
  container.append(grid);
}

function renderTable(container, columns, items, emptyMessage) {
  container.replaceChildren();
  container.className = "report-result";
  if (!items.length) {
    container.classList.add("muted");
    container.textContent = emptyMessage;
    return;
  }
  const table = document.createElement("table");
  table.className = "mini-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach(([, title]) => {
    const cell = document.createElement("th");
    cell.textContent = title;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  items.forEach((item) => {
    const row = document.createElement("tr");
    columns.forEach(([key]) => addCell(row, item[key]));
    body.append(row);
  });
  table.append(head, body);
  container.append(table);
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function asEpoch(value) {
  if (!value) return undefined;
  const epoch = new Date(value).getTime();
  if (!Number.isSafeInteger(epoch)) throw new Error("Enter a valid date and time.");
  return epoch;
}

function queryString(values) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== "" && value !== undefined && value !== null) params.set(key, value);
  });
  return params.toString();
}

async function onEmployeeSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  await withLoading(button, async () => {
    await api("/employees", { method: "POST", body: JSON.stringify(formValues(form)) });
    form.reset();
    form.classList.add("hidden");
    await Promise.all([loadEmployees(), loadDashboard()]);
    showToast("Employee created successfully.");
  }, "Creating…");
}

async function onPunchSubmit(event, route, includeStatus) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  await withLoading(button, async () => {
    const values = formValues(form);
    const body = { emp_code: values.emp_code };
    if (includeStatus) body.status = values.status;
    const punchedAt = asEpoch(values.punched_at);
    if (punchedAt !== undefined) body.punched_at = punchedAt;
    await api(route, { method: "POST", body: JSON.stringify(body) });
    form.reset();
    await Promise.all([loadAttendance(), loadDashboard()]);
    showToast(includeStatus ? "Punch-in recorded successfully." : "Punch-out recorded successfully.");
  }, "Saving…");
}

async function onReportSubmit(event, route, resultSelector, render, queryValues) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  const container = $(resultSelector);
  await withLoading(button, async () => {
    reportMessage(container, "Loading report…");
    try {
      const query = queryString(queryValues || formValues(form));
      const result = await api(query ? `${route}?${query}` : route);
      render(container, result);
    } catch (error) {
      reportMessage(container, error.message || "Unable to load report.");
      throw error;
    }
  }, "Loading…");
}

function bindEvents() {
  $("#refresh-all").addEventListener("click", (event) => withLoading(event.currentTarget, async () => {
    await Promise.all([loadDashboard(), loadEmployees(), loadAttendance()]);
    showToast("Dashboard data refreshed.");
  }, "Refreshing…"));
  $("#refresh-employees").addEventListener("click", (event) => withLoading(event.currentTarget, async () => {
    await loadEmployees();
    showToast("Employee list refreshed.");
  }, "Loading…"));
  $("#employees-prev").addEventListener("click", (event) => {
    employeePage = Math.max(1, employeePage - 1);
    withLoading(event.currentTarget, loadEmployees, "Loading…");
  });
  $("#employees-next").addEventListener("click", (event) => {
    if (employeePage * employeePageSize >= employeeTotal) return;
    employeePage += 1;
    withLoading(event.currentTarget, loadEmployees, "Loading…");
  });
  $("#toggle-employee-form").addEventListener("click", () => {
    $("#employee-form").classList.toggle("hidden");
    if (!$("#employee-form").classList.contains("hidden")) $("#employee-form [name='emp_code']").focus();
  });
  $("#cancel-employee").addEventListener("click", () => $("#employee-form").classList.add("hidden"));
  $("#employee-form").addEventListener("submit", onEmployeeSubmit);
  $("#punch-in-form").addEventListener("submit", (event) => onPunchSubmit(event, "/attendance/punch-in", true));
  $("#punch-out-form").addEventListener("submit", (event) => onPunchSubmit(event, "/attendance/punch-out", false));
  $("#attendance-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    attendancePage = 1;
    withLoading(event.submitter || $("#attendance-filter button[type='submit']"), async () => loadAttendance(), "Filtering…");
  });
  $("#attendance-prev").addEventListener("click", (event) => {
    attendancePage = Math.max(1, attendancePage - 1);
    withLoading(event.currentTarget, loadAttendance, "Loading…");
  });
  $("#attendance-next").addEventListener("click", (event) => {
    if (attendancePage * pageSize >= attendanceTotal) return;
    attendancePage += 1;
    withLoading(event.currentTarget, loadAttendance, "Loading…");
  });
  $("#monthly-form").addEventListener("submit", (event) => onReportSubmit(
    event, `/analytics/employees/${encodeURIComponent($("#monthly-form [name='emp_code']").value)}/monthly`,
    "#monthly-result", (container, result) => renderMetrics(container, [
      ["Working days", result.working_days], ["Present days", result.present_days], ["Attendance", result.attendance_pct === null ? "—" : `${result.attendance_pct}%`],
      ["Leave days", result.leave_days], ["Late arrivals", result.late_count], ["Late minutes", result.total_late_minutes],
      ["Overtime minutes", result.total_overtime_minutes],
    ]), { month: $("#monthly-form [name='month']").value },
  ));
  $("#department-summary-form").addEventListener("submit", (event) => {
    const values = formValues(event.currentTarget);
    onReportSubmit(event, "/analytics/departments/summary", "#department-summary-result",
      (container, result) => renderTable(container, [
        ["department", "Department"], ["headcount", "Team"], ["present_days", "Present days"],
        ["avg_work_hours", "Avg. hours"], ["late_count", "Late"], ["leave_count", "Leave"], ["on_duty_count", "On duty"],
      ], result.items, "No departments found for this month."), values);
  });
  $("#leaderboard-form").addEventListener("submit", (event) => {
    const values = formValues(event.currentTarget);
    onReportSubmit(event, "/analytics/leaderboard/late", "#leaderboard-result",
      (container, result) => renderTable(container, [
        ["rank", "Rank"], ["emp_code", "Code"], ["name", "Employee"], ["department", "Department"],
        ["total_late_minutes", "Late min"], ["late_count", "Late days"],
      ], result.items, "No late arrivals for this month."), { ...values, limit: 10 });
  });
  $("#trend-form").addEventListener("submit", (event) => {
    const values = formValues(event.currentTarget);
    const department = encodeURIComponent(values.department);
    onReportSubmit(event, `/analytics/departments/${department}/trend`, "#trend-result",
      (container, result) => renderTable(container, [
        ["date", "Date"], ["is_working_day", "Work day"], ["headcount", "Team"],
        ["present_count", "Present"], ["late_count", "Late"], ["attendance_rate", "Rate"], ["moving_avg_7d", "7-day avg"],
      ], result.items.map((item) => ({
        ...item,
        is_working_day: item.is_working_day ? "Yes" : "No",
        attendance_rate: item.attendance_rate === null ? "—" : `${(item.attendance_rate * 100).toFixed(2)}%`,
        moving_avg_7d: item.moving_avg_7d === null ? "—" : `${(item.moving_avg_7d * 100).toFixed(2)}%`,
      })), "No trend data in this range."), { from: values.from, to: values.to });
  });
}

async function initialLoad(task, target, columns) {
  try {
    await task();
  } catch (error) {
    if (target) emptyRow($(target), columns, error.message || "Unable to load data.");
    showToast(error.message || "Unable to load data.", true);
  }
}

setTodayDefaults();
bindEvents();
initialLoad(loadDashboard);
initialLoad(loadEmployees, "#employees-body", 5);
initialLoad(loadAttendance, "#attendance-body", 7);
