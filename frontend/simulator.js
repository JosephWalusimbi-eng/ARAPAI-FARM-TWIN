// ARAPAI FARM TWIN phone simulator: a pretend basic phone (dialer, USSD pop-up, Mobile Money PIN
// prompt, Messages inbox). Every action goes to our real backend; nothing is faked in the browser.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
let STATUS = null, SESSION = null, TEXT = "", DIALED = "";
let SEEN = 0;          // messages already read on the Messages tab
let TOTAL = 0;         // messages the backend has delivered to this phone
const SENT = {};       // texts the user sent from the phone, per number (the backend does not echo them)

const phone = () => ($("preset").value ? `+${$("preset").value}` : $("phone").value.trim());
const onMessages = () => $("pane-msgs").classList.contains("on");

function log(title, body) {
  const div = document.createElement("div");
  div.innerHTML = `<b>${esc(title)}</b>\n${esc(body)}`;
  $("log").prepend(div);
}

async function postForm(url, fields) {
  const body = new URLSearchParams(fields);
  const res = await fetch(url, { method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  const text = await res.text();
  log(`POST ${url} → ${res.status}`, `${body.toString()}\n\n${text || "(empty)"}`);
  return { ok: res.ok, text };
}

/* ---------- tabs ---------- */
function showTab(name) {
  $("pane-phone").classList.toggle("on", name === "phone");
  $("pane-msgs").classList.toggle("on", name === "msgs");
  $("tab-phone").classList.toggle("on", name === "phone");
  $("tab-msgs").classList.toggle("on", name === "msgs");
  if (name === "msgs") { SEEN = TOTAL; updateBadge(); const t = $("thread"); t.scrollTop = t.scrollHeight; }
}
function updateBadge() {
  const unread = onMessages() ? 0 : Math.max(0, TOTAL - SEEN);
  $("badge").textContent = unread;
  $("badge").style.display = unread ? "inline-block" : "none";
}

/* ---------- dialer ---------- */
function renderNumber() {
  $("number").textContent = DIALED || "Enter a USSD code";
  $("number").classList.toggle("empty", !DIALED);
}
const press = k => { DIALED += k; renderNumber(); };

function buildKeys() {
  const sub = { 2: "ABC", 3: "DEF", 4: "GHI", 5: "JKL", 6: "MNO", 7: "PQRS", 8: "TUV", 9: "WXYZ" };
  $("keys").innerHTML = ["1","2","3","4","5","6","7","8","9","*","0","#"].map(k =>
    `<button class="key" data-k="${k}">${k}${sub[k] ? `<small>${sub[k]}</small>` : ""}</button>`).join("");
  $("keys").querySelectorAll(".key").forEach(b => b.onclick = () => press(b.dataset.k));
  $("erase").onclick = () => { DIALED = DIALED.slice(0, -1); renderNumber(); };
  document.addEventListener("keydown", e => {
    if (!$("pane-phone").classList.contains("on") || SESSION || $("ussd").style.display === "flex" ||
        $("ussd-end").style.display === "flex" || e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (/^[0-9*#]$/.test(e.key)) press(e.key);
    else if (e.key === "Backspace") { DIALED = DIALED.slice(0, -1); renderNumber(); }
    else if (e.key === "Enter") call();
  });
}

/* ---------- USSD ---------- */
function showUssd(text, active) {
  $("ussd").style.display = active ? "flex" : "none";
  $("ussd-end").style.display = active ? "none" : "flex";
  if (active) { $("ussd-body").textContent = text; $("reply").value = ""; $("reply").focus(); }
  else $("ussd-end-body").textContent = text;
}

async function ussd() {
  const r = await postForm("/ussd", { sessionId: SESSION, serviceCode: STATUS.ussd_code, phoneNumber: phone(),
                                      networkCode: "64110", text: TEXT });
  if (!r.ok) { showUssd(`Connection problem or invalid MMI code.\n(${r.text})`, false); SESSION = null; return; }
  const active = r.text.startsWith("CON ");
  showUssd(r.text.replace(/^(CON|END) /, ""), active);
  if (!active) { SESSION = null; setTimeout(poll, 300); }
}

function call() {
  if (!DIALED || SESSION) return;
  if (DIALED !== STATUS.ussd_code) {
    showUssd(`Connection problem or invalid MMI code.\n\nThis simulator only knows ${STATUS.ussd_code}.`, false);
    return;
  }
  SESSION = "sim_" + Math.random().toString(16).slice(2, 14);
  TEXT = "";  // the first request of a session carries no choices yet
  DIALED = ""; renderNumber();  // like a real phone, the dialer clears once the call goes out
  ussd();
}

function reply() {
  const v = $("reply").value.trim();
  if (!v) return;
  TEXT = TEXT ? `${TEXT}*${v}` : v;  // all choices so far, joined by '*'
  ussd();
}

/* ---------- messages and Mobile Money prompt ---------- */
function renderThread(msgs) {
  const mine = SENT[phone()] || [];
  const items = [
    ...msgs.map(m => ({ t: new Date(m.sent_at).getTime(), me: false, from: m.from, text: m.text, extra: `${m.segments} SMS` })),
    ...mine.map(m => ({ t: m.t, me: true, from: "You", text: m.text, extra: "" })),
  ].sort((a, b) => (Math.floor(a.t / 1000) - Math.floor(b.t / 1000)) || (b.me - a.me));  // server times are whole seconds: on a tie my text came first
  $("thread").innerHTML = items.length ? items.map(m => `<div class="bubble${m.me ? " me" : ""}"><div class="meta">${esc(m.from)} · ${esc(new Date(m.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}${m.extra ? " · " + esc(m.extra) : ""}</div>${esc(m.text)}</div>`).join("")
    : `<div class="empty-msg">No messages yet. Dial ${esc(STATUS.ussd_code)} or text HELP to get started.</div>`;
  if (onMessages()) $("thread").scrollTop = $("thread").scrollHeight;
}

async function poll() {
  const p = encodeURIComponent(phone());
  try {
    const inbox = await (await fetch(`/api/sim/sms?phone=${p}`)).json();
    const msgs = (inbox.messages || []).slice().reverse();  // oldest first
    TOTAL = msgs.length;
    if (onMessages()) SEEN = TOTAL;
    renderThread(msgs);
    updateBadge();
    const pend = await (await fetch(`/api/sim/momo/pending?phone=${p}`)).json();
    const m = (pend.pending || [])[0];
    const box = $("momo");
    if (m) {
      if (box.dataset.ref !== m.ref) {
        box.dataset.ref = m.ref;
        box.innerHTML = `<div class="dialog"><b>Mobile Money</b><p style="font-size:14px">${esc(m.payer_message)}<br>Pay ${esc(m.currency)} ${esc(m.amount)}?</p>
          <input type="password" placeholder="Enter PIN" maxlength="5" inputmode="numeric">
          <div class="btns"><button data-ok="0">Reject</button><button data-ok="1">Approve</button></div></div>`;
        box.querySelectorAll("button").forEach(b => b.onclick = async () => {
          box.style.display = "none";
          const res = await fetch(`/api/sim/momo/${m.ref}`, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ approve: b.dataset.ok === "1" }) });
          log(`POST /api/sim/momo/${m.ref.slice(0, 8)}… → ${res.status}`, JSON.stringify(await res.json(), null, 1));
          setTimeout(poll, 300);
        });
      }
      box.style.display = "flex";
    } else { box.style.display = "none"; delete box.dataset.ref; }
  } catch (e) { /* server restarting: try again next tick */ }
}

async function sendSms() {
  const text = $("sms-text").value.trim();
  if (!text) return;
  $("sms-text").value = "";
  (SENT[phone()] ||= []).push({ t: Date.now(), text });
  renderThread([]);  // show my bubble at once; poll() redraws with the full thread
  await postForm("/sms/incoming", { from: phone(), to: STATUS.sender, text, date: new Date().toISOString(),
                                    id: Math.random().toString(16).slice(2) });
  setTimeout(poll, 300);
}

async function main() {
  STATUS = await (await fetch("/api/sim/status")).json();
  const code = STATUS.ussd_code;
  $("how-code").textContent = code;
  $("quick").innerHTML = `Shortcut: <button id="q-code">${esc(code)}</button>`;
  $("q-code").onclick = () => { DIALED = code; renderNumber(); };
  $("preset").innerHTML = `<option value="">Use the number above (approve or reject on the phone)</option>` +
    Object.entries(STATUS.momo_test_numbers).map(([n, s]) => `<option value="${n}">${n}: payment ${s}</option>`).join("");
  buildKeys();
  renderNumber();
  $("call").onclick = call;
  $("send").onclick = reply;
  $("reply").addEventListener("keydown", e => { if (e.key === "Enter") reply(); });
  $("cancel").onclick = () => { SESSION = null; $("ussd").style.display = "none"; };
  $("ok").onclick = () => { $("ussd-end").style.display = "none"; };
  $("sms-send").onclick = sendSms;
  $("sms-text").addEventListener("keydown", e => { if (e.key === "Enter") sendSms(); });
  $("tab-phone").onclick = () => showTab("phone");
  $("tab-msgs").onclick = () => showTab("msgs");
  $("phone").onchange = $("preset").onchange = () => { SEEN = 0; TOTAL = 0; poll(); };
  const clock = () => { $("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  clock(); setInterval(clock, 20000);
  $("who").textContent = "";
  poll();
  setInterval(poll, 2000);
}
main();
