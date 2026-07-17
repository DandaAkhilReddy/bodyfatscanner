/* Reddy-Fit Body Scanner — auth + camera + estimation + daily tracking + compare */

const $ = (id) => document.getElementById(id);
const API = "";
let TOKEN = localStorage.getItem("rf_token") || "";
let entries = [];
let lastLandmarks = null, lastSnapshot = null, lastResult = null, chart = null;
let cameraStarted = false;
let lastVideoBlob = null;           // recorded scan video (webm)
let recording = false;
let recRatios = [];                  // pose ratios sampled during video scan
let camStream = null;

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
    camStream = stream;
    video.srcObject = stream;
    await video.play();
    overlay.width = video.videoWidth; overlay.height = video.videoHeight;
    $("camera-msg").style.display = "none";
    $("btn-capture").disabled = false;
    $("btn-timer").disabled = false;
    $("btn-video").disabled = false;
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
  if (recording) {
    const [ls, rs, lh, rh] = [11, 12, 23, 24].map((i) => lastLandmarks[i]);
    if ([ls, rs, lh, rh].every((p) => p.visibility > .5)) {
      recRatios.push(Math.hypot(lh.x-rh.x, lh.y-rh.y) / Math.hypot(ls.x-rs.x, ls.y-rs.y));
    }
  }
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

async function countdown(sec) {
  const cd = $("countdown");
  cd.classList.remove("hidden");
  for (let i = sec; i > 0; i--) {
    cd.textContent = i;
    await new Promise((r) => setTimeout(r, 1000));
  }
  cd.classList.add("hidden");
}

function snapshotNow() {
  const snap = document.createElement("canvas");
  snap.width = video.videoWidth; snap.height = video.videoHeight;
  snap.getContext("2d").drawImage(video, 0, 0);
  return snap.toDataURL("image/jpeg", 0.85);
}

function showResult(res, mediaLabel) {
  lastResult = res;
  $("bf-number").textContent = res.bf.toFixed(1) + "%";
  $("bf-category").textContent = categorize(res.bf, res.sex);
  $("bf-detail").textContent = `BMI ${res.bmi.toFixed(1)} · ${res.visualNote}${mediaLabel ? " · " + mediaLabel : ""}`;
  $("result-panel").classList.remove("hidden");
  $("btn-retry").classList.remove("hidden");
  $("result-panel").scrollIntoView({behavior: "smooth"});
}

function captureInstant() {
  const res = estimate(); if (!res) return;
  lastVideoBlob = null;
  lastSnapshot = snapshotNow();
  showResult(res, "📸 photo");
}

$("btn-timer").addEventListener("click", async () => {
  if (!$("height").value || !$("weight").value || !$("age").value) { alert("Fill in height, weight, and age first."); return; }
  $("btn-timer").disabled = true;
  await countdown(10);
  captureInstant();
  $("btn-timer").disabled = false;
});

$("btn-video").addEventListener("click", async () => {
  if (!$("height").value || !$("weight").value || !$("age").value) { alert("Fill in height, weight, and age first."); return; }
  if (!camStream) { alert("Camera not ready"); return; }
  $("btn-video").disabled = true;
  await countdown(5); // get in position
  recRatios = []; recording = true; lastVideoBlob = null;
  const chunks = [];
  const mime = MediaRecorder.isTypeSupported("video/webm;codecs=vp9") ? "video/webm;codecs=vp9" : "video/webm";
  const rec = new MediaRecorder(camStream, {mimeType: mime, videoBitsPerSecond: 1_200_000});
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const done = new Promise((r) => { rec.onstop = r; });
  rec.start(250);
  $("rec-badge").classList.remove("hidden");
  let posterTaken = false;
  const hints = ["face the camera", "hold…", "now turn slowly", "show your back", "keep turning", "back to front", "almost done…"];
  for (let s = 0; s < 10; s++) {
    $("rec-hint").textContent = hints[Math.min(Math.floor(s / 1.5), hints.length - 1)];
    if (s === 2 && !posterTaken) { lastSnapshot = snapshotNow(); posterTaken = true; }
    await new Promise((r) => setTimeout(r, 1000));
  }
  rec.stop(); await done;
  recording = false;
  $("rec-badge").classList.add("hidden");
  lastVideoBlob = new Blob(chunks, {type: "video/webm"});
  // median ratio across the whole turn = far more stable than one frame
  const res = estimate(); if (!res) { $("btn-video").disabled = false; return; }
  if (recRatios.length >= 5) {
    const sorted = [...recRatios].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const sex = res.sex;
    const base = 1.2 * res.bmi + 0.23 * res.age - (sex === "male" ? 10.8 : 0) - 5.4;
    const adj = Math.max(-6, Math.min(6, (median - (sex === "male" ? .72 : .8)) * 35));
    res.bf = Math.max(3, Math.min(60, base + adj));
    res.ratio = median;
    res.visualNote = `🎥 video scan: ${recRatios.length} pose samples, median ratio ${median.toFixed(2)}`;
  }
  showResult(res, "🎥 video attached");
  $("btn-video").disabled = false;
});

$("btn-capture").addEventListener("click", () => { captureInstant(); });

// (legacy inline handler body replaced)
function _unused() {
  const res = estimate(); if (!res) return;
  lastResult = res;
  const snap = document.createElement("canvas");
  snap.width = video.videoWidth; snap.height = video.videoHeight;
  snap.getContext("2d").drawImage(video, 0, 0);
  lastSnapshot = snap.toDataURL("image/jpeg", 0.85);
}
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
  if (withPhoto && lastVideoBlob) {
    payload.video_base64 = await new Promise((r) => {
      const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(lastVideoBlob);
    });
  }
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
  updateReelPanel();

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
    `<div class="hrow"><span class="hdate">${e.entry_date}${e.has_video ? " 🎥" : e.has_image ? " 📷" : ""}</span>
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

/* ================= SIX-PACK PLAN ================= */
function sixPackPlan() {
  const heightIn = parseFloat($("height").value), weightLbs = parseFloat($("weight").value);
  const age = parseInt($("age").value, 10), sex = $("sex").value;
  if (!heightIn || !weightLbs || !age) { alert("Fill in height, weight, and age in Your Numbers first."); return; }

  // current bf%: manual field > latest scan > Deurenberg from BMI
  let bf = parseFloat($("plan-bf").value);
  if (!bf) {
    const scans = entries.filter((e) => e.bf_percent != null);
    if (scans.length) bf = scans[scans.length - 1].bf_percent;
  }
  const bmi = 703 * weightLbs / (heightIn * heightIn);
  if (!bf) bf = Math.max(3, Math.min(60, 1.2 * bmi + 0.23 * age - (sex === "male" ? 10.8 : 0) - 5.4));

  const kg = weightLbs / 2.2046, cm = heightIn * 2.54;
  const activity = parseFloat($("plan-activity").value);
  const deficit = Math.min(1000, parseInt($("plan-deficit").value, 10));
  const proteinPerKg = parseFloat($("plan-protein").value);

  // Mifflin-St Jeor BMR -> TDEE
  const bmr = 10 * kg + 6.25 * cm - 5 * age + (sex === "male" ? 5 : -161);
  const tdee = bmr * activity;

  // target bf for visible six-pack; keep lean mass constant
  const targetBf = sex === "male" ? 12 : 20;
  if (bf <= targetBf) {
    $("plan-result").innerHTML = `<div class="plan-hero"><div class="big">🎉 You're there!</div>
      <div class="sub">At ~${bf.toFixed(1)}% you're already at six-pack territory (${targetBf}%). Maintain: eat ~${Math.round(tdee)} kcal/day, protein ${Math.round(proteinPerKg * kg)}g/day.</div></div>`;
    $("plan-result").classList.remove("hidden");
    return;
  }
  const leanKg = kg * (1 - bf / 100);
  const goalKg = leanKg / (1 - targetBf / 100);
  const fatLossKg = kg - goalKg;
  const fatLossLbs = fatLossKg * 2.2046;
  const totalKcal = fatLossLbs * 3500;
  const days = Math.ceil(totalKcal / deficit);
  const weeks = (days / 7).toFixed(1);
  const doneDate = new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);

  // eating target with safety floor
  const floor = sex === "male" ? 1500 : 1200;
  let eatTarget = Math.round(tdee - deficit);
  let floorNote = "";
  if (eatTarget < floor) {
    eatTarget = floor;
    floorNote = ` (raised to the safe minimum of ${floor} kcal — cover the rest of your ${deficit} kcal deficit with exercise: ~${Math.round(deficit - (tdee - floor))} kcal of cardio/lifting)`;
  }
  const proteinG = Math.round(proteinPerKg * kg);
  const proteinKcal = proteinG * 4;
  const lossPerWeekLbs = (deficit * 7 / 3500).toFixed(1);

  $("plan-result").innerHTML = `
    <div class="plan-hero">
      <div class="big">${days} days</div>
      <div class="sub">to visible abs at ${targetBf}% body fat · target date <strong>${doneDate}</strong> · ${weeks} weeks at −${lossPerWeekLbs} lbs/week</div>
    </div>
    <div class="plan-cards">
      <div class="plan-card"><div class="v">${fatLossLbs.toFixed(1)} lbs</div><div class="k">fat to lose (${fatLossKg.toFixed(1)} kg)</div></div>
      <div class="plan-card"><div class="v">${Math.round(totalKcal).toLocaleString()}</div><div class="k">total kcal to burn</div></div>
      <div class="plan-card"><div class="v">${Math.round(tdee)}</div><div class="k">your TDEE (kcal/day)</div></div>
      <div class="plan-card"><div class="v">${eatTarget}</div><div class="k">eat this many kcal/day</div></div>
      <div class="plan-card"><div class="v">${proteinG} g</div><div class="k">protein/day (${proteinPerKg} g/kg = ${proteinKcal} kcal)</div></div>
      <div class="plan-card"><div class="v">${goalKg.toFixed(1)} kg</div><div class="k">goal weight (${(goalKg * 2.2046).toFixed(0)} lbs)</div></div>
    </div>
    <div class="plan-note">
      <strong>Your daily playbook:</strong> eat <strong>${eatTarget} kcal</strong>${floorNote} with <strong>${proteinG}g protein</strong>
      (≈ ${Math.round(proteinG / 31 * 100)}g chicken breast, or eggs + Greek yogurt + dal/paneer if veg) —
      high protein protects muscle so the weight you lose is fat, not gains.
      Hit your deficit with food first, add cardio to close the gap. Weigh in and scan daily here —
      your Transformation Reel unlocks on day 15. 💪
    </div>`;
  $("plan-result").classList.remove("hidden");
}
$("btn-plan").addEventListener("click", sixPackPlan);

/* ================= TRANSFORMATION REEL ================= */
const REEL_DAYS = 15;

function journeySpan() {
  const withImg = entries.filter((e) => e.has_image);
  if (withImg.length < 2) return {days: 0, first: null, last: null};
  const first = withImg[0], last = withImg[withImg.length - 1];
  const days = Math.round((new Date(last.entry_date) - new Date(first.entry_date)) / 86400000);
  return {days, first, last};
}

function updateReelPanel() {
  const {days, first, last} = journeySpan();
  const pct = Math.min(100, Math.round((days / REEL_DAYS) * 100));
  $("reel-bar").style.width = pct + "%";
  if (days >= REEL_DAYS && first && last) {
    $("reel-locked").classList.add("hidden");
    $("reel-ready").classList.remove("hidden");
  } else {
    $("reel-locked").classList.remove("hidden");
    $("reel-ready").classList.add("hidden");
    $("reel-status").textContent = !first
      ? "Save daily photos to unlock your reel — day 15 is when the magic happens ✨"
      : `Day ${days} of ${REEL_DAYS} — ${REEL_DAYS - days} more day${REEL_DAYS - days === 1 ? "" : "s"} until your transformation reel unlocks 🔓`;
  }
}

function loadImg(url) {
  return new Promise((res, rej) => {
    const i = new Image();
    i.onload = () => res(i); i.onerror = rej; i.src = url;
  });
}

function drawFrame(ctx, W, H, t, imgA, imgB, A, B) {
  /* t: 0..1 timeline. Phases: 0-.18 intro | .18-.42 before | .42-.6 morph | .6-.84 after | .84-1 outro */
  ctx.fillStyle = "#0d1117"; ctx.fillRect(0, 0, W, H);
  const drawCover = (img, alpha) => {
    if (!img) return;
    ctx.save(); ctx.globalAlpha = alpha;
    const s = Math.max(W / img.width, (H - 200) / img.height);
    const w = img.width * s, h = img.height * s;
    ctx.translate(W, 0); ctx.scale(-1, 1); // mirror like the app
    ctx.drawImage(img, (W - w) / 2, 110 + (H - 200 - h) / 2, w, h);
    ctx.restore();
  };
  const header = (txt, sub) => {
    ctx.textAlign = "center";
    ctx.fillStyle = "#22d3ee"; ctx.font = "800 44px Inter, sans-serif";
    ctx.fillText(txt, W / 2, 64);
    if (sub) { ctx.fillStyle = "#8b949e"; ctx.font = "400 24px Inter, sans-serif"; ctx.fillText(sub, W / 2, 96); }
  };
  const statsBar = (e, label) => {
    ctx.fillStyle = "rgba(13,17,23,.85)"; ctx.fillRect(0, H - 90, W, 90);
    ctx.textAlign = "center"; ctx.fillStyle = "#e6edf3"; ctx.font = "700 30px Inter, sans-serif";
    ctx.fillText(`${label} · ${e.entry_date}`, W / 2, H - 52);
    ctx.fillStyle = "#22d3ee"; ctx.font = "600 26px Inter, sans-serif";
    ctx.fillText(`${e.weight_lbs ?? "—"} lbs   ·   ${e.bf_percent ?? "—"}% body fat`, W / 2, H - 16);
  };
  if (t < .18) {
    const k = t / .18;
    ctx.textAlign = "center"; ctx.globalAlpha = Math.min(1, k * 2);
    ctx.fillStyle = "#e6edf3"; ctx.font = "800 58px Inter, sans-serif";
    ctx.fillText("💪 Reddy-Fit", W / 2, H / 2 - 40);
    ctx.fillStyle = "#22d3ee"; ctx.font = "700 40px Inter, sans-serif";
    ctx.fillText("MY TRANSFORMATION", W / 2, H / 2 + 24);
    ctx.fillStyle = "#8b949e"; ctx.font = "400 26px Inter, sans-serif";
    const days = Math.round((new Date(B.entry_date) - new Date(A.entry_date)) / 86400000);
    ctx.fillText(`${days} days · one photo at a time`, W / 2, H / 2 + 72);
    ctx.globalAlpha = 1;
  } else if (t < .42) {
    drawCover(imgA, 1); header("DAY 1", "where it started"); statsBar(A, "BEFORE");
  } else if (t < .6) {
    const k = (t - .42) / .18;
    drawCover(imgA, 1 - k); drawCover(imgB, k);
    header("THE GRIND", "every single day");
  } else if (t < .84) {
    drawCover(imgB, 1); header("TODAY", "look at you now"); statsBar(B, "AFTER");
  } else {
    drawCover(imgB, .25);
    const dw = (B.weight_lbs != null && A.weight_lbs != null) ? B.weight_lbs - A.weight_lbs : null;
    const db = (B.bf_percent != null && A.bf_percent != null) ? B.bf_percent - A.bf_percent : null;
    ctx.textAlign = "center"; ctx.fillStyle = "#e6edf3"; ctx.font = "800 46px Inter, sans-serif";
    ctx.fillText("THE RESULT", W / 2, H / 2 - 90);
    ctx.font = "800 56px Inter, sans-serif"; ctx.fillStyle = "#22c55e";
    if (dw != null) ctx.fillText(`${dw > 0 ? "+" : ""}${dw.toFixed(1)} lbs`, W / 2, H / 2 - 10);
    if (db != null) ctx.fillText(`${db > 0 ? "+" : ""}${db.toFixed(1)}% body fat`, W / 2, H / 2 + 60);
    ctx.fillStyle = "#8b949e"; ctx.font = "400 26px Inter, sans-serif";
    ctx.fillText("tracked with Reddy-Fit Body Scanner", W / 2, H / 2 + 130);
  }
}

async function generateReel() {
  const {first, last} = journeySpan();
  if (!first || !last) return;
  const btn = $("btn-make-reel");
  btn.disabled = true; btn.textContent = "🎬 Rendering…";
  try {
    const [ua, ub] = await Promise.all([authedImageURL(first.id), authedImageURL(last.id)]);
    const [imgA, imgB] = await Promise.all([loadImg(ua), loadImg(ub)]);
    const canvas = $("reel-canvas"), ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const stream = canvas.captureStream(30);
    const mime = MediaRecorder.isTypeSupported("video/webm;codecs=vp9") ? "video/webm;codecs=vp9" : "video/webm";
    const rec = new MediaRecorder(stream, {mimeType: mime, videoBitsPerSecond: 4_000_000});
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    const done = new Promise((r) => { rec.onstop = r; });
    rec.start(200);
    const DUR = 12000, t0 = performance.now();
    await new Promise((resolve) => {
      const frame = (now) => {
        const t = Math.min(1, (now - t0) / DUR);
        drawFrame(ctx, W, H, t, imgA, imgB, first, last);
        if (t < 1) requestAnimationFrame(frame); else resolve();
      };
      requestAnimationFrame(frame);
    });
    rec.stop(); await done;
    const blob = new Blob(chunks, {type: "video/webm"});
    const url = URL.createObjectURL(blob);
    $("reel-preview").src = url;
    $("reel-download").href = url;
    $("reel-output").classList.remove("hidden");
    $("reel-preview").play().catch(() => {});
  } catch (e) {
    alert("Could not render reel: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "🎬 Generate My Reel";
  }
}

async function generateShareCard() {
  const {first, last, days} = journeySpan();
  if (!first || !last) return;
  const [ua, ub] = await Promise.all([authedImageURL(first.id), authedImageURL(last.id)]);
  const [imgA, imgB] = await Promise.all([loadImg(ua), loadImg(ub)]);
  const c = document.createElement("canvas"); c.width = 1080; c.height = 720;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#0d1117"; ctx.fillRect(0, 0, 1080, 720);
  const half = (img, x) => {
    const s = Math.max(500 / img.width, 520 / img.height);
    ctx.save(); ctx.beginPath(); ctx.rect(x, 120, 500, 520); ctx.clip();
    ctx.translate(x + 500, 0); ctx.scale(-1, 1);
    ctx.drawImage(img, (500 - img.width * s) / 2, 120 + (520 - img.height * s) / 2, img.width * s, img.height * s);
    ctx.restore();
  };
  half(imgA, 30); half(imgB, 550);
  ctx.textAlign = "center"; ctx.fillStyle = "#22d3ee"; ctx.font = "800 44px Inter, sans-serif";
  ctx.fillText(`💪 ${days}-DAY TRANSFORMATION`, 540, 70);
  ctx.fillStyle = "#e6edf3"; ctx.font = "700 30px Inter, sans-serif";
  ctx.fillText(`${first.entry_date} · ${first.weight_lbs ?? "—"} lbs`, 280, 685);
  ctx.fillText(`${last.entry_date} · ${last.weight_lbs ?? "—"} lbs`, 800, 685);
  const dw = (last.weight_lbs != null && first.weight_lbs != null) ? last.weight_lbs - first.weight_lbs : null;
  if (dw != null) {
    ctx.fillStyle = "#22c55e"; ctx.font = "800 36px Inter, sans-serif";
    ctx.fillText(`${dw > 0 ? "+" : ""}${dw.toFixed(1)} lbs`, 540, 400);
  }
  const a = document.createElement("a");
  a.download = "reddyfit-transformation.png";
  a.href = c.toDataURL("image/png");
  a.click();
}

$("btn-make-reel").addEventListener("click", generateReel);
$("btn-make-card").addEventListener("click", generateShareCard);

/* ================= BOOT ================= */
(async () => {
  if (TOKEN) {
    try { const me = await api("/api/me"); showApp(me.email); return; } catch {}
  }
  showLogin();
})();
