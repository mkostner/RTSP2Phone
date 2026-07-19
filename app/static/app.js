const $ = selector => document.querySelector(selector);
let editing = null;
let streamEditing = null;
let streams = [];
let recipients = [];
let previewObjectUrl = null;
let hasRunningJobs = false;
let activeTab = "tasks";

const headers = () => ({"Content-Type":"application/json","X-Api-Token":localStorage.token || ""});
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const say = (text, error = false) => { $("#message").textContent = text; $("#message").style.color = error ? "#fecaca" : "#fbbf24"; };
const statusClass = status => ["running","sent","error","interrupted"].includes(status) ? status : "muted";
const statusText = status => ({running:"Ejecutando",sent:"Enviado",error:"Error",interrupted:"Interrumpido"}[status] || "En espera");

function streamEndpoint(sourceUrl) {
  try {
    const url = new URL(sourceUrl);
    return `${url.protocol}//${url.host}${url.pathname}`;
  } catch (_) {
    return sourceUrl.replace(/:\/\/[^/@]+(?::[^/@]*)?@/, "://").split("?")[0];
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {...options, headers:{...headers(), ...(options.headers || {})}});
  if (!response.ok) throw new Error((await response.text()).replace(/^.*"detail":"?([^"}]+).*$/, "$1"));
  return response.status === 204 ? null : response.json();
}

function selectTab(tab) {
  activeTab = tab;
  document.querySelectorAll("[data-tab]").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".tab-pane").forEach(pane => pane.hidden = pane.id !== `tab-${tab}`);
  if (tab === "logs") loadLogs(true);
}

async function loadLogs(silent = false) {
  const output = $("#logs-output");
  const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 36;
  try {
    const data = await request("/api/logs?limit=200");
    output.textContent = data.lines.length ? data.lines.join("\n") : "Aún no hay logs persistidos.";
    if (nearBottom || !output.dataset.loaded) output.scrollTop = output.scrollHeight;
    output.dataset.loaded = "true";
  } catch (error) {
    if (!silent) say(error.message, true);
    output.textContent = `No se pudieron cargar los logs: ${error.message}`;
  }
}

function setConnection(ok) {
  const chip = $("#connection-status");
  chip.textContent = ok ? "Conectado" : "Sin conectar";
  chip.className = `status-chip ${ok ? "sent" : "muted"}`;
}

function resetJob() {
  editing = null;
  $("#job-form").reset();
  [["duration",10],["timelapse_duration",3600],["timelapse_interval",60],["timelapse_frames",61],["timelapse_fps",24],["schedule_time","08:00"]].forEach(([id, value]) => $("#" + id).value = value);
  $("#enabled").checked = true;
  $("#form-title").textContent = "Nueva tarea";
  $("#cancel").hidden = true;
  updateFormControls();
}

function resetStream() {
  streamEditing = null;
  $("#stream-form").reset();
  $("#warmup_seconds").value = 10;
  $("#stream_enabled").checked = true;
  $("#stream-title").textContent = "Nueva fuente";
  $("#stream-cancel").hidden = true;
}

function updateTimelapseHelper() {
  const duration = Math.max(1, Number($("#timelapse_duration").value) || 3600);
  const cadence = effectiveTimelapseCadence();
  const interval = cadence.effective;
  const frames = Math.floor(duration / interval) + 1;
  const videoSeconds = frames / 24;
  $("#timelapse_frames").value = frames;
  $("#timelapse_fps").value = 24;
  $("#timelapse-estimate").textContent = `${friendlyDuration(duration)} real → video de ${videoSeconds.toFixed(videoSeconds % 1 ? 1 : 0)} s`;
  $("#timelapse-detail").textContent = cadence.adjusted
    ? `${frames} fotos. La estabilización RTSP de ${friendlyDuration(cadence.warmup)} fija una cadencia real de ${friendlyDuration(interval)}.`
    : `${frames} fotos, una cada ${friendlyDuration(interval)}.`;
}

function effectiveTimelapseCadence() {
  const requested = Math.max(1, Number($("#timelapse_interval").value) || 1);
  const stream = streams.find(item => item.id === $("#stream_id").value);
  const warmup = stream?.source_type === "rtsp" ? Number(stream.warmup_seconds || 0) : 0;
  return {effective:Math.max(requested, warmup), warmup, adjusted:warmup > requested};
}

function friendlyDuration(seconds) {
  if (seconds % 3600 === 0) return `${seconds / 3600} ${seconds === 3600 ? "hora" : "horas"}`;
  if (seconds % 60 === 0) return `${seconds / 60} ${seconds === 60 ? "minuto" : "minutos"}`;
  return `${seconds} segundos`;
}

function updateScheduleControls() {
  const custom = $("#schedule_mode").value === "custom";
  $("#schedule-time-field").hidden = $("#schedule_mode").value !== "daily";
  $("#cron-field").hidden = !custom;
  $("#cron").required = custom;
}

function updateFormControls() {
  const kind = $("#kind").value;
  $("#video-duration-field").hidden = kind !== "video";
  $("#timelapse-fields").hidden = kind !== "timelapse";
  updateScheduleControls();
  if (kind === "timelapse") updateTimelapseHelper();
}

function cronFromSchedule() {
  const mode = $("#schedule_mode").value;
  if (mode === "hourly") return "0 * * * *";
  if (mode === "every_15") return "*/15 * * * *";
  if (mode === "every_30") return "*/30 * * * *";
  if (mode === "custom") return $("#cron").value.trim();
  const [hour, minute] = $("#schedule_time").value.split(":");
  return `${Number(minute)} ${Number(hour)} * * *`;
}

function scheduleFromCron(cron) {
  if (cron === "0 * * * *") return {mode:"hourly"};
  if (cron === "*/15 * * * *") return {mode:"every_15"};
  if (cron === "*/30 * * * *") return {mode:"every_30"};
  const daily = cron.match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$/);
  if (daily) return {mode:"daily", time:`${daily[2].padStart(2,"0")}:${daily[1].padStart(2,"0")}`};
  return {mode:"custom", cron};
}

function scheduleLabel(cron) {
  const schedule = scheduleFromCron(cron);
  if (schedule.mode === "hourly") return "Cada hora";
  if (schedule.mode === "every_15") return "Cada 15 minutos";
  if (schedule.mode === "every_30") return "Cada 30 minutos";
  if (schedule.mode === "daily") return `Todos los días a las ${schedule.time}`;
  return "Horario avanzado";
}

function chooseTimelapseDuration(seconds) {
  const select = $("#timelapse_duration");
  const exact = [...select.options].find(option => Number(option.value) === seconds);
  if (!exact) select.add(new Option(`${friendlyDuration(seconds)} (actual)`, String(seconds), true, true));
  select.value = String(seconds);
}

function renderStreams() {
  const select = $("#stream_id");
  const current = select.value;
  select.innerHTML = streams.filter(stream => stream.enabled).map(stream => `<option value="${stream.id}">${esc(stream.name)} (${stream.source_type.toUpperCase()})</option>`).join("");
  if ([...select.options].some(option => option.value === current)) select.value = current;
  $("#streams").innerHTML = streams.map(stream => `<article class="job"><div class="job-head"><strong>${esc(stream.name)}</strong><span class="status-chip ${stream.enabled ? "sent" : "muted"}">${stream.enabled ? "Activa" : "Pausada"}</span></div><p class="meta">${stream.source_type.toUpperCase()} · estabilización ${stream.warmup_seconds}s</p><p class="meta">Apunta a ${esc(streamEndpoint(stream.source_url))}</p><div class="job-actions"><button type="button" class="compact" data-snapshot-stream="${stream.id}" ${stream.enabled ? "" : "disabled"}>Traer instantánea</button><button type="button" class="secondary compact" data-test-stream="${stream.id}">Probar</button><button type="button" class="secondary compact" data-edit-stream="${stream.id}">Editar</button><button type="button" class="danger compact" data-del-stream="${stream.id}">Eliminar</button></div></article>`).join("") || "<p class='empty'>Agrega una fuente de video.</p>";
  document.querySelectorAll("[data-snapshot-stream]").forEach(button => button.onclick = () => showSnapshot(streams.find(item => item.id === button.dataset.snapshotStream), button));
  document.querySelectorAll("[data-test-stream]").forEach(button => button.onclick = async () => {
    try { button.disabled = true; button.textContent = "Probando…"; say("Capturando snapshot de prueba…"); await request(`/api/streams/${button.dataset.testStream}/test`, {method:"POST"}); say("La fuente respondió correctamente."); }
    catch (error) { say(error.message, true); }
    finally { button.disabled = false; button.textContent = "Probar"; }
  });
  document.querySelectorAll("[data-edit-stream]").forEach(button => button.onclick = () => {
    const stream = streams.find(item => item.id === button.dataset.editStream);
    streamEditing = stream.id;
    [["stream_name","name"],["source_type","source_type"],["source_url","source_url"],["warmup_seconds","warmup_seconds"]].forEach(([id,key]) => $("#" + id).value = stream[key]);
    $("#stream_enabled").checked = stream.enabled;
    $("#stream-title").textContent = "Editar fuente";
    $("#stream-cancel").hidden = false;
  });
  document.querySelectorAll("[data-del-stream]").forEach(button => button.onclick = async () => {
    if (!confirm("¿Eliminar esta fuente?")) return;
    try { await request(`/api/streams/${button.dataset.delStream}`, {method:"DELETE"}); await load(); } catch (error) { say(error.message, true); }
  });
}

function renderRecipients() {
  const select = $("#recipient_id");
  const current = select.value;
  select.innerHTML = recipients.filter(item => item.enabled).map(item => `<option value="${item.id}">${esc(item.name)} · ${item.channel === "waha" ? "WhatsApp" : "Telegram"}</option>`).join("");
  if ([...select.options].some(option => option.value === current)) select.value = current;
  $("#recipients").innerHTML = recipients.map(item => `<article class="job"><div class="job-head"><strong>${esc(item.name)}</strong><span class="status-chip ${item.enabled ? "sent" : "muted"}">${item.channel === "waha" ? "WhatsApp" : "Telegram"}</span></div><p class="meta">${esc(item.destination)}</p><button class="danger compact" data-delete-recipient="${item.id}">Eliminar</button></article>`).join("") || "<p class='empty'>Agrega un destinatario para usarlo en tareas.</p>";
  document.querySelectorAll("[data-delete-recipient]").forEach(button => button.onclick = async () => { if (!confirm("¿Eliminar este destinatario?")) return; try { await request(`/api/recipients/${button.dataset.deleteRecipient}`, {method:"DELETE"}); await load(true); } catch (error) { say(error.message, true); } });
  updateRecipientDestination();
}

function updateRecipientDestination() {
  const recipient = recipients.find(item => item.id === $("#recipient_id").value);
  $("#chat_id").value = recipient?.destination || "";
  $("#delivery_channel").value = recipient?.channel || "waha";
  $("#recipient-hint").textContent = recipient ? `${recipient.channel === "waha" ? "WhatsApp" : "Telegram"}: ${recipient.destination}` : "Configura un destinatario en la pestaña Configuración.";
}

function renderSettings(settings) {
  $("#settings_waha_url").value = settings.waha_url || "";
  $("#settings_waha_session").value = settings.waha_session || "default";
  $("#settings_waha_api_key").value = "";
  $("#settings_telegram_token").value = "";
  $("#waha-summary").textContent = settings.waha_api_key_configured ? "Canal configurado" : "Falta API key";
  $("#telegram-summary").textContent = settings.telegram_configured ? "Bot configurado" : "Falta token";
}

async function showSnapshot(stream, button) {
  selectTab("tasks");
  const preview = $("#snapshot-preview");
  const image = $("#preview-image");
  const loading = $("#preview-loading");
  preview.hidden = false;
  $("#preview-title").textContent = stream.name;
  $("#preview-meta").textContent = `Solicitando imagen desde ${streamEndpoint(stream.source_url)}…`;
  image.removeAttribute("src");
  loading.hidden = false;
  button.disabled = true;
  button.textContent = "Capturando…";
  try {
    const response = await fetch(`/api/streams/${stream.id}/snapshot`, {method:"POST", headers:headers()});
    if (!response.ok) throw new Error((await response.text()).replace(/^.*"detail":"?([^"}]+).*$/, "$1"));
    const blob = await response.blob();
    if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = URL.createObjectURL(blob);
    image.onload = () => { loading.hidden = true; };
    image.src = previewObjectUrl;
    $("#preview-meta").textContent = `Instantánea recibida ahora · ${streamEndpoint(stream.source_url)}`;
  } catch (error) {
    loading.hidden = true;
    $("#preview-meta").textContent = error.message;
    say(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Traer instantánea";
  }
}

function editJob(job) {
  editing = job.id;
  const fields = {duration_seconds:"duration",timelapse_interval_seconds:"timelapse_interval",name:"name",kind:"kind",stream_id:"stream_id",caption:"caption"};
  Object.entries(fields).forEach(([key,id]) => $("#" + id).value = job[key] ?? "");
  const schedule = scheduleFromCron(job.cron);
  $("#schedule_mode").value = schedule.mode;
  if (schedule.time) $("#schedule_time").value = schedule.time;
  if (schedule.cron) $("#cron").value = schedule.cron;
  if (job.kind === "timelapse") chooseTimelapseDuration(Math.max(1, (job.timelapse_frames - 1) * job.timelapse_interval_seconds));
  $("#recipient_id").value = job.recipient_id || "";
  updateRecipientDestination();
  $("#enabled").checked = job.enabled;
  $("#form-title").textContent = "Editar tarea";
  $("#cancel").hidden = false;
  updateFormControls();
  window.scrollTo({top:0, behavior:"smooth"});
}

function progressData(job) {
  const total = Math.max(0, Number(job.progress_total) || 0);
  const current = Math.min(total, Math.max(0, Number(job.progress_current) || 0));
  const percent = total ? Math.round(current * 100 / total) : 0;
  const phase = job.progress_phase || (job.last_status === "running" ? "Preparando" : statusText(job.last_status));
  const detail = job.progress_message || (total ? `${current} de ${total}` : "Aún sin actividad");
  return {current,total,percent,phase,detail};
}

function renderJobs(jobs) {
  const root = $("#jobs");
  hasRunningJobs = jobs.some(job => job.last_status === "running");
  if (!jobs.length) { root.innerHTML = "<p class='empty'>No hay tareas aún.</p>"; return; }
  root.innerHTML = jobs.map(job => {
    const source = streams.find(stream => stream.id === job.stream_id);
    const progress = progressData(job);
    const running = job.last_status === "running";
    const showProgress = running || progress.total > 0;
    return `<article class="job ${statusClass(job.last_status)}"><div class="job-head"><strong>${esc(job.name)}</strong><span class="status-chip ${statusClass(job.last_status)}">${statusText(job.last_status)}</span></div><div class="job-meta"><span class="meta">${esc(job.kind)} · ${esc(source?.name || "sin fuente")} · ${esc(scheduleLabel(job.cron))}</span><span class="meta">${running ? `Inicio: ${esc(job.run_started_at || "ahora")}` : `Último: ${esc(job.last_run || "nunca")}`}</span></div>${showProgress ? `<div class="progress-wrap"><div class="progress-label"><span>${esc(progress.phase)} · ${esc(progress.detail)}</span><span>${progress.total ? `${progress.percent}%` : ""}</span></div><progress value="${progress.current}" max="${progress.total || 1}"></progress></div>` : ""}${job.last_error ? `<p class="error-text">${esc(job.last_error)}</p>` : ""}<div class="job-footer"><span class="meta">${job.enabled ? "Activa" : "Pausada"}</span><div class="job-actions"><button class="compact" data-run="${job.id}" ${running ? "disabled" : ""}>${running ? "En ejecución" : "Enviar ahora"}</button><button class="secondary compact" data-edit="${job.id}" ${running ? "disabled" : ""}>Editar</button><button class="danger compact" data-del="${job.id}" ${running ? "disabled" : ""}>Eliminar</button></div></div></article>`;
  }).join("");
  document.querySelectorAll("[data-run]").forEach(button => button.onclick = async () => {
    try { button.disabled = true; button.textContent = "Iniciando…"; await request(`/api/jobs/${button.dataset.run}/run`, {method:"POST"}); say("Tarea iniciada; el avance se actualizará automáticamente."); setTimeout(() => load(true), 350); }
    catch (error) { say(error.message, true); button.disabled = false; button.textContent = "Enviar ahora"; }
  });
  document.querySelectorAll("[data-edit]").forEach(button => button.onclick = () => editJob(jobs.find(job => job.id === button.dataset.edit)));
  document.querySelectorAll("[data-del]").forEach(button => button.onclick = async () => {
    if (!confirm("¿Eliminar esta tarea?")) return;
    try { await request(`/api/jobs/${button.dataset.del}`, {method:"DELETE"}); await load(); } catch (error) { say(error.message, true); }
  });
}

async function load(silent = false) {
  try {
    const [streamData, jobData, recipientData, settings] = await Promise.all([request("/api/streams"), request("/api/jobs"), request("/api/recipients"), request("/api/settings")]);
    streams = streamData;
    recipients = recipientData;
    renderStreams();
    renderRecipients();
    renderSettings(settings);
    renderJobs(jobData);
    setConnection(true);
  } catch (error) {
    setConnection(false);
    if (!silent) say(error.message, true);
  }
}

$("#connect").onclick = async () => {
  localStorage.token = $("#token").value;
  try { await request("/api/health"); $("#login").hidden = true; $("#panel").hidden = false; setConnection(true); await load(); }
  catch (error) { setConnection(false); say(error.message, true); }
};

$("#stream-form").onsubmit = async event => {
  event.preventDefault();
  const body = {name:$("#stream_name").value,source_type:$("#source_type").value,source_url:$("#source_url").value,warmup_seconds:+$("#warmup_seconds").value,enabled:$("#stream_enabled").checked};
  try { await request("/api/streams" + (streamEditing ? "/" + streamEditing : ""), {method:streamEditing ? "PUT" : "POST", body:JSON.stringify(body)}); say("Fuente guardada."); resetStream(); await load(true); }
  catch (error) { say(error.message, true); }
};

$("#waha-settings-form").onsubmit = async event => {
  event.preventDefault();
  const body = {waha_url:$("#settings_waha_url").value, waha_session:$("#settings_waha_session").value, waha_api_key:$("#settings_waha_api_key").value};
  try { renderSettings(await request("/api/settings", {method:"PUT", body:JSON.stringify(body)})); say("WAHA guardado."); } catch (error) { say(error.message, true); }
};

$("#telegram-settings-form").onsubmit = async event => {
  event.preventDefault();
  try { renderSettings(await request("/api/settings", {method:"PUT", body:JSON.stringify({telegram_bot_token:$("#settings_telegram_token").value})})); say("Telegram guardado."); } catch (error) { say(error.message, true); }
};

$("#recipient-form").onsubmit = async event => {
  event.preventDefault();
  const body = {name:$("#recipient_name").value, channel:$("#recipient_channel").value, destination:$("#recipient_destination").value, enabled:$("#recipient_enabled").checked};
  try { await request("/api/recipients", {method:"POST", body:JSON.stringify(body)}); event.target.reset(); $("#recipient_enabled").checked = true; await load(true); say("Destinatario agregado."); } catch (error) { say(error.message, true); }
};

$("#job-form").onsubmit = async event => {
  event.preventDefault();
  updateTimelapseHelper();
  const body = {name:$("#name").value,kind:$("#kind").value,stream_id:$("#stream_id").value,chat_id:$("#chat_id").value,delivery_channel:$("#delivery_channel").value,recipient_id:$("#recipient_id").value || null,cron:cronFromSchedule(),caption:$("#caption").value,duration_seconds:+$("#duration").value,timelapse_interval_seconds:effectiveTimelapseCadence().effective,timelapse_frames:+$("#timelapse_frames").value,timelapse_fps:24,image_delivery:"image",enabled:$("#enabled").checked};
  try { await request("/api/jobs" + (editing ? "/" + editing : ""), {method:editing ? "PUT" : "POST", body:JSON.stringify(body)}); say("Tarea guardada."); resetJob(); await load(true); }
  catch (error) { say(error.message, true); }
};

$("#kind").onchange = updateFormControls;
[$("#timelapse_duration"), $("#timelapse_interval")].forEach(input => input.onchange = updateTimelapseHelper);
$("#stream_id").onchange = updateTimelapseHelper;
$("#recipient_id").onchange = updateRecipientDestination;
$("#schedule_mode").onchange = updateScheduleControls;
$("#cancel").onclick = resetJob;
$("#stream-cancel").onclick = resetStream;
$("#preview-close").onclick = () => {
  $("#snapshot-preview").hidden = true;
  $("#preview-image").removeAttribute("src");
  if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
  previewObjectUrl = null;
};
$("#refresh").onclick = () => load();
$("#logs-refresh").onclick = () => loadLogs();
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => selectTab(button.dataset.tab));
setInterval(() => {
  if (!localStorage.token || document.hidden) return;
  if (hasRunningJobs) load(true);
  if (activeTab === "logs") loadLogs(true);
}, 3000);
updateFormControls();
if (localStorage.token) { $("#token").value = localStorage.token; $("#connect").click(); }
