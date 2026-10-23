/* ngrok-chat client — iPad Messages visual language
 *
 * Features:
 *   - Login / Register / Nickname screens
 *   - Two-pane layout: sidebar (conversations) + thread
 *   - Bubble messages with grouping (consecutive same-sender merged)
 *   - Infinite scroll up for history
 *   - SSE real-time with presence + typing
 *   - Web Push (VAPID) with notification preview toggle
 *   - Offline composer disable, XSS-safe textContent rendering
 *   - PWA installable, scoped to mount point (/chat/ or /)
 */

'use strict';

/* ------------------------------------------------------------------
   Base path — the app is served at "/" or "/chat/" (nginx strips prefix).
   BASE is the mount point ("" or "/chat"). Browser-facing URLs for API calls
   use relative paths (since browser is at mount point), but SW scope uses BASE.
   ------------------------------------------------------------------ */
const BASE = /^\/chat(?=\/|$)/.test(location.pathname) ? "/chat" : "";
function at(p) {
  // Return path relative to mount point (no leading slash) so fetch()
  // resolves relative to the current page (/chat/), giving /chat/...
  if (p.startsWith("/")) p = p.slice(1);
  return p;
}

/* ------------------------------------------------------------------
   Global state
   ------------------------------------------------------------------ */
const state = {
  code: "",
  nick: "",
  username: null,
  userId: null,
  room: "general",
  clientId: "",
  oldest: null,
  more: false,
  seen: new Set(),
  sending: false,
  es: null,
  typingTimer: null,
  pushInitPromise: null,
  offline: false,
};
if (!localStorage.getItem("chat.client")) {
  localStorage.setItem("chat.client", crypto.randomUUID());
}
state.clientId = localStorage.getItem("chat.client");

/* ------------------------------------------------------------------
   DOM refs (lazy - queried on first access)
   ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const els = new Proxy({}, {
  get(_, prop) {
    const map = {
      gate: "gate", gateForm: "gate-form", gateInput: "gate-input", gateErr: "gate-err",
      loginScreen: "login-screen", loginForm: "login-form", loginErr: "login-err",
      registerScreen: "register-screen", registerForm: "register-form", registerErr: "register-err",
      nickPick: "nick-pick", nickForm: "nick-form", nickInput: "nick-input", nickErr: "nick-err",
      showLogin: "show-login", showRegister: "show-register", showLoginFromNick: "show-login-from-nick",
      app: "app", sidebar: "sidebar", rooms: "rooms", roomSearch: "room-search",
      userAvatar: "user-avatar", userName: "user-name", userHandle: "user-handle",
      btnLogout: "btn-logout", btnProfile: "btn-profile",
      menu: "menu", scrim: "scrim",
      roomTitle: "room-title", conn: "conn", connText: "conn-text",
      messagesWrapper: "messages-wrapper", messages: "messages",
      loadOlder: "load-older", typing: "typing",
      composer: "composer", input: "input", send: "send",
      offlineBanner: "offline-banner", prefNotifyPreview: "pref-notify-preview",
    };
    const id = map[prop];
    return id ? document.getElementById(id) : undefined;
  }
});

/* ------------------------------------------------------------------
   API helper
   ------------------------------------------------------------------ */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  if (state.code) headers["X-Access-Code"] = state.code;
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(at(path), Object.assign({}, opts, { headers }));
  if (res.status === 401) throw Object.assign(new Error("access"), { code: 401 });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || data.error || res.statusText), { status: res.status, data });
  return data;
}

/* ------------------------------------------------------------------
   Global state initialization
   ------------------------------------------------------------------ */
if (!localStorage.getItem("chat.client")) {
  localStorage.setItem("chat.client", crypto.randomUUID());
}
state.clientId = localStorage.getItem("chat.client");

/* ------------------------------------------------------------------
   UI state helpers
   ------------------------------------------------------------------ */
function showGate() {
  hideAllGates();
  els.gate.classList.remove("hidden");
  els.gateInput.focus();
}

function showLoginScreen() {
  hideAllGates();
  els.loginScreen.classList.remove("hidden");
  els.loginForm.querySelector("input").focus();
}

function showRegisterScreen() {
  hideAllGates();
  els.registerScreen.classList.remove("hidden");
  els.registerForm.querySelector("input").focus();
}

function showNickPick() {
  hideAllGates();
  els.nickPick.classList.remove("hidden");
  els.nickInput.focus();
}

function hideAllGates() {
  [els.gate, els.loginScreen, els.registerScreen, els.nickPick].forEach(el => el?.classList.add("hidden"));
}

function enterApp() {
  hideAllGates();
  els.app.classList.remove("hidden");
  els.userName.textContent = state.nick || "—";
  els.userHandle.textContent = state.username ? "@" + state.username : "";
  updateUserAvatar();
  loadRooms();
  joinRoom(state.room);
  initPush();
  wirePreviewToggle();
  applyOnlineState();
  els.input.focus();
}

function updateUserAvatar() {
  const letter = (state.nick || "?")[0].toUpperCase();
  els.userAvatar.textContent = letter;
}

/* ------------------------------------------------------------------
   Gate / Auth forms
   ------------------------------------------------------------------ */
function wireEvents() {
  // Gate form
  if (els.gateForm) {
    els.gateForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const code = els.gateInput?.value?.trim();
      if (!code) return;
      try {
        state.code = code;
        await api("/api/rooms");
        if (els.gateErr) els.gateErr.textContent = "";
        if (state.nick) return enterApp();
        showNickPick();
      } catch (err) {
        if (err.code === 401) { state.code = ""; if (els.gateErr) els.gateErr.textContent = "Invalid code"; }
        else if (els.gateErr) els.gateErr.textContent = err.message || "Error";
      }
    });
  }

  if (els.showRegister) els.showRegister.addEventListener("click", (e) => { e.preventDefault(); showRegisterScreen(); });
  if (els.showLogin) els.showLogin.addEventListener("click", (e) => { e.preventDefault(); showLoginScreen(); });
  if (els.showLoginFromNick) els.showLoginFromNick.addEventListener("click", (e) => { e.preventDefault(); showLoginScreen(); });

  if (els.loginForm) {
    els.loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = els.loginForm.querySelector('input[name="username"]')?.value?.trim()?.toLowerCase();
      const password = els.loginForm.querySelector('input[name="password"]')?.value;
      if (!username || !password) return;
      try {
        const data = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
        state.username = data.username;
        state.nick = data.nick;
        state.userId = null;
        if (els.loginErr) els.loginErr.textContent = "";
        enterApp();
      } catch (err) {
        if (els.loginErr) els.loginErr.textContent = err.data?.message || err.message || "Login failed";
      }
    });
  }

  if (els.registerForm) {
    els.registerForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = els.registerForm.querySelector('input[name="username"]')?.value?.trim()?.toLowerCase();
      const password = els.registerForm.querySelector('input[name="password"]')?.value;
      const nick = els.registerForm.querySelector('input[name="nick"]')?.value?.trim() || username;
      if (!username || !password) return;
      try {
        const data = await api("/api/auth/register", { method: "POST", body: JSON.stringify({ username, password, nick }) });
        state.username = data.username;
        state.nick = data.nick;
        if (els.registerErr) els.registerErr.textContent = "";
        enterApp();
      } catch (err) {
        if (els.registerErr) els.registerErr.textContent = err.data?.message || err.data?.error || err.message || "Registration failed";
      }
    });
  }

  if (els.nickForm) {
    els.nickForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const nick = els.nickInput?.value?.trim();
      if (!nick) return;
      if (nick.length > 24) { if (els.nickErr) els.nickErr.textContent = "Nickname too long (max 24)"; return; }
      state.nick = nick;
      if (els.nickErr) els.nickErr.textContent = "";
      enterApp();
    });
  }

  if (els.btnLogout) {
    els.btnLogout.addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      state.username = null;
      state.nick = "";
      state.userId = null;
      els.app?.classList.add("hidden");
      showLoginScreen();
    });
  }

  if (els.roomSearch) {
    els.roomSearch.addEventListener("input", () => {
      const q = els.roomSearch.value.toLowerCase();
      els.rooms?.querySelectorAll(".room-item").forEach(btn => {
        const name = btn.dataset.room.toLowerCase();
        btn.style.display = name.includes(q) ? "flex" : "none";
      });
    });
  }

  if (els.menu) {
    els.menu.addEventListener("click", () => {
      els.sidebar?.classList.add("open");
      els.scrim?.classList.remove("hidden");
    });
  }

  if (els.scrim) {
    els.scrim.addEventListener("click", closeSidebar);
  }

  if (els.messages) {
    els.messages.addEventListener("scroll", () => {
      if (loadingOlder) return;
      if (!state.more) return;
      if (els.messages.scrollTop > 150) return;
      loadingOlder = true;
      const prevHeight = els.messages.scrollHeight;
      loadHistory(true).then(() => {
        els.messages.scrollTop = els.messages.scrollHeight - prevHeight;
        loadingOlder = false;
      });
    });
  }

  if (els.loadOlder) {
    els.loadOlder.addEventListener("click", () => {
      if (loadingOlder) return;
      loadingOlder = true;
      const prevHeight = els.messages?.scrollHeight;
      loadHistory(true).then(() => {
        if (els.messages) els.messages.scrollTop = els.messages.scrollHeight - prevHeight;
        loadingOlder = false;
      });
    });
  }

  if (els.input) {
    els.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        els.composer?.requestSubmit();
      }
    });
  }

  if (els.composer) {
    els.composer.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (state.sending) return;
      const body = els.input?.value?.trim();
      if (!body) return;
      if (state.offline) return;

      state.sending = true;
      if (els.send) els.send.disabled = true;
      try {
        await api("/api/messages", {
          method: "POST",
          body: JSON.stringify({ room: state.room, nick: state.nick, body }),
        });
        if (els.input) els.input.value = "";
      } catch (err) {
        if (err.code === 401) { state.code = ""; showGate(); }
        else if (err.code === 429) { /* rate limited */ }
        else console.warn("send failed:", err);
      } finally {
        state.sending = false;
        if (els.send) els.send.disabled = false;
      }
    });
  }

  if (els.input) {
      api("/api/typing", { method: "POST", body: JSON.stringify({ client_id: state.clientId }) }).catch(() => {});
  }

  if (els.btnLogout) {
    els.btnLogout.addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      state.username = null;
      state.nick = "";
      state.userId = null;
      if (els.app) els.app.classList.add("hidden");
      showLoginScreen();
    });
  }
}

/* ------------------------------------------------------------------
   Rooms sidebar
   ------------------------------------------------------------------ */
async function loadRooms() {
  try {
    const data = await api("/api/rooms");
    renderRooms(data.rooms);
  } catch (e) {
    if (e.code === 401) { state.code = ""; return showGate(); }
    console.warn("loadRooms failed:", e);
  }
}

function renderRooms(rooms) {
  const current = state.room;
  els.rooms.innerHTML = "";
  for (const r of rooms) {
    const li = document.createElement("li");
    const unread = r.unread || 0;
    li.innerHTML = `
      <button class="room-item ${r.name === state.room ? "active" : ""}" data-room="${escapeHtml(r.name)}" type="button" role="option" aria-selected="${r.name === state.room}">
        <span class="room-avatar">${(r.name || "#")[0].toUpperCase()}</span>
        <div class="room-info">
          <span class="room-name">#${escapeHtml(r.name)}</span>
          <span class="room-preview">${escapeHtml(r.last_body ? r.last_body.slice(0, 40) : "No messages yet")}</span>
        </div>
        <span class="room-time">${formatTime(r.last_ts)}</span>
        ${unread ? `<span class="unread-badge">${unread > 9 ? "9+" : unread}</span>` : ""}
      </button>
    `;
    li.querySelector(".room-item").addEventListener("click", () => joinRoom(r.name));
    els.rooms.appendChild(li);
  }
  highlightActiveRoom();
}

function highlightActiveRoom() {
  els.rooms.querySelectorAll(".room-item").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.room === state.room);
    btn.setAttribute("aria-selected", btn.dataset.room === state.room);
  });
}

  const q = els.roomSearch.value.toLowerCase();
  els.rooms.querySelectorAll(".room-item").forEach(btn => {
    const name = btn.dataset.room.toLowerCase();
    btn.style.display = name.includes(q) ? "flex" : "none";
  });

/* ------------------------------------------------------------------
   Join room
   ------------------------------------------------------------------ */
async function joinRoom(name) {
  state.room = name;
  location.hash = "#" + name;
  els.roomTitle.textContent = "#" + name;
  highlightActiveRoom();
  closeSidebar();
  await loadHistory();
  openStream();
  syncPushSubscription();
}

function closeSidebar() {
  els.sidebar.classList.remove("open");
  els.scrim.classList.add("hidden");
}



/* ------------------------------------------------------------------
   History — infinite scroll up
   ------------------------------------------------------------------ */
let loadingOlder = false;

async function loadHistory(older = false) {
  if (loadingOlder) return;
  loadingOlder = true;
  try {
    const params = new URLSearchParams({ room: state.room, limit: "50" });
    if (older && state.oldest) params.set("before_id", state.oldest);
    const data = await api("/api/messages?" + params);
    if (!data.exists) { state.more = false; return; }

    const atBottom = isNearBottom();
    const frag = document.createDocumentFragment();
    let prev = null;

    const list = older ? data.messages.reverse() : data.messages;
    for (const m of list) {
      if (state.seen.has(m.id)) continue;
      state.seen.add(m.id);
      frag.appendChild(renderMsg(m, prev));
      prev = m;
    }

    if (older) els.messages.prepend(frag);
    else els.messages.appendChild(frag);

    state.oldest = data.oldest;
    state.more = data.more;
    els.loadOlder.classList.toggle("hidden", !state.more);

    if (!older && atBottom) scrollBottom();
  } catch (e) {
    if (e.code === 401) { state.code = ""; showGate(); }
  } finally {
    loadingOlder = false;
  }
}

/* Infinite scroll up */


/* ------------------------------------------------------------------
   SSE real-time stream
   ------------------------------------------------------------------ */
function openStream() {
  closeStream();
  setConn("connecting");

  const params = new URLSearchParams({
    room: state.room,
    nick: state.nick,
    client_id: state.clientId,
  });
  if (state.code) params.set("code", state.code);

  const es = new EventSource(at("/api/stream?" + params));
  state.es = es;

  es.addEventListener("hello", () => setConn("live"));
  es.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    if (msg.room !== state.room) return;
    appendMessage(msg);
    scrollIfNearBottom();
  });
  es.addEventListener("presence", (e) => {
    const data = JSON.parse(e.data);
    renderPresence(data);
  });
  es.addEventListener("typing", (e) => {
    const data = JSON.parse(e.data);
    showTyping(data.nick);
  });
  es.addEventListener("history", (e) => { /* handled by loadHistory */ });
  es.onerror = () => { setConn("reconnecting"); es.close(); setTimeout(openStream, 2000); };
}

function closeStream() {
  if (state.es) { state.es.close(); state.es = null; }
}

function setConn(text) {
  els.connText.textContent = text;
}

function scrollBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

function isNearBottom() {
  const el = els.messages;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}

function scrollIfNearBottom() {
  if (isNearBottom()) scrollBottom();
}

/* ------------------------------------------------------------------
   Rendering — Messages with grouping (bubble tails)
   ------------------------------------------------------------------ */
function renderMsg(m, prev = null) {
  const isMe = m.nick === state.nick;
  const isFirst = !prev || prev.nick !== m.nick || prev.user_id !== m.user_id;
  const isLast = true;

  const wrap = document.createElement("div");
  wrap.className = "msg-group" + (isFirst ? " msg-first" : " msg-cont");
  wrap.dataset.msgId = m.id;

  const el = document.createElement("div");
  el.className = "msg " + (isMe ? "me" : "them");
  el.dataset.msgId = m.id;
  el.dataset.userId = m.user_id || "";

  const avatarLetter = (m.nick || "?")[0].toUpperCase();
  el.innerHTML = `
    <div class="msg-avatar" aria-hidden="true">${escapeHtml(avatarLetter)}</div>
    <div class="msg-body">
      <div class="msg-head">
        <span class="msg-nick">${escapeHtml(m.nick)}</span>
        <time class="msg-time">${formatTime(m.ts)}</time>
      </div>
      <div class="msg-bubble ${isLast ? "msg-last" : ""}">${escapeHtml(m.body)}</div>
    </div>
  `;

  if (isMe) el.querySelector(".msg-avatar").style.order = "1";

  const isFirstInGroup = true;
  wrap.classList.add(isFirstInGroup ? "msg-first" : "msg-cont");
  wrap.appendChild(el);

  fixGrouping();

  return wrap;
}

function fixGrouping() {
  const groups = els.messages.querySelectorAll(".msg-group");
  groups.forEach((group, i) => {
    const nextGroup = groups[i + 1];
    const thisMsg = group.querySelector(".msg");
    if (!nextGroup) return;
    const nextMsg = nextGroup.querySelector(".msg");
    const sameSender = thisMsg.querySelector(".msg-nick").textContent === nextMsg.querySelector(".msg-nick").textContent;

    const thisBubble = thisMsg.querySelector(".msg-bubble");
    const nextBubble = nextMsg.querySelector(".msg-bubble");

    thisMsg.classList.remove("msg-last");
    thisBubble.classList.remove("msg-last");

    if (sameSender) {
      nextGroup.classList.add("msg-cont");
      nextGroup.classList.remove("msg-first");
      if (thisMsg.classList.contains("me")) thisBubble.style.borderBottomRightRadius = "18px";
      else thisBubble.style.borderBottomLeftRadius = "18px";
      nextMsg.querySelector(".msg-avatar").style.visibility = "hidden";
      nextMsg.querySelector(".msg-nick").style.display = "none";
    } else {
      nextGroup.classList.remove("msg-cont");
      nextGroup.classList.add("msg-first");
      if (thisMsg.classList.contains("me")) thisBubble.style.borderBottomRightRadius = "4px";
      else thisBubble.style.borderBottomLeftRadius = "4px";
      nextMsg.querySelector(".msg-avatar").style.visibility = "visible";
      nextMsg.querySelector(".msg-nick").style.display = "inline";
    }
  });
}

function appendMessage(m) {
  if (state.seen.has(m.id)) return;
  state.seen.add(m.id);

  const prevMsgEl = els.messages.querySelector(".msg:last-child");
  const prev = prevMsgEl ? { nick: prevMsgEl.querySelector(".msg-nick").textContent, user_id: prevMsgEl.dataset.userId } : null;

  const wrap = renderMsg(m, prev);
  els.messages.appendChild(wrap);

  fixGrouping();

  scrollIfNearBottom();
}

/* ------------------------------------------------------------------
   Presence + Typing
   ------------------------------------------------------------------ */
function renderPresence(data) {
  const online = data.online || 0;
  const nicks = data.nicks || [];
  els.connText.textContent = online + " online" + (nicks.length ? " · " + nicks.join(", ") : "");
}

let typingTimer = null;
function showTyping(nick) {
  if (nick === state.nick) return;
  els.typing.textContent = nick + " is typing…";
  els.typing.classList.add("visible");
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => { els.typing.classList.remove("visible"); }, 2000);
}

/* ------------------------------------------------------------------
   Composer
   ------------------------------------------------------------------ */
/* ------------------------------------------------------------------
   Ping / Presence heartbeat
   ------------------------------------------------------------------ */
setInterval(() => {
  if (state.nick) api("/api/ping", { method: "POST", body: JSON.stringify({ client_id: state.clientId }) }).catch(() => {});
}, 20000);

/* ------------------------------------------------------------------
   Offline / Online
   ------------------------------------------------------------------ */
function applyOnlineState() {
  state.offline = !navigator.onLine;
  els.input.disabled = state.offline;
  els.send.disabled = state.offline;
  if (state.offline) {
    els.typing.textContent = "You're offline. Messages will send when you reconnect.";
    els.typing.classList.add("visible");
    els.offlineBanner.classList.remove("hidden");
  } else {
    els.offlineBanner.classList.add("hidden");
    if (!els.typing.textContent.includes("typing")) els.typing.classList.remove("visible");
  }
}
window.addEventListener("online", applyOnlineState);
window.addEventListener("offline", applyOnlineState);

/* ------------------------------------------------------------------
   Web Push
   ------------------------------------------------------------------ */
function pushSupported() {
  return "Notification" in window && "serviceWorker" in navigator && "PushManager" in window;
}

const PUSH_FLAG = "chat.pushSub";
const PUSH_ROOM = "chat.pushRoom";
const PUSH_NICK = "chat.pushNick";
const PUSH_PREVIEW = "chat.pushPreview";

function previewPref() {
  return localStorage.getItem(PUSH_PREVIEW) !== "0";
}

let previewWired = false;
function wirePreviewToggle() {
  if (previewWired) return;
  const box = els.prefNotifyPreview;
  if (!box) return;
  previewWired = true;
  box.checked = previewPref();
  box.addEventListener("change", () => {
    localStorage.setItem(PUSH_PREVIEW, box.checked ? "1" : "0");
    syncPushSubscription();
  });
}

async function ensureServiceWorker() {
  if (!("serviceWorker" in navigator)) throw new Error("service workers unavailable");
  // Use explicit scope "/chat" since BASE might be empty for API paths
  return navigator.serviceWorker.register(at("/sw.js"), { scope: "/chat" });
}

function registerServiceWorker() {
  ensureServiceWorker().catch((err) => console.warn("[push] SW registration failed:", err));
}

async function getSubscription() {
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function doInitPush() {
  if (!pushSupported()) return;

  let perm = Notification.permission;
  if (perm !== "granted" && perm !== "denied") perm = await Notification.requestPermission();
  if (perm !== "granted") return;

  const reg = await ensureServiceWorker();
  await navigator.serviceWorker.ready;

  let keyData;
  try { keyData = await api("/api/push/vapid-public-key"); }
  catch (err) { console.warn("[push] no VAPID public key yet:", err.message); return; }
  const publicKey = keyData && keyData.public_key;
  if (!publicKey) return;

  const existing = await getSubscription();
  const known = localStorage.getItem("chat.pushSub") === "1";
  const stale = !existing || !known
    || localStorage.getItem("chat.pushRoom") !== state.room
    || localStorage.getItem("chat.pushNick") !== state.nick
    || (localStorage.getItem("chat.pushPreview") || "1") !== (previewPref() ? "1" : "0");

  const sub = existing || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(publicKey) });
  if (!sub) return;
  if (stale) await postPush(sub);
  localStorage.setItem("chat.pushSub", "1");
}

async function postPush(sub) {
  const subJson = sub.toJSON();
  await api("/api/push/subscribe", {
    method: "POST",
    body: JSON.stringify({ subscription: subJson, nick: state.nick, room: state.room, preview: previewPref() }),
  });
}

async function syncPushSubscription() {
  const sub = await getSubscription();
  if (!sub) return;
  await postPush(sub);
}

let pushInitPromise = null;
function initPush() {
  if (pushInitPromise) return pushInitPromise;
  pushInitPromise = doInitPush().catch((err) => {
    console.warn("[push] init failed:", err);
    pushInitPromise = null;
  });
  return pushInitPromise;
}

function pushSupported() {
  return "Notification" in window && "serviceWorker" in navigator && "PushManager" in window;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>">']/g, c => ({
    "&": "&",
    "<": "<",
    ">": ">",
    "\"": "\"",
    "'": "'"
  }[c]));
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return "now";
  if (diff < 3600000) return Math.floor(diff / 60000) + "m";
  if (diff < 86400000) return Math.floor(diff / 3600000) + "h";
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/* ------------------------------------------------------------------
   Gate / Auth UI functions
   ------------------------------------------------------------------ */
function showGate() {
  hideAllGates();
  els.gate.classList.remove("hidden");
  els.gateInput.focus();
}

function showLoginScreen() {
  hideAllGates();
  els.loginScreen.classList.remove("hidden");
  els.loginForm.querySelector("input").focus();
}

function showRegisterScreen() {
  hideAllGates();
  els.registerScreen.classList.remove("hidden");
  els.registerForm.querySelector("input").focus();
}

function showNickPick() {
  hideAllGates();
  els.nickPick.classList.remove("hidden");
  els.nickInput.focus();
}

function hideAllGates() {
  [els.gate, els.loginScreen, els.registerScreen, els.nickPick].forEach(el => el?.classList.add("hidden"));
}

function enterApp() {
  hideAllGates();
  els.app.classList.remove("hidden");
  els.userName.textContent = state.nick || "—";
  els.userHandle.textContent = state.username ? "@" + state.username : "";
  updateUserAvatar();
  loadRooms();
  joinRoom(state.room);
  initPush();
  wirePreviewToggle();
  applyOnlineState();
  els.input.focus();
}

function updateUserAvatar() {
  const letter = (state.nick || "?")[0].toUpperCase();
  els.userAvatar.textContent = letter;
}

/* ------------------------------------------------------------------
   Boot
   ------------------------------------------------------------------ */
async function boot() {
  wireEvents();

  let cfg;
  try {
    cfg = await api(at("/api/config"));
  } catch (e) {
    document.body.innerHTML = "<p style='padding:40px;text-align:center'>Server unreachable.</p>";
    return;
  }

  if (cfg.access_required && !state.code) return showGate();
  if (cfg.access_required) {
    try { await api(at("/api/rooms")); }
    catch (e) { if (e.code === 401) { state.code = ""; return showGate(); } }
  }

  // Check for existing session
  try {
    const auth = await api(at("/api/auth/me"));
    if (auth.authenticated) {
      state.username = auth.username;
      state.nick = auth.nick;
      return enterApp();
    }
  } catch (_) {}

  // No session - show login screen (accounts are open registration)
  showLoginScreen();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

/* ------------------------------------------------------------------
   Service Worker registration (deferred)
   ------------------------------------------------------------------ */
if ("serviceWorker" in navigator) {
  if (document.readyState === "complete") registerServiceWorker();
  else window.addEventListener("load", registerServiceWorker);
}

/* ------------------------------------------------------------------
   Utility
   ------------------------------------------------------------------ */
function escapeHtml(s) {
  return String(s).replace(/[&<>\"']/g, c => ({
    "&": "&",
    "<": "<",
    ">": ">",
    "\"": "\"",
    "'": "'"
  }[c]));
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return "now";
  if (diff < 3600000) return Math.floor(diff / 60000) + "m";
  if (diff < 86400000) return Math.floor(diff / 3600000) + "h";
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/* ------------------------------------------------------------------
   Service Worker registration helpers
   ------------------------------------------------------------------ */