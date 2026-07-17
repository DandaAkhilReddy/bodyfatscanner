/* Reddy-Fit Body Scanner — auth + camera + estimation + daily tracking + compare */

const $ = (id) => document.getElementById(id);
const API = "";
let TOKEN = localStorage.getItem("rf_token") || "";
let entries = [];
let lastLandmarks = null, lastSnapshot = null, lastResult = null, chart = null;
let cameraStarted = false;

/* ================= AUTH ================= */
async function api(path, opts = {}) {
  opts.headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  const r = await fetch(API + path, opts);
  if (r.status === 401) { showLogin(); throw new Error("auth"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Request failed");
  return r.json();
}

function showLogin() {
  TOKEN = ""; localStorage.removeItem("rf_token");
  $("login-panel").classList.remove("hidden");
  $("app").classList.add("hidden");
  $("user-chip").classList.add("hidden");
}

async function showApp(email) {
  $("login-panel").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("user-chip").classList.remove("hidden");
  $("user-email").textContent = email;
  $("today-label").textContent = new Date().toISOString().slice(0, 10);
  if (!cameraStarted) startCamera();
  loadEntries();
}

$("btn-send-otp").addEventListener("click", async () => {
  const email = $("login-email").value.trim();
  $("auth-msg").textContent = "Sending…";
  try {
    const r = await api("/api/auth/request-otp", {method: "POST", body: JSON.stringify({email})});
    $("step-email").classList.add("hidden");
    $("step-otp").classList.remove("hidden");
    $("otp-email-label").textContent = email;
    $("auth-msg").textContent = r.emailed
      ? "Code sent! Check your inbox (and spam)."
      : `Your code: ${r.dev_otp} (email delivery not configured yet — code shown here instead)`;
    $("login-otp").focus();
  } catch (e) { $("auth-msg").textContent = "⚠️ " + e.message; }
});

$("btn-verify-otp").addEventListener("click", async () => {
  const email = $("login-email").value.trim();
  const code = $("login-otp").value.trim();
  try {
    const r = await api("/api/auth/verify-otp", {method: "POST", body: JSON.stringify({email, code})});
    TOKEN = r.token; localStorage.setItem("rf_token", TOKEN);
    $("auth-msg").textContent = "";
    showApp(r.email);
  } catch (e) { $("auth-msg").textContent = "⚠️ " + e.message; }
});

$("login-otp").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-verify-otp").click(); });
$("login-email").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-send-otp").click(); });
$("btn-back-email").addEventListener("click", (e) => {
  e.preventDefault();
  $("step-otp").classList.add("hidden");
  $("step-email").classList.remove("hidden");
});
$("btn-logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", {method: "POST"}); } catch {}
  showLogin();
});

/* ================= PROFILE FIELDS ================= */
["height", "weight", "age", "sex"].forEach((f) => {
  const el = $(f);
  const saved = localStorage.getItem("rf_" + f);
  if (saved) el.value = saved;
  el.addEventListener("change", () => localStorage.setItem("rf_" + f, el.value));
});

/* ================= CAMERA + POSE ================= */
const video = $("video"), overlay = $("overlay"), octx = overlay.getContext("2d");

async function startCamera() {
  cameraStarted = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: "user", width: {ideal: 720}, height: {ideal: 960}}, audio: false});
    video.srcObject = stream;
    await video.play();
    overlay.width = video.videoWidth; overlay.height = video.videoHeight;
    $("camera-msg").style.display = "none";
    $("btn-capture").disabled = false;
    detectLoop();
  } catch {
    $("camera-msg").textContent = "Camera unavailable — you can still log weight manually below.";
  }
}

const pose = new Pose({locateFile: (f) => `https://cdn.jsdelivr.net/npm/@mediapipe/pose@0.5.1675469404/${f}`});
pose.setOptions({modelComplexity: 1, smoothLandmarks: true, minDetectionConfidence: 0.5, minTrackingConfidence: 0.5});
pose.onResults((results) => {
  octx.clearRect(0, 0, overlay.width, overlay.height);
  if (!results.poseLandmarks) { lastLandmarks = null; return; }
  lastLandmarks = results.poseLandmarks;
  const C = [[11,12],[11,23],[12,24],[23,24],[11,13],[13,15],[12,14],[14,16],[23,25],[25,27],[24,26],[26,28]];
  octx.strokeStyle = "rgba(34,211,238,.85)"; octx.lineWidth = 3;
  C.forEach(([a,b]) => {
    const p = lastLandmarks[a], q = lastLandmarks[b];
    if (p.visibility > .4 && q.visibility > .4) {
      octx.beginPath(); octx.moveTo(p.x*overlay.width, p.y*overlay.height);
      octx.lineTo(q.x*overlay.width, q.y*overlay.height); octx.stroke();
    }
  });
  octx.fillStyle = "rgba(79,70,229,.9)";
  lastLandmarks.forEach((p) => { if (p.visibility > .4) {
    octx.beginPath(); octx.arc(p.x*overlay.width, p.y*overlay.height, 4, 0, Math.PI*2); octx.fill(); }});
});

let detecting = false;
function detectLoop() {
  if (detecting) return; detecting = true;
  const tick = async () => { if (video.readyState >= 2) await pose.send({image: video}); requestAnimationFrame(tick); };
  tick();
}

/* ================= ESTIMATION ================= */
function estimate() {
  const heightIn = parseFloat($("height").value), weightLbs = parseFloat($("weight").value);
  const age = parseInt($("age").value, 10), sex = $("sex").value;
  if (!heightIn || !weightLbs || !age) { alert("Fill in height, weight, and age first."); return null; }
  const bmi = 703 * weightLbs / (heightIn * heightIn);
  let bf = 1.2 * bmi + 0.23 * age - (sex === "male" ? 10.8 : 0) - 5.4;
  let ratio = null, visualNote = "No body detected — estimate uses your numbers only.";
  if (lastLandmarks) {
    const [ls, rs, lh, rh] = [11, 12, 23, 24].map((i) => lastLandmarks[i]);
    if ([ls, rs, lh, rh].every((p) => p.visibility > .5)) {
      ratio = Math.hypot(lh.x-rh.x, lh.y-rh.y) / Math.hypot(ls.x-rs.x, ls.y-rs.y);
      const adj = Math.max(-6, Math.min(6, (ratio - (sex === "male" ? .72 : .8)) * 35));
      bf += adj;
      visualNote = `Camera saw you ✓ hip/shoulder ${ratio.toFixed(2)} adjusted ${adj >= 0 ? "+" : ""}${adj.toFixed(1)}%`;
    }
  }
  bf = Math.max(3, Math.min(60, bf));
  return {bf, bmi, ratio, visualNote, heightIn, weightLbs, age, sex};
}

function categorize(bf, sex) {
  const bands = sex === "male"
    ? [[6,"Essential"],[14,"Athlete 🏆"],[18,"Fit 💪"],[25,"Average"],[100,"Above average"]]
    : [[14,"Essential"],[21,"Athlete 🏆"],[25,"Fit 💪"],[32,"Average"],[100,"Above average"]];
  for (const [max, label] of bands) if (bf < max) return label;
  return "—";
}

$("btn-capture").addEventListener("click", () => {
  const res = estimate(); if (!res) return;
  lastResult = res;
  const snap = document.createElement("canvas");
  snap.width = video.videoWidth; snap.height = video.videoHeight;
  snap.getContext("2d").drawImage(video, 0, 0);
  lastSnapshot = snap.toDataURL("image/jpeg", 0.85);
  $("bf-number").textContent = res.bf.toFixed(1) + "%";
  $("bf-category").textContent = categorize(res.bf, res.sex);
  $("bf-detail").textContent = `BMI ${res.bmi.toFixed(1)} · ${res.visualNote}`;
  $("result-panel").classList.remove("hidden");
  $("btn-retry").classList.remove("hidden");
  $("result-panel").scrollIntoView({behavior: "smooth"});
});
$("btn-retry").addEventListener("click", () => {
  $("result-panel").classList.add("hidden"); $("btn-retry").classList.add("hidden");
});

/* ================= SAVE ================= */
async function saveEntry(withPhoto) {
  const res = withPhoto ? lastResult : estimate();
  if (!res) return;
  const payload = {
    entry_date: new Date().toISOString().slice(0, 10),
    weight_lbs: res.weightLbs, height_in: res.heightIn, age: res.age, sex: res.sex,
    bf_percent: +res.bf.toFixed(1), bmi: +res.bmi.toFixed(1),
    waist_shoulder_ratio: res.ratio, notes: $("notes").value || null,
  };
  if (withPhoto && lastSnapshot) payload.image_base64 = lastSnapshot;
  const btn = withPhoto ? $("btn-save") : $("btn-save-manual");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Saving…";
  try {
    const r = await api("/api/entries", {method: "POST", body: JSON.stringify(payload)});
    btn.textContent = r.replaced ? "✅ Updated today!" : "✅ Saved!";
    loadEntries();
  } catch (e) { btn.textContent = "⚠️ " + e.message; }
  finally { setTimeout(() => { btn.disabled = false; btn.textContent = orig; }, 2000); }
}
$("btn-save").addEventListener("click", () => saveEntry(true));
$("btn-save-manual").addEventListener("click", () => saveEntry(false));

/* ================= HISTORY + CHART + COMPARE ================= */
async function authedImageURL(entryId) {
  const r = await fetch(`/api/entries/${entryId}/image`, {headers: {Authorization: "Bearer " + TOKEN}});
  if (!r.ok) return null;
  return URL.createObjectURL(await r.blob());
}

async function loadEntries() {
  try { entries = await api("/api/entries"); } catch { return; }
  $("entry-count").textContent = entries.length
    ? `${entries.length} day${entries.length > 1 ? "s" : ""} logged — consistency wins 🔥`
    : "No entries yet — scan and save your first day!";

  // chart
  if (chart) chart.destroy();
  chart = new Chart($("chart"), {
    type: "line",
    data: {labels: entries.map((e) => e.entry_date), datasets: [
      {label: "Body Fat %", data: entries.map((e) => e.bf_percent), borderColor: "#22d3ee",
       backgroundColor: "rgba(34,211,238,.15)", tension: .3, yAxisID: "y"},
      {label: "Weight (lbs)", data: entries.map((e) => e.weight_lbs), borderColor: "#4f46e5",
       backgroundColor: "rgba(79,70,229,.15)", tension: .3, yAxisID: "y1"}]},
    options: {responsive: true, scales: {
      y: {position: "left", ticks: {color: "#22d3ee"}, grid: {color: "#30363d"}},
      y1: {position: "right", ticks: {color: "#8b949e"}, grid: {drawOnChartArea: false}},
      x: {ticks: {color: "#8b949e"}, grid: {color: "#30363d"}}},
      plugins: {legend: {labels: {color: "#e6edf3"}}}}
  });

  // compare pickers: default before=first, after=last
  const withImg = entries.filter((e) => e.has_image);
  for (const sel of [$("cmp-before"), $("cmp-after")]) {
    sel.innerHTML = withImg.map((e) => `<option value="${e.id}">${e.entry_date}</option>`).join("");
  }
  if (withImg.length) {
    $("cmp-before").value = withImg[0].id;
    $("cmp-after").value = withImg[withImg.length - 1].id;
    renderCompare();
  }

  // history list
  $("history-list").innerHTML = entries.slice().reverse().map((e) =>
    `<div class="hrow"><span class="hdate">${e.entry_date}${e.has_image ? " 📷" : ""}</span>
     <span>${e.weight_lbs ?? "—"} lbs</span><span>${e.bf_percent ?? "—"}% bf</span>
     <span class="hnotes">${e.notes || ""}</span>
     <button class="mini danger" data-del="${e.id}">✕</button></div>`).join("");
  document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this entry?")) return;
    await api(`/api/entries/${b.dataset.del}`, {method: "DELETE"});
    loadEntries();
  }));
}

async function renderCompare() {
  const b = entries.find((e) => e.id === $("cmp-before").value);
  const a = entries.find((e) => e.id === $("cmp-after").value);
  if (!b || !a) return;
  const [bu, au] = await Promise.all([authedImageURL(b.id), authedImageURL(a.id)]);
  $("cmp-img-before").src = bu || ""; $("cmp-img-after").src = au || "";
  $("cmp-stats-before").innerHTML = `<strong>${b.entry_date}</strong><br>${b.weight_lbs ?? "—"} lbs · ${b.bf_percent ?? "—"}% bf`;
  $("cmp-stats-after").innerHTML = `<strong>${a.entry_date}</strong><br>${a.weight_lbs ?? "—"} lbs · ${a.bf_percent ?? "—"}% bf`;
  const dw = (a.weight_lbs != null && b.weight_lbs != null) ? (a.weight_lbs - b.weight_lbs) : null;
  const db = (a.bf_percent != null && b.bf_percent != null) ? (a.bf_percent - b.bf_percent) : null;
  const days = Math.round((new Date(a.entry_date) - new Date(b.entry_date)) / 86400000);
  $("cmp-delta").innerHTML =
    `<div class="delta-days">${days} day${Math.abs(days) === 1 ? "" : "s"}</div>` +
    (dw != null ? `<div class="delta ${dw <= 0 ? "good" : "warn"}">${dw > 0 ? "+" : ""}${dw.toFixed(1)} lbs</div>` : "") +
    (db != null ? `<div class="delta ${db <= 0 ? "good" : "warn"}">${db > 0 ? "+" : ""}${db.toFixed(1)}% bf</div>` : "");
}
$("cmp-before").addEventListener("change", renderCompare);
$("cmp-after").addEventListener("change", renderCompare);

/* ================= BOOT ================= */
(async () => {
  if (TOKEN) {
    try { const me = await api("/api/me"); showApp(me.email); return; } catch {}
  }
  showLogin();
})();
