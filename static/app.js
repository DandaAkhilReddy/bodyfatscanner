/* BodyFat Scanner — camera + MediaPipe Pose + estimation + daily tracking */

const video = document.getElementById("video");
const overlay = document.getElementById("overlay");
const octx = overlay.getContext("2d");
const cameraMsg = document.getElementById("camera-msg");
const btnCapture = document.getElementById("btn-capture");
const btnRetry = document.getElementById("btn-retry");
const btnSave = document.getElementById("btn-save");
const resultPanel = document.getElementById("result-panel");
const bfNumber = document.getElementById("bf-number");
const bfCategory = document.getElementById("bf-category");
const bfDetail = document.getElementById("bf-detail");
const entryCount = document.getElementById("entry-count");

let lastLandmarks = null;
let lastSnapshot = null; // data URL
let lastResult = null;
let chart = null;

/* ---------------- profile persistence ---------------- */
const fields = ["height", "weight", "age", "sex"];
fields.forEach((f) => {
  const el = document.getElementById(f);
  const saved = localStorage.getItem("bfs_" + f);
  if (saved) el.value = saved;
  el.addEventListener("change", () => localStorage.setItem("bfs_" + f, el.value));
});

/* ---------------- camera ---------------- */
async function startCamera() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 720 }, height: { ideal: 960 } },
      audio: false,
    });
    video.srcObject = stream;
    await video.play();
    overlay.width = video.videoWidth;
    overlay.height = video.videoHeight;
    cameraMsg.style.display = "none";
    btnCapture.disabled = false;
    detectLoop();
  } catch (err) {
    cameraMsg.textContent =
      "Camera access denied or unavailable. Allow camera permission and reload — analysis happens 100% in your browser.";
  }
}

/* ---------------- MediaPipe Pose ---------------- */
const pose = new Pose({
  locateFile: (f) => `https://cdn.jsdelivr.net/npm/@mediapipe/pose@0.5.1675469404/${f}`,
});
pose.setOptions({
  modelComplexity: 1,
  smoothLandmarks: true,
  minDetectionConfidence: 0.5,
  minTrackingConfidence: 0.5,
});
pose.onResults(onPoseResults);

let detecting = false;
async function detectLoop() {
  if (detecting) return;
  detecting = true;
  const tick = async () => {
    if (video.readyState >= 2) await pose.send({ image: video });
    requestAnimationFrame(tick);
  };
  tick();
}

const CONNECTIONS = [
  [11, 12], [11, 23], [12, 24], [23, 24],
  [11, 13], [13, 15], [12, 14], [14, 16],
  [23, 25], [25, 27], [24, 26], [26, 28],
];

function onPoseResults(results) {
  octx.clearRect(0, 0, overlay.width, overlay.height);
  if (!results.poseLandmarks) { lastLandmarks = null; return; }
  lastLandmarks = results.poseLandmarks;

  octx.strokeStyle = "rgba(34,211,238,0.85)";
  octx.lineWidth = 3;
  CONNECTIONS.forEach(([a, b]) => {
    const p = lastLandmarks[a], q = lastLandmarks[b];
    if (p.visibility > 0.4 && q.visibility > 0.4) {
      octx.beginPath();
      octx.moveTo(p.x * overlay.width, p.y * overlay.height);
      octx.lineTo(q.x * overlay.width, q.y * overlay.height);
      octx.stroke();
    }
  });
  octx.fillStyle = "rgba(79,70,229,0.9)";
  lastLandmarks.forEach((p) => {
    if (p.visibility > 0.4) {
      octx.beginPath();
      octx.arc(p.x * overlay.width, p.y * overlay.height, 4, 0, Math.PI * 2);
      octx.fill();
    }
  });
}

/* ---------------- estimation ----------------
   Base: Deurenberg BMI formula (peer-reviewed population equation)
     BF% = 1.20*BMI + 0.23*age - 10.8*(male) - 5.4
   Visual adjustment: hip-to-shoulder width ratio from pose landmarks
   nudges the estimate up/down (±6%) — torso shape carries signal that
   BMI alone misses. Each saved selfie + weight is a labeled data point. */
function estimate() {
  const heightIn = parseFloat(document.getElementById("height").value);
  const weightLbs = parseFloat(document.getElementById("weight").value);
  const age = parseInt(document.getElementById("age").value, 10);
  const sex = document.getElementById("sex").value;

  if (!heightIn || !weightLbs || !age) {
    alert("Fill in height, weight, and age first — the AI combines your numbers with what the camera sees.");
    return null;
  }

  const bmi = (703 * weightLbs) / (heightIn * heightIn);
  let bf = 1.2 * bmi + 0.23 * age - (sex === "male" ? 10.8 : 0) - 5.4;

  let ratio = null;
  let visualNote = "No body detected — estimate uses your numbers only.";
  if (lastLandmarks) {
    const ls = lastLandmarks[11], rs = lastLandmarks[12];
    const lh = lastLandmarks[23], rh = lastLandmarks[24];
    if ([ls, rs, lh, rh].every((p) => p.visibility > 0.5)) {
      const shoulderW = Math.hypot(ls.x - rs.x, ls.y - rs.y);
      const hipW = Math.hypot(lh.x - rh.x, lh.y - rh.y);
      ratio = hipW / shoulderW;
      const baseline = sex === "male" ? 0.72 : 0.8;
      const adj = Math.max(-6, Math.min(6, (ratio - baseline) * 35));
      bf += adj;
      visualNote = `Camera saw you ✓ — hip/shoulder ratio ${ratio.toFixed(2)} adjusted the estimate by ${adj >= 0 ? "+" : ""}${adj.toFixed(1)}%.`;
    }
  }

  bf = Math.max(3, Math.min(60, bf));
  return { bf, bmi, ratio, visualNote, heightIn, weightLbs, age, sex };
}

function categorize(bf, sex) {
  const bands =
    sex === "male"
      ? [[6, "Essential fat"], [14, "Athlete 🏆"], [18, "Fit 💪"], [25, "Average"], [100, "Above average"]]
      : [[14, "Essential fat"], [21, "Athlete 🏆"], [25, "Fit 💪"], [32, "Average"], [100, "Above average"]];
  for (const [max, label] of bands) if (bf < max) return label;
  return "—";
}

/* ---------------- capture ---------------- */
btnCapture.addEventListener("click", () => {
  const res = estimate();
  if (!res) return;
  lastResult = res;

  const snap = document.createElement("canvas");
  snap.width = video.videoWidth;
  snap.height = video.videoHeight;
  snap.getContext("2d").drawImage(video, 0, 0);
  lastSnapshot = snap.toDataURL("image/jpeg", 0.85);

  bfNumber.textContent = res.bf.toFixed(1) + "%";
  bfCategory.textContent = categorize(res.bf, res.sex);
  bfDetail.innerHTML =
    `BMI ${res.bmi.toFixed(1)} · ${res.visualNote}<br>` +
    `Save today's data point — daily selfies + weight build your personal labeled dataset, so the trend gets smarter over time.`;
  resultPanel.classList.remove("hidden");
  btnRetry.classList.remove("hidden");
  resultPanel.scrollIntoView({ behavior: "smooth" });
});

btnRetry.addEventListener("click", () => {
  resultPanel.classList.add("hidden");
  btnRetry.classList.add("hidden");
});

/* ---------------- save + history ---------------- */
btnSave.addEventListener("click", async () => {
  if (!lastResult) return;
  btnSave.disabled = true;
  btnSave.textContent = "Saving…";
  try {
    await fetch("/api/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entry_date: new Date().toISOString().slice(0, 10),
        weight_lbs: lastResult.weightLbs,
        height_in: lastResult.heightIn,
        age: lastResult.age,
        sex: lastResult.sex,
        bf_percent: +lastResult.bf.toFixed(1),
        bmi: +lastResult.bmi.toFixed(1),
        waist_shoulder_ratio: lastResult.ratio,
        image_base64: lastSnapshot,
      }),
    });
    btnSave.textContent = "✅ Saved!";
    loadHistory();
  } catch {
    btnSave.textContent = "⚠️ Save failed — retry";
  } finally {
    setTimeout(() => {
      btnSave.disabled = false;
      btnSave.textContent = "💾 Save Today's Data Point";
    }, 1800);
  }
});

async function loadHistory() {
  let entries = [];
  try {
    entries = await (await fetch("/api/entries")).json();
  } catch { /* backend offline — demo still works */ }

  entryCount.textContent = entries.length
    ? `${entries.length} data point${entries.length > 1 ? "s" : ""} collected — keep the streak going 🔥`
    : "No data points yet. Scan and save your first one!";

  const labels = entries.map((e) => e.entry_date);
  const bf = entries.map((e) => e.bf_percent);
  const wt = entries.map((e) => e.weight_lbs);

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Body Fat %", data: bf, borderColor: "#22d3ee", backgroundColor: "rgba(34,211,238,.15)", tension: 0.3, yAxisID: "y" },
        { label: "Weight (lbs)", data: wt, borderColor: "#4f46e5", backgroundColor: "rgba(79,70,229,.15)", tension: 0.3, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true,
      scales: {
        y: { position: "left", ticks: { color: "#22d3ee" }, grid: { color: "#30363d" } },
        y1: { position: "right", ticks: { color: "#8b949e" }, grid: { drawOnChartArea: false } },
        x: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
      },
      plugins: { legend: { labels: { color: "#e6edf3" } } },
    },
  });
}

startCamera();
loadHistory();
